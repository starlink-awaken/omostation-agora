"""Tests for Agora OpenTelemetry integration."""

from __future__ import annotations

from agora.otel import _NoOpTracer, get_tracer, init_otel

# ============================================================================
# init_otel
# ============================================================================


class TestInitOtel:
    def test_init_missing_otlp_exporter(self, monkeypatch):
        """OTLP exporter not installed — init falls back via ImportError."""
        # 实测 (2026-11-23): fastapi[standard] 的 extra 会装上
        # opentelemetry-exporter-otlp-proto-http (uv.lock 自 fastapi 0.165 起
        # 含该包), 「exporter 缺席」不再是测试环境的默认状态。改为**显式模拟**
        # 该模块缺失, 以保留本用例原意: init_otel 在缺 exporter 时必须降级。
        import builtins

        real_import = builtins.__import__

        def _raise_for_otlp_exporter(name, *args, **kwargs):
            if name.startswith("opentelemetry.exporter.otlp"):
                raise ImportError(f"no module: {name}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _raise_for_otlp_exporter)
        result = init_otel()
        assert result is False

    def test_init_import_error(self, monkeypatch):
        """Simulate opentelemetry packages missing entirely."""

        def _raise_import(name, *args, **kwargs):
            raise ImportError(f"no module: {name}")

        monkeypatch.setattr("builtins.__import__", _raise_import)
        result = init_otel()
        assert result is False

    def test_init_unexpected_exception(self, monkeypatch):
        """Simulate a RuntimeError during init."""

        def _fake_import(name, *args, **kwargs):
            if name == "opentelemetry":
                raise RuntimeError("unexpected error")
            return __import__(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", _fake_import)
        result = init_otel()
        assert result is False


# ============================================================================
# _NoOpTracer
# ============================================================================


class TestNoOpTracer:
    def test_start_span_returns_self(self):
        t = _NoOpTracer()
        span = t.start_span("test")
        assert span is t

    def test_start_as_current_span_returns_self(self):
        t = _NoOpTracer()
        ctx = t.start_as_current_span("test")
        assert ctx is t

    def test_any_attr_returns_callable(self):
        t = _NoOpTracer()
        result = t.some_random_method("arg", key="val")
        assert result is None

    def test_end_on_span(self):
        t = _NoOpTracer()
        result = t.end()
        assert result is None


# ============================================================================
# get_tracer
# ============================================================================


class TestGetTracer:
    def test_returns_real_tracer_when_available(self):
        tracer = get_tracer("test-module")
        assert tracer is not None

    def test_returns_noop_on_import_error(self, monkeypatch):
        def _raise_import(name, *args, **kwargs):
            raise ImportError(f"no module: {name}")

        monkeypatch.setattr("builtins.__import__", _raise_import)
        tracer = get_tracer("test-module")
        assert isinstance(tracer, _NoOpTracer)

    def test_real_tracer_can_start_span(self):
        tracer = get_tracer("test-module")
        with tracer.start_as_current_span("test-span") as span:  # type: ignore[reportGeneralTypeIssues]
            span.set_attribute("key", "value")
        assert span is not None
