"""Tests for TinyBOS P2P mesh encoding/decoding, bridge, and sensor frames."""

from __future__ import annotations

import struct
import asyncio
import time
import pytest

from agora.transport.p2p_mesh import (
    MAGIC,
    VERSION,
    MAX_PAYLOAD,
    FrameType,
    MeshNode,
    MeshManager,
    MeshError,
    DecodeError,
    EncodeError,
    compute_digest,
    encode_frame,
    decode_frame,
    encode_message,
    decode_message,
)
from agora.transport.tinybos_bridge import (
    SENSOR_HRV,
    SENSOR_ACCEL,
    FALL_IMPACT_THRESHOLD,
    HRV_CRITICAL_LOW,
    SensorReading,
    SceneTrigger,
    BridgeError,
    decode_sensor_frame,
    map_to_trigger,
)


# ===========================================================================
# p2p_mesh: frame encoding / decoding
# ===========================================================================


class TestDigest:
    def test_digest_length(self):
        d = compute_digest(b"hello")
        assert len(d) == 4

    def test_deterministic(self):
        assert compute_digest(b"abc") == compute_digest(b"abc")

    def test_differs(self):
        assert compute_digest(b"abc") != compute_digest(b"xyz")


class TestEncodeFrame:
    def test_ping_frame(self):
        frame = encode_frame(FrameType.PING)
        assert len(frame) == 16  # header only
        assert frame[:4] == MAGIC
        assert frame[4] == VERSION
        assert frame[5] == FrameType.PING

    def test_with_payload(self):
        payload = b'{"hello":"world"}'
        frame = encode_frame(FrameType.DATA, payload)
        assert len(frame) == 16 + len(payload)
        # payload length in header
        encoded_len = struct.unpack("!I", frame[8:12])[0]
        assert encoded_len == len(payload)

    def test_flags_preserved(self):
        frame = encode_frame(FrameType.DATA, b"", flags=0xABCD)
        _, flags, _ = decode_frame(frame)
        assert flags == 0xABCD

    def test_payload_too_large(self):
        huge = b"\x00" * (MAX_PAYLOAD + 1)
        with pytest.raises(EncodeError, match="too large"):
            encode_frame(FrameType.DATA, huge)


class TestDecodeFrame:
    def test_roundtrip(self):
        payload = b"test payload 1234"
        frame = encode_frame(FrameType.DATA, payload)
        ft, flags, decoded = decode_frame(frame)
        assert ft == FrameType.DATA
        assert decoded == payload

    def test_short_frame(self):
        with pytest.raises(DecodeError, match="too short"):
            decode_frame(b"\x00\x01\x02")

    def test_bad_magic(self):
        bad = b"\x00\x00\x00\x00" + b"\x00" * 12
        with pytest.raises(DecodeError, match="bad magic"):
            decode_frame(bad)

    def test_wrong_version(self):
        frame = bytearray(encode_frame(FrameType.PING))
        frame[4] = 0xFF
        with pytest.raises(DecodeError, match="unsupported version"):
            decode_frame(bytes(frame))

    def test_digest_mismatch(self):
        frame = bytearray(encode_frame(FrameType.DATA, b"hello"))
        # corrupt the payload
        frame[-1] ^= 0xFF
        with pytest.raises(DecodeError, match="digest mismatch"):
            decode_frame(bytes(frame))

    def test_truncated_payload(self):
        frame = bytearray(encode_frame(FrameType.DATA, b"hello world"))
        # shorten the frame to simulate truncation
        with pytest.raises(DecodeError, match="truncated"):
            decode_frame(bytes(frame[:18]))


class TestMessageCodec:
    def test_roundtrip(self):
        msg = {"node_id": "abc", "ts": 1234567890.123, "tags": ["a", "b"]}
        payload = encode_message(msg)
        assert isinstance(payload, bytes)
        decoded = decode_message(payload)
        assert decoded == msg


# ===========================================================================
# p2p_mesh: MeshManager
# ===========================================================================


class FakeTransport:
    """In-memory transport for testing."""

    def __init__(self):
        self.outbox: list[tuple[str, bytes]] = []
        self.inbox: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()

    async def send(self, endpoint: str, frame: bytes) -> None:
        self.outbox.append((endpoint, frame))

    async def receive(self) -> tuple[str, bytes]:
        return await self.inbox.get()


