"""
Multi-operator European rail API client.
Radar with automatic station-based fallback.
"""

from __future__ import annotations
import asyncio, httpx, logging
from datetime import datetime, timezone
from typing import Optional
from .models import (
    Station, Coordinates, Departure,
    TrainPosition, Trip, Stopover,
)
from .station_store import haversine
from .cache import cache
from .config import settings

log = logging.getLogger(__name__)

PROFILES: dict[str, dict] = {
    "db": {
        "type": "rest",
        "base_url": "https://v6.db.transport.rest",
        "name": "Deutsche Bahn",
        "status": "stable",
    },
    "sbb": {
        "type": "rest_ch",
        "base_url": "https://transport.opendata.ch/v1",
        "name": "SBB / Swiss Railways",
        "status": "stable",
    },
    "oebb": {
        "type": "hafas",
        "base_url": "https://fahrplan.oebb.at/bin/mgate.exe",
        "name": "ÖBB",
        "auth": {"type": "AID", "aid": "OWDL4fE4ixNiPBBm"},
        "client": {"id": "OEBB", "type": "WEB", "name": "oebb", "v": ""},
        "ext": "OEBB.13",
        "ver": "1.57",
        "lang": "de",
        "status": "beta",
    },
    "sncf": {
        "type": "hafas",
        "base_url": "https://gateway.prod.caa-fran.hafas.de/bin/mgate.exe",
        "name": "SNCF",
        "auth": {"type": "AID", "aid": "n91dB8Z77MLdoR0K"},
        "client": {"id": "SNCF", "type": "WEB", "name": "webapp",
                   "l": "vs_webapp", "v": "2000000"},
        "ext": "SNCF.1",
        "ver": "1.46",
        "lang": "fr",
        "status": "beta",
    },
}


