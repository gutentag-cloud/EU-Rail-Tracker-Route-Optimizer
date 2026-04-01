"""
WebSocket connection manager.
Broadcasts live train positions to all connected clients.
"""

from __future__ import annotations
import asyncio, json, logging, time
from typing import Optional
from fastapi import WebSocket
from .config import settings

log = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections grouped by stop_id."""

    def __init__(self) -> None:
        # stop_id → set of WebSocket connections
        self._connections: dict[str, set[WebSocket]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def connect(self, websocket: WebSocket,
                      stop_id: str) -> None:
        await websocket.accept()
        self._connections.setdefault(stop_id, set()).add(websocket)
        log.info(
            f"WS connected: {stop_id} "
            f"(total: {self.total_connections})"
        )

    def disconnect(self, websocket: WebSocket,
                   stop_id: str) -> None:
        conns = self._connections.get(stop_id, set())
        conns.discard(websocket)
        if not conns and stop_id in self._connections:
            del self._connections[stop_id]
        log.info(
            f"WS disconnected: {stop_id} "
            f"(total: {self.total_connections})"
        )

    async def broadcast(self, stop_id: str,
                        data: dict | list) -> int:
        """Send data to all connections watching a stop_id."""
        conns = self._connections.get(stop_id, set())
        if not conns:
            return 0

        message = json.dumps(data, default=str)
        dead: list[WebSocket] = []
        sent = 0

        for ws in conns:
            try:
                await ws.send_text(message)
                sent += 1
            except Exception:
                dead.append(ws)

        for ws in dead:
            conns.discard(ws)

        return sent

    async def broadcast_all(self, data: dict | list) -> int:
        """Send data to ALL connected clients."""
        total = 0
        for stop_id in list(self._connections.keys()):
            total += await self.broadcast(stop_id, data)
        return total

    @property
    def active_stop_ids(self) -> list[str]:
        return [
            sid for sid, conns in self._connections.items()
            if conns
        ]

    @property
    def total_connections(self) -> int:
        return sum(
            len(conns) for conns in self._connections.values()
        )


# Global singleton
ws_manager = ConnectionManager()


async def train_broadcast_loop(get_trains_fn) -> None:
    """
    Background task that periodically fetches train positions
    and broadcasts to all WebSocket clients.
    """
    interval = settings.ws_broadcast_interval
    log.info(
        f"🔄 Train broadcast loop started "
        f"(interval: {interval}s)"
    )

    while True:
        try:
            active = ws_manager.active_stop_ids
            for stop_id in active:
                try:
                    trains = await get_trains_fn(stop_id)
                    train_data = [
                        t.model_dump() for t in trains
                    ]
                    await ws_manager.broadcast(stop_id, {
                        "type": "train_positions",
                        "stop_id": stop_id,
                        "trains": train_data,
                        "timestamp": time.time(),
                    })
                except Exception as e:
                    log.warning(
                        f"Broadcast error for {stop_id}: {e}"
                    )
        except Exception as e:
            log.error(f"Broadcast loop error: {e}")

        await asyncio.sleep(interval)
