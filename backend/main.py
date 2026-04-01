"""
EU Rail Tracker — FastAPI Application
"""

from __future__ import annotations
import asyncio, time, logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Query, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pathlib import Path

from .config import settings
from .models import GraphStats, HealthCheck
from .api_client import get_client, close_all_clients, PROFILES
from .station_store import StationStore, haversine
from .optimizer import RailwayGraph
from .timetable import TimetableGraph
from .overpass import overpass
from .delay_tracker import DelayTracker
from .cache import cache
from .database import db
from .websocket_manager import ws_manager, train_broadcast_loop

logging.basicConfig(level=settings.log_level.upper())
log = logging.getLogger(__name__)

store = StationStore()
graph = RailwayGraph(store)
timetable = TimetableGraph()
delay_tracker = DelayTracker(store)
start_time = time.time()

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    n = store.load_csv()
    log.info(f"✅ Loaded {n:,} stations")
    c = graph.load_connections()
    if c == 0:
        c = graph.build_from_nearby(max_km=120)
        graph.save_connections()
    log.info(f"✅ Graph: {graph.node_count:,} nodes, {graph.edge_count:,} edges")
    await cache.connect()
    await db.connect()

    async def fetch_trains(stop_id):
        client = get_client("db")
        deps = await client.get_departures(stop_id, duration=15)
        now = datetime.now(timezone.utc)
        positions = []
        for dep in deps[:4]:
            if not dep.trip_id:
                continue
            trip = await client.get_trip(dep.trip_id)
            if trip:
                pos = client.interpolate(trip, now)
                if pos:
                    positions.append(pos)
        return positions

    task = asyncio.create_task(train_broadcast_loop(fetch_trains))
    yield
    task.cancel()
    await close_all_clients()
    await overpass.close()
    await cache.close()
    await db.close()


