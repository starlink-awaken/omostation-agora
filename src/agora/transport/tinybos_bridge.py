"""agora.transport.tinybos_bridge — Bridge between TinyBOS edge daemon and Agora.

Translates physical sensor signals (heart-rate variability, fall acceleration,
spatial displacement) from the TinyBOS Rust edge binary into Agora scene-trigger
events. Communicates with the TinyBOS daemon over a local IPC channel
(Unix domain socket on macOS/Linux, named pipe on Windows).

Protocol:
- TinyBOS pushes binary-encoded sensor frames
- Bridge decodes, validates, and maps to health-visit scene card triggers
- Bridge emits Agora-compatible event messages upstream
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Callable

from .p2p_mesh import encode_message

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sensor frame layout (from TinyBOS Rust daemon)
#
#   sensor_type(1) | timestamp(8) | precision(2) | value_count(1) | values(N*4)
#
#   sensor_type: 1=HRV, 2=accelerometer, 3=gyroscope, 4=barometer
#   timestamp: milliseconds since epoch (u64 LE)
#   precision: decimal places for fixed-point values (u16 LE)
#   value_count: number of f32 values that follow
#   values: array of f32 LE
# ---------------------------------------------------------------------------

SENSOR_HRV = 0x01
SENSOR_ACCEL = 0x02
SENSOR_GYRO = 0x03
SENSOR_BARO = 0x04

SENSOR_NAMES = {
    SENSOR_HRV: "hrv",
    SENSOR_ACCEL: "accel",
    SENSOR_GYRO: "gyro",
    SENSOR_BARO: "baro",
}

# Thresholds for scene triggers
HRV_CRITICAL_LOW = 20.0       # ms — RMSSD below this triggers alert
FALL_IMPACT_THRESHOLD = 4.0   # g — accel magnitude above this = possible fall
DISPLACEMENT_THRESHOLD = 5.0  # meters — spatial movement threshold


class BridgeError(Exception):
    """Base for bridge errors."""


@dataclass
class SensorReading:
    """A decoded sensor reading."""
    sensor_type: int
    sensor_name: str
    timestamp_ms: int
    precision: int
    values: list[float]

    @property
    def unix_timestamp(self) -> float:
        return self.timestamp_ms / 1000.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sensor_type": self.sensor_type,
            "sensor_name": self.sensor_name,
            "timestamp_ms": self.timestamp_ms,
            "precision": self.precision,
            "values": self.values,
        }


def decode_sensor_frame(raw: bytes) -> SensorReading:
    """Decode a binary sensor frame from TinyBOS.

    Args:
        raw: Binary frame payload.

    Returns:
        Decoded SensorReading.

    Raises:
        BridgeError: If the frame is malformed.
    """
    if len(raw) < 12:
        raise BridgeError(f"sensor frame too short: {len(raw)}")

    sensor_type = raw[0]
    timestamp_ms = struct.unpack("<Q", raw[1:9])[0]
    precision = struct.unpack("<H", raw[9:11])[0]
    value_count = raw[11]

    expected_len = 12 + value_count * 4
    if len(raw) < expected_len:
        raise BridgeError(
            f"sensor frame truncated: got {len(raw)}, need {expected_len}"
        )

    values = []
    for i in range(value_count):
        offset = 12 + i * 4
        raw_val = struct.unpack("<f", raw[offset : offset + 4])[0]
        values.append(round(raw_val, precision))

    return SensorReading(
        sensor_type=sensor_type,
        sensor_name=SENSOR_NAMES.get(sensor_type, f"unknown_{sensor_type}"),
        timestamp_ms=timestamp_ms,
        precision=precision,
        values=values,
    )


# ---------------------------------------------------------------------------
# Scene trigger mapper
# ---------------------------------------------------------------------------

@dataclass
class SceneTrigger:
    """A trigger event mapped from sensor data."""
    trigger_type: str
    source_sensor: str
    severity: str  # "info", "warning", "critical"
    payload: dict[str, Any]


def map_to_trigger(reading: SensorReading) -> SceneTrigger | None:
    """Map a sensor reading to a scene trigger, if thresholds are exceeded.

    Args:
        reading: Decoded sensor reading.

    Returns:
        SceneTrigger if a threshold is exceeded, None otherwise.
    """
    if reading.sensor_type == SENSOR_HRV:
        rmssd = reading.values[0] if reading.values else 0.0
        if rmssd < HRV_CRITICAL_LOW:
            return SceneTrigger(
                trigger_type="health-visit.hrv_critical",
                source_sensor="hrv",
                severity="critical",
                payload={
                    "rmssd": rmssd,
                    "threshold": HRV_CRITICAL_LOW,
                    "timestamp": reading.unix_timestamp,
                },
            )

    elif reading.sensor_type == SENSOR_ACCEL:
        if len(reading.values) >= 3:
            magnitude = (
                reading.values[0] ** 2
                + reading.values[1] ** 2
                + reading.values[2] ** 2
            ) ** 0.5
            if magnitude > FALL_IMPACT_THRESHOLD:
                return SceneTrigger(
                    trigger_type="health-visit.fall_detected",
                    source_sensor="accel",
                    severity="critical",
                    payload={
                        "magnitude_g": round(magnitude, 2),
                        "threshold_g": FALL_IMPACT_THRESHOLD,
                        "timestamp": reading.unix_timestamp,
                    },
                )

    return None


# ---------------------------------------------------------------------------
# Bridge manager
# ---------------------------------------------------------------------------

class TinyBOSBridge:
    """Bridges TinyBOS edge sensor data to Agora scene triggers."""

    def __init__(self, socket_path: str = "/tmp/tinybos.sock") -> None:
        self.socket_path = socket_path
        self._handlers: list[Callable[[SceneTrigger], None]] = []
        self._running = False
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    def on_trigger(self, handler: Callable[[SceneTrigger], None]) -> None:
        """Register a handler for scene triggers."""
        self._handlers.append(handler)

    async def start(self) -> None:
        """Start listening for TinyBOS sensor frames."""
        self._running = True
        server = await asyncio.start_unix_server(
            self._handle_connection, path=self.socket_path
        )
        logger.info("TinyBOS bridge listening on %s", self.socket_path)
        async with server:
            await server.serve_forever()

    async def stop(self) -> None:
        """Stop the bridge."""
        self._running = False
        if self._writer:
            self._writer.close()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a new TinyBOS daemon connection."""
        self._reader = reader
        self._writer = writer
        peer = writer.get_extra_info("peername")
        logger.info("TinyBOS daemon connected: %s", peer)

        try:
            while self._running:
                # Read frame length prefix (4 bytes, u32 LE)
                length_bytes = await reader.readexactly(4)
                frame_len = struct.unpack("<I", length_bytes)[0]

                if frame_len > 4096:
                    logger.warning("oversized frame: %d bytes", frame_len)
                    break

                raw = await reader.readexactly(frame_len)
                await self._process_sensor_frame(raw)

        except asyncio.IncompleteReadError:
            logger.info("TinyBOS daemon disconnected")
        except Exception:
            logger.exception("bridge connection error")
        finally:
            writer.close()
            self._reader = None
            self._writer = None

    async def _process_sensor_frame(self, raw: bytes) -> None:
        """Decode a sensor frame and emit triggers."""
        try:
            reading = decode_sensor_frame(raw)
        except BridgeError as exc:
            logger.warning("sensor decode error: %s", exc)
            return

        trigger = map_to_trigger(reading)
        if trigger:
            logger.info("scene trigger: %s", trigger.trigger_type)
            for handler in self._handlers:
                try:
                    handler(trigger)
                except Exception:
                    logger.exception("trigger handler error")
