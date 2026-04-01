"""
Time-expanded graph for timetable-based routing.

Nodes = (station_id, timestamp)
Edges = specific train departures or waiting at a station.

This provides exact departure/arrival times unlike the
distance-based optimizer.
"""

from __future__ import annotations
import heapq
from datetime import datetime, timedelta, timezone
from typing import Optional
from .models import (
    TimetableEdge, TimetableRoute, Stopover, Trip,
)

INF = float("inf")


class TimeNode:
    """A node in the time-expanded graph: (station, time)."""
    __slots__ = ("station_id", "time")

    def __init__(self, station_id: str, time: datetime):
        self.station_id = station_id
        self.time = time

    def __hash__(self):
        return hash((self.station_id, self.time.timestamp()))

    def __eq__(self, other):
        return (self.station_id == other.station_id and
                abs((self.time - other.time).total_seconds()) < 30)

    def __lt__(self, other):
        return self.time < other.time

    @property
    def key(self) -> str:
        return f"{self.station_id}@{int(self.time.timestamp())}"


class TimeEdge:
    """An edge in the time-expanded graph."""
    __slots__ = ("from_node", "to_node", "line_name",
                 "trip_id", "is_transfer")

    def __init__(self, from_node: TimeNode, to_node: TimeNode,
                 line_name: str = "", trip_id: str = "",
                 is_transfer: bool = False):
        self.from_node = from_node
        self.to_node = to_node
        self.line_name = line_name
        self.trip_id = trip_id
        self.is_transfer = is_transfer

    @property
    def duration_minutes(self) -> float:
        return (self.to_node.time -
                self.from_node.time).total_seconds() / 60.0


class TimetableGraph:
    """
    Time-expanded graph built from trip/stopover data.
    """

    def __init__(self) -> None:
        self.edges: dict[str, list[TimeEdge]] = {}
        self._trip_count = 0

    def add_trip(self, trip: Trip) -> int:
        """
        Add all edges from a trip's stopovers to the graph.
        Returns number of edges added.
        """
        count = 0
        stopovers = trip.stopovers

        for i in range(len(stopovers) - 1):
            dep_str = stopovers[i].departure
            arr_str = stopovers[i + 1].arrival
            if not dep_str or not arr_str:
                continue

            try:
                dep_time = datetime.fromisoformat(dep_str)
                arr_time = datetime.fromisoformat(arr_str)
            except ValueError:
                continue

            from_node = TimeNode(
                stopovers[i].station.id, dep_time,
            )
            to_node = TimeNode(
                stopovers[i + 1].station.id, arr_time,
            )
            edge = TimeEdge(
                from_node, to_node,
                line_name=trip.line_name,
                trip_id=trip.id,
            )

            self.edges.setdefault(
                from_node.key, []
            ).append(edge)
            count += 1

        # Add waiting edges at each stop (up to 2h)
        for so in stopovers:
            if not so.arrival and not so.departure:
                continue
            try:
                t = datetime.fromisoformat(
                    so.arrival or so.departure
                )
            except ValueError:
                continue

            # Create waiting edges in 5-minute increments
            for wait_min in range(5, 121, 5):
                wait_from = TimeNode(so.station.id, t)
                wait_to = TimeNode(
                    so.station.id,
                    t + timedelta(minutes=wait_min),
                )
                wait_edge = TimeEdge(
                    wait_from, wait_to,
                    line_name="(wait)",
                    is_transfer=True,
                )
                self.edges.setdefault(
                    wait_from.key, []
                ).append(wait_edge)
                count += 1

        self._trip_count += 1
        return count

    def find_route(
        self,
        from_station_id: str,
        to_station_id: str,
        depart_after: datetime,
        max_transfers: int = 5,
    ) -> Optional[TimetableRoute]:
        """
        Find the earliest arrival route using Dijkstra
        on the time-expanded graph.
        """
        # Find all departure nodes from this station after the given time
        start_nodes = []
        for key, edges in self.edges.items():
            for edge in edges:
                if (edge.from_node.station_id == from_station_id and
                        edge.from_node.time >= depart_after):
                    start_nodes.append(edge.from_node)

        if not start_nodes:
            return None

        # Dijkstra by earliest arrival
        best_arrival: dict[str, datetime] = {}
        prev: dict[str, tuple[TimeEdge, str]] = {}

        # Priority queue: (arrival_time_ts, transfers, node_key)
        pq: list[tuple[float, int, str, TimeNode]] = []

        for sn in start_nodes:
            key = sn.key
            if key not in best_arrival or sn.time < best_arrival[key]:
                best_arrival[key] = sn.time
                heapq.heappush(
                    pq, (sn.time.timestamp(), 0, key, sn)
                )

        found_key: Optional[str] = None
        found_node: Optional[TimeNode] = None

        while pq:
            arr_ts, transfers, node_key, node = heapq.heappop(pq)

            if node.station_id == to_station_id:
                found_key = node_key
                found_node = node
                break

            arr_time = datetime.fromtimestamp(
                arr_ts, tz=timezone.utc
            )
            if node_key in best_arrival and \
                    arr_time > best_arrival[node_key]:
                continue

            for edge in self.edges.get(node_key, []):
                new_transfers = transfers + (
                    1 if edge.is_transfer and
                    not edge.line_name.startswith("(wait)")
                    else 0
                )
                if new_transfers > max_transfers:
                    continue

                to_key = edge.to_node.key
                to_time = edge.to_node.time

                if to_key not in best_arrival or \
                        to_time < best_arrival[to_key]:
                    best_arrival[to_key] = to_time
                    prev[to_key] = (edge, node_key)
                    heapq.heappush(pq, (
                        to_time.timestamp(), new_transfers,
                        to_key, edge.to_node,
                    ))

        if not found_key:
            return None

        # Reconstruct path
        legs: list[TimetableEdge] = []
        current_key = found_key
        while current_key in prev:
            edge, from_key = prev[current_key]
            if not edge.is_transfer:
                legs.append(TimetableEdge(
                    from_station_id=edge.from_node.station_id,
                    to_station_id=edge.to_node.station_id,
                    depart_time=edge.from_node.time,
                    arrive_time=edge.to_node.time,
                    line_name=edge.line_name,
                    trip_id=edge.trip_id,
                ))
            current_key = from_key

        legs.reverse()

        if not legs:
            return None

        total_dur = (
            legs[-1].arrive_time - legs[0].depart_time
        ).total_seconds() / 60.0

        # Count transfers (changes in line_name)
        transfers = 0
        for i in range(1, len(legs)):
            if legs[i].line_name != legs[i - 1].line_name:
                transfers += 1

        return TimetableRoute(
            legs=legs,
            total_duration_minutes=round(total_dur, 1),
            num_transfers=transfers,
            depart_time=legs[0].depart_time.isoformat(),
            arrive_time=legs[-1].arrive_time.isoformat(),
        )

    @property
    def node_count(self) -> int:
        return len(self.edges)

    @property
    def edge_count(self) -> int:
        return sum(len(e) for e in self.edges.values())

    @property
    def trip_count(self) -> int:
        return self._trip_count