class TestMeshManager:
    def test_register_peer(self):
        mm = MeshManager("self")
        node = MeshNode(node_id="peer1", endpoint="10.0.0.1:9100")
        mm.register_peer(node)
        assert "peer1" in mm._peers

    def test_get_alive_peers(self):
        mm = MeshManager("self")
        n1 = MeshNode(node_id="a", endpoint="10.0.0.1:9100", is_alive=True)
        n2 = MeshNode(node_id="b", endpoint="10.0.0.2:9100", is_alive=False)
        mm.register_peer(n1)
        mm.register_peer(n2)
        alive = mm.get_alive_peers()
        assert len(alive) == 1
        assert alive[0].node_id == "a"

    def test_remove_peer(self):
        mm = MeshManager("self")
        mm.register_peer(MeshNode(node_id="x", endpoint="10.0.0.3:9100"))
        mm.remove_peer("x")
        assert "x" not in mm._peers

    @pytest.mark.asyncio
    async def test_broadcast(self):
        mm = MeshManager("self", transport=FakeTransport())
        mm.register_peer(MeshNode(node_id="a", endpoint="10.0.0.1:9100", is_alive=True))
        mm.register_peer(MeshNode(node_id="b", endpoint="10.0.0.2:9100", is_alive=True))
        await mm.broadcast(FrameType.PING)
        assert len(mm._transport.outbox) == 2

    @pytest.mark.asyncio
    async def test_send_to_unknown_peer(self):
        mm = MeshManager("self", transport=FakeTransport())
        with pytest.raises(MeshError, match="unknown peer"):
            await mm.send_to("ghost", FrameType.PING)

    @pytest.mark.asyncio
    async def test_handler_dispatch(self):
        mm = MeshManager("self", transport=FakeTransport())
        received = []

        @mm.on(FrameType.DATA)
        async def handle_data(manager, endpoint, flags, payload):
            received.append((endpoint, payload))

        frame = encode_frame(FrameType.DATA, b"hello")
        await mm._dispatch("10.0.0.1:9100", frame)
        assert len(received) == 1
        assert received[0][1] == b"hello"

    def test_state_digest(self):
        mm = MeshManager("self")
        mm.register_peer(MeshNode(node_id="b", endpoint="10.0.0.2:9100"))
        mm.register_peer(MeshNode(node_id="a", endpoint="10.0.0.1:9100"))
        d = mm._state_digest()
        assert len(d) == 16  # hex


# ===========================================================================
# tinybos_bridge: sensor frame decoding
# ===========================================================================


class TestDecodeSensorFrame:
    def _pack(
        self, sensor_type: int, timestamp_ms: int, precision: int, values: list[float]
    ) -> bytes:
        """Helper to pack a sensor frame."""
        raw = struct.pack("<B", sensor_type)
        raw += struct.pack("<Q", timestamp_ms)
        raw += struct.pack("<H", precision)
        raw += struct.pack("<B", len(values))
        for v in values:
            raw += struct.pack("<f", v)
        return raw

    def test_hrv_frame(self):
        raw = self._pack(SENSOR_HRV, 1_700_000_000_000, 2, [42.5])
        reading = decode_sensor_frame(raw)
        assert reading.sensor_type == SENSOR_HRV
        assert reading.sensor_name == "hrv"
        assert reading.precision == 2
        assert reading.values == [42.5]

    def test_accel_3axis(self):
        raw = self._pack(SENSOR_ACCEL, 1_700_000_000_000, 3, [0.1, -0.2, 9.8])
        reading = decode_sensor_frame(raw)
        assert reading.sensor_type == SENSOR_ACCEL
        assert len(reading.values) == 3

    def test_too_short(self):
        with pytest.raises(BridgeError, match="too short"):
            decode_sensor_frame(b"\x01\x02\x03")

    def test_truncated_values(self):
        raw = struct.pack("<B", SENSOR_HRV)
        raw += struct.pack("<Q", 1_700_000_000_000)
        raw += struct.pack("<H", 2)
        raw += struct.pack("<B", 5)  # claims 5 values but provides 0
        with pytest.raises(BridgeError, match="truncated"):
            decode_sensor_frame(raw)


