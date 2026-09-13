"""agora.transport.p2p_mesh — Home LAN P2P mesh network management.

Implements a WireGuard-based self-organizing mesh for edge devices.
Handles node discovery, connection lifecycle, message encoding/decoding,
and automatic reconnection during network fluctuations.

Design goals:
- Local-First: no cloud relay dependency
- P2P encrypted tunnels via WireGuard
- Resilient to network partitions and node churn
- < 10MB memory footprint for the edge daemon
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import struct
import time
from dataclasses import dataclass, field, asdict
from enum import IntEnum
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Wire protocol — binary frame layout
#
#   magic(4) | version(1) | type(1) | flags(2) | payload_len(4) | digest(4) | payload(var)
#
#   Total header: 16 bytes
#   digest = first 4 bytes of sha256(payload)  (lightweight integrity check)
# ---------------------------------------------------------------------------

MAGIC = b"\x74\x42\x4f\x53"  # "tBOS"
VERSION = 0x01
HEADER_SIZE = 16
MAX_PAYLOAD = 65_536  # 64 KiB per frame


class FrameType(IntEnum):
    """Mesh frame type codes."""
    PING = 0x01
    PONG = 0x02
    DATA = 0x10
    DISCOVERY = 0x20
    DISCOVERY_ACK = 0x21
    ROUTE_UPDATE = 0x30
    ROAM_ANNOUNCE = 0x40


class MeshError(Exception):
    """Base exception for mesh operations."""


class DecodeError(MeshError):
    """Raised when a frame cannot be decoded."""


class EncodeError(MeshError):
    """Raised when a frame cannot be encoded."""


# ---------------------------------------------------------------------------
# Encoding / decoding
# ---------------------------------------------------------------------------

def compute_digest(payload: bytes) -> bytes:
    """Return the first 4 bytes of SHA-256(payload)."""
    return hashlib.sha256(payload).digest()[:4]


def encode_frame(
    frame_type: FrameType,
    payload: bytes = b"",
    flags: int = 0,
) -> bytes:
    """Encode a mesh frame (header + payload).

    Args:
        frame_type: Purpose of the frame.
        payload: Raw payload bytes (max 64 KiB).
        flags: 16-bit flags bitmask.

    Returns:
        Wire-format frame as bytes.

    Raises:
        EncodeError: If payload exceeds maximum size.
    """
    if len(payload) > MAX_PAYLOAD:
        raise EncodeError(f"payload too large: {len(payload)} > {MAX_PAYLOAD}")

    header = struct.pack(
        "!4sBBHI",
        MAGIC,
        VERSION,
        int(frame_type),
        flags,
        len(payload),
    )
    digest = compute_digest(payload)
    return header + digest + payload


def decode_frame(data: bytes) -> tuple[FrameType, int, bytes]:
    """Decode a mesh frame from wire format.

    Args:
        data: Raw bytes containing at least one full frame.

    Returns:
        Tuple of (frame_type, flags, payload).

    Raises:
        DecodeError: If the frame is malformed or integrity check fails.
    """
    if len(data) < HEADER_SIZE:
        raise DecodeError(f"frame too short: {len(data)} < {HEADER_SIZE}")

    magic, version, frame_type, flags, payload_len = struct.unpack(
        "!4sBBHI", data[:12]
    )

    if magic != MAGIC:
        raise DecodeError(f"bad magic: {magic!r}")

    if version != VERSION:
        raise DecodeError(f"unsupported version: {version}")

    digest = data[12:HEADER_SIZE]
    payload = data[HEADER_SIZE : HEADER_SIZE + payload_len]

    if len(payload) != payload_len:
        raise DecodeError(
            f"truncated payload: got {len(payload)}, expected {payload_len}"
        )

    expected_digest = compute_digest(payload)
    if digest != expected_digest:
        raise DecodeError("digest mismatch")

    return FrameType(frame_type), flags, payload


def encode_message(content: dict[str, Any]) -> bytes:
    """Serialize a JSON-serializable dict to wire payload."""
    return json.dumps(content, separators=(",", ":")).encode("utf-8")


def decode_message(payload: bytes) -> dict[str, Any]:
    """Deserialize a JSON payload dict."""
    return json.loads(payload.decode("utf-8"))


# ---------------------------------------------------------------------------
# Node identity
# ---------------------------------------------------------------------------

@dataclass
class MeshNode:
    """A peer in the mesh."""
    node_id: str
    endpoint: str  # "host:port"
    public_key: str = ""
    last_seen: float = 0.0
    rtt_ms: float = 0.0
    is_alive: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MeshNode":
        return cls(**data)


# ---------------------------------------------------------------------------
# Mesh manager
# ---------------------------------------------------------------------------

class Transport(Protocol):
    """Abstract transport for sending frames."""

    async def send(self, endpoint: str, frame: bytes) -> None: ...
    async def receive(self) -> tuple[str, bytes]: ...


class MeshManager:
    """Manages the local node's participation in a P2P mesh.

    Responsibilities:
    - Node registry (peers discovered and their liveness)
    - Frame routing and delivery
    - Automatic reconnection after network partition
    - Roaming handoff announcement when primary workstation sleeps
    """

    def __init__(
        self,
        node_id: str,
        listen_addr: str = "0.0.0.0:9100",
        transport: Transport | None = None,
    ) -> None:
        self.node_id = node_id
        self.listen_addr = listen_addr
        self._transport = transport
        self._peers: dict[str, MeshNode] = {}
        self._handlers: dict[FrameType, list[Callable]] = {}
        self._running = False
        self._ping_interval = 30.0  # seconds
        self._reconnect_delay = 5.0  # seconds

    # -- peer management ---------------------------------------------------

    def register_peer(self, node: MeshNode) -> None:
        """Add or update a peer node."""
        self._peers[node.node_id] = node
        logger.info("peer registered: %s @ %s", node.node_id, node.endpoint)

    def remove_peer(self, node_id: str) -> None:
        """Remove a peer from the registry."""
        self._peers.pop(node_id, None)

    def get_alive_peers(self) -> list[MeshNode]:
        """Return currently alive peers."""
        return [n for n in self._peers.values() if n.is_alive]

    # -- frame I/O ---------------------------------------------------------

    async def send_to(
        self, peer_id: str, frame_type: FrameType, payload: bytes = b""
    ) -> None:
        """Send a frame to a specific peer."""
        peer = self._peers.get(peer_id)
        if not peer:
            raise MeshError(f"unknown peer: {peer_id}")
        frame = encode_frame(frame_type, payload)
        if self._transport:
            await self._transport.send(peer.endpoint, frame)

    async def broadcast(
        self, frame_type: FrameType, payload: bytes = b""
    ) -> None:
        """Send a frame to all alive peers."""
        frame = encode_frame(frame_type, payload)
        for peer in self.get_alive_peers():
            if self._transport:
                await self._transport.send(peer.endpoint, frame)

    def on(self, frame_type: FrameType) -> Callable:
        """Decorator to register a handler for a frame type."""
        def decorator(fn: Callable) -> Callable:
            self._handlers.setdefault(frame_type, []).append(fn)
            return fn
        return decorator

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start the mesh event loop."""
        self._running = True
        logger.info("mesh started: node=%s listen=%s", self.node_id, self.listen_addr)
        await self._event_loop()

    async def stop(self) -> None:
        """Gracefully shut down the mesh."""
        self._running = False
        logger.info("mesh stopped: node=%s", self.node_id)

    async def _event_loop(self) -> None:
        """Main loop: receive frames and dispatch to handlers."""
        while self._running:
            try:
                if self._transport:
                    endpoint, raw = await self._transport.receive()
                    await self._dispatch(endpoint, raw)
                else:
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("mesh event loop error")

    async def _dispatch(self, endpoint: str, raw: bytes) -> None:
        """Decode and dispatch a received frame."""
        try:
            frame_type, flags, payload = decode_frame(raw)
        except DecodeError:
            logger.warning("decode error from %s", endpoint)
            return

        for handler in self._handlers.get(frame_type, []):
            try:
                await handler(self, endpoint, flags, payload)
            except Exception:
                logger.exception("handler error for %s", frame_type.name)

    # -- roaming -----------------------------------------------------------

    async def announce_roam(self, target_endpoint: str) -> None:
        """Announce a roaming handoff to a target node.

        Called when the primary workstation is going to sleep and
        the daemon context needs to migrate to a backup host.
        """
        msg = encode_message({
            "action": "roam_announce",
            "node_id": self.node_id,
            "timestamp": time.time(),
            "state_digest": self._state_digest(),
        })
        if self._transport:
            frame = encode_frame(FrameType.ROAM_ANNOUNCE, msg)
            await self._transport.send(target_endpoint, frame)

    def _state_digest(self) -> str:
        """Compute a digest of the current mesh state for consistency checks."""
        peer_ids = sorted(self._peers.keys())
        return hashlib.sha256(",".join(peer_ids).encode()).hexdigest()[:16]
