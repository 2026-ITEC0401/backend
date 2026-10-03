from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self):
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)
        self._user_connections: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def add(
        self,
        household_id: str,
        user_id: str,
        websocket: WebSocket,
    ) -> None:
        async with self._lock:
            self._connections[household_id].add(websocket)
            self._user_connections[user_id].add(websocket)

    async def remove(
        self,
        household_id: str,
        user_id: str,
        websocket: WebSocket,
    ) -> None:
        async with self._lock:
            connections = self._connections.get(household_id)
            if connections:
                connections.discard(websocket)
                if not connections:
                    self._connections.pop(household_id, None)
            user_connections = self._user_connections.get(user_id)
            if user_connections:
                user_connections.discard(websocket)
                if not user_connections:
                    self._user_connections.pop(user_id, None)

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
            async with self._lock:
                for user_id, user_connections in list(self._user_connections.items()):
                    if websocket in user_connections:
                        user_connections.discard(websocket)
                        if not user_connections:
                            self._user_connections.pop(user_id, None)
                connections = self._connections.get(household_id)
                if connections:
                    connections.discard(websocket)
                    if not connections:
                        self._connections.pop(household_id, None)

    async def close_user(self, user_id: str, code: int = 1000) -> None:
        async with self._lock:
            connections = list(self._user_connections.pop(user_id, set()))
            for household_id, household_connections in list(self._connections.items()):
                household_connections.difference_update(connections)
                if not household_connections:
                    self._connections.pop(household_id, None)
        for websocket in connections:
            try:
                await websocket.close(code=code)
            except Exception:
                pass

    async def close_household(self, household_id: str, code: int = 1000) -> None:
        async with self._lock:
            connections = list(self._connections.pop(household_id, set()))
            for user_id, user_connections in list(self._user_connections.items()):
                user_connections.difference_update(connections)
                if not user_connections:
                    self._user_connections.pop(user_id, None)
        for websocket in connections:
            try:
                await websocket.close(code=code)
            except Exception:
                pass
