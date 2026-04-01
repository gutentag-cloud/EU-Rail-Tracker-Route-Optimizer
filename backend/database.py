"""
PostgreSQL + PostGIS database layer with graceful fallback.
"""

from __future__ import annotations
import logging
from typing import Optional, Any
from contextlib import asynccontextmanager
from .config import settings

log = logging.getLogger(__name__)

try:
    from sqlalchemy.ext.asyncio import (
        create_async_engine, AsyncSession, async_sessionmaker,
    )
    from sqlalchemy import text
    HAS_DB = True
except ImportError:
    HAS_DB = False


class Database:
    """Async PostgreSQL + PostGIS connection manager."""

    def __init__(self) -> None:
        self._engine: Any = None
        self._session_factory: Any = None
        self.connected = False

    async def connect(self) -> bool:
        if not HAS_DB or not settings.database_url:
            log.info("⚠️  PostgreSQL not configured — using in-memory")
            return False
        try:
            self._engine = create_async_engine(
                settings.database_url,
                echo=False,
                pool_size=10,
                max_overflow=20,
                pool_pre_ping=True,
            )
            self._session_factory = async_sessionmaker(
                self._engine, expire_on_commit=False,
            )
            # Test connection
            async with self._engine.begin() as conn:
                await conn.execute(text("SELECT 1"))
            self.connected = True
            log.info("✅ PostgreSQL connected")
            return True
        except Exception as e:
            log.warning(f"⚠️  PostgreSQL connection failed: {e}")
            self.connected = False
            return False

    async def close(self) -> None:
        if self._engine:
            await self._engine.dispose()

    @asynccontextmanager
    async def session(self):
        if not self._session_factory:
            yield None
            return
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def execute(self, query: str,
                      params: Optional[dict] = None) -> list[dict]:
        """Execute raw SQL and return rows as dicts."""
        if not self._session_factory:
            return []
        async with self.session() as session:
            if session is None:
                return []
            result = await session.execute(
                text(query), params or {}
            )
            if result.returns_rows:
                return [dict(row._mapping) for row in result.fetchall()]
            return []

    # ── Station Operations ────────────────────────────
    async def upsert_station(self, station_data: dict) -> None:
        await self.execute("""
            INSERT INTO stations (id, name, country, db_id, uic, is_main, geom)
            VALUES (
                :id, :name, :country, :db_id, :uic, :is_main,
                ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
            )
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                geom = EXCLUDED.geom
        """, station_data)

    async def nearby_stations(self, lat: float, lon: float,
                              radius_km: float = 50,
                              limit: int = 20) -> list[dict]:
        return await self.execute("""
            SELECT id, name, country, db_id, uic, is_main,
                   ST_Y(geom) AS latitude, ST_X(geom) AS longitude,
                   ST_Distance(
                       geom::geography,
                       ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography
                   ) / 1000.0 AS distance_km
            FROM stations
            WHERE ST_DWithin(
                geom::geography,
                ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
                :radius_m
            )
            ORDER BY distance_km
            LIMIT :limit
        """, {
            "lat": lat, "lon": lon,
            "radius_m": radius_km * 1000,
            "limit": limit,
        })

    async def search_stations_fuzzy(self, query: str,
                                    limit: int = 10) -> list[dict]:
        return await self.execute("""
            SELECT id, name, country, db_id, uic, is_main,
                   ST_Y(geom) AS latitude, ST_X(geom) AS longitude,
                   similarity(name, :query) AS sim
            FROM stations
            WHERE name % :query OR name ILIKE :pattern
            ORDER BY sim DESC
            LIMIT :limit
        """, {
            "query": query,
            "pattern": f"%{query}%",
            "limit": limit,
        })

    # ── Delay Operations ──────────────────────────────
    async def record_delay(self, station_id: str, trip_id: str,
                           line_name: str, delay_sec: int) -> None:
        await self.execute("""
            INSERT INTO delay_records (station_id, trip_id, line_name, delay_sec)
            VALUES (:station_id, :trip_id, :line_name, :delay_sec)
        """, {
            "station_id": station_id,
            "trip_id": trip_id,
            "line_name": line_name,
            "delay_sec": delay_sec,
        })

    async def get_delay_stats(self,
                              country: Optional[str] = None) -> list[dict]:
        where = ""
        params: dict = {}
        if country:
            where = "WHERE s.country = :country"
            params["country"] = country

        return await self.execute(f"""
            SELECT
                d.station_id,
                s.name AS station_name,
                ST_Y(s.geom) AS latitude,
                ST_X(s.geom) AS longitude,
                COUNT(*) AS total_records,
                AVG(d.delay_sec) AS avg_delay_sec,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY d.delay_sec)
                    AS median_delay_sec,
                MAX(d.delay_sec) AS max_delay_sec,
                COUNT(*) FILTER (WHERE d.delay_sec <= 300)::float
                    / NULLIF(COUNT(*), 0) * 100 AS on_time_pct
            FROM delay_records d
            JOIN stations s ON s.id = d.station_id
            {where}
            AND d.recorded_at > NOW() - INTERVAL '24 hours'
            GROUP BY d.station_id, s.name, s.geom
            HAVING COUNT(*) >= 3
            ORDER BY avg_delay_sec DESC
        """, params)

    # ── Geometry Cache ────────────────────────────────
    async def get_cached_geometry(self, from_lat: float, from_lon: float,
                                  to_lat: float,
                                  to_lon: float) -> Optional[list]:
        rows = await self.execute("""
            SELECT ST_AsGeoJSON(geom) AS geojson
            FROM track_geometry
            WHERE ABS(from_lat - :from_lat) < 0.01
              AND ABS(from_lon - :from_lon) < 0.01
              AND ABS(to_lat - :to_lat) < 0.01
              AND ABS(to_lon - :to_lon) < 0.01
              AND fetched_at > NOW() - INTERVAL '7 days'
            LIMIT 1
        """, {
            "from_lat": from_lat, "from_lon": from_lon,
            "to_lat": to_lat, "to_lon": to_lon,
        })
        if rows:
            import json
            geojson = json.loads(rows[0]["geojson"])
            return geojson.get("coordinates", [])
        return None


# Global singleton
db = Database()
