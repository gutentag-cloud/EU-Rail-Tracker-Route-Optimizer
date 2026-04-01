"""
Multi-operator European rail API client.

Supported operators:
  db   — Deutsche Bahn       via transport.rest (REST)
  oebb — ÖBB (Austria)       via HAFAS mgate
  sbb  — SBB (Switzerland)   via transport.opendata.ch (REST)
  sncf — SNCF (France)       via HAFAS mgate
"""

from __future__ import annotations
import httpx, json, math
from datetime import datetime, timezone
from typing import Optional
from .models import (
    Station, Coordinates, Departure,
    TrainPosition, Trip, Stopover,
)
from .station_store import haversine
from .cache import cache
from .config import settings

# ══════════════════════════════════════════════════════
#  OPERATOR PROFILES
# ══════════════════════════════════════════════════════

PROFILES: dict[str, dict] = {
    "db": {
        "type": "rest",
        "base_url": "https://v6.db.transport.rest",
        "name": "Deutsche Bahn",
    },
    "oebb": {
        "type": "hafas",
        "base_url": "https://fahrplan.oebb.at/bin/mgate.exe",
        "name": "ÖBB",
        "auth": {"type": "AID", "aid": "OWDL4fE4ixNiPBBm"},
        "client": {
            "id": "OEBB", "type": "WEB", "name": "oebb",
            "v": "1.0",
        },
        "ver": "1.57",
        "lang": "en",
    },
    "sbb": {
        "type": "rest_ch",
        "base_url": "https://transport.opendata.ch/v1",
        "name": "SBB / Swiss Railways",
    },
    "sncf": {
        "type": "hafas",
        "base_url": (
            "https://gateway.prod.caa-fran.hafas.de/"
            "bin/mgate.exe"
        ),
        "name": "SNCF",
        "auth": {"type": "AID", "aid": "n91dB8Z77MLdoR0K"},
        "client": {
            "id": "SNCF", "type": "WEB",
            "name": "webapp", "v": "2000000",
        },
        "ver": "1.46",
        "lang": "fr",
    },
}


