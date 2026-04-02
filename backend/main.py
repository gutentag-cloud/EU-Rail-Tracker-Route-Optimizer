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
# Quiet down httpx
logging.getLogger("httpx").setLevel(logging.WARNING)

store = StationStore()
graph = RailwayGraph(store)
timetable = TimetableGraph()
delay_tracker = DelayTracker(store)
start_time = time.time()
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

# Track radar availability
_radar_works: bool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    n = store.load_csv()
    log.info(f"✅ Loaded {n:,} stations")
    # Count stations with db_id
    with_dbid = sum(1 for s in store.all_stations if s.db_id)
    main_count = sum(1 for s in store.all_stations if s.is_main)
    log.info(f"   {with_dbid:,} with DB HAFAS ID, {main_count:,} main stations")

    c = graph.load_connections()
    if c == 0:
        c = graph.build_from_nearby(max_km=120)
        graph.save_connections()
    log.info(f"✅ Graph: {graph.node_count:,} nodes, {graph.edge_count:,} edges")
    await cache.connect()
    await db.connect()

    async def fetch_trains(stop_id):
        return []

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

@app.get("/")
async def index():
    return FileResponse(FRONTEND / "index.html")

@app.get("/manifest.json")
async def manifest():
    return FileResponse(FRONTEND / "manifest.json")

@app.get("/sw.js")
async def sw():
    return FileResponse(FRONTEND / "sw.js", media_type="application/javascript")

@app.get("/api/operators")
async def ops():
    return {k: {"name": v["name"], "status": v.get("status")}
            for k, v in PROFILES.items()}

# ══════════════════════════════════════════════════════
#  STATION SEARCH
# ══════════════════════════════════════════════════════

@app.get("/api/stations/search")
async def search_stations(
    q: str = Query(..., min_length=1),
    country: str | None = None,
    limit: int = Query(8, le=30),
    operator: str = Query("db"),
):
    # Search local store first
    local = store.search(q, limit=limit, country=country)

    # Also search via API for stations not in local store
    if len(local) < limit:
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
#  LIVE TRAINS — radar + station-based fallback
# ══════════════════════════════════════════════════════

@app.get("/api/radar")
async def radar(
    north: float = Query(...), south: float = Query(...),
    east: float = Query(...), west: float = Query(...),
):
    global _radar_works

    if north - south > 8.0 or east - west > 12.0:
        return {"trains": [], "source": "none",
                "message": "Zoom in more to see trains"}

    client = get_client("db")

    # Strategy 1: Try v5 radar (skip if we know it's down)
    if _radar_works is not False:
        trains = await client.get_radar(north, south, east, west)
        if trains:
            _radar_works = True
            return {"trains": [t.model_dump() for t in trains],
                    "source": "radar", "count": len(trains)}
        else:
            _radar_works = False
            log.info("Radar API unavailable, using station-based tracking")

    # Strategy 2: Station-based interpolation
    trains = await _station_based_trains(client, north, south, east, west)
    return {"trains": trains, "source": "stations",
            "count": len(trains),
            "message": f"Found {len(trains)} trains via station interpolation"}


async def _station_based_trains(client, north: float, south: float,
                                east: float, west: float) -> list[dict]:
    """Find trains by checking departures from stations in the viewport."""
    center_lat = (north + south) / 2
    center_lon = (east + west) / 2

    # Calculate search radius from viewport
    radius_km = haversine(south, west, north, east) / 2
    radius_km = min(radius_km, 200)

    # Find stations with valid DB HAFAS IDs in viewport
    nearby = store.nearby(center_lat, center_lon, radius_km=radius_km, limit=60)

    stations_in_view = [
        s for s in nearby
        if s.db_id                                      # MUST have DB HAFAS ID
        and south <= s.coords.latitude <= north
        and west <= s.coords.longitude <= east
    ]

    if not stations_in_view:
        log.debug(f"No stations with db_id in viewport ({north:.2f},{south:.2f},{east:.2f},{west:.2f})")
        return []

    # Prefer main stations, then take any
    main = [s for s in stations_in_view if s.is_main]
    selected = main[:6] if main else stations_in_view[:4]

    log.debug(f"Querying {len(selected)} stations for live trains: {[s.name for s in selected]}")

    now = datetime.now(timezone.utc)

    async def process_station(station):
        results = []
        try:
            deps = await client.get_departures(station.db_id, duration=30)
            if not deps:
                return results

            # Record delays
            delay_tracker.record_from_departures(deps)

            # Try to get trip details and interpolate
            for dep in deps[:5]:
                if not dep.trip_id:
                    continue
                try:
                    trip = await client.get_trip(dep.trip_id)
                    if not trip or len(trip.stopovers) < 2:
                        continue
                    pos = client.interpolate(trip, now)
                    if pos:
                        # Check if position is in viewport (with margin)
                        margin = 0.5
                        if (south - margin <= pos.coords.latitude <= north + margin and
                                west - margin <= pos.coords.longitude <= east + margin):
                            results.append(pos.model_dump())
                except Exception:
                    continue
        except Exception as e:
            log.debug(f"Station {station.name} error: {e}")
        return results

    # Fetch concurrently
    tasks = [process_station(s) for s in selected]
    all_results = await asyncio.gather(*tasks, return_exceptions=True)

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

    log.info(f"Found {len(trains)} live trains from {len(selected)} stations")
    return trains


