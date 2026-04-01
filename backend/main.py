"""
EU Rail Tracker & Route Optimizer — FastAPI application.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pathlib import Path

from .api_client import RailAPIClient
from .station_store import StationStore
from .optimizer import RailwayGraph

# ── globals ───────────────────────────────────────────────
store = StationStore()
graph = RailwayGraph(store)
api = RailAPIClient("db")

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    n = store.load_csv()
    print(f"✅ Loaded {n:,} stations")
    c = graph.load_connections()
    if c == 0:
        c = graph.build_from_nearby(max_km=120)
        graph.save_connections()
    print(f"✅ Graph: {graph.node_count:,} nodes, "
          f"{graph.edge_count:,} edges")
    yield
    # shutdown
    await api.close()


app = FastAPI(
    title="EU Rail Tracker",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── frontend ──────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse(FRONTEND / "index.html")


# ── station endpoints ─────────────────────────────────────
@app.get("/api/stations/search")
async def search_stations(
    q: str = Query(..., min_length=1),
    country: str | None = None,
    limit: int = Query(10, le=50),
):
    """Search stations by name (local DB + live API)."""
    local = store.search(q, limit=limit, country=country)
    if len(local) >= limit:
        return local
    try:
        remote = await api.search_stations(q, limit=limit - len(local))
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


# ── live data endpoints ───────────────────────────────────
@app.get("/api/departures/{stop_id}")
async def departures(stop_id: str, duration: int = Query(30, le=120)):
    try:
        return await api.get_departures(stop_id, duration=duration)
    except Exception as e:
        raise HTTPException(502, detail=str(e))


@app.get("/api/trains/live/{stop_id}")
async def live_trains(stop_id: str):
    """Get interpolated positions of trains near a station."""
    try:
        return await api.get_live_trains(stop_id, duration=30)
    except Exception as e:
        raise HTTPException(502, detail=str(e))


@app.get("/api/trip/{trip_id:path}")
async def trip_details(trip_id: str):
    trip = await api.get_trip(trip_id)
    if not trip:
        raise HTTPException(404, "Trip not found")
    return trip


@app.get("/api/journeys")
async def journeys(
    from_id: str = Query(...),
    to_id: str = Query(...),
    results: int = Query(3, le=6),
):
    try:
        return await api.search_journeys(from_id, to_id, results)
    except Exception as e:
        raise HTTPException(502, detail=str(e))


# ── route optimizer endpoints ─────────────────────────────
@app.get("/api/route/optimize")
async def optimize_route(
    from_id: str = Query(...),
    to_id: str = Query(...),
    algorithm: str = Query("astar", regex="^(dijkstra|astar)$"),
    weight: str = Query("duration", regex="^(duration|distance)$"),
):
    """Find optimal route using graph algorithms."""
    if algorithm == "astar":
        route = graph.astar(from_id, to_id, weight=weight)
    else:
        route = graph.dijkstra(from_id, to_id, weight=weight)

    if not route:
        raise HTTPException(
            404,
            f"No route found from {from_id} to {to_id}. "
            f"Stations may not be in the graph.",
        )
    return route


@app.get("/api/graph/stats")
async def graph_stats():
    return {
        "stations_loaded": store.count,
        "graph_nodes": graph.node_count,
        "graph_edges": graph.edge_count,
    }