class RailAPIClient:
    """Unified async client for multiple European rail operators."""

    def __init__(self, operator: str = "db",
                 timeout: float = 15.0):
        if operator not in PROFILES:
            raise ValueError(
                f"Unknown operator: {operator}. "
                f"Available: {list(PROFILES.keys())}"
            )
        self.operator = operator
        self.profile = PROFILES[operator]
        self.api_type = self.profile["type"]
        self.base = self.profile["base_url"]
        self.client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self.client.aclose()

    # ══════════════════════════════════════════════════
    #  PUBLIC API (operator-agnostic)
    # ══════════════════════════════════════════════════

    async def search_stations(self, query: str,
                              limit: int = 5) -> list[Station]:
        # Check cache first
        cached = await cache.get(
            "stations", op=self.operator, q=query, l=limit,
        )
        if cached:
            return [Station(**s) for s in cached]

        if self.api_type == "rest":
            result = await self._rest_search_stations(query, limit)
        elif self.api_type == "rest_ch":
            result = await self._ch_search_stations(query, limit)
        elif self.api_type == "hafas":
            result = await self._hafas_search_stations(query, limit)
        else:
            result = []

        await cache.set(
            "stations",
            [s.model_dump() for s in result],
            ttl=settings.cache_ttl_stations,
            op=self.operator, q=query, l=limit,
        )
        return result

    async def get_departures(self, stop_id: str,
                             duration: int = 30) -> list[Departure]:
        cached = await cache.get(
            "departures", op=self.operator, sid=stop_id, d=duration,
        )
        if cached:
            return [Departure(**d) for d in cached]

        if self.api_type == "rest":
            result = await self._rest_get_departures(stop_id, duration)
        elif self.api_type == "rest_ch":
            result = await self._ch_get_departures(stop_id, duration)
        elif self.api_type == "hafas":
            result = await self._hafas_get_departures(stop_id, duration)
        else:
            result = []

        await cache.set(
            "departures",
            [d.model_dump() for d in result],
            ttl=settings.cache_ttl_departures,
            op=self.operator, sid=stop_id, d=duration,
        )
        return result

    async def get_trip(self, trip_id: str) -> Optional[Trip]:
        cached = await cache.get(
            "trip", op=self.operator, tid=trip_id,
        )
        if cached:
            return Trip(**cached)

        if self.api_type == "rest":
            result = await self._rest_get_trip(trip_id)
        elif self.api_type == "rest_ch":
            result = None  # CH API doesn't have direct trip lookup
        elif self.api_type == "hafas":
            result = await self._hafas_get_trip(trip_id)
        else:
            result = None

        if result:
            await cache.set(
                "trip", result.model_dump(),
                ttl=settings.cache_ttl_trips,
                op=self.operator, tid=trip_id,
            )
        return result

    async def search_journeys(self, from_id: str, to_id: str,
                              results: int = 3) -> dict:
        if self.api_type == "rest":
            return await self._rest_search_journeys(
                from_id, to_id, results,
            )
        elif self.api_type == "rest_ch":
            return await self._ch_search_journeys(
                from_id, to_id, results,
            )
        elif self.api_type == "hafas":
            return await self._hafas_search_journeys(
                from_id, to_id, results,
            )
        return {}

    async def get_live_trains(self, stop_id: str,
                              duration: int = 30,
                              ) -> list[TrainPosition]:
        deps = await self.get_departures(stop_id, duration=duration)
        now = datetime.now(timezone.utc)
        positions: list[TrainPosition] = []

        for dep in deps[:8]:
            if not dep.trip_id:
                continue
            trip = await self.get_trip(dep.trip_id)
            if not trip or len(trip.stopovers) < 2:
                continue
            pos = self._interpolate_position(trip, now)
            if pos:
                positions.append(pos)
        return positions

    # ══════════════════════════════════════════════════
    #  DB — transport.rest
    # ══════════════════════════════════════════════════

    async def _rest_search_stations(self, query: str,
                                    limit: int) -> list[Station]:
        resp = await self.client.get(
            f"{self.base}/locations",
            params={
                "query": query, "results": limit,
                "stops": "true", "addresses": "false",
                "poi": "false",
            },
        )
        resp.raise_for_status()
        out: list[Station] = []
        for loc in resp.json():
            if loc.get("type") not in ("stop", "station"):
                continue
            ld = loc.get("location") or {}
            lat, lon = ld.get("latitude"), ld.get("longitude")
            if lat is None or lon is None:
                continue
            out.append(Station(
                id=str(loc["id"]),
                name=loc.get("name", ""),
                coords=Coordinates(latitude=lat, longitude=lon),
                db_id=str(loc["id"]),
                operator=self.operator,
            ))
        return out

    async def _rest_get_departures(self, stop_id: str,
                                   duration: int) -> list[Departure]:
        resp = await self.client.get(
            f"{self.base}/stops/{stop_id}/departures",
            params={"duration": duration, "results": 30},
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("departures", data) if isinstance(
            data, dict
        ) else data
        departures: list[Departure] = []
        for dep in items:
            sd = dep.get("stop") or {}
            loc = sd.get("location") or {}
            station = Station(
                id=str(sd.get("id", stop_id)),
                name=sd.get("name", ""),
                coords=Coordinates(
                    latitude=loc.get("latitude", 0),
                    longitude=loc.get("longitude", 0),
                ),
                db_id=str(sd.get("id", stop_id)),
                operator=self.operator,
            )
            line = dep.get("line") or {}
            departures.append(Departure(
                trip_id=dep.get("tripId", ""),
                line_name=line.get("name", "?"),
                direction=dep.get("direction", ""),
                planned_time=dep.get("plannedWhen") or "",
                actual_time=dep.get("when"),
                delay_seconds=dep.get("delay"),
                station=station,
                platform=dep.get("platform"),
                operator=self.operator,
            ))
        return departures

    async def _rest_get_trip(self, trip_id: str) -> Optional[Trip]:
        resp = await self.client.get(
            f"{self.base}/trips/{trip_id}",
            params={"stopovers": "true", "polyline": "false"},
        )
        if resp.status_code != 200:
            return None
        data = resp.json().get("trip", resp.json())
        line = data.get("line") or {}
        stopovers: list[Stopover] = []
        for so in data.get("stopovers", []):
            s = so.get("stop") or {}
            loc = s.get("location") or {}
            stopovers.append(Stopover(
                station=Station(
                    id=str(s.get("id", "")),
                    name=s.get("name", ""),
                    coords=Coordinates(
                        latitude=loc.get("latitude", 0),
                        longitude=loc.get("longitude", 0),
                    ),
                    db_id=str(s.get("id", "")),
                    operator=self.operator,
                ),
                arrival=so.get("arrival"),
                departure=so.get("departure"),
                delay_seconds=(
                    so.get("arrivalDelay") or so.get("departureDelay")
                ),
            ))
        return Trip(
            id=data.get("id", trip_id),
            line_name=line.get("name", ""),
            direction=data.get("direction", ""),
            stopovers=stopovers,
            operator=self.operator,
        )

    async def _rest_search_journeys(self, from_id: str,
                                    to_id: str,
                                    results: int) -> dict:
        resp = await self.client.get(
            f"{self.base}/journeys",
            params={
                "from": from_id, "to": to_id,
                "results": results, "stopovers": "true",
                "transferTime": 0,
                "national": "true", "nationalExpress": "true",
                "regional": "true", "regionalExpress": "true",
            },
        )
        resp.raise_for_status()
        return resp.json()

    # ══════════════════════════════════════════════════
    #  SBB — transport.opendata.ch
    # ══════════════════════════════════════════════════

    async def _ch_search_stations(self, query: str,
                                  limit: int) -> list[Station]:
        resp = await self.client.get(
            f"{self.base}/locations",
            params={"query": query, "type": "station"},
        )
        resp.raise_for_status()
        out: list[Station] = []
        for s in resp.json().get("stations", [])[:limit]:
            coord = s.get("coordinate") or {}
            if not coord.get("x") or not coord.get("y"):
                continue
            out.append(Station(
                id=str(s.get("id", "")),
                name=s.get("name", ""),
                coords=Coordinates(
                    latitude=coord["y"], longitude=coord["x"],
                ),
                operator="sbb",
            ))
        return out

    async def _ch_get_departures(self, stop_id: str,
                                 duration: int) -> list[Departure]:
        resp = await self.client.get(
            f"{self.base}/stationboard",
            params={"station": stop_id, "limit": 30},
        )
        resp.raise_for_status()
        departures: list[Departure] = []
        for entry in resp.json().get("stationboard", []):
            s = entry.get("stop", {}).get("station", {})
            coord = s.get("coordinate") or {}
            station = Station(
                id=str(s.get("id", stop_id)),
                name=s.get("name", ""),
                coords=Coordinates(
                    latitude=coord.get("y", 0),
                    longitude=coord.get("x", 0),
                ),
                operator="sbb",
            )
            departures.append(Departure(
                trip_id=entry.get("name", ""),
                line_name=entry.get("category", "")
                          + " " + entry.get("number", ""),
                direction=entry.get("to", ""),
                planned_time=entry.get("stop", {}).get(
                    "departure", ""
                ),
                delay_seconds=(
                    int(entry["stop"]["delay"]) * 60
                    if entry.get("stop", {}).get("delay")
                    else None
                ),
                station=station,
                platform=entry.get("stop", {}).get("platform"),
                operator="sbb",
            ))
        return departures

    async def _ch_search_journeys(self, from_id: str,
                                  to_id: str,
                                  results: int) -> dict:
        resp = await self.client.get(
            f"{self.base}/connections",
            params={"from": from_id, "to": to_id, "limit": results},
        )
        resp.raise_for_status()
        return resp.json()

    # ══════════════════════════════════════════════════
    #  HAFAS mgate protocol (ÖBB, SNCF, etc.)
    # ══════════════════════════════════════════════════

    def _hafas_request(self, method: str, req: dict) -> dict:
        """Build a HAFAS mgate JSON request body."""
        return {
            "id": "1",
            "ver": self.profile.get("ver", "1.46"),
            "lang": self.profile.get("lang", "en"),
            "auth": self.profile.get("auth", {}),
            "client": self.profile.get("client", {}),
            "formatted": False,
            "svcReqL": [{"meth": method, "req": req}],
        }

    def _parse_hafas_response(self, data: dict) -> Optional[dict]:
        """Extract result from HAFAS response wrapper."""
        svc = data.get("svcResL", [{}])
        if not svc:
            return None
        res = svc[0]
        if res.get("err") and res["err"] != "OK":
            return None
        return res.get("res", {})

    async def _hafas_search_stations(self, query: str,
                                     limit: int) -> list[Station]:
        body = self._hafas_request("LocMatch", {
            "input": {
                "field": "S", "loc": {"name": query + "?"},
                "maxLoc": limit,
            },
        })
        resp = await self.client.post(self.base, json=body)
        resp.raise_for_status()
        res = self._parse_hafas_response(resp.json())
        if not res:
            return []

        out: list[Station] = []
        match = res.get("match", {})
        for loc in match.get("locL", []):
            crd = loc.get("crd", {})
            lat = crd.get("y", 0) / 1_000_000
            lon = crd.get("x", 0) / 1_000_000
            if not lat or not lon:
                continue
            out.append(Station(
                id=loc.get("extId", loc.get("lid", "")),
                name=loc.get("name", ""),
                coords=Coordinates(latitude=lat, longitude=lon),
                operator=self.operator,
            ))
        return out

    async def _hafas_get_departures(self, stop_id: str,
                                    duration: int,
                                    ) -> list[Departure]:
        body = self._hafas_request("StationBoard", {
            "stbLoc": {"lid": f"A=1@L={stop_id}@"},
            "type": "DEP",
            "dur": duration,
            "maxJny": 30,
        })
        resp = await self.client.post(self.base, json=body)
        resp.raise_for_status()
        res = self._parse_hafas_response(resp.json())
        if not res:
            return []

        common = res.get("common", {})
        prod_list = common.get("prodL", [])
        loc_list = common.get("locL", [])
        departures: list[Departure] = []

        for jny in res.get("jnyL", []):
            stb_stop = jny.get("stbStop", {})
            prod_idx = jny.get("prodX", 0)
            prod = prod_list[prod_idx] if prod_idx < len(
                prod_list
            ) else {}
            loc_idx = stb_stop.get("locX", 0)
            loc = loc_list[loc_idx] if loc_idx < len(
                loc_list
            ) else {}
            crd = loc.get("crd", {})

            planned = stb_stop.get("dTimeS", "")
            actual = stb_stop.get("dTimeR", "")
            date_str = jny.get("date", "")

            station = Station(
                id=loc.get("extId", stop_id),
                name=loc.get("name", ""),
                coords=Coordinates(
                    latitude=crd.get("y", 0) / 1_000_000,
                    longitude=crd.get("x", 0) / 1_000_000,
                ),
                operator=self.operator,
            )
            departures.append(Departure(
                trip_id=jny.get("jid", ""),
                line_name=prod.get("name", "?"),
                direction=jny.get("dirTxt", ""),
                planned_time=f"{date_str}T{planned}" if planned
                             else "",
                actual_time=f"{date_str}T{actual}" if actual
                            else None,
                delay_seconds=None,
                station=station,
                platform=stb_stop.get("dPlatfS"),
                operator=self.operator,
            ))
        return departures

    async def _hafas_get_trip(self, trip_id: str) -> Optional[Trip]:
        body = self._hafas_request("JourneyDetails", {
            "jid": trip_id,
            "getPolyline": False,
        })
        resp = await self.client.post(self.base, json=body)
        if resp.status_code != 200:
            return None
        res = self._parse_hafas_response(resp.json())
        if not res:
            return None

        common = res.get("common", {})
        loc_list = common.get("locL", [])
        prod_list = common.get("prodL", [])
        journey = res.get("journey", {})
        stops_l = journey.get("stopL", [])
        date_str = journey.get("date", "")

        stopovers: list[Stopover] = []
        for stop in stops_l:
            loc_idx = stop.get("locX", 0)
            loc = loc_list[loc_idx] if loc_idx < len(
                loc_list
            ) else {}
            crd = loc.get("crd", {})
            arr = stop.get("aTimeS", "")
            dep = stop.get("dTimeS", "")

            stopovers.append(Stopover(
                station=Station(
                    id=loc.get("extId", ""),
                    name=loc.get("name", ""),
                    coords=Coordinates(
                        latitude=crd.get("y", 0) / 1_000_000,
                        longitude=crd.get("x", 0) / 1_000_000,
                    ),
                    operator=self.operator,
                ),
                arrival=f"{date_str}T{arr}" if arr else None,
                departure=f"{date_str}T{dep}" if dep else None,
            ))

        prod_idx = journey.get("prodX", 0)
        prod = prod_list[prod_idx] if prod_idx < len(
            prod_list
        ) else {}

        return Trip(
            id=trip_id,
            line_name=prod.get("name", ""),
            direction=journey.get("dirTxt", ""),
            stopovers=stopovers,
            operator=self.operator,
        )

    async def _hafas_search_journeys(self, from_id: str,
                                     to_id: str,
                                     results: int) -> dict:
        body = self._hafas_request("TripSearch", {
            "depLocL": [
                {"lid": f"A=1@L={from_id}@", "type": "S"},
            ],
            "arrLocL": [
                {"lid": f"A=1@L={to_id}@", "type": "S"},
            ],
            "maxChg": 5,
            "numF": results,
            "getPolyline": False,
        })
        resp = await self.client.post(self.base, json=body)
        resp.raise_for_status()
        return resp.json()

    # ══════════════════════════════════════════════════
    #  POSITION INTERPOLATION
    # ══════════════════════════════════════════════════

    def _interpolate_position(self, trip: Trip,
                              now: datetime,
                              ) -> Optional[TrainPosition]:
        stopovers = trip.stopovers
        for i in range(len(stopovers) - 1):
            dep_str = stopovers[i].departure
            arr_str = stopovers[i + 1].arrival
            if not dep_str or not arr_str:
                continue
            try:
                dep_t = datetime.fromisoformat(dep_str)
                arr_t = datetime.fromisoformat(arr_str)
            except ValueError:
                continue

            if dep_t <= now <= arr_t:
                total = (arr_t - dep_t).total_seconds()
                elapsed = (now - dep_t).total_seconds()
                frac = max(0.0, min(1.0,
                    elapsed / total if total > 0 else 0.0
                ))

                p = stopovers[i].station.coords
                n = stopovers[i + 1].station.coords
                lat = p.latitude + frac * (n.latitude - p.latitude)
                lon = p.longitude + frac * (n.longitude - p.longitude)

                dist = haversine(
                    p.latitude, p.longitude,
                    n.latitude, n.longitude,
                )
                speed = (dist / (total / 3600)) if total > 0 \
                    else None

                return TrainPosition(
                    trip_id=trip.id,
                    line_name=trip.line_name,
                    direction=trip.direction,
                    coords=Coordinates(
                        latitude=lat, longitude=lon,
                    ),
                    speed_kmh=round(speed, 1) if speed else None,
                    prev_station=stopovers[i].station.name,
                    next_station=stopovers[i + 1].station.name,
                    progress=round(frac, 3),
                    operator=self.operator,
                )
        return None


# ── Factory ───────────────────────────────────────────

_clients: dict[str, RailAPIClient] = {}


def get_client(operator: str = "db") -> RailAPIClient:
    if operator not in _clients:
        _clients[operator] = RailAPIClient(operator)
    return _clients[operator]


async def close_all_clients() -> None:
    for client in _clients.values():
        await client.close()
    _clients.clear()