class RailAPIClient:
    def __init__(self, operator: str = "db", timeout: float = 20.0):
        if operator not in PROFILES:
            raise ValueError(f"Unknown operator: {operator}")
        self.operator = operator
        self.profile = PROFILES[operator]
        self.api_type = self.profile["type"]
        self.base = self.profile["base_url"]
        self.client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "EURailTracker/2.0 (github.com/gutentag-cloud)"},
        )

    async def close(self):
        await self.client.aclose()

    # ══════════════════════════════════════════════════
    #  STATION SEARCH
    # ══════════════════════════════════════════════════

    async def search_stations(self, query: str, limit: int = 8) -> list[Station]:
        cached = await cache.get("stations", op=self.operator, q=query, l=limit)
        if cached:
            return [Station(**s) for s in cached]
        try:
            if self.api_type == "rest":
                result = await self._rest_search(query, limit)
            elif self.api_type == "rest_ch":
                result = await self._ch_search(query, limit)
            elif self.api_type == "hafas":
                result = await self._hafas_search(query, limit)
            else:
                result = []
        except Exception as e:
            log.warning(f"{self.operator} search error: {e}")
            result = []
        if result:
            await cache.set("stations", [s.model_dump() for s in result],
                            ttl=settings.cache_ttl_stations,
                            op=self.operator, q=query, l=limit)
        return result

    # ══════════════════════════════════════════════════
    #  DEPARTURES
    # ══════════════════════════════════════════════════

    async def get_departures(self, stop_id: str, duration: int = 30) -> list[Departure]:
        cached = await cache.get("departures", op=self.operator, sid=stop_id, d=duration)
        if cached:
            return [Departure(**d) for d in cached]
        try:
            if self.api_type == "rest":
                result = await self._rest_departures(stop_id, duration)
            elif self.api_type == "rest_ch":
                result = await self._ch_departures(stop_id, duration)
            elif self.api_type == "hafas":
                result = await self._hafas_departures(stop_id, duration)
            else:
                result = []
        except Exception as e:
            log.warning(f"{self.operator} departures error for {stop_id}: {e}")
            result = []
        if result:
            await cache.set("departures", [d.model_dump() for d in result],
                            ttl=settings.cache_ttl_departures,
                            op=self.operator, sid=stop_id, d=duration)
        return result

    # ══════════════════════════════════════════════════
    #  TRIP DETAILS
    # ══════════════════════════════════════════════════

    async def get_trip(self, trip_id: str) -> Optional[Trip]:
        cached = await cache.get("trip", op=self.operator, tid=trip_id)
        if cached:
            return Trip(**cached)
        try:
            if self.api_type == "rest":
                result = await self._rest_trip(trip_id)
            elif self.api_type == "hafas":
                result = await self._hafas_trip(trip_id)
            else:
                result = None
        except Exception as e:
            log.warning(f"{self.operator} trip error: {e}")
            result = None
        if result:
            await cache.set("trip", result.model_dump(),
                            ttl=settings.cache_ttl_trips,
                            op=self.operator, tid=trip_id)
        return result

    # ══════════════════════════════════════════════════
    #  JOURNEY SEARCH (real timetable routing)
    # ══════════════════════════════════════════════════

    async def search_journeys(self, from_id: str, to_id: str,
                              results: int = 5) -> list[dict]:
        """Search real connections with actual departure times."""
        try:
            if self.api_type == "rest":
                return await self._rest_journeys(from_id, to_id, results)
            elif self.api_type == "rest_ch":
                return await self._ch_journeys(from_id, to_id, results)
            elif self.api_type == "hafas":
                return await self._hafas_journeys(from_id, to_id, results)
        except Exception as e:
            log.warning(f"{self.operator} journey error: {e}")
        return []

    # ══════════════════════════════════════════════════
    #  RADAR (bounding box trains)
    # ══════════════════════════════════════════════════

    async def try_radar(self, north: float, south: float,
                        east: float, west: float) -> list[TrainPosition]:
        """Try the DB radar API. Returns empty list on failure."""
        if self.api_type != "rest":
            return []
        try:
            resp = await self.client.get(f"{self.base}/radar", params={
                "north": north, "south": south,
                "east": east, "west": west,
                "duration": 30, "frames": 0,
                "results": 256, "polylines": "false",
                "language": "en",
            })
            if resp.status_code != 200:
                log.debug(f"Radar returned {resp.status_code}")
                return []
            data = resp.json()
            movements = data if isinstance(data, list) else \
                data.get("movements", data.get("journeys", []))
            return self._parse_radar(movements)
        except Exception as e:
            log.debug(f"Radar failed: {e}")
            return []

    def _parse_radar(self, movements: list) -> list[TrainPosition]:
        positions: list[TrainPosition] = []
        for mov in movements:
            loc = mov.get("location") or {}
            lat, lon = loc.get("latitude"), loc.get("longitude")
            if lat is None or lon is None:
                continue
            line = mov.get("line") or {}
            trip_id = mov.get("tripId", "")
            stopovers = mov.get("nextStopovers") or mov.get("stopovers") or []
            prev_name, next_name = "", ""
            for so in stopovers:
                stop = so.get("stop") or {}
                name = stop.get("name", "")
                if so.get("departure") and not prev_name:
                    prev_name = name
                if so.get("arrival") and not next_name:
                    next_name = name
                if prev_name and next_name:
                    break
            positions.append(TrainPosition(
                trip_id=trip_id,
                line_name=line.get("name", line.get("productName", "?")),
                direction=mov.get("direction", ""),
                coords=Coordinates(latitude=lat, longitude=lon),
                speed_kmh=None,
                prev_station=prev_name,
                next_station=next_name,
                progress=0.5,
                operator="db",
            ))
        return positions

    # ══════════════════════════════════════════════════
    #  INTERPOLATION
    # ══════════════════════════════════════════════════

    def interpolate(self, trip: Trip, now: datetime) -> Optional[TrainPosition]:
        for i in range(len(trip.stopovers) - 1):
            dep_str = trip.stopovers[i].departure
            arr_str = trip.stopovers[i + 1].arrival
            if not dep_str or not arr_str:
                continue
            try:
                dep_t = datetime.fromisoformat(dep_str)
                arr_t = datetime.fromisoformat(arr_str)
            except ValueError:
                continue
            if dep_t <= now <= arr_t:
                total = (arr_t - dep_t).total_seconds()
                frac = (now - dep_t).total_seconds() / total if total > 0 else 0
                frac = max(0.0, min(1.0, frac))
                p = trip.stopovers[i].station.coords
                n = trip.stopovers[i + 1].station.coords
                lat = p.latitude + frac * (n.latitude - p.latitude)
                lon = p.longitude + frac * (n.longitude - p.longitude)
                dist = haversine(p.latitude, p.longitude, n.latitude, n.longitude)
                speed = (dist / (total / 3600)) if total > 0 else None
                return TrainPosition(
                    trip_id=trip.id, line_name=trip.line_name,
                    direction=trip.direction,
                    coords=Coordinates(latitude=lat, longitude=lon),
                    speed_kmh=round(speed, 1) if speed else None,
                    prev_station=trip.stopovers[i].station.name,
                    next_station=trip.stopovers[i + 1].station.name,
                    progress=round(frac, 3), operator=self.operator,
                )
        return None

    # ══════════════════════════════════════════════════
    #  DB transport.rest implementations
    # ══════════════════════════════════════════════════

    async def _rest_search(self, query, limit):
        resp = await self.client.get(f"{self.base}/locations", params={
            "query": query, "results": limit,
            "stops": "true", "addresses": "false", "poi": "false",
        })
        resp.raise_for_status()
        out = []
        for loc in resp.json():
            if loc.get("type") not in ("stop", "station"):
                continue
            ld = loc.get("location") or {}
            lat, lon = ld.get("latitude"), ld.get("longitude")
            if lat is None or lon is None:
                continue
            out.append(Station(
                id=str(loc["id"]), name=loc.get("name", ""),
                coords=Coordinates(latitude=lat, longitude=lon),
                db_id=str(loc["id"]), operator="db",
            ))
        return out

    async def _rest_departures(self, stop_id, duration):
        resp = await self.client.get(
            f"{self.base}/stops/{stop_id}/departures",
            params={"duration": duration, "results": 30},
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("departures", data) if isinstance(data, dict) else data
        deps = []
        for dep in items:
            sd = dep.get("stop") or {}
            loc = sd.get("location") or {}
            line = dep.get("line") or {}
            deps.append(Departure(
                trip_id=dep.get("tripId", ""),
                line_name=line.get("name", "?"),
                direction=dep.get("direction", ""),
                planned_time=dep.get("plannedWhen") or "",
                actual_time=dep.get("when"),
                delay_seconds=dep.get("delay"),
                station=Station(
                    id=str(sd.get("id", stop_id)), name=sd.get("name", ""),
                    coords=Coordinates(latitude=loc.get("latitude", 0),
                                       longitude=loc.get("longitude", 0)),
                    db_id=str(sd.get("id", stop_id)), operator="db",
                ),
                platform=dep.get("platform"), operator="db",
            ))
        return deps

    async def _rest_trip(self, trip_id):
        resp = await self.client.get(
            f"{self.base}/trips/{trip_id}",
            params={"stopovers": "true", "polyline": "false"},
        )
        if resp.status_code != 200:
            return None
        data = resp.json().get("trip", resp.json())
        line = data.get("line") or {}
        stopovers = []
        for so in data.get("stopovers", []):
            s = so.get("stop") or {}
            loc = s.get("location") or {}
            stopovers.append(Stopover(
                station=Station(
                    id=str(s.get("id", "")), name=s.get("name", ""),
                    coords=Coordinates(latitude=loc.get("latitude", 0),
                                       longitude=loc.get("longitude", 0)),
                    db_id=str(s.get("id", "")), operator="db",
                ),
                arrival=so.get("arrival"), departure=so.get("departure"),
                delay_seconds=so.get("arrivalDelay") or so.get("departureDelay"),
            ))
        return Trip(id=data.get("id", trip_id), line_name=line.get("name", ""),
                    direction=data.get("direction", ""), stopovers=stopovers, operator="db")

    async def _rest_journeys(self, from_id, to_id, results):
        resp = await self.client.get(f"{self.base}/journeys", params={
            "from": from_id, "to": to_id, "results": results,
            "stopovers": "true", "transferTime": 0,
            "national": "true", "nationalExpress": "true",
            "regional": "true", "regionalExpress": "true",
        })
        resp.raise_for_status()
        raw = resp.json()
        return self._parse_journeys_rest(raw)

    def _parse_journeys_rest(self, raw: dict) -> list[dict]:
        """Parse DB transport.rest journeys into a clean format."""
        journeys = []
        for j in raw.get("journeys", []):
            legs = []
            for leg in j.get("legs", []):
                origin = leg.get("origin") or {}
                dest = leg.get("destination") or {}
                o_loc = origin.get("location") or {}
                d_loc = dest.get("location") or {}
                line = leg.get("line") or {}
                stopovers = []
                for so in leg.get("stopovers", []):
                    s = so.get("stop") or {}
                    sl = s.get("location") or {}
                    stopovers.append({
                        "name": s.get("name", ""),
                        "lat": sl.get("latitude", 0),
                        "lon": sl.get("longitude", 0),
                        "arrival": so.get("arrival"),
                        "departure": so.get("departure"),
                        "arr_delay": so.get("arrivalDelay"),
                        "dep_delay": so.get("departureDelay"),
                        "platform": so.get("arrivalPlatform") or so.get("departurePlatform"),
                    })
                legs.append({
                    "origin": origin.get("name", ""),
                    "origin_lat": o_loc.get("latitude", 0),
                    "origin_lon": o_loc.get("longitude", 0),
                    "destination": dest.get("name", ""),
                    "dest_lat": d_loc.get("latitude", 0),
                    "dest_lon": d_loc.get("longitude", 0),
                    "departure": leg.get("departure"),
                    "arrival": leg.get("arrival"),
                    "dep_delay": leg.get("departureDelay"),
                    "arr_delay": leg.get("arrivalDelay"),
                    "line": line.get("name", ""),
                    "product": line.get("productName", ""),
                    "direction": leg.get("direction", ""),
                    "platform": leg.get("departurePlatform"),
                    "walking": leg.get("walking", False),
                    "trip_id": leg.get("tripId", ""),
                    "stopovers": stopovers,
                })
            dep = j.get("legs", [{}])[0].get("departure")
            arr = j.get("legs", [{}])[-1].get("arrival") if j.get("legs") else None
            journeys.append({
                "legs": legs,
                "transfers": len(legs) - 1,
                "departure": dep,
                "arrival": arr,
            })
        return journeys

    # ══════════════════════════════════════════════════
    #  SBB implementations
    # ══════════════════════════════════════════════════

    async def _ch_search(self, query, limit):
        resp = await self.client.get(f"{self.base}/locations",
                                     params={"query": query, "type": "station"})
        resp.raise_for_status()
        out = []
        for s in resp.json().get("stations", [])[:limit]:
            c = s.get("coordinate") or {}
            if not c.get("x") or not c.get("y"):
                continue
            out.append(Station(
                id=str(s.get("id", "")), name=s.get("name", ""),
                coords=Coordinates(latitude=c["y"], longitude=c["x"]),
                db_id=str(s.get("id", "")), operator="sbb",
            ))
        return out

    async def _ch_departures(self, stop_id, duration):
        resp = await self.client.get(f"{self.base}/stationboard",
                                     params={"station": stop_id, "limit": 30})
        resp.raise_for_status()
        deps = []
        for e in resp.json().get("stationboard", []):
            st = e.get("stop", {}).get("station", {})
            co = st.get("coordinate") or {}
            delay_raw = e.get("stop", {}).get("delay")
            deps.append(Departure(
                trip_id=e.get("name", ""),
                line_name=f"{e.get('category', '')} {e.get('number', '')}".strip(),
                direction=e.get("to", ""),
                planned_time=e.get("stop", {}).get("departure", ""),
                delay_seconds=int(delay_raw) * 60 if delay_raw else None,
                station=Station(
                    id=str(st.get("id", stop_id)), name=st.get("name", ""),
                    coords=Coordinates(latitude=co.get("y", 0), longitude=co.get("x", 0)),
                    operator="sbb",
                ),
                platform=e.get("stop", {}).get("platform"), operator="sbb",
            ))
        return deps

    async def _ch_journeys(self, from_id, to_id, results):
        resp = await self.client.get(f"{self.base}/connections",
                                     params={"from": from_id, "to": to_id, "limit": results})
        resp.raise_for_status()
        raw = resp.json()
        journeys = []
        for conn in raw.get("connections", []):
            legs = []
            for sec in conn.get("sections", []):
                dep = sec.get("departure") or {}
                arr = sec.get("arrival") or {}
                dep_st = dep.get("station") or {}
                arr_st = arr.get("station") or {}
                dep_co = dep_st.get("coordinate") or {}
                arr_co = arr_st.get("coordinate") or {}
                j = sec.get("journey") or {}
                legs.append({
                    "origin": dep_st.get("name", ""),
                    "origin_lat": dep_co.get("y", 0),
                    "origin_lon": dep_co.get("x", 0),
                    "destination": arr_st.get("name", ""),
                    "dest_lat": arr_co.get("y", 0),
                    "dest_lon": arr_co.get("x", 0),
                    "departure": dep.get("departure"),
                    "arrival": arr.get("arrival"),
                    "dep_delay": dep.get("delay"),
                    "arr_delay": arr.get("delay"),
                    "line": j.get("name", ""),
                    "product": j.get("category", ""),
                    "direction": j.get("to", ""),
                    "platform": dep.get("platform"),
                    "walking": sec.get("walk") is not None,
                    "trip_id": "",
                    "stopovers": [],
                })
            journeys.append({
                "legs": legs,
                "transfers": max(0, len(legs) - 1),
                "departure": conn.get("from", {}).get("departure"),
                "arrival": conn.get("to", {}).get("arrival"),
            })
        return journeys

    # ══════════════════════════════════════════════════
    #  HAFAS implementations (ÖBB, SNCF)
    # ══════════════════════════════════════════════════

    def _hafas_body(self, method, req):
        body = {
            "id": "1", "ver": self.profile.get("ver", "1.46"),
            "lang": self.profile.get("lang", "en"),
            "auth": self.profile.get("auth", {}),
            "client": self.profile.get("client", {}),
            "formatted": False,
            "svcReqL": [{"meth": method, "req": req}],
        }
        if self.profile.get("ext"):
            body["ext"] = self.profile["ext"]
        return body

    def _hafas_parse(self, data):
        svc = data.get("svcResL", [])
        if not svc:
            return None
        res = svc[0]
        err = res.get("err")
        if err and err != "OK":
            log.warning(f"HAFAS {self.operator}: {err} — {res.get('errTxt', '')}")
            return None
        return res.get("res", {})

    async def _hafas_search(self, query, limit):
        body = self._hafas_body("LocMatch", {
            "input": {"field": "S", "loc": {"name": query + "?"},
                      "maxLoc": limit},
        })
        resp = await self.client.post(self.base, json=body)
        resp.raise_for_status()
        res = self._hafas_parse(resp.json())
        if not res:
            return []
        out = []
        for loc in res.get("match", {}).get("locL", []):
            crd = loc.get("crd", {})
            lat = crd.get("y", 0) / 1_000_000
            lon = crd.get("x", 0) / 1_000_000
            if not lat or not lon:
                continue
            out.append(Station(
                id=loc.get("extId", loc.get("lid", "")),
                name=loc.get("name", ""),
                coords=Coordinates(latitude=lat, longitude=lon),
                db_id=loc.get("extId", ""), operator=self.operator,
            ))
        return out

    async def _hafas_departures(self, stop_id, duration):
        body = self._hafas_body("StationBoard", {
            "stbLoc": {"lid": f"A=1@L={stop_id}@"},
            "type": "DEP", "dur": duration, "maxJny": 30,
        })
        resp = await self.client.post(self.base, json=body)
        resp.raise_for_status()
        res = self._hafas_parse(resp.json())
        if not res:
            return []
        common = res.get("common", {})
        prod_list = common.get("prodL", [])
        loc_list = common.get("locL", [])
        deps = []
        for jny in res.get("jnyL", []):
            stb = jny.get("stbStop", {})
            pi = jny.get("prodX", 0)
            prod = prod_list[pi] if pi < len(prod_list) else {}
            li = stb.get("locX", 0)
            loc = loc_list[li] if li < len(loc_list) else {}
            crd = loc.get("crd", {})
            planned = stb.get("dTimeS", "")
            date_str = jny.get("date", "")
            deps.append(Departure(
                trip_id=jny.get("jid", ""),
                line_name=prod.get("name", "?"),
                direction=jny.get("dirTxt", ""),
                planned_time=f"{date_str}T{planned}" if planned else "",
                station=Station(
                    id=loc.get("extId", stop_id), name=loc.get("name", ""),
                    coords=Coordinates(latitude=crd.get("y", 0) / 1_000_000,
                                       longitude=crd.get("x", 0) / 1_000_000),
                    operator=self.operator,
                ),
                platform=stb.get("dPlatfS"), operator=self.operator,
            ))
        return deps

    async def _hafas_trip(self, trip_id):
        body = self._hafas_body("JourneyDetails", {
            "jid": trip_id, "getPolyline": False,
        })
        resp = await self.client.post(self.base, json=body)
        if resp.status_code != 200:
            return None
        res = self._hafas_parse(resp.json())
        if not res:
            return None
        common = res.get("common", {})
        loc_list = common.get("locL", [])
        prod_list = common.get("prodL", [])
        journey = res.get("journey", {})
        date_str = journey.get("date", "")
        stopovers = []
        for stop in journey.get("stopL", []):
            li = stop.get("locX", 0)
            loc = loc_list[li] if li < len(loc_list) else {}
            crd = loc.get("crd", {})
            arr = stop.get("aTimeS", "")
            dep = stop.get("dTimeS", "")
            stopovers.append(Stopover(
                station=Station(
                    id=loc.get("extId", ""), name=loc.get("name", ""),
                    coords=Coordinates(latitude=crd.get("y", 0) / 1_000_000,
                                       longitude=crd.get("x", 0) / 1_000_000),
                    operator=self.operator,
                ),
                arrival=f"{date_str}T{arr}" if arr else None,
                departure=f"{date_str}T{dep}" if dep else None,
            ))
        pi = journey.get("prodX", 0)
        prod = prod_list[pi] if pi < len(prod_list) else {}
        return Trip(id=trip_id, line_name=prod.get("name", ""),
                    direction=journey.get("dirTxt", ""),
                    stopovers=stopovers, operator=self.operator)

    async def _hafas_journeys(self, from_id, to_id, results):
        body = self._hafas_body("TripSearch", {
            "depLocL": [{"lid": f"A=1@L={from_id}@", "type": "S"}],
            "arrLocL": [{"lid": f"A=1@L={to_id}@", "type": "S"}],
            "maxChg": 5, "numF": results, "getPolyline": False,
        })
        resp = await self.client.post(self.base, json=body)
        resp.raise_for_status()
        # Return raw HAFAS for now
        return []


# ── factory ──────────────────────────────────────────
_clients: dict[str, RailAPIClient] = {}

def get_client(operator: str = "db") -> RailAPIClient:
    if operator not in _clients:
        _clients[operator] = RailAPIClient(operator)
    return _clients[operator]

async def close_all_clients():
    for c in _clients.values():
        await c.close()
    _clients.clear()
