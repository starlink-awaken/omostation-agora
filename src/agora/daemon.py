"""Agora 2.0 Daemon: In-Memory Agent-to-Agent Bus."""

import asyncio
import json
import logging
import os
import sys
import time
from typing import TYPE_CHECKING

# Optional fastapi, fallback to basic if not present
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from pydantic import BaseModel
    import uvicorn

    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("agora.daemon")

# Process start time for /health uptime reporting (monotonic wall clock).
_STARTED_AT = time.time()

# HTTP listen port. 7432 is registered to ecos-event-listener in
# protocols/port-registry.yaml, so the daemon defaults to 7433.
DEFAULT_HTTP_PORT = int(os.environ.get("AGORA_DAEMON_HTTP_PORT", "7433"))
# Reported (not bound) swarm discovery UDP port — registry: AGORA_SWARM_UDP_PORT=7455.
# /health only reports this config value; the daemon itself opens no UDP socket.
SWARM_UDP_PORT = int(os.environ.get("AGORA_SWARM_UDP_PORT", "7455"))


class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}
        self.subscribers: dict[str, set[WebSocket]] = {}

    async def connect(self, client_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[client_id] = websocket
        logger.info(f"Agent connected: {client_id}")

    def disconnect(self, client_id: str):
        ws = self.active_connections.pop(client_id, None)
        if ws:
            for subs in self.subscribers.values():
                subs.discard(ws)
        logger.info(f"Agent disconnected: {client_id}")

    async def broadcast(self, topic: str, message: dict):
        subs = self.subscribers.get(topic, set())
        if not subs:
            return
        dead = set()
        for ws in subs:
            try:
                await ws.send_json({"topic": topic, "payload": message})
            except Exception:
                dead.add(ws)
        for ws in dead:
            subs.discard(ws)

    def subscribe(self, client_id: str, topic: str):
        if client_id in self.active_connections:
            ws = self.active_connections[client_id]
            self.subscribers.setdefault(topic, set()).add(ws)
            logger.info(f"Agent {client_id} subscribed to {topic}")


if HAS_FASTAPI:
    app = FastAPI(title="Agora 2.0 Agent Bus")
    manager = ConnectionManager()

    class MessagePayload(BaseModel):
        topic: str
        payload: dict

    @app.post("/publish")
    async def publish_message(msg: MessagePayload):
        await manager.broadcast(msg.topic, msg.payload)
        return {
            "status": "ok",
            "subscribers_notified": len(manager.subscribers.get(msg.topic, set())),
        }

    @app.get("/health")
    async def health() -> dict:
        """Liveness probe consumed by the cron-service health scan (SIRI-51).

        udp_port reports the configured swarm discovery port
        (AGORA_SWARM_UDP_PORT, registry default 7455) — this daemon opens no
        UDP socket itself; the value surfaces config for the health scanner.
        """
        return {
            "status": "ok",
            "uptime_s": int(time.time() - _STARTED_AT),
            "udp_port": SWARM_UDP_PORT,
        }

    @app.websocket("/ws/{client_id}")
    async def websocket_endpoint(websocket: WebSocket, client_id: str):
        await manager.connect(client_id, websocket)
        try:
            while True:
                data = await websocket.receive_text()
                msg = json.loads(data)
                action = msg.get("action")
                if action == "subscribe":
                    manager.subscribe(client_id, msg.get("topic"))
                elif action == "publish":
                    await manager.broadcast(msg.get("topic"), msg.get("payload"))
        except WebSocketDisconnect:
            manager.disconnect(client_id)
        except Exception as e:
            logger.error(f"Error handling websocket for {client_id}: {e}")
            manager.disconnect(client_id)


def run_daemon(port: int | None = None):
    if not HAS_FASTAPI:
        logger.error("FastAPI not installed. Cannot run Agora 2.0 Daemon.")
        sys.exit(1)
    if port is None:
        port = DEFAULT_HTTP_PORT
    logger.info(f"Starting Agora 2.0 Daemon on port {port}")
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    run_daemon()
