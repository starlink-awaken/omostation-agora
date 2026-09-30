"""F-04: resolver transport 分派对齐测试。

覆盖:
- inline transport 无 command → 结构化错误 (不再 Popen([]))
- inline transport 有 command → 走 stdio
- mcp_proxy transport → 显式降级错误 (需 ProxyManager)
- http transport → 对 http_url 发起请求
- stdio transport → 正常子进程调用
"""

from __future__ import annotations

from agora.mcp.resolver.adapter import StdioAdapter
from agora.mcp.resolver.services_types import BosService


def _svc(transport: str, **overrides) -> BosService:
    defaults = {
        "uri": f"bos://capability/test/{transport}",
        "domain": "capability",
        "package": "test",
        "action": "ping",
        "transport": transport,  # type: ignore[arg-type]
        "command": ["echo", "{}"],
    }
    defaults.update(overrides)
    return BosService(**defaults)


def test_inline_without_command_returns_structured_error():
    """inline 无 command → 结构化错误 (F-04: 消除 Popen([]))。"""
    svc = _svc("inline", command=[])
    result = StdioAdapter().call(svc)
    assert result["status"] == "error"
    assert result["transport"] == "inline"
    assert "no command" in result["error"]


def test_inline_with_command_goes_stdio():
    """inline 有 command → 走 stdio 协议。"""
    svc = _svc("inline", command=["echo", '{"status":"ok"}'])
    result = StdioAdapter().call(svc)
    assert result["status"] == "ok"
    assert "result" in result


def test_mcp_proxy_returns_explicit_degradation():
    """mcp_proxy 无 ProxyManager 上下文 → 显式降级错误。"""
    svc = _svc("mcp_proxy")
    result = StdioAdapter().call(svc)
    assert result["status"] == "error"
    assert result["transport"] == "mcp_proxy"
    assert "ProxyManager" in result["error"]


def test_http_transport_calls_http_url(monkeypatch):
    """http transport → 对 http_url 发起请求。"""
    import json

    calls: dict = {}

    class _FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"status": "ok", "data": 1}).encode()

    def _fake_urlopen(req, timeout=None):
        calls["url"] = req.full_url
        calls["method"] = req.get_method()
        return _FakeResp()

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    svc = _svc("http", http_url="http://127.0.0.1:9999/api/ping")
    result = StdioAdapter().call(svc, {"query": "x"})
    assert result["status"] == "ok"
    assert calls["url"] == "http://127.0.0.1:9999/api/ping"
    assert calls["method"] == "POST"


def test_http_transport_error_returns_structured(monkeypatch):
    """http transport 请求失败 → 结构化错误。"""
    import urllib.request

    def _fail_urlopen(req, timeout=None):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _fail_urlopen)
    svc = _svc("http", http_url="http://127.0.0.1:9999/nope")
    result = StdioAdapter().call(svc)
    assert result["status"] == "error"
    assert result["transport"] == "http"
    assert "connection refused" in result["error"]


def test_stdio_transport_normal_call():
    """stdio transport → 正常子进程调用。"""
    svc = _svc("stdio", command=["echo", '{"status":"ok"}'])
    result = StdioAdapter().call(svc)
    assert result["status"] == "ok"


# ── Memory OS env 注入 (bos://memory/mos/*) ────────────────────────────────


def _mos_env_fixture(root, monkeypatch, *, example: str, local: str) -> None:
    """构造单根 fixture: example 模板 + config 本地覆盖。"""
    (root / "docs" / "operations").mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "operations" / "memory-os.env.example").write_text(
        example, encoding="utf-8"
    )
    (root / "config" / "memory-os.env").write_text(local, encoding="utf-8")
    # 隔离: 只让 fixture 根参与合并 (否则宿主 ~/Workspace/config 会插队)
    monkeypatch.setattr(
        "agora.mcp.resolver.memory_os_env.workspace_roots", lambda: [root]
    )


def test_child_env_only_for_mos_namespace(tmp_path, monkeypatch):
    """非 bos://memory/mos/* → None (继承进程 env, 零行为变化)。"""
    _mos_env_fixture(
        tmp_path,
        monkeypatch,
        example="MOS_LIVE_KOS=0\n",
        local="MOS_LIVE_KOS=1\n",
    )
    from agora.mcp.resolver.memory_os_env import child_env

    assert child_env("bos://memory/kos/search") is None
    assert child_env("bos://capability/evaluator/x") is None
    mos_env = child_env("bos://memory/mos/status")
    assert mos_env is not None
    # config/memory-os.env 覆盖 example 模板 → 旧实现这里根本不存在这个 key
    assert mos_env["MOS_LIVE_KOS"] == "1"


def test_child_env_process_env_always_wins(tmp_path, monkeypatch):
    """进程 env 非空值优先 — 与 bin/memory-os-env.sh 运维契约一致。"""
    _mos_env_fixture(
        tmp_path,
        monkeypatch,
        example="MOS_LIVE_KOS=0\n",
        local="MOS_LIVE_KOS=1\n",
    )
    from agora.mcp.resolver.memory_os_env import child_env

    mos_env = child_env("bos://memory/mos/write", base={"MOS_LIVE_KOS": "0"})
    assert mos_env is not None
    assert mos_env["MOS_LIVE_KOS"] == "0"
    # 空缺键仍被补齐
    assert mos_env["NEO4J_URI"]


