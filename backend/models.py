from __future__ import annotations
from pydantic import BaseModel
from typing import Optional


class Coordinates(BaseModel):
    latitude: float
    longitude: float


class Station(BaseModel):
    id: str
    name: str
    coords: Coordinates
    country: str = ""
    db_id: Optional[str] = None        # Deutsche Bahn HAFAS id
    uic: Optional[str] = None          # International UIC code
    is_main: bool = False


class Departure(BaseModel):
    trip_id: str
    line_name: str
    direction: str
    planned_time: str
    actual_time: Optional[str] = None
    delay_seconds: Optional[int] = None
    station: Station
    platform: Optional[str] = None


class TrainPosition(BaseModel):
    trip_id: str
    line_name: str
    direction: str
    coords: Coordinates
    speed_kmh: Optional[float] = None
    prev_station: str
    next_station: str
    progress: float                     # 0.0 → 1.0 between stops


class Stopover(BaseModel):
    station: Station
    arrival: Optional[str] = None
    departure: Optional[str] = None
    delay_seconds: Optional[int] = None


class Trip(BaseModel):
    id: str
    line_name: str
    direction: str
    stopovers: list[Stopover]


class RouteSegment(BaseModel):
    from_station: Station
    to_station: Station
    duration_minutes: float
    distance_km: float


class OptimizedRoute(BaseModel):
    segments: list[RouteSegment]
    total_duration_minutes: float
    total_distance_km: float
    num_stops: int
    path_station_ids: list[str]