class TestMapToTrigger:
    def test_hrv_critical_triggers(self):
        reading = SensorReading(
            sensor_type=SENSOR_HRV,
            sensor_name="hrv",
            timestamp_ms=int(time.time() * 1000),
            precision=2,
            values=[15.0],  # below HRV_CRITICAL_LOW (20.0)
        )
        trigger = map_to_trigger(reading)
        assert trigger is not None
        assert trigger.trigger_type == "health-visit.hrv_critical"
        assert trigger.severity == "critical"

    def test_hrv_normal_no_trigger(self):
        reading = SensorReading(
            sensor_type=SENSOR_HRV,
            sensor_name="hrv",
            timestamp_ms=int(time.time() * 1000),
            precision=2,
            values=[45.0],  # normal
        )
        assert map_to_trigger(reading) is None

    def test_fall_detected(self):
        # accel magnitude > FALL_IMPACT_THRESHOLD (4.0 g)
        reading = SensorReading(
            sensor_type=SENSOR_ACCEL,
            sensor_name="accel",
            timestamp_ms=int(time.time() * 1000),
            precision=2,
            values=[3.0, 3.0, 3.0],  # magnitude ≈ 5.2g
        )
        trigger = map_to_trigger(reading)
        assert trigger is not None
        assert trigger.trigger_type == "health-visit.fall_detected"
        assert trigger.severity == "critical"

    def test_normal_movement_no_trigger(self):
        reading = SensorReading(
            sensor_type=SENSOR_ACCEL,
            sensor_name="accel",
            timestamp_ms=int(time.time() * 1000),
            precision=2,
            values=[0.0, 0.0, 1.0],  # ~1g, normal
        )
        assert map_to_trigger(reading) is None

    def test_empty_values_no_crash(self):
        reading = SensorReading(
            sensor_type=SENSOR_HRV,
            sensor_name="hrv",
            timestamp_ms=int(time.time() * 1000),
            precision=2,
            values=[],
        )
        # empty values → rmssd=0.0 → triggers
        trigger = map_to_trigger(reading)
        assert trigger is not None  # 0.0 < HRV_CRITICAL_LOW


# ---------------------------------------------------------------------------
# TinyBOSBridge class tests
# ---------------------------------------------------------------------------

class TestTinyBOSBridgeInit:
    """Tests for TinyBOSBridge construction and configuration."""

    def test_default_socket_path(self):
        from agora.transport.tinybos_bridge import TinyBOSBridge
        bridge = TinyBOSBridge()
        assert bridge.socket_path == "/tmp/tinybos.sock"
        assert bridge._running is False
        assert bridge._handlers == []

    def test_custom_socket_path(self):
        from agora.transport.tinybos_bridge import TinyBOSBridge
        bridge = TinyBOSBridge(socket_path="/tmp/custom.sock")
        assert bridge.socket_path == "/tmp/custom.sock"

    def test_initial_state(self):
        from agora.transport.tinybos_bridge import TinyBOSBridge
        bridge = TinyBOSBridge()
        assert bridge._reader is None
        assert bridge._writer is None


class TestTinyBOSBridgeOnTrigger:
    """Tests for trigger handler registration."""

    def test_register_handler(self):
        from agora.transport.tinybos_bridge import TinyBOSBridge, SceneTrigger
        bridge = TinyBOSBridge()

        called = []
        def handler(trigger: SceneTrigger) -> None:
            called.append(trigger)

        bridge.on_trigger(handler)
        assert len(bridge._handlers) == 1

    def test_register_multiple_handlers(self):
        from agora.transport.tinybos_bridge import TinyBOSBridge
        bridge = TinyBOSBridge()

        bridge.on_trigger(lambda t: None)
        bridge.on_trigger(lambda t: None)
        assert len(bridge._handlers) == 2

    def test_handler_invoked_on_trigger(self):
        from agora.transport.tinybos_bridge import TinyBOSBridge, SceneTrigger, decode_sensor_frame
        bridge = TinyBOSBridge()

        called = []
        def handler(trigger: SceneTrigger) -> None:
            called.append(trigger.trigger_type)

        bridge.on_trigger(handler)

        # Simulate a critical HRV reading
        raw = struct.pack("<BQHI", 0x01, 1700000000000, 1, 1) + struct.pack("<f", 15.0)
        raw = _pack_sensor_frame(0x01, 1700000000000, 1, [15.0])
        asyncio.run(bridge._process_sensor_frame(raw))
        assert called == ["health-visit.hrv_critical"]


def _pack_sensor_frame(sensor_type: int, timestamp_ms: int, precision: int, values: list[float]) -> bytes:
    """Helper to pack a sensor frame for testing."""
    data = struct.pack("<BQHB", sensor_type, timestamp_ms, precision, len(values))
    for v in values:
        data += struct.pack("<f", v)
    return data


