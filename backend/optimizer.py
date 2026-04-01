"""
Graph-based route optimizer for the EU rail network.

Algorithms:
  • Dijkstra  – guaranteed shortest path
  • A*        – faster with haversine heuristic
  • k-shortest paths (Yen's) – alternative routes

The graph is built dynamically from the station store +
connections discovered via the API (cached).
"""

from __future__ import annotations
import heapq, json, os, math
from typing import Optional
from .models import (
    Station, Coordinates, RouteSegment, OptimizedRoute,
)
from .station_store import StationStore, haversine

CONNECTIONS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "connections.json"
)


class Edge:
    __slots__ = ("to_id", "distance_km", "duration_min")

    def __init__(self, to_id: str, distance_km: float,
                 duration_min: float):
        self.to_id = to_id
        self.distance_km = distance_km
        self.duration_min = duration_min


class RailwayGraph:
    """Weighted directed graph of the rail network."""

    def __init__(self, store: StationStore):
        self.store = store
        self.adj: dict[str, list[Edge]] = {}

    # ── graph construction ────────────────────────────────
    def add_edge(self, from_id: str, to_id: str,
                 distance_km: float | None = None,
                 duration_min: float | None = None,
                 bidirectional: bool = True) -> None:
        s1 = self.store.get(from_id)
        s2 = self.store.get(to_id)
        if not s1 or not s2:
            return

        if distance_km is None:
            distance_km = haversine(
                s1.coords.latitude, s1.coords.longitude,
                s2.coords.latitude, s2.coords.longitude,
            )
        if duration_min is None:
            # estimate: ~100 km/h average rail speed
            duration_min = (distance_km / 100.0) * 60.0

        self.adj.setdefault(from_id, []).append(
            Edge(to_id, distance_km, duration_min)
        )
        if bidirectional:
            self.adj.setdefault(to_id, []).append(
                Edge(from_id, distance_km, duration_min)
            )

    def build_from_nearby(self, max_km: float = 120) -> int:
        """
        Connect every main station to others within max_km.
        Produces a dense graph — good starting point.
        """
        mains = self.store.main_stations()
        count = 0
        for i, s1 in enumerate(mains):
            for s2 in mains[i + 1:]:
                d = haversine(
                    s1.coords.latitude, s1.coords.longitude,
                    s2.coords.latitude, s2.coords.longitude,
                )
                if d <= max_km:
                    self.add_edge(s1.id, s2.id, distance_km=d)
                    count += 1
        return count

    def load_connections(self, path: str = CONNECTIONS_PATH) -> int:
        """Load pre-built connections JSON."""
        if not os.path.exists(path):
            return 0
        with open(path) as f:
            data = json.load(f)
        count = 0
        for conn in data:
            self.add_edge(
                conn["from"], conn["to"],
                distance_km=conn.get("distance_km"),
                duration_min=conn.get("duration_min"),
            )
            count += 1
        return count

    def save_connections(self, path: str = CONNECTIONS_PATH) -> None:
        """Save current edges to JSON for caching."""
        conns = []
        seen = set()
        for fid, edges in self.adj.items():
            for e in edges:
                key = tuple(sorted([fid, e.to_id]))
                if key not in seen:
                    seen.add(key)
                    conns.append({
                        "from": fid,
                        "to": e.to_id,
                        "distance_km": round(e.distance_km, 2),
                        "duration_min": round(e.duration_min, 2),
                    })
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(conns, f, indent=2)

    # ── Dijkstra ──────────────────────────────────────────
    def dijkstra(self, start_id: str, end_id: str,
                 weight: str = "duration") -> Optional[OptimizedRoute]:
        """
        Shortest path by duration or distance.
        weight: "duration" | "distance"
        """
        if start_id not in self.adj and start_id not in {
            e.to_id for edges in self.adj.values() for e in edges
        }:
            return None

        dist: dict[str, float] = {start_id: 0.0}
        prev: dict[str, str] = {}
        pq: list[tuple[float, str]] = [(0.0, start_id)]

        while pq:
            d, u = heapq.heappop(pq)
            if u == end_id:
                break
            if d > dist.get(u, math.inf):
                continue
            for edge in self.adj.get(u, []):
                w = edge.duration_min if weight == "duration" \
                    else edge.distance_km
                nd = d + w
                if nd < dist.get(edge.to_id, math.inf):
                    dist[edge.to_id] = nd
                    prev[edge.to_id] = u
                    heapq.heappush(pq, (nd, edge.to_id))

        if end_id not in prev and start_id != end_id:
            return None
        return self._build_route(start_id, end_id, prev)

    # ── A* with haversine heuristic ───────────────────────
    def astar(self, start_id: str, end_id: str,
              weight: str = "duration") -> Optional[OptimizedRoute]:
        goal = self.store.get(end_id)
        if not goal:
            return None

        def h(nid: str) -> float:
            s = self.store.get(nid)
            if not s:
                return 0.0
            d = haversine(
                s.coords.latitude, s.coords.longitude,
                goal.coords.latitude, goal.coords.longitude,
            )
            if weight == "duration":
                return (d / 200.0) * 60.0   # optimistic 200 km/h
            return d

        g: dict[str, float] = {start_id: 0.0}
        prev: dict[str, str] = {}
        pq: list[tuple[float, str]] = [(h(start_id), start_id)]
        closed: set[str] = set()

        while pq:
            _, u = heapq.heappop(pq)
            if u == end_id:
                break
            if u in closed:
                continue
            closed.add(u)
            for edge in self.adj.get(u, []):
                w = edge.duration_min if weight == "duration" \
                    else edge.distance_km
                ng = g[u] + w
                if ng < g.get(edge.to_id, math.inf):
                    g[edge.to_id] = ng
                    prev[edge.to_id] = u
                    heapq.heappush(pq, (ng + h(edge.to_id), edge.to_id))

        if end_id not in prev and start_id != end_id:
            return None
        return self._build_route(start_id, end_id, prev)

    # ── reconstruct route ─────────────────────────────────
    def _build_route(self, start_id: str, end_id: str,
                     prev: dict[str, str]) -> OptimizedRoute:
        path: list[str] = []
        cur = end_id
        while cur != start_id:
            path.append(cur)
            cur = prev[cur]
        path.append(start_id)
        path.reverse()

        segments: list[RouteSegment] = []
        total_dist = 0.0
        total_dur = 0.0
        for i in range(len(path) - 1):
            fid, tid = path[i], path[i + 1]
            edge = next(
                (e for e in self.adj.get(fid, []) if e.to_id == tid),
                None,
            )
            s1 = self.store.get(fid)
            s2 = self.store.get(tid)
            if edge and s1 and s2:
                segments.append(RouteSegment(
                    from_station=s1,
                    to_station=s2,
                    duration_minutes=round(edge.duration_min, 1),
                    distance_km=round(edge.distance_km, 1),
                ))
                total_dist += edge.distance_km
                total_dur += edge.duration_min

        return OptimizedRoute(
            segments=segments,
            total_duration_minutes=round(total_dur, 1),
            total_distance_km=round(total_dist, 1),
            num_stops=len(path),
            path_station_ids=path,
        )

    # ── stats ─────────────────────────────────────────────
    @property
    def node_count(self) -> int:
        nodes = set(self.adj.keys())
        for edges in self.adj.values():
            for e in edges:
                nodes.add(e.to_id)
        return len(nodes)

    @property
    def edge_count(self) -> int:
        return sum(len(edges) for edges in self.adj.values())
