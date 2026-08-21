from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self):
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def add(self, household_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections[household_id].add(websocket)

    async def remove(self, household_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            connections = self._connections.get(household_id)
            if not connections:
                return
            connections.discard(websocket)
            if not connections:
                self._connections.pop(household_id, None)

    async def broadcast(self, household_id: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            connections = list(self._connections.get(household_id, set()))
        stale = []
        for websocket in connections:
            try:
                await websocket.send_json(payload)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            await self.remove(household_id, websocket)

    async def close_household(self, household_id: str, code: int = 1000) -> None:
        async with self._lock:
            connections = list(self._connections.pop(household_id, set()))
        for websocket in connections:
            try:
                await websocket.close(code=code)
            except Exception:
                pass
