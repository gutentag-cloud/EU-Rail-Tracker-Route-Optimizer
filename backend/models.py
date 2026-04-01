"""
All Pydantic models for the EU Rail Tracker.
"""

from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


# ── Core ──────────────────────────────────────────────

class Coordinates(BaseModel):
    latitude: float
    longitude: float


class Station(BaseModel):
    id: str
    name: str
    coords: Coordinates
    country: str = ""
    db_id: Optional[str] = None
    uic: Optional[str] = None
    is_main: bool = False
    operator: str = "db"


class Departure(BaseModel):
    trip_id: str
    line_name: str
    direction: str
    planned_time: str
    actual_time: Optional[str] = None
    delay_seconds: Optional[int] = None
    station: Station
    platform: Optional[str] = None
    operator: str = "db"


class TrainPosition(BaseModel):
    trip_id: str
    line_name: str
    direction: str
    coords: Coordinates
    speed_kmh: Optional[float] = None
    prev_station: str
    next_station: str
    progress: float  # 0.0 → 1.0
    operator: str = "db"


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
    operator: str = "db"


# ── Route Optimization ───────────────────────────────

class RouteSegment(BaseModel):
    from_station: Station
    to_station: Station
    duration_minutes: float
    distance_km: float
    line_name: Optional[str] = None
    departure_time: Optional[str] = None
    arrival_time: Optional[str] = None


class OptimizedRoute(BaseModel):
    segments: list[RouteSegment]
    total_duration_minutes: float
    total_distance_km: float
    num_stops: int
    num_transfers: int = 0
    path_station_ids: list[str]
    algorithm: str = ""


class ParetoRoute(BaseModel):
    """One solution on the Pareto frontier."""
    route: OptimizedRoute
    objectives: dict[str, float]  # e.g. {"time": 120, "transfers": 2, "distance": 450}


class ParetoResult(BaseModel):
    """All non-dominated solutions."""
    routes: list[ParetoRoute]
    total_explored: int = 0


# ── Timetable ────────────────────────────────────────

class TimetableEdge(BaseModel):
    from_station_id: str
    to_station_id: str
    depart_time: datetime
    arrive_time: datetime
    line_name: str
    trip_id: str


class TimetableRoute(BaseModel):
    legs: list[TimetableEdge]
    total_duration_minutes: float
    num_transfers: int
    depart_time: str
    arrive_time: str


# ── Delay Heatmap ────────────────────────────────────

class StationDelayStats(BaseModel):
    station_id: str
    station_name: str
    coords: Coordinates
    total_records: int
    avg_delay_seconds: float
    median_delay_seconds: float
    max_delay_seconds: int
    on_time_pct: float  # percentage with delay <= 300s
    color: str  # hex color for heatmap


class DelayHeatmapData(BaseModel):
    stations: list[StationDelayStats]
    generated_at: str
    period_hours: int


# ── Geometry ─────────────────────────────────────────

class TrackGeometry(BaseModel):
    coordinates: list[list[float]]  # [[lon, lat], ...]
    source: str = "overpass"


# ── System ───────────────────────────────────────────

class GraphStats(BaseModel):
    stations_loaded: int
    graph_nodes: int
    graph_edges: int
    operators: list[str]
    redis_connected: bool
    postgres_connected: bool


class HealthCheck(BaseModel):
    status: str
    version: str
    uptime_seconds: float