# ══════════════════════════════════════════════════════
#  DEPARTURES & TRIPS
# ══════════════════════════════════════════════════════

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

@app.get("/api/trip/{trip_id:path}")
async def trip_details(trip_id: str, operator: str = Query("db")):
    client = get_client(operator)
    trip = await client.get_trip(trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")
    timetable.add_trip(trip)
    return trip

# ══════════════════════════════════════════════════════
#  JOURNEY SEARCH (real timetable)
# ══════════════════════════════════════════════════════

@app.get("/api/journey/search")
async def journey_search(
    from_id: str = Query(...), to_id: str = Query(...),
    results: int = Query(5, le=8), operator: str = Query("db"),
):
    """Search real train connections."""
    try:
        client = get_client(operator)
        journeys = await client.search_journeys(from_id, to_id, results)
        if not journeys:
            raise HTTPException(404, "No connections found between these stations")
        return {"journeys": journeys, "operator": operator}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, detail=str(e))

# ══════════════════════════════════════════════════════
#  GRAPH ROUTE OPTIMIZER
# ══════════════════════════════════════════════════════

@app.get("/api/route/optimize")
async def optimize_route(
    from_id: str = Query(...), to_id: str = Query(...),
    algorithm: str = Query("astar"), weight: str = Query("duration"),
):
    # Try direct lookup
    route = None
    if algorithm == "astar":
        route = graph.astar(from_id, to_id, weight=weight)
    else:
        route = graph.dijkstra(from_id, to_id, weight=weight)

    # If not found, try looking up by db_id
    if not route:
        s1 = store.get(from_id)
        s2 = store.get(to_id)
        f_id = s1.id if s1 else from_id
        t_id = s2.id if s2 else to_id

        if f_id != from_id or t_id != to_id:
            if algorithm == "astar":
                route = graph.astar(f_id, t_id, weight=weight)
            else:
                route = graph.dijkstra(f_id, t_id, weight=weight)

    if not route:
        raise HTTPException(
            404,
            f"No graph route found. These stations may not be connected in the "
            f"pre-built graph. Use the Journeys tab for real timetable routing."
        )
    return route

@app.get("/api/route/pareto")
async def pareto_route(
    from_id: str = Query(...), to_id: str = Query(...),
    max_solutions: int = Query(10, le=20),
):
    result = graph.pareto(from_id, to_id, max_solutions=max_solutions)
    if not result.routes:
        s1 = store.get(from_id)
        s2 = store.get(to_id)
        if s1 and s2:
            result = graph.pareto(s1.id, s2.id, max_solutions=max_solutions)
    if not result.routes:
        raise HTTPException(404, "No Pareto routes found. Use Journeys tab instead.")
    return result

# ══════════════════════════════════════════════════════
#  DELAYS
# ══════════════════════════════════════════════════════

@app.get("/api/delays/heatmap")
async def delay_heatmap(country: str | None = None):
    result = await delay_tracker.get_heatmap_from_db(country)
    if result and result.stations:
        return result
    return delay_tracker.get_heatmap(country=country)

# ══════════════════════════════════════════════════════
#  GEOMETRY
# ══════════════════════════════════════════════════════

@app.get("/api/geometry/network")
async def rail_network(min_lat: float, min_lon: float,
                       max_lat: float, max_lon: float):
    lines = await overpass.get_rail_network_bbox(min_lat, min_lon, max_lat, max_lon)
    return {"lines": lines, "count": len(lines)}

# ══════════════════════════════════════════════════════
#  DEBUG
# ══════════════════════════════════════════════════════

@app.get("/api/debug/stations-in-view")
async def debug_stations(
    north: float, south: float, east: float, west: float,
):
    """Debug endpoint: show what stations are found in viewport."""
    center_lat = (north + south) / 2
    center_lon = (east + west) / 2
    radius_km = haversine(south, west, north, east) / 2

    nearby = store.nearby(center_lat, center_lon, radius_km=min(radius_km, 200), limit=30)
    in_view = [s for s in nearby
               if south <= s.coords.latitude <= north
               and west <= s.coords.longitude <= east]

    return {
        "total_nearby": len(nearby),
        "in_viewport": len(in_view),
        "with_db_id": len([s for s in in_view if s.db_id]),
        "main_stations": len([s for s in in_view if s.is_main]),
        "stations": [
            {"name": s.name, "id": s.id, "db_id": s.db_id,
             "is_main": s.is_main, "country": s.country,
             "lat": s.coords.latitude, "lon": s.coords.longitude}
            for s in in_view[:20]
        ],
    }

# ══════════════════════════════════════════════════════
#  WEBSOCKET & SYSTEM
# ══════════════════════════════════════════════════════

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
