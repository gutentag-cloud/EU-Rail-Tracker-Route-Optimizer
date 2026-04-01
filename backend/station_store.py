"""
Load and index the trainline-eu/stations open dataset.
CSV source: https://github.com/trainline-eu/stations
"""

from __future__ import annotations
import csv, math, os
from typing import Optional
from .models import Station, Coordinates

DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "stations.csv")


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in km between two coordinates."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) *
         math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class StationStore:
    def __init__(self) -> None:
        self._by_id: dict[str, Station] = {}
        self._by_db_id: dict[str, Station] = {}
        self._all: list[Station] = []

    # ── loading ───────────────────────────────────────────
    def load_csv(self, path: str = DATA_PATH) -> int:
        """Load trainline-eu stations CSV. Returns count loaded."""
        self._by_id.clear()
        self._by_db_id.clear()
        self._all.clear()

        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                lat = row.get("latitude", "")
                lon = row.get("longitude", "")
                if not lat or not lon:
                    continue
                try:
                    coords = Coordinates(
                        latitude=float(lat),
                        longitude=float(lon),
                    )
                except (ValueError, TypeError):
                    continue

                sid = row.get("id", "")
                station = Station(
                    id=sid,
                    name=row.get("name", ""),
                    coords=coords,
                    country=row.get("country", ""),
                    db_id=row.get("db_id") or None,
                    uic=row.get("uic") or None,
                    is_main=row.get("is_main_station") == "t",
                )
                self._by_id[sid] = station
                if station.db_id:
                    self._by_db_id[station.db_id] = station
                self._all.append(station)

        return len(self._all)

    # ── queries ───────────────────────────────────────────
    def get(self, station_id: str) -> Optional[Station]:
        return self._by_id.get(station_id) or self._by_db_id.get(station_id)

    def search(self, query: str, limit: int = 10,
               country: str | None = None) -> list[Station]:
        q = query.lower()
        results: list[Station] = []
        for s in self._all:
            if country and s.country.lower() != country.lower():
                continue
            if q in s.name.lower():
                results.append(s)
                if len(results) >= limit:
                    break
        return results

    def nearby(self, lat: float, lon: float,
               radius_km: float = 50, limit: int = 20) -> list[Station]:
        scored: list[tuple[float, Station]] = []
        for s in self._all:
            d = haversine(lat, lon, s.coords.latitude, s.coords.longitude)
            if d <= radius_km:
                scored.append((d, s))
        scored.sort(key=lambda x: x[0])
        return [s for _, s in scored[:limit]]

    def main_stations(self, country: str | None = None) -> list[Station]:
        return [
            s for s in self._all
            if s.is_main and (not country or s.country.lower() == country.lower())
        ]

    @property
    def count(self) -> int:
        return len(self._all)

    @property
    def all_stations(self) -> list[Station]:
        return self._all
