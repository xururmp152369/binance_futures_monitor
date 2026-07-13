import asyncio
import json
from fastapi import WebSocket


class ConnectionManager:
    def __init__(self):
        self._clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket):
        self._clients.discard(ws)

    async def broadcast(self, data: dict):
        if not self._clients:
            return
        message = json.dumps(data)
        clients = list(self._clients)
        results = await asyncio.gather(
            *[ws.send_text(message) for ws in clients],
            return_exceptions=True,
        )
        for ws, result in zip(clients, results):
            if isinstance(result, Exception):
                self._clients.discard(ws)


manager = ConnectionManager()