def test_stdio_spawn_injects_memory_os_env(tmp_path, monkeypatch):
    """_call_stdio 对 mos 子进程传 env; 其余服务保持继承 (env=None)。"""
    _mos_env_fixture(
        tmp_path,
        monkeypatch,
        example="MOS_LIVE_KOS=0\n",
        local="MOS_LIVE_KOS=1\n",
    )
    captured: list[dict] = []

    class _FakeProc:
        def __init__(self, cmd, **kwargs):
            captured.append(kwargs)
            self.pid = 4242
            self.returncode = 0

        def communicate(self, input=None, timeout=None):
            return ('{"status":"ok"}', "")

        def poll(self):
            return 0

        def kill(self):  # pragma: no cover — 不触发
            return None

    import subprocess

    monkeypatch.setattr(subprocess, "Popen", _FakeProc)

    adapter = StdioAdapter()
    mos = _svc(
        "stdio",
        uri="bos://memory/mos/status",
        domain="memory",
        package="mos",
        command=["echo", "{}"],
    )
    other = _svc("stdio", command=["echo", "{}"])

    assert adapter.call(mos)["status"] == "ok"
    assert adapter.call(other)["status"] == "ok"

    mos_env, other_env = captured[0]["env"], captured[1]["env"]
    assert mos_env is not None and mos_env.get("MOS_LIVE_KOS") == "1"
    assert other_env is None  # 非 mos: 逐字节保持改造前的继承行为


# ── gbrain mcp_tool 映射 (unknown_tool 修复) ───────────────────────────────


def test_mcp_tool_name_prefers_declared_tool():
    """YAML 声明 mcp_tool → 用它; 否则退回 house 约定 {package}/{action}。"""
    from agora.mcp.resolver.adapter import StdioAdapter as A

    plain = _svc("mcp_stdio")
    assert A._mcp_tool_name(plain) == "test/ping"

    mapped = _svc("mcp_stdio", mcp_tool="search")
    assert A._mcp_tool_name(mapped) == "search"


def test_mcp_tool_arguments_flatten_for_native_contract():
    """声明 mcp_tool 的服务吃具名参数 (gbrain validateParams 要求 query 必填);
    未声明的服务保持 {args, kwargs} envelope 不变。"""
    from agora.mcp.resolver.adapter import StdioAdapter as A

    plain = _svc("mcp_stdio")
    assert A._mcp_tool_arguments(plain, ({"query": "x"},), {}) == {
        "args": ({"query": "x"},),
        "kwargs": {},
    }

    native = _svc("mcp_stdio", mcp_tool="search")
    assert A._mcp_tool_arguments(native, ({"query": "x"},), {"limit": 3}) == {
        "query": "x",
        "limit": 3,
    }


def test_gbrain_rows_declare_real_mcp_tool_names():
    """etc/bos-services.yaml 的 gbrain 行必须映射到 gbrain 真实 MCP 工具名。

    gbrain 的 tools/list 暴露的是 operation 名 (search / query / sync_brain),
    没有 `gbrain/` 前缀 — 旧行的 `gbrain/search` 会被对端判 unknown_tool。
    """
    from agora.mcp.resolver.bos_registry import load_from_yaml

    by_uri = {s.uri: s for s in load_from_yaml()}
    expected = {
        "bos://memory/gbrain/search": "search",
        "bos://memory/gbrain/query": "query",
        "bos://memory/gbrain/sync": "sync_brain",
    }
    for uri, tool in expected.items():
        service = by_uri.get(uri)
        assert service is not None, f"missing registry row: {uri}"
        assert service.transport == "mcp_stdio", uri
        assert service.mcp_tool == tool, uri


# ── http transport: http_method 显式声明 ───────────────────────────────────


def test_http_transport_get_declared_sends_query_string(monkeypatch):
    """http_method: get → GET + query string (KOS /api/v1/search 只收 GET+q)。"""
    import json

    calls: dict = {}

    class _FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"status": "ok"}).encode()

    def _fake_urlopen(req, timeout=None):
        calls["url"] = req.full_url
        calls["method"] = req.get_method()
        return _FakeResp()

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    svc = _svc(
        "http",
        http_url="http://127.0.0.1:9999/api/v1/search",
        http_method="get",
    )
    result = StdioAdapter().call(svc, {"q": "hello world"})
    assert result["status"] == "ok"
    assert calls["method"] == "GET"
    assert calls["url"] == "http://127.0.0.1:9999/api/v1/search?q=hello+world"


def test_rest_api_row_points_at_live_endpoint():
    """bos://memory/kos/rest-api 的 http_url 必须是可达端点 (旧值 /api/v1 裸前缀 404)。"""
    from agora.mcp.resolver.bos_registry import load_from_yaml, validate_registry

    by_uri = {s.uri: s for s in load_from_yaml()}
    service = by_uri.get("bos://memory/kos/rest-api")
    assert service is not None
    assert service.transport == "http"
    assert service.http_url.startswith("http://localhost:8766/api/v1/")
    assert service.http_url.rstrip("/").endswith("health")
    assert service.http_method == "get"
    assert not any("http_method" in e for e in validate_registry())
