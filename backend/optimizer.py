"""
Graph-based route optimizer with multiple algorithms:
  • Dijkstra  — guaranteed shortest path
  • A*        — faster with haversine heuristic
  • Pareto    — multi-criteria (time × transfers × distance)
"""

from __future__ import annotations
import heapq, json, os, math
from typing import Optional
from .models import (
    Station, RouteSegment, OptimizedRoute,
    ParetoRoute, ParetoResult,
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
    def __init__(self, store: StationStore):
        self.store = store
        self.adj: dict[str, list[Edge]] = {}

    # ── graph construction ────────────────────────────

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
            duration_min = (distance_km / 100.0) * 60.0

        self.adj.setdefault(from_id, []).append(
            Edge(to_id, distance_km, duration_min)
        )
        if bidirectional:
            self.adj.setdefault(to_id, []).append(
                Edge(from_id, distance_km, duration_min)
            )

    def build_from_nearby(self, max_km: float = 120) -> int:
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

    def load_connections(self,
                         path: str = CONNECTIONS_PATH) -> int:
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

    def save_connections(self,
                         path: str = CONNECTIONS_PATH) -> None:
        conns = []
        seen: set[tuple[str, str]] = set()
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

    # ── Dijkstra ──────────────────────────────────────

    def dijkstra(self, start_id: str, end_id: str,
                 weight: str = "duration",
                 ) -> Optional[OptimizedRoute]:
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
                w = (edge.duration_min if weight == "duration"
                     else edge.distance_km)
                nd = d + w
                if nd < dist.get(edge.to_id, math.inf):
                    dist[edge.to_id] = nd
                    prev[edge.to_id] = u
                    heapq.heappush(pq, (nd, edge.to_id))

        if end_id not in prev and start_id != end_id:
            return None
        route = self._build_route(start_id, end_id, prev)
        route.algorithm = "dijkstra"
        return route

    # ── A* ────────────────────────────────────────────

    def astar(self, start_id: str, end_id: str,
              weight: str = "duration",
              ) -> Optional[OptimizedRoute]:
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
                return (d / 200.0) * 60.0
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
                w = (edge.duration_min if weight == "duration"
                     else edge.distance_km)
                ng = g[u] + w
                if ng < g.get(edge.to_id, math.inf):
                    g[edge.to_id] = ng
                    prev[edge.to_id] = u
                    heapq.heappush(
                        pq, (ng + h(edge.to_id), edge.to_id)
                    )

        if end_id not in prev and start_id != end_id:
            return None
        route = self._build_route(start_id, end_id, prev)
        route.algorithm = "astar"
        return route

    # ── Pareto Multi-Criteria ─────────────────────────

    def pareto(self, start_id: str, end_id: str,
               max_solutions: int = 10,
               ) -> ParetoResult:
        """
        Find all Pareto-optimal routes considering:
          - total duration (minutes)
          - number of hops (proxy for transfers)
          - total distance (km)

        Uses label-setting algorithm with dominance pruning.
        """

        # Label: (duration, hops, distance, node_id, path)
        Label = tuple[float, int, float, str, list[str]]

        initial: Label = (0.0, 0, 0.0, start_id, [start_id])
        pq: list[Label] = [initial]

        # Best labels per node (Pareto set)
        labels: dict[str, list[tuple[float, int, float]]] = {
            start_id: [(0.0, 0, 0.0)],
        }
        solutions: list[Label] = []
        explored = 0

        def dominates(a: tuple, b: tuple) -> bool:
            """a dominates b if a <= b in all and a < b in at least one."""
            return (a[0] <= b[0] and a[1] <= b[1] and a[2] <= b[2] and
                    (a[0] < b[0] or a[1] < b[1] or a[2] < b[2]))

        while pq and len(solutions) < max_solutions * 3:
            dur, hops, dist, uid, path = heapq.heappop(pq)
            explored += 1

            if uid == end_id:
                # Check if dominated by existing solutions
                obj = (dur, hops, dist)
                if not any(dominates(
                    (s[0], s[1], s[2]), obj
                ) for s in solutions):
                    solutions.append(
                        (dur, hops, dist, uid, path)
                    )
                    # Remove dominated solutions
                    solutions = [
                        s for s in solutions
                        if not dominates(obj, (s[0], s[1], s[2]))
                        or s == (dur, hops, dist, uid, path)
                    ]
                continue

            if explored > 50_000:
                break

            for edge in self.adj.get(uid, []):
                if edge.to_id in path:  # no cycles
                    continue
                new_dur = dur + edge.duration_min
                new_hops = hops + 1
                new_dist = dist + edge.distance_km
                new_obj = (new_dur, new_hops, new_dist)

                # Check dominance against existing labels
                existing = labels.get(edge.to_id, [])
                if any(dominates(e, new_obj) for e in existing):
                    continue

                # Remove dominated labels
                labels[edge.to_id] = [
                    e for e in existing
                    if not dominates(new_obj, e)
                ]
                labels[edge.to_id].append(new_obj)

                new_path = path + [edge.to_id]
                heapq.heappush(pq, (
                    new_dur, new_hops, new_dist,
                    edge.to_id, new_path,
                ))

        # Build ParetoResult
        pareto_routes: list[ParetoRoute] = []
        for dur, hops, dist, _, path in solutions[:max_solutions]:
            route = self._build_route_from_path(path)
            if route:
                route.algorithm = "pareto"
                pareto_routes.append(ParetoRoute(
                    route=route,
                    objectives={
                        "duration_minutes": round(dur, 1),
                        "transfers": hops - 1,
                        "distance_km": round(dist, 1),
                    },
                ))

        return ParetoResult(
            routes=pareto_routes,
            total_explored=explored,
        )

    # ── route reconstruction ──────────────────────────

    def _build_route(self, start_id: str, end_id: str,
                     prev: dict[str, str]) -> OptimizedRoute:
        path: list[str] = []
        cur = end_id
        while cur != start_id:
            path.append(cur)
            cur = prev[cur]
        path.append(start_id)
        path.reverse()
        return self._build_route_from_path(path) or OptimizedRoute(
            segments=[], total_duration_minutes=0,
            total_distance_km=0, num_stops=0,
            path_station_ids=path,
        )

    def _build_route_from_path(self,
                               path: list[str],
                               ) -> Optional[OptimizedRoute]:
        segments: list[RouteSegment] = []
        total_dist = 0.0
        total_dur = 0.0

        for i in range(len(path) - 1):
            fid, tid = path[i], path[i + 1]
            edge = next(
                (e for e in self.adj.get(fid, [])
                 if e.to_id == tid),
                None,
            )
            s1 = self.store.get(fid)
            s2 = self.store.get(tid)
            if edge and s1 and s2:
                segments.append(RouteSegment(
                    from_station=s1, to_station=s2,
                    duration_minutes=round(edge.duration_min, 1),
                    distance_km=round(edge.distance_km, 1),
                ))
                total_dist += edge.distance_km
                total_dur += edge.duration_min

        if not segments:
            return None

        return OptimizedRoute(
            segments=segments,
            total_duration_minutes=round(total_dur, 1),
            total_distance_km=round(total_dist, 1),
            num_stops=len(path),
            num_transfers=max(0, len(path) - 2),
            path_station_ids=path,
        )

    # ── stats ─────────────────────────────────────────

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
