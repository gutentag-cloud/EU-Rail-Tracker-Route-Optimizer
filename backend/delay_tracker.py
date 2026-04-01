"""
Delay data aggregation and heatmap generation.
Works in-memory with optional PostgreSQL persistence.
"""

from __future__ import annotations
import time, logging
from collections import defaultdict
from typing import Optional
from .models import (
    StationDelayStats, DelayHeatmapData, Coordinates,
)
from .station_store import StationStore
from .database import db
from .config import settings
from datetime import datetime, timezone

log = logging.getLogger(__name__)


class DelayRecord:
    __slots__ = ("station_id", "trip_id", "line_name",
                 "delay_sec", "timestamp")

    def __init__(self, station_id: str, trip_id: str,
                 line_name: str, delay_sec: int):
        self.station_id = station_id
        self.trip_id = trip_id
        self.line_name = line_name
        self.delay_sec = delay_sec
        self.timestamp = time.time()


class DelayTracker:
    """Collects delay data and produces heatmap statistics."""

    def __init__(self, store: StationStore) -> None:
        self.store = store
        self._records: list[DelayRecord] = []
        self._retention = settings.delay_retention_hours * 3600

    def record(self, station_id: str, trip_id: str,
               line_name: str, delay_sec: int) -> None:
        """Record a delay observation."""
        rec = DelayRecord(station_id, trip_id, line_name, delay_sec)
        self._records.append(rec)

        # Async DB write (fire-and-forget in the caller)
        # handled externally

    def prune_old(self) -> int:
        """Remove records older than retention period."""
        cutoff = time.time() - self._retention
        before = len(self._records)
        self._records = [
            r for r in self._records if r.timestamp >= cutoff
        ]
        return before - len(self._records)

    def record_from_departures(self, departures: list) -> int:
        """Extract delay data from a list of Departure objects."""
        count = 0
        for dep in departures:
            if dep.delay_seconds is not None:
                self.record(
                    station_id=dep.station.id,
                    trip_id=dep.trip_id,
                    line_name=dep.line_name,
                    delay_sec=dep.delay_seconds,
                )
                count += 1
        return count

    def get_heatmap(self,
                    country: Optional[str] = None,
                    ) -> DelayHeatmapData:
        """Generate heatmap data from collected records."""
        self.prune_old()

        # Group by station
        by_station: dict[str, list[int]] = defaultdict(list)
        for rec in self._records:
            by_station[rec.station_id].append(rec.delay_sec)

        stats: list[StationDelayStats] = []
        for sid, delays in by_station.items():
            station = self.store.get(sid)
            if not station:
                continue
            if country and station.country.lower() != country.lower():
                continue
            if len(delays) < 2:
                continue

            avg_d = sum(delays) / len(delays)
            sorted_d = sorted(delays)
            median_d = sorted_d[len(sorted_d) // 2]
            max_d = max(delays)
            on_time = sum(1 for d in delays if d <= 300)
            on_time_pct = (on_time / len(delays)) * 100

            color = self._delay_color(avg_d)

            stats.append(StationDelayStats(
                station_id=sid,
                station_name=station.name,
                coords=station.coords,
                total_records=len(delays),
                avg_delay_seconds=round(avg_d, 1),
                median_delay_seconds=float(median_d),
                max_delay_seconds=max_d,
                on_time_pct=round(on_time_pct, 1),
                color=color,
            ))

        stats.sort(key=lambda s: s.avg_delay_seconds, reverse=True)

        return DelayHeatmapData(
            stations=stats,
            generated_at=datetime.now(timezone.utc).isoformat(),
            period_hours=settings.delay_retention_hours,
        )

    async def get_heatmap_from_db(
        self, country: Optional[str] = None,
    ) -> Optional[DelayHeatmapData]:
        """Get heatmap from PostgreSQL if available."""
        if not db.connected:
            return None

        rows = await db.get_delay_stats(country=country)
        if not rows:
            return None

        stats = []
        for row in rows:
            avg_d = row.get("avg_delay_sec", 0)
            stats.append(StationDelayStats(
                station_id=row["station_id"],
                station_name=row.get("station_name", ""),
                coords=Coordinates(
                    latitude=row.get("latitude", 0),
                    longitude=row.get("longitude", 0),
                ),
                total_records=row.get("total_records", 0),
                avg_delay_seconds=round(float(avg_d), 1),
                median_delay_seconds=float(
                    row.get("median_delay_sec", 0)
                ),
                max_delay_seconds=int(
                    row.get("max_delay_sec", 0)
                ),
                on_time_pct=round(
                    float(row.get("on_time_pct", 100)), 1
                ),
                color=self._delay_color(float(avg_d)),
            ))

        return DelayHeatmapData(
            stations=stats,
            generated_at=datetime.now(timezone.utc).isoformat(),
            period_hours=settings.delay_retention_hours,
        )

    @staticmethod
    def _delay_color(avg_delay_sec: float) -> str:
        """Map average delay to a color: green → yellow → red."""
        if avg_delay_sec <= 60:
            return "#2ecc71"    # green — on time
        elif avg_delay_sec <= 180:
            return "#f1c40f"    # yellow — minor delays
        elif avg_delay_sec <= 300:
            return "#e67e22"    # orange — moderate delays
        elif avg_delay_sec <= 600:
            return "#e74c3c"    # red — significant delays
        else:
            return "#8e44ad"    # purple — severe delays

    @property
    def record_count(self) -> int:
        return len(self._records)
