"""
Async client for European rail APIs.

Primary:  DB transport.rest  (Deutsche Bahn – open, no key needed)
Docs:     https://v6.db.transport.rest/
Extends:  ÖBB, SBB via HAFAS mgate (same protocol, different endpoint)
"""

from __future__ import annotations
import httpx, math
from datetime import datetime, timezone
from typing import Optional
from .models import (
    Station, Coordinates, Departure,
    TrainPosition, Trip, Stopover,
)
from .station_store import haversine

# ── operator profiles ─────────────────────────────────────
PROFILES: dict[str, dict] = {
    "db": {
        "base_url": "https://v6.db.transport.rest",
        "type": "rest",
    },
    # Add more operators here – ÖBB, SBB, etc. use HAFAS mgate
}


class RailAPIClient:
    """Async client wrapping the transport.rest / HAFAS APIs."""

    def __init__(self, operator: str = "db", timeout: float = 15.0):
        profile = PROFILES.get(operator)
        if not profile:
            raise ValueError(f"Unknown operator: {operator}")
        self.base = profile["base_url"]
        self.client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self.client.aclose()

    # ── station search ────────────────────────────────────
    async def search_stations(self, query: str,
                              limit: int = 5) -> list[Station]:
        resp = await self.client.get(
            f"{self.base}/locations",
            params={"query": query, "results": limit,
                    "stops": "true", "addresses": "false",
                    "poi": "false"},
        )
        resp.raise_for_status()
        out: list[Station] = []
        for loc in resp.json():
            if loc.get("type") not in ("stop", "station"):
                continue
            loc_data = loc.get("location") or {}
            lat = loc_data.get("latitude")
            lon = loc_data.get("longitude")
            if lat is None or lon is None:
                continue
            out.append(Station(
                id=str(loc["id"]),
                name=loc.get("name", ""),
                coords=Coordinates(latitude=lat, longitude=lon),
                db_id=str(loc["id"]),
            ))
        return out

    # ── departures ────────────────────────────────────────
    async def get_departures(self, stop_id: str,
                             duration: int = 30) -> list[Departure]:
        resp = await self.client.get(
            f"{self.base}/stops/{stop_id}/departures",
            params={"duration": duration, "results": 30},
        )
        resp.raise_for_status()
        departures: list[Departure] = []
        for dep in resp.json().get("departures", resp.json()):
            stop_data = dep.get("stop") or {}
            loc = stop_data.get("location") or {}
            station = Station(
                id=str(stop_data.get("id", stop_id)),
                name=stop_data.get("name", ""),
                coords=Coordinates(
                    latitude=loc.get("latitude", 0),
                    longitude=loc.get("longitude", 0),
                ),
                db_id=str(stop_data.get("id", stop_id)),
            )
            line = dep.get("line") or {}
            departures.append(Departure(
                trip_id=dep.get("tripId", ""),
                line_name=line.get("name", "?"),
                direction=dep.get("direction", ""),
                planned_time=dep.get("plannedWhen") or "",
                actual_time=dep.get("when"),
                delay_seconds=dep.get("delay"),
                station=station,
                platform=dep.get("platform"),
            ))
        return departures

    # ── full trip details ─────────────────────────────────
    async def get_trip(self, trip_id: str) -> Optional[Trip]:
        resp = await self.client.get(
            f"{self.base}/trips/{trip_id}",
            params={"stopovers": "true", "polyline": "false"},
        )
        if resp.status_code != 200:
            return None
        data = resp.json().get("trip", resp.json())
        line = data.get("line") or {}
        stopovers: list[Stopover] = []
        for so in data.get("stopovers", []):
            s = so.get("stop") or {}
            loc = s.get("location") or {}
            stopovers.append(Stopover(
                station=Station(
                    id=str(s.get("id", "")),
                    name=s.get("name", ""),
                    coords=Coordinates(
                        latitude=loc.get("latitude", 0),
                        longitude=loc.get("longitude", 0),
                    ),
                    db_id=str(s.get("id", "")),
                ),
                arrival=so.get("arrival"),
                departure=so.get("departure"),
                delay_seconds=so.get("arrivalDelay") or so.get("departureDelay"),
            ))
        return Trip(
            id=data.get("id", trip_id),
            line_name=line.get("name", ""),
            direction=data.get("direction", ""),
            stopovers=stopovers,
        )

    # ── journey search ────────────────────────────────────
    async def search_journeys(self, from_id: str, to_id: str,
                              results: int = 3):
        resp = await self.client.get(
            f"{self.base}/journeys",
            params={
                "from": from_id, "to": to_id,
                "results": results, "stopovers": "true",
                "transferTime": 0, "national": "true",
                "nationalExpress": "true", "regional": "true",
                "regionalExpress": "true",
            },
        )
        resp.raise_for_status()
        return resp.json()

    # ── live train positions (interpolated) ───────────────
    async def get_live_trains(self, stop_id: str,
                              duration: int = 30) -> list[TrainPosition]:
        """
        Get trains currently in motion near a station.
        Fetches departures → trip details → interpolates position.
        """
        deps = await self.get_departures(stop_id, duration=duration)
        now = datetime.now(timezone.utc)
        positions: list[TrainPosition] = []

        for dep in deps[:8]:          # limit API calls
            if not dep.trip_id:
                continue
            trip = await self.get_trip(dep.trip_id)
            if not trip or len(trip.stopovers) < 2:
                continue
            pos = self._interpolate_position(trip, now)
            if pos:
                positions.append(pos)
        return positions

    @staticmethod
    def _interpolate_position(trip: Trip,
                              now: datetime) -> Optional[TrainPosition]:
        """Find which segment the train is on and interpolate GPS."""
        stopovers = trip.stopovers
        for i in range(len(stopovers) - 1):
            dep_time_str = stopovers[i].departure
            arr_time_str = stopovers[i + 1].arrival
            if not dep_time_str or not arr_time_str:
                continue
            try:
                dep_t = datetime.fromisoformat(dep_time_str)
                arr_t = datetime.fromisoformat(arr_time_str)
            except ValueError:
                continue

            if dep_t <= now <= arr_t:
                total = (arr_t - dep_t).total_seconds()
                elapsed = (now - dep_t).total_seconds()
                frac = elapsed / total if total > 0 else 0.0
                frac = max(0.0, min(1.0, frac))

                p = stopovers[i].station.coords
                n = stopovers[i + 1].station.coords
                lat = p.latitude + frac * (n.latitude - p.latitude)
                lon = p.longitude + frac * (n.longitude - p.longitude)

                dist = haversine(
                    p.latitude, p.longitude,
                    n.latitude, n.longitude,
                )
                speed = (dist / (total / 3600)) if total > 0 else None

                return TrainPosition(
                    trip_id=trip.id,
                    line_name=trip.line_name,
                    direction=trip.direction,
                    coords=Coordinates(latitude=lat, longitude=lon),
                    speed_kmh=round(speed, 1) if speed else None,
                    prev_station=stopovers[i].station.name,
                    next_station=stopovers[i + 1].station.name,
                    progress=round(frac, 3),
                )
        return None
