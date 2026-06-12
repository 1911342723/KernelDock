"""Router 纯逻辑单测（不依赖网络/Docker）。"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from router.kerneldock_router import (  # noqa: E402
    NodeState,
    Router,
    RouterMetrics,
    _parse_nodes,
    inject_node_label,
    reprefix_ids,
    split_prefixed_id,
    strip_id_prefixes,
)

NODES = {"n1": "http://a:9527", "n2": "http://b:9527"}


class TestParseNodes:
    def test_basic(self):
        assert _parse_nodes("n1=http://a:9527, n2=http://b:9527/") == NODES

    def test_invalid_name(self):
        with pytest.raises(ValueError):
            _parse_nodes("N!=http://a")

    def test_empty_allowed(self):
        # Phase 2：允许空静态表（节点自注册加入）
        assert _parse_nodes("") == {}

    def test_missing_eq(self):
        with pytest.raises(ValueError):
            _parse_nodes("http://a:9527")


class TestPrefix:
    def test_split_known(self):
        assert split_prefixed_id("n1:abc-123", NODES) == ("n1", "abc-123")

    def test_split_unknown_prefix(self):
        assert split_prefixed_id("nx:abc", NODES) == (None, "nx:abc")

    def test_split_no_prefix(self):
        assert split_prefixed_id("abc-123", NODES) == (None, "abc-123")

    def test_split_uuid_untouched(self):
        # uuid 不含冒号，不会被误拆
        uid = "49d15bb2-f58b-4578-a941-35ca37ae0fdd"
        assert split_prefixed_id(uid, NODES) == (None, uid)

    def test_reprefix_nested(self):
        body = {
            "session_id": "s1",
            "result": {"job_id": "job-1", "items": [{"sandbox_id": "b1"}]},
            "other": "keep",
            "none_id": None,
        }
        out = reprefix_ids(body, "n2")
        assert out["session_id"] == "n2:s1"
        assert out["result"]["job_id"] == "n2:job-1"
        assert out["result"]["items"][0]["sandbox_id"] == "n2:b1"
        assert out["other"] == "keep"
        assert out["none_id"] is None

    def test_reprefix_idempotent(self):
        body = {"session_id": "n1:s1"}
        assert reprefix_ids(body, "n1")["session_id"] == "n1:s1"

    def test_strip_prefixes_and_pin(self):
        body = {"code": "x", "session_id": "n2:s9"}
        cleaned, pinned = strip_id_prefixes(body, NODES)
        assert cleaned == {"code": "x", "session_id": "s9"}
        assert pinned == "n2"

    def test_strip_no_prefix(self):
        body = {"code": "x", "session_id": "s9"}
        cleaned, pinned = strip_id_prefixes(body, NODES)
        assert cleaned == body
        assert pinned is None


class TestMetricsLabel:
    def test_inject_label(self):
        text = (
            "# HELP foo_total help text\n"
            "# TYPE foo_total counter\n"
            "foo_total 3\n"
            'bar{x="1"} 2.5\n'
        )
        seen: set = set()
        out = inject_node_label(text, "n1", seen)
        assert '# HELP foo_total help text' in out
        assert 'foo_total{node="n1"} 3' in out
        assert 'bar{node="n1",x="1"} 2.5' in out

    def test_meta_dedup_across_nodes(self):
        text = "# HELP foo_total t\nfoo_total 1\n"
        seen: set = set()
        out1 = inject_node_label(text, "n1", seen)
        out2 = inject_node_label(text, "n2", seen)
        assert out1.count("# HELP") == 1
        assert "# HELP" not in out2
        assert 'foo_total{node="n2"} 1' in out2


class TestScheduling:
    def _router(self):
        r = Router.__new__(Router)  # 跳过 __init__（不建 http client）
        r.nodes = {
            "n1": NodeState("n1", "http://a"),
            "n2": NodeState("n2", "http://b"),
        }
        r.node_urls = {k: v.url for k, v in r.nodes.items()}
        r._rr_counter = 0
        r.metrics = RouterMetrics()
        return r

    def test_pick_stateless_prefers_low_queue(self):
        r = self._router()
        r.nodes["n1"].healthy = True
        r.nodes["n1"].queue_load = 5
        r.nodes["n2"].healthy = True
        r.nodes["n2"].queue_load = 1
        assert r.pick_for_stateless().name == "n2"

    def test_pick_session_prefers_low_sandboxes(self):
        r = self._router()
        r.nodes["n1"].healthy = True
        r.nodes["n1"].active_sandboxes = 0
        r.nodes["n2"].healthy = True
        r.nodes["n2"].active_sandboxes = 7
        assert r.pick_for_session().name == "n1"

    def test_skip_unhealthy(self):
        r = self._router()
        r.nodes["n1"].healthy = False
        r.nodes["n2"].healthy = True
        r.nodes["n2"].queue_load = 99
        assert r.pick_for_stateless().name == "n2"

    def test_all_down(self):
        r = self._router()
        assert r.pick_for_stateless() is None
        assert r.pick_for_session() is None


class TestDynamicNodes:
    def _router(self):
        r = Router.__new__(Router)
        r.nodes = {"n1": NodeState("n1", "http://a")}
        r.node_urls = {"n1": "http://a"}
        r.node_ttl = 30.0
        r._rr_counter = 0
        r.metrics = RouterMetrics()
        return r

    def test_register_new(self):
        r = self._router()
        result = r.register_node("n3", "http://c:9527/")
        assert result["registered"] and not result["renewed"]
        assert r.node_urls["n3"] == "http://c:9527"
        assert r.nodes["n3"].dynamic

    def test_register_heartbeat_renew(self):
        import time as _t

        r = self._router()
        r.register_node("n3", "http://c:9527")
        r.nodes["n3"].last_heartbeat = _t.time() - 25
        result = r.register_node("n3", "http://c:9527")
        assert result["renewed"]
        assert _t.time() - r.nodes["n3"].last_heartbeat < 1

    def test_register_url_update(self):
        r = self._router()
        r.register_node("n3", "http://c:9527")
        r.register_node("n3", "http://d:9527")
        assert r.node_urls["n3"] == "http://d:9527"

    def test_register_conflicts_with_static(self):
        r = self._router()
        with pytest.raises(PermissionError):
            r.register_node("n1", "http://evil:9527")

    def test_register_invalid(self):
        r = self._router()
        with pytest.raises(ValueError):
            r.register_node("BAD NAME", "http://c")
        with pytest.raises(ValueError):
            r.register_node("n3", "ftp://c")

    def test_expire_dynamic_only(self):
        import time as _t

        r = self._router()
        r.register_node("n3", "http://c:9527")
        r.nodes["n3"].last_heartbeat = _t.time() - 60
        # 静态节点不参与过期
        expired = r.expire_dynamic_nodes()
        assert expired == ["n3"]
        assert "n3" not in r.nodes and "n3" not in r.node_urls
        assert "n1" in r.nodes

    def test_remove_dynamic(self):
        r = self._router()
        r.register_node("n3", "http://c:9527")
        assert r.remove_node("n3") is True
        assert r.remove_node("n3") is False  # 已不存在

    def test_remove_static_forbidden(self):
        r = self._router()
        with pytest.raises(PermissionError):
            r.remove_node("n1")

    def test_register_with_load_marks_healthy(self):
        r = self._router()
        r.register_node(
            "n3", "http://c:9527",
            load={"active_sandboxes": 3, "pool_available": 1, "pool_total": 2, "queue_load": 5},
        )
        node = r.nodes["n3"]
        assert node.healthy is True
        assert node.active_sandboxes == 3
        assert node.queue_load == 5
        assert node.pool_total == 2

    def test_heartbeat_updates_load(self):
        r = self._router()
        r.register_node("n3", "http://c:9527", load={"queue_load": 1})
        r.register_node("n3", "http://c:9527", load={"queue_load": 9})
        assert r.nodes["n3"].queue_load == 9

    def test_dynamic_node_schedulable_after_heartbeat(self):
        # 仅靠心跳（不反向探活）动态节点即可进调度
        r = self._router()
        r.register_node("n3", "http://c:9527", load={"queue_load": 0})
        assert r.pick_for_stateless() is not None


class TestAdminAuth:
    """_check_admin_token 安全默认：未配 token 时写操作默认拒绝。"""

    class _FakeReq:
        def __init__(self, token=None):
            self.headers = {"X-Admin-Token": token} if token is not None else {}

    def _check(self, *, token_env, allow_insecure, header, write):
        import importlib

        mod = importlib.import_module("router.kerneldock_router")
        old_env = (os.environ.get("ROUTER_ADMIN_TOKEN"), os.environ.get("ROUTER_ALLOW_INSECURE_ADMIN"))
        try:
            if token_env is None:
                os.environ.pop("ROUTER_ADMIN_TOKEN", None)
            else:
                os.environ["ROUTER_ADMIN_TOKEN"] = token_env
            if allow_insecure is None:
                os.environ.pop("ROUTER_ALLOW_INSECURE_ADMIN", None)
            else:
                os.environ["ROUTER_ALLOW_INSECURE_ADMIN"] = allow_insecure
            return mod._check_admin_token(self._FakeReq(header), write=write)
        finally:
            for key, val in zip(("ROUTER_ADMIN_TOKEN", "ROUTER_ALLOW_INSECURE_ADMIN"), old_env):
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val

    def test_no_token_write_denied_by_default(self):
        # 未配 token + 未放行 → 写被拒（核心安全默认）
        assert self._check(token_env=None, allow_insecure=None, header=None, write=True) is not None

    def test_no_token_read_allowed(self):
        # 未配 token，读放行
        assert self._check(token_env=None, allow_insecure=None, header=None, write=False) is None

    def test_no_token_insecure_flag_allows_write(self):
        assert self._check(token_env=None, allow_insecure="true", header=None, write=True) is None

    def test_token_required_and_matched(self):
        assert self._check(token_env="s3cr3t", allow_insecure=None, header="s3cr3t", write=True) is None

    def test_token_mismatch_denied(self):
        assert self._check(token_env="s3cr3t", allow_insecure=None, header="wrong", write=True) is not None


class TestRouterMetrics:
    def test_render_contains_core_series(self):
        nodes = {"n1": NodeState("n1", "http://a"), "n2": NodeState("n2", "http://b")}
        nodes["n1"].healthy = True
        nodes["n2"].dynamic = True
        m = RouterMetrics()
        m.record_schedule("n1", "stateless")
        m.record_schedule("n1", "stateless")
        m.record_no_node("session")
        m.node_expired_total = 1
        out = m.render(nodes)
        assert "router_up 1" in out
        assert "router_nodes_total 2" in out
        assert "router_nodes_healthy 1" in out
        assert "router_nodes_dynamic 1" in out
        assert 'router_schedule_total{node="n1",kind="stateless"} 2' in out
        assert 'router_no_healthy_node_total{kind="session"} 1' in out
        assert "router_node_expired_total 1" in out


class TestHeartbeatUrls:
    def test_parse_multi(self):
        from app.services.router_heartbeat import parse_router_urls

        assert parse_router_urls(
            "http://r0:9500, http://r1:9500/,http://r0:9500"
        ) == ["http://r0:9500", "http://r1:9500"]

    def test_parse_empty(self):
        from app.services.router_heartbeat import parse_router_urls

        assert parse_router_urls("") == []
        assert parse_router_urls(" , ") == []
