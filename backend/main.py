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
from .station_store import StationStore
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
        client = get_client(settings.default_operator)
        return await client.get_live_trains(stop_id, duration=30)

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
    return {k: {"name": v["name"], "status": v.get("status", "unknown"),
                "has_radar": v.get("has_radar", False)}
            for k, v in PROFILES.items()}


# ── station search (autocomplete) ─────────────────────
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
async def nearby_stations(
    lat: float, lon: float,
    radius: float = Query(50, le=200),
    limit: int = Query(20, le=100),
):
    return store.nearby(lat, lon, radius_km=radius, limit=limit)

@app.get("/api/stations/main")
async def main_stations(country: str | None = None):
    return store.main_stations(country=country)


# ── RADAR — all trains in bounding box ────────────────
@app.get("/api/radar")
async def radar(
    north: float = Query(...),
    south: float = Query(...),
    east: float = Query(...),
    west: float = Query(...),
    duration: int = Query(30, le=60),
):
    """Get all trains in viewport. Uses DB transport.rest radar."""
    bbox_lat = north - south
    bbox_lon = east - west
    if bbox_lat > 8.0 or bbox_lon > 12.0:
        return []  # too zoomed out, return empty

    try:
        client = get_client("db")
        positions = await client.get_radar(north, south, east, west, duration)

        # record delays from nearby stations while we're at it
        return positions
    except Exception as e:
        log.warning(f"Radar error: {e}")
        return []


# ── departures ────────────────────────────────────────
@app.get("/api/departures/{stop_id}")
async def departures(
    stop_id: str,
    duration: int = Query(30, le=120),
    operator: str = Query("db"),
):
    try:
        client = get_client(operator)
        deps = await client.get_departures(stop_id, duration)
        delay_tracker.record_from_departures(deps)
        return deps
    except Exception as e:
        raise HTTPException(502, detail=str(e))


@app.get("/api/trains/live/{stop_id}")
async def live_trains(stop_id: str, operator: str = Query("db")):
    try:
        client = get_client(operator)
        return await client.get_live_trains(stop_id, duration=30)
    except Exception as e:
        log.warning(f"Live trains error: {e}")
        return []


@app.get("/api/trip/{trip_id:path}")
async def trip_details(trip_id: str, operator: str = Query("db")):
    client = get_client(operator)
    trip = await client.get_trip(trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")
    timetable.add_trip(trip)
    return trip


@app.get("/api/journeys")
async def journeys(
    from_id: str = Query(...), to_id: str = Query(...),
    results: int = Query(3, le=6), operator: str = Query("db"),
):
    try:
        client = get_client(operator)
        return await client.search_journeys(from_id, to_id, results)
    except Exception as e:
        raise HTTPException(502, detail=str(e))


# ── route optimizer ───────────────────────────────────
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
        raise HTTPException(404, f"No routes found")
    return result


@app.get("/api/route/timetable")
async def timetable_route(
    from_id: str = Query(...), to_id: str = Query(...),
    depart: str | None = Query(None),
):
    dt = datetime.fromisoformat(depart) if depart else datetime.now(timezone.utc)
    route = timetable.find_route(from_id, to_id, depart_after=dt)
    if not route:
        raise HTTPException(404, "No timetable route. Fetch trips first.")
    return route


# ── delays ────────────────────────────────────────────
@app.get("/api/delays/heatmap")
async def delay_heatmap(country: str | None = None):
    result = await delay_tracker.get_heatmap_from_db(country)
    if result and result.stations:
        return result
    return delay_tracker.get_heatmap(country=country)

@app.get("/api/delays/station/{stop_id}")
async def station_delays(stop_id: str):
    heatmap = delay_tracker.get_heatmap()
    for s in heatmap.stations:
        if s.station_id == stop_id:
            return s
    raise HTTPException(404, "No delay data")


# ── geometry ──────────────────────────────────────────
@app.get("/api/geometry/track")
async def track_geometry(
    from_lat: float, from_lon: float,
    to_lat: float, to_lon: float,
    buffer_km: float = Query(5.0, le=20.0),
):
    result = await overpass.get_rail_geometry(
        from_lat, from_lon, to_lat, to_lon, buffer_km=buffer_km)
    if not result:
        raise HTTPException(404, "No track geometry")
    return result

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
        client = get_client(settings.default_operator)
        try:
            trains = await client.get_live_trains(stop_id, duration=30)
            await websocket.send_json({
                "type": "train_positions", "stop_id": stop_id,
                "trains": [t.model_dump() for t in trains],
                "timestamp": time.time(),
            })
        except Exception as e:
            await websocket.send_json({"type": "error", "message": str(e)})
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