class TestTinyBOSBridgeProcessFrame:
    """Tests for sensor frame processing pipeline."""

    def test_process_valid_hrv_frame(self):
        from agora.transport.tinybos_bridge import TinyBOSBridge, decode_sensor_frame, map_to_trigger
        bridge = TinyBOSBridge()

        # Normal HRV reading (65 ms) — no trigger
        raw = _pack_sensor_frame(0x01, 1700000000000, 1, [65.0])
        reading = decode_sensor_frame(raw)
        assert reading.sensor_type == 0x01
        assert reading.sensor_name == "hrv"
        assert map_to_trigger(reading) is None

    def test_process_critical_hrv_frame(self):
        from agora.transport.tinybos_bridge import TinyBOSBridge, decode_sensor_frame, map_to_trigger
        bridge = TinyBOSBridge()

        # Critical HRV (15 ms) — should trigger
        raw = _pack_sensor_frame(0x01, 1700000000000, 1, [15.0])
        reading = decode_sensor_frame(raw)
        trigger = map_to_trigger(reading)
        assert trigger is not None
        assert trigger.trigger_type == "health-visit.hrv_critical"
        assert trigger.severity == "critical"

    def test_process_fall_detection_frame(self):
        from agora.transport.tinybos_bridge import TinyBOSBridge, decode_sensor_frame, map_to_trigger
        bridge = TinyBOSBridge()

        # Fall: 3g on each axis → magnitude ~5.2g
        raw = _pack_sensor_frame(0x02, 1700000000000, 2, [3.0, 3.0, 3.0])
        reading = decode_sensor_frame(raw)
        trigger = map_to_trigger(reading)
        assert trigger is not None
        assert trigger.trigger_type == "health-visit.fall_detected"

    def test_process_malformed_frame_no_crash(self):
        from agora.transport.tinybos_bridge import TinyBOSBridge, BridgeError
        bridge = TinyBOSBridge()

        # Too short
        raw = b"\x01\x02\x03"
        import pytest
        with pytest.raises(BridgeError):
            from agora.transport.tinybos_bridge import decode_sensor_frame
            decode_sensor_frame(raw)

    def test_process_truncated_frame_no_crash(self):
        from agora.transport.tinybos_bridge import TinyBOSBridge, BridgeError
        bridge = TinyBOSBridge()

        # Header says 3 values but only 2 present
        import struct
        raw = struct.pack("<BQHI", 0x02, 1700000000000, 2, 3)
        raw += struct.pack("<f", 1.0) + struct.pack("<f", 2.0)
        with pytest.raises(BridgeError):
            from agora.transport.tinybos_bridge import decode_sensor_frame
            decode_sensor_frame(raw)


class TestSensorReading:
    """Tests for SensorReading dataclass."""

    def test_unix_timestamp_property(self):
        from agora.transport.tinybos_bridge import SensorReading
        reading = SensorReading(
            sensor_type=0x01,
            sensor_name="hrv",
            timestamp_ms=1700000000000,
            precision=2,
            values=[65.0],
        )
        assert abs(reading.unix_timestamp - 1700000000.0) < 0.001

    def test_to_dict(self):
        from agora.transport.tinybos_bridge import SensorReading
        reading = SensorReading(
            sensor_type=0x02,
            sensor_name="accel",
            timestamp_ms=1700000000000,
            precision=3,
            values=[0.1, -0.2, 9.8],
        )
        d = reading.to_dict()
        assert d["sensor_type"] == 0x02
        assert d["sensor_name"] == "accel"
        assert d["timestamp_ms"] == 1700000000000
        assert d["values"] == [0.1, -0.2, 9.8]


class TestTinyBOSBridgeLifecycle:
    """Tests for bridge start/stop lifecycle (non-blocking)."""

    def test_stop_without_start(self):
        """stop() should be safe to call without start()."""
        import asyncio
        from agora.transport.tinybos_bridge import TinyBOSBridge
        bridge = TinyBOSBridge()
        asyncio.run(bridge.stop())
        assert bridge._running is False

    def test_running_flag(self):
        from agora.transport.tinybos_bridge import TinyBOSBridge
        bridge = TinyBOSBridge()
        assert bridge._running is False
        bridge._running = True
        assert bridge._running is True