app = FastAPI(title="EU Rail Tracker", version="2.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ── frontend ──────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse(FRONTEND / "index.html")

@app.get("/manifest.json")
async def manifest():
    return FileResponse(FRONTEND / "manifest.json")

@app.get("/sw.js")
async def service_worker():
    return FileResponse(FRONTEND / "sw.js", media_type="application/javascript")


# ── operators ─────────────────────────────────────────
@app.get("/api/operators")
async def list_operators():
    return {k: {"name": v["name"], "status": v.get("status", "unknown")}
            for k, v in PROFILES.items()}


# ── autocomplete station search ───────────────────────
@app.get("/api/stations/search")
async def search_stations(
    q: str = Query(..., min_length=1),
    country: str | None = None,
    limit: int = Query(8, le=30),
    operator: str = Query("db"),
):
    local = store.search(q, limit=limit, country=country)
    if len(local) >= limit:
        return local
    try:
        client = get_client(operator)
        remote = await client.search_stations(q, limit=limit - len(local))
        seen = {s.id for s in local}
        for s in remote:
            if s.id not in seen:
                local.append(s)
    except Exception:
        pass
    return local

@app.get("/api/stations/nearby")
async def nearby_stations(lat: float, lon: float,
                          radius: float = Query(50, le=200),
                          limit: int = Query(20, le=100)):
    return store.nearby(lat, lon, radius_km=radius, limit=limit)

@app.get("/api/stations/main")
async def main_stations(country: str | None = None):
    return store.main_stations(country=country)


# ══════════════════════════════════════════════════════
#  LIVE TRAINS — radar with station-based fallback
# ══════════════════════════════════════════════════════

@app.get("/api/radar")
async def radar(
    north: float = Query(...), south: float = Query(...),
    east: float = Query(...), west: float = Query(...),
):
    """Get live trains in viewport. Tries radar API, falls back to station-based."""
    if north - south > 8.0 or east - west > 12.0:
        return {"trains": [], "source": "none", "message": "Zoom in more"}

    # Strategy 1: Try DB radar API
    client = get_client("db")
    trains = await client.try_radar(north, south, east, west)
    if trains:
        return {"trains": [t.model_dump() for t in trains],
                "source": "radar", "count": len(trains)}

    # Strategy 2: Station-based interpolation
    trains = await _station_based_trains(north, south, east, west)
    return {"trains": trains, "source": "stations",
            "count": len(trains)}


async def _station_based_trains(north: float, south: float,
                                east: float, west: float) -> list[dict]:
    """Find trains by checking departures from stations in the viewport."""
    center_lat = (north + south) / 2
    center_lon = (east + west) / 2
    radius_km = haversine(south, west, north, east) / 2
    radius_km = min(radius_km, 200)

    # Find main stations in viewport
    nearby = store.nearby(center_lat, center_lon,
                          radius_km=radius_km, limit=40)
    main_only = [s for s in nearby
                 if s.is_main and
                 south <= s.coords.latitude <= north and
                 west <= s.coords.longitude <= east][:8]

    if not main_only:
        main_only = [s for s in nearby
                     if south <= s.coords.latitude <= north and
                     west <= s.coords.longitude <= east][:5]

    if not main_only:
        return []

    client = get_client("db")
    now = datetime.now(timezone.utc)

    async def get_trains_from_station(station):
        """Fetch departures from a station and interpolate train positions."""
        results = []
        try:
            stop_id = station.db_id or station.id
            deps = await client.get_departures(stop_id, duration=20)

            for dep in deps[:4]:
                if not dep.trip_id:
                    continue
                try:
                    trip = await client.get_trip(dep.trip_id)
                    if not trip or len(trip.stopovers) < 2:
                        continue
                    pos = client.interpolate(trip, now)
                    if pos and south <= pos.coords.latitude <= north \
                           and west <= pos.coords.longitude <= east:
                        results.append(pos.model_dump())
                except Exception:
                    continue
        except Exception as e:
            log.debug(f"Station trains error {station.name}: {e}")
        return results

    # Fetch concurrently
    all_results = await asyncio.gather(
        *[get_trains_from_station(s) for s in main_only],
        return_exceptions=True,
    )

    # Flatten and deduplicate
    seen = set()
    trains = []
    for result in all_results:
        if isinstance(result, Exception):
            continue
        for t in result:
            tid = t.get("trip_id", "")
            if tid and tid not in seen:
                seen.add(tid)
                trains.append(t)

    return trains


# ── trip details ──────────────────────────────────────
@app.get("/api/trip/{trip_id:path}")
async def trip_details(trip_id: str, operator: str = Query("db")):
    client = get_client(operator)
    trip = await client.get_trip(trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")
    timetable.add_trip(trip)
    return trip


# ── departures ────────────────────────────────────────
@app.get("/api/departures/{stop_id}")
async def departures(stop_id: str,
                     duration: int = Query(30, le=120),
                     operator: str = Query("db")):
    try:
        client = get_client(operator)
        deps = await client.get_departures(stop_id, duration)
        delay_tracker.record_from_departures(deps)
        return deps
    except Exception as e:
        raise HTTPException(502, detail=str(e))


# ══════════════════════════════════════════════════════
#  REAL JOURNEY SEARCH (timetable-based routing)
# ══════════════════════════════════════════════════════

@app.get("/api/journey/search")
async def journey_search(
    from_id: str = Query(..., description="Origin station ID (from autocomplete)"),
    to_id: str = Query(..., description="Destination station ID"),
    results: int = Query(5, le=8),
    operator: str = Query("db"),
):
    """Search real train connections using operator timetable APIs."""
    try:
        client = get_client(operator)
        journeys = await client.search_journeys(from_id, to_id, results)
        if not journeys:
            raise HTTPException(404, "No connections found")
        return {"journeys": journeys, "operator": operator}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, detail=str(e))


# ── graph-based route optimizer (educational) ─────────
@app.get("/api/route/optimize")
async def optimize_route(
    from_id: str = Query(...), to_id: str = Query(...),
    algorithm: str = Query("astar"),
    weight: str = Query("duration"),
):
    if algorithm == "astar":
        route = graph.astar(from_id, to_id, weight=weight)
    else:
        route = graph.dijkstra(from_id, to_id, weight=weight)
    if not route:
        raise HTTPException(404, f"No route from {from_id} to {to_id}")
    return route

@app.get("/api/route/pareto")
async def pareto_route(
    from_id: str = Query(...), to_id: str = Query(...),
    max_solutions: int = Query(10, le=20),
):
    result = graph.pareto(from_id, to_id, max_solutions=max_solutions)
    if not result.routes:
        raise HTTPException(404, "No routes found")
    return result


# ── delays ────────────────────────────────────────────
@app.get("/api/delays/heatmap")
async def delay_heatmap(country: str | None = None):
    result = await delay_tracker.get_heatmap_from_db(country)
    if result and result.stations:
        return result
    return delay_tracker.get_heatmap(country=country)


# ── geometry ──────────────────────────────────────────
@app.get("/api/geometry/network")
async def rail_network(
    min_lat: float, min_lon: float,
    max_lat: float, max_lon: float,
):
    lines = await overpass.get_rail_network_bbox(
        min_lat, min_lon, max_lat, max_lon)
    return {"lines": lines, "count": len(lines)}


# ── websocket ─────────────────────────────────────────
@app.websocket("/ws/trains/{stop_id}")
async def ws_trains(websocket: WebSocket, stop_id: str):
    await ws_manager.connect(websocket, stop_id)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, stop_id)
    except Exception:
        ws_manager.disconnect(websocket, stop_id)


# ── system ────────────────────────────────────────────
@app.get("/api/graph/stats")
async def graph_stats():
    return GraphStats(
        stations_loaded=store.count, graph_nodes=graph.node_count,
        graph_edges=graph.edge_count, operators=list(PROFILES.keys()),
        redis_connected=cache.connected, postgres_connected=db.connected,
    )

@app.get("/api/health")
async def health():
    return HealthCheck(status="healthy", version="2.0.0",
                       uptime_seconds=round(time.time() - start_time, 1))
