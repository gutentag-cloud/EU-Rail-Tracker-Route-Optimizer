"""
OpenStreetMap Overpass API client for rail track geometry.
Returns actual rail polylines instead of straight lines.
"""

from __future__ import annotations
import asyncio, time, logging
import httpx
from typing import Optional
from .models import TrackGeometry
from .cache import cache
from .config import settings

log = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Simple rate limiter
_last_request_times: list[float] = []


class OverpassClient:
    """Fetch railway track geometry from OpenStreetMap."""

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self.client.aclose()

    async def _rate_limit(self) -> None:
        """Enforce rate limiting: max N requests per 10 seconds."""
        now = time.time()
        window = 10.0
        max_req = settings.overpass_rate_limit

        # Clean old timestamps
        while _last_request_times and \
                _last_request_times[0] < now - window:
            _last_request_times.pop(0)

        if len(_last_request_times) >= max_req:
            wait = _last_request_times[0] + window - now
            if wait > 0:
                log.info(f"Overpass rate limit — waiting {wait:.1f}s")
                await asyncio.sleep(wait)

        _last_request_times.append(time.time())

    async def get_rail_geometry(
        self,
        from_lat: float, from_lon: float,
        to_lat: float, to_lon: float,
        buffer_km: float = 5.0,
    ) -> Optional[TrackGeometry]:
        """
        Fetch railway track lines in the bounding box between
        two points, with a buffer around the straight line.
        """
        # Check cache first
        cached = await cache.get(
            "geometry",
            flat=round(from_lat, 3), flon=round(from_lon, 3),
            tlat=round(to_lat, 3), tlon=round(to_lon, 3),
        )
        if cached:
            return TrackGeometry(**cached)

        # Calculate bounding box with buffer
        import math
        deg_buffer = buffer_km / 111.0  # rough km-to-degree

        min_lat = min(from_lat, to_lat) - deg_buffer
        max_lat = max(from_lat, to_lat) + deg_buffer
        min_lon = min(from_lon, to_lon) - deg_buffer
        max_lon = max(from_lon, to_lon) + deg_buffer

        query = f"""
        [out:json][timeout:25];
        (
          way["railway"="rail"]({min_lat},{min_lon},{max_lat},{max_lon});
        );
        out geom;
        """

        await self._rate_limit()

        try:
            resp = await self.client.post(
                OVERPASS_URL,
                data={"data": query},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning(f"Overpass API error: {e}")
            return None

        # Extract coordinates from all ways
        all_coords: list[list[float]] = []
        for element in data.get("elements", []):
            if element.get("type") != "way":
                continue
            geometry = element.get("geometry", [])
            for point in geometry:
                lat = point.get("lat")
                lon = point.get("lon")
                if lat is not None and lon is not None:
                    all_coords.append([lon, lat])

        if not all_coords:
            return None

        result = TrackGeometry(
            coordinates=all_coords,
            source="overpass",
        )

        # Cache the result
        await cache.set(
            "geometry",
            result.model_dump(),
            ttl=settings.cache_ttl_geometry,
            flat=round(from_lat, 3), flon=round(from_lon, 3),
            tlat=round(to_lat, 3), tlon=round(to_lon, 3),
        )

        return result

    async def get_rail_network_bbox(
        self,
        min_lat: float, min_lon: float,
        max_lat: float, max_lon: float,
    ) -> list[list[list[float]]]:
        """
        Get all rail ways in a bounding box.
        Returns list of line segments [[lon, lat], ...].
        """
        # Limit bbox size to prevent huge queries
        if (max_lat - min_lat) > 2.0 or (max_lon - min_lon) > 2.0:
            return []

        query = f"""
        [out:json][timeout:25];
        (
          way["railway"="rail"]["usage"~"main|branch"]
            ({min_lat},{min_lon},{max_lat},{max_lon});
        );
        out geom;
        """

        await self._rate_limit()

        try:
            resp = await self.client.post(
                OVERPASS_URL, data={"data": query},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning(f"Overpass API error: {e}")
            return []

        lines: list[list[list[float]]] = []
        for element in data.get("elements", []):
            if element.get("type") != "way":
                continue
            line: list[list[float]] = []
            for pt in element.get("geometry", []):
                lat, lon = pt.get("lat"), pt.get("lon")
                if lat is not None and lon is not None:
                    line.append([lon, lat])
            if len(line) >= 2:
                lines.append(line)
        return lines


# Global singleton
overpass = OverpassClient()
