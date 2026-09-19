"""Behavioral contract tests for the Action Center plugin backend.

``dashboard/plugin_api.py`` ports the closed core PR's inbox aggregation, the
scoped request-detail read layer and the direct-state pause/resume path onto the
gateway's real runtime state. Fixtures mirror the core suites
(tests/tui_gateway/test_inbox.py + test_inbox_requests.py + test_session_control.py):
an isolated HERMES_HOME with a real state.db, the tui_gateway.server module
imported with its env-heavy siblings stubbed, fake ``_sessions`` entries in the
REAL launch-profile shape (``profile_home=None``), a live approval queue and
live server→client requests.

The route functions are called directly (no HTTP transport needed); the last
test class also exercises the full FastAPI stack over the mounted router to
prove the REST contract (error envelopes become HTTP statuses + detail).
"""

from __future__ import annotations

import importlib
import json
import sys
import threading
import time
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Import the plugin router by path (the plugin repo is not a package).
import importlib.util as _ilu
import pathlib as _pathlib

# "approval" is referenced by fixtures; bind the module-level name used below.
import tools.approval as approval

_PLUGIN_API_PATH = _pathlib.Path(__file__).resolve().parent.parent / "dashboard" / "plugin_api.py"
_SPEC = _ilu.spec_from_file_location("action_center_plugin_api", _PLUGIN_API_PATH)
plugin_api = _ilu.module_from_spec(_SPEC)
# Register BEFORE exec: pydantic resolves the models' lazy annotations against
# sys.modules[module_name].__dict__, which only exists once the module is registered.
sys.modules["action_center_plugin_api"] = plugin_api
_SPEC.loader.exec_module(plugin_api)


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    """Isolated persisted-manager database + state.db for every test."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / 'config.yaml').write_text('plugins:\n  enabled: [action-center]\n', encoding='utf-8')
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import goals

    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


@pytest.fixture()
def server(hermes_home, monkeypatch):
    import tui_gateway.server as mod
    monkeypatch.setattr(mod, "_hermes_home", hermes_home)
    monkeypatch.setattr(mod, "_cfg_cache", None)
    monkeypatch.setattr(mod, "_cfg_sig", None)
    monkeypatch.setattr(mod, "_cfg_path", None)
    monkeypatch.setattr(mod, "_db", None)
    monkeypatch.setattr(mod, "_db_error", None)
    # Model a mounted, consent-enabled USER dashboard plugin, never bundled trust.
    plugin_api.unregister_observed_request_producer()
    monkeypatch.setattr(plugin_api, '_dashboard_discovery', lambda: ([{
        'name': 'action-center', 'source': 'user', 'version': '0.1.0',
        '_dir': str(_PLUGIN_API_PATH.parent),
    }], None))
    yield mod
    plugin_api.unregister_observed_request_producer()
    mod._sessions.clear()
    mod._server_requests.reset_for_tests()
    from tools import approval

    with approval._lock:
        approval._gateway_queues.clear()


@pytest.fixture()
def db(server, hermes_home):
    """Launch/own-profile SessionDB handle (sessions listed by /summary)."""
    handle = server._get_db()
    yield handle


def _new_key(tag="inx"):
    return f"{tag}-{uuid.uuid4().hex[:12]}"


def _create_row(db, key, *, source="cli", title=""):
    db.create_session(key, source=source)
    if title:
        db.set_session_title(key, title)
    return key


def _save_goal(key, **overrides):
    from hermes_cli.goals import GoalState, save_goal

    fields = {
        "goal": "Finish the inbox slice",
        "status": "active",
        "turns_used": 1,
        "max_turns": 6,
        "created_at": 100.0,
        "last_turn_at": 200.0,
    }
    fields.update(overrides)
    save_goal(key, GoalState(**fields))


def _save_loop(key, **overrides):
    from hermes_cli.loops import LoopState, save_loop

    fields = {
        "prompt": "Check the deployment",
        "status": "active",
        "mode": "interval",
        "interval_seconds": 300,
        "current_delay": 300,
        "created_at": 100.0,
        "next_due_at": 400.0,
    }
    fields.update(overrides)
    save_loop(key, LoopState(**fields))


def _save_heartbeat(key, **overrides):
    from hermes_cli.heartbeat import HeartbeatState, save_heartbeat

    fields = {
        "prompt": "Check the deployment",
        "interval_seconds": 600,
        "status": "active",
        "created_at": 100.0,
        "last_fired_at": 150.0,
        "fire_count": 2,
    }
    fields.update(overrides)
    save_heartbeat(key, HeartbeatState(**fields))


def _queue_approval(server, key, *, command="rm -rf /tmp/secret-value-abc", description="run removal", **extra):
    """One pending approval in the REAL queue-entry shape (the /respond path pops
    the entry and commits its outcome, so the fixture must carry event/result)."""
    from tools.approval_gateway_wait import _ApprovalEntry

    data = {
        "request_id": f"rid-{uuid.uuid4().hex[:8]}",
        "command": command,
        "description": description,
    }
    data.update(extra)
    with approval._lock:
        approval._gateway_queues.setdefault(key, []).append(_ApprovalEntry(data))
    return data["request_id"]


def _open_session(server, key, *, profile_home=None):
    sid = f"sid-{uuid.uuid4().hex[:8]}"
    server._sessions[sid] = {
        "session_key": key,
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "cols": 120,
        "agent": None,
        "created_at": time.time(),
        # The REAL launch-profile shape: server._add_session stores None for the
        # launch profile, never the home path.
        "profile_home": profile_home,
    }
    return sid


def _queue_clarify(server, key, *, question="Which provider?", choices=None,
                   multi_select=False, profile_home=None, sid=None, on_result=None):
    from tui_gateway.server_requests import ServerRequest

    if sid is None:
        sid = _open_session(server, key, profile_home=profile_home)
    params = {"question": question}
    if choices is not None:
        params["choices"] = choices
    if multi_select:
        params["multi_select"] = True
    req = ServerRequest(sid, "clarify", params, on_result=on_result)
    sr = server._server_requests
    with sr._lock:
        sr._open[req.id] = req
    return sid, req.id


def _queue_batch_clarify(server, key, *, questions, profile_home=None, sid=None):
    from tui_gateway.server_requests import ServerRequest

    if sid is None:
        sid = _open_session(server, key, profile_home=profile_home)
    qids = [q.get("qid", f"q{i}") for i, q in enumerate(questions)]
    req = ServerRequest(sid, "clarify", {"questions": questions}, qids=qids)
    sr = server._server_requests
    with sr._lock:
        sr._open[req.id] = req
    return sid, req.id


def _record_expired(db, key, *, request_id="exp-1", outcome="timeout", command="rm -rf /tmp/probe"):
    assert plugin_api.record_expired_request(db, key, {
        "request_id": request_id,
        "command": command,
        "description": "Delete scratch dir",
        "pattern_keys": ["rm:-rf"],
    }, outcome)


class _FailingScanDB:
    """DB wrapper whose ``list_meta_prefix`` always fails (failed-scan regression tests)."""

    def __init__(self, inner):
        self._inner = inner

    def list_meta_prefix(self, prefix):
        raise RuntimeError("canary-secret-scan-text")

    def __getattr__(self, name):
        return getattr(self._inner, name)


_LOG_HYGIENE_CANARY = "canary-secret-9f3a"


def _assert_logs_are_class_only(caplog):
    """Every backend log record carries the exception CLASS only: no raw text, no tracebacks."""
    records = [r for r in caplog.records if r.name == "action_center_plugin_api"]
    assert records, "expected the forced failures to produce backend log records"
    assert _LOG_HYGIENE_CANARY not in caplog.text  # nowhere, any logger
    assert all(r.exc_info is None for r in records), "no traceback/exception payload in logs"
    assert "RuntimeError" in caplog.text  # the safe class name still reaches the log


# ── failed DB scans: no false all-clear (direct tests) ────────────────────────
class TestExpiredScanFailure:
    """``list_meta_prefix`` failing must never read as "nothing expired"."""

    def test_load_expired_requests_failure_is_named_not_empty(self, server, db):
        failed = plugin_api.load_expired_requests(_FailingScanDB(db), _new_key())
        assert plugin_api._expired_read_failed(failed) is True
        assert failed[0]["error"] == "RuntimeError"  # class only, no raw text
        assert "canary-secret-scan-text" not in json.dumps(failed)

    def test_load_expired_requests_clean_empty_stays_clean(self, server, db):
        assert plugin_api.load_expired_requests(db, _new_key()) == []
        assert plugin_api._expired_read_failed([]) is False

    def test_load_expired_request_counts_failure_is_named(self, server, db):
        counts, error = plugin_api.load_expired_request_counts(_FailingScanDB(db))
        assert counts == {}
        assert error == "RuntimeError"  # class only, no raw text

    def test_load_expired_request_counts_clean_empty_is_not_an_error(self, server, db):
        assert plugin_api.load_expired_request_counts(db) == ({}, None)

    def test_prune_never_classifies_from_a_failed_scan(self, server, db):
        key = _create_row(db, _new_key())
        _record_expired(db, key, request_id="exp-prune")
        # Under the failing wrapper nothing can be read, so nothing may be deleted.
        plugin_api._prune_expired_requests(_FailingScanDB(db), key)
        assert plugin_api.load_expired_requests(db, key)[0]["request_id"] == "exp-prune"

    def test_details_surfaces_failed_expired_read(self, server, db):
        key = _create_row(db, _new_key())
        db.append_message(key, "user", "hello")
        out = plugin_api.action_center_details(session_key=key)
        assert out["coverage"]["errors"] == []  # clean read: no error
        assert out["sessions"][0]["expired_requests"] == []

        def failing_load(dbh, session_key):
            return [{"error": "RuntimeError"}]

        with patch.object(plugin_api, "load_expired_requests", failing_load):
            failed = plugin_api.action_center_details(session_key=key)
        assert any(
            "expired-request read failed" in e for e in failed["coverage"]["errors"])
        assert failed["sessions"][0]["expired_requests"] == []  # sentinel never rendered

    def test_redo_failed_scan_is_503_not_false_404(self, server, db):
        key = _create_row(db, _new_key())
        _record_expired(db, key, request_id="exp-redo")
        sid = _open_session(server, key)
        assert sid
        with patch.object(plugin_api, "_live_session_for_key", lambda *a, **k: sid), \
                patch.object(plugin_api, "load_expired_requests",
                             lambda dbh, session_key: [{"error": "RuntimeError"}]):
            with pytest.raises(Exception) as excinfo:  # noqa: PT011
                plugin_api.action_center_redo(
                    plugin_api.RedoBody(request_id="exp-redo", session_key=key))
        assert getattr(excinfo.value, "status_code", None) == 503
        assert "expired-request read failed" in str(excinfo.value.detail)

    def test_log_hygiene_canary_never_leaks_secrets(self, server, db, caplog):
        """Every backend log line carries the exception CLASS only — no raw exception
        text, no tracebacks — even when every logged failure path is force-failed."""
        import logging as _logging

        canary = _LOG_HYGIENE_CANARY
        key = _create_row(db, _new_key())

        with caplog.at_level(_logging.DEBUG):
            # 0. install the enumeration-failing sessions FIRST: every summary call
            # below then also exercises the live-session enumeration failure site.
            server._sessions.clear()
            server._sessions.update(_BoomSessions())
            # 1. route guard: unexpected failure inside a guarded route
            with patch.object(plugin_api, "badge_state",
                              side_effect=RuntimeError(f"{canary}-guard")):
                with pytest.raises(Exception):  # noqa: PT011
                    plugin_api.action_center_summary()
            # 3. record_expired_request write failure
            with patch.object(plugin_api, "_prune_expired_requests",
                              side_effect=RuntimeError(f"{canary}-record")):
                plugin_api.record_expired_request(db, key, {"request_id": "exp-c"}, "timeout")
            # 4. expired-request read/scan failures + direct-state snapshot/emit.
            # Fail at the DB seam so the plugin's OWN handlers (and their logging) run.
            with patch.object(db, "list_meta_prefix",
                              side_effect=RuntimeError(f"{canary}-load")):
                plugin_api.load_expired_requests(db, key)
                plugin_api.load_expired_request_counts(db)
            key_stored = _create_row(db, _new_key())
            _save_goal(key_stored, status="active")
            with patch.object(plugin_api._server(), "_snapshot_control",
                              side_effect=RuntimeError(f"{canary}-snapshot")):
                with pytest.raises(Exception):  # noqa: PT011
                    plugin_api.action_center_control(
                        plugin_api.ControlBody(action="goal.pause", session_key=key_stored))
            with patch.object(plugin_api, "_live_session_for_key", lambda *a, **k: "sid-x"), \
                    patch.object(plugin_api._server(), "_emit",
                                 side_effect=RuntimeError(f"{canary}-emit")):
                plugin_api.action_center_control(
                    plugin_api.ControlBody(action="goal.resume", session_key=key_stored))
            # 5. summary listing scan failure
            with patch.object(plugin_api, "_listing_rows",
                              side_effect=RuntimeError(f"{canary}-scan")):
                with pytest.raises(Exception):  # noqa: PT011
                    plugin_api.action_center_summary()

        _assert_logs_are_class_only(caplog)


class _BoomSessions(dict):
    """dict whose ``items()`` fails (enumeration failure fixture).

    Note: ``dict.update`` bypasses ``items()`` (CPython fast path), so install it
    with ``clear()`` + ``update()`` and rely on ``items()`` failing only when the
    plugin actually enumerates.
    """

    def items(self):
        raise RuntimeError("boom")


# ── /summary: aggregation ──────────────────────────────────────────────────────
class TestSummary:
    def test_empty_summary_is_stable(self, server, db):
        first = plugin_api.action_center_summary()
        second = plugin_api.action_center_summary()
        assert first == second
        assert first["items"] == []
        assert first["counts"] == {"needs_you": 0, "running": 0, "waiting": 0, "scheduled": 0, "total": 0}
        assert first["badge"] == "none"
        coverage = first["coverage"]
        assert coverage["profile"]
        assert coverage["partial"] is False
        assert coverage["approval_scope"] == "live gateway approval queue"
        assert coverage["connection_scope"] == "active connection and profile only"

    def test_persisted_running_goal_surfaces_without_an_open_session(self, server, db):
        key = _create_row(db, _new_key())
        _save_goal(key)
        assert server._sessions == {}  # no live session; persisted state alone must show it
        out = plugin_api.action_center_summary()
        assert out["badge"] == "none"
        assert out["counts"]["running"] == 1
        item = out["items"][0]
        assert item["session_key"] == key
        assert item["lanes"] == ["running"]
        assert item["goal"]["status"] == "active"
        assert "loop" in item and "heartbeat" in item
        # SPEC item contract fields
        assert item["needs_you_count"] == 0
        assert item["pending_count"] == 0
        assert item["expired_request_count"] == 0
        assert item["updated_at"] == 200.0
        assert item["profile"]  # non-empty profile name for the coverage/item contract

    def test_needs_you_and_scheduled_counts(self, server, db):
        approval_key = _create_row(db, _new_key())
        _queue_approval(server, approval_key)
        hb_key = _create_row(db, _new_key())
        _save_heartbeat(hb_key)
        out = plugin_api.action_center_summary()
        assert out["badge"] == "amber"
        counts = out["counts"]
        assert counts["needs_you"] == 1
        assert counts["scheduled"] == 1
        assert counts["total"] == 2

    def test_live_clarify_for_open_session_marks_needs_you(self, server, db):
        key = _create_row(db, _new_key())
        _queue_clarify(server, key, question="Pick a backend")
        out = plugin_api.action_center_summary()
        assert out["badge"] == "amber"
        assert out["counts"]["needs_you"] == 1
        assert out["items"][0]["pending_clarify"] == {"count": 1}

    def test_launch_profile_none_home_still_counts(self, server, db):
        """A launch-profile session (profile_home=None, the real shape) still counts."""
        key = _create_row(db, _new_key())
        _queue_clarify(server, key, sid=_open_session(server, key, profile_home=None))
        out = plugin_api.action_center_summary()
        assert out["counts"]["needs_you"] == 1
        assert out["items"][0]["pending_clarify"] == {"count": 1}

    def test_approval_egress_never_leaks_raw_credential(self, server, db):
        key = _create_row(db, _new_key())
        _queue_approval(server, key, command="curl -H 'Authorization: Bearer SECRET...2345' https://x")
        serialized = json.dumps(plugin_api.action_center_summary())
        for forbidden in ("Bearer", "Authorization: Bearer SECRET...2345"):
            assert forbidden not in serialized
        item = plugin_api.action_center_summary()["items"][0]
        assert item["pending_approval"] == {"count": 1, "description": "pending approval", "command_redacted": True}

    def test_deny_list_sources_are_excluded(self, server, db):
        visible = _create_row(db, _new_key(), source="cli")
        _save_goal(visible)
        hidden = _create_row(db, _new_key(), source="kanban")
        _save_goal(hidden)
        out = plugin_api.action_center_summary()
        assert out["counts"]["running"] == 1
        assert {item["session_key"] for item in out["items"]} == {visible}

    def test_clarify_is_profile_isolated_by_live_owner(self, server, db):
        """A live session owned by ANOTHER profile must not surface here."""
        from tui_gateway.server_requests import ServerRequest

        foreign = _new_key()
        sid = f"sid-foreign-{uuid.uuid4().hex[:8]}"
        server._sessions[sid] = {
            "session_key": foreign, "history": [], "history_lock": threading.Lock(),
            "history_version": 0, "running": False, "attached_images": [], "cols": 120,
            "agent": None, "created_at": time.time(), "profile_home": "/some/other/profile/home",
        }
        req = ServerRequest(sid, "clarify", {"question": "foreign"})
        sr = server._server_requests
        with sr._lock:
            sr._open[req.id] = req
        out = plugin_api.action_center_summary()
        assert out["counts"]["needs_you"] == 0
        assert out["items"] == []

    def test_approval_queue_state_is_never_consumed_by_reading(self, server, db):
        from tools import approval

        key = _create_row(db, _new_key())
        _queue_approval(server, key)
        with approval._lock:
            before = len(approval._gateway_queues.get(key, []))
        plugin_api.action_center_summary()
        with approval._lock:
            after = len(approval._gateway_queues.get(key, []))
        assert before == after == 1

    def test_partial_coverage_is_declared_when_scan_hits_cap(self, server, db):
        for _ in range(3):
            k = _create_row(db, _new_key())
            _save_goal(k)
        out = plugin_api.action_center_summary(limit=2)
        assert out["coverage"]["partial"] is True
        assert out["coverage"]["scanned_sessions"] == 2

    def test_not_partial_when_exactly_cap_rows(self, server, db):
        for _ in range(3):
            k = _create_row(db, _new_key())
            _save_goal(k)
        out = plugin_api.action_center_summary(limit=3)
        assert out["coverage"]["partial"] is False
        assert out["coverage"]["scanned_sessions"] == 3

    def test_db_unavailable_raises_503(self, server, monkeypatch):
        class _NoDB:
            def __enter__(self):
                return None

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(server, "_profile_db", lambda params: _NoDB())
        with pytest.raises(Exception) as excinfo:  # noqa: PT011 - HTTPException below
            plugin_api.action_center_summary()
        assert getattr(excinfo.value, "status_code", None) == 503

    def test_snapshot_read_error_surfaces_as_red_badge_not_all_clear(self, server, db, monkeypatch):
        key = _create_row(db, _new_key())
        _save_goal(key)

        def boom(session_key):
            raise RuntimeError("simulated snapshot failure")

        monkeypatch.setattr(server, "_snapshot_control", boom)
        out = plugin_api.action_center_summary()
        assert out["badge"] == "red"
        assert out["coverage"]["errors"]
        for err in out["coverage"]["errors"]:
            assert "simulated snapshot failure" not in err
            assert "RuntimeError" in err  # safe type name only

    def test_approval_read_failure_surfaces_in_coverage(self, server, db, monkeypatch):
        from tools import approval

        key = _create_row(db, _new_key())
        _save_goal(key)

        def boom(session_key, *, strict=False):
            raise RuntimeError("approval queue corrupted")

        monkeypatch.setattr(server, "_pending_approval_request_payload", boom)
        out = plugin_api.action_center_summary()
        assert out["badge"] == "red"
        assert any("approval read failed" in e for e in out["coverage"]["errors"])
        assert not any("approval queue corrupted" in e for e in out["coverage"]["errors"])

    def test_clarify_enumeration_failure_surfaces_in_coverage(self, server, db, monkeypatch):
        class _EnumerationBoom(dict):
            def items(self):
                raise TypeError("lock not acquired")

        monkeypatch.setattr(server, "_sessions", _EnumerationBoom())
        out = plugin_api.action_center_summary()
        assert out["badge"] == "red"
        assert any("live-session enumeration failed" in e for e in out["coverage"]["errors"])

    def test_clarify_count_not_question_text(self, server, db):
        key = _create_row(db, _new_key())
        _queue_clarify(server, key, question="What is the API key?")
        serialized = json.dumps(plugin_api.action_center_summary())
        assert "API key" not in serialized
        assert "What is" not in serialized

    def test_multiple_clarify_counted(self, server, db):
        key = _create_row(db, _new_key())
        sid, _rid = _queue_clarify(server, key, question="Q1")
        _queue_clarify(server, key, question="Q2", sid=sid)
        out = plugin_api.action_center_summary()
        assert out["items"][0]["pending_clarify"] == {"count": 2}

    def test_empty_lane_session_included_in_other_category(self, server, db):
        key = _create_row(db, _new_key())
        out = plugin_api.action_center_summary()
        assert len(out["items"]) == 1
        item = out["items"][0]
        assert item["session_key"] == key
        assert "other" in item["categories"]
        assert item["lanes"] == []
        assert out["categories"]["other"] == 1

    def test_background_tasks_counted_by_session_key(self, server, db, monkeypatch):
        from tools import process_registry as pr_mod

        key = _create_row(db, _new_key())
        _save_goal(key)
        fake_sessions = [{
            "session_id": "proc-1", "command": "npm test", "cwd": "/tmp",
            "owner_task_id": key, "started_at": "2026-01-01T00:00:00",
            "uptime_seconds": 10, "status": "running", "output_preview": "",
        }]

        def mock_ls(task_id=None, session_key=None, *, include_retained=False):
            if session_key == key:
                return fake_sessions
            return []

        monkeypatch.setattr(pr_mod.process_registry, "list_sessions", mock_ls)
        item = plugin_api.action_center_summary()["items"][0]
        assert item["background_task_count"] == 1

    def test_subagents_counted_by_owner(self, server, db, monkeypatch):
        from tools import delegate_tool_registry as dtr

        key = _create_row(db, _new_key())
        _save_goal(key)
        fake_records = [
            {"subagent_id": "sa-1", "owner_agent_session_id": key, "goal": "task1", "status": "running"},
            {"subagent_id": "sa-2", "owner_agent_session_id": key, "goal": "task2", "status": "running"},
        ]
        monkeypatch.setattr(dtr, "list_active_subagents", lambda: fake_records)
        out = plugin_api.action_center_summary()
        item = out["items"][0]
        assert item["subagent_count"] == 2
        assert "subagents" in item["categories"]
        assert out["categories"]["subagents"] == 1

    def test_subagent_enumeration_failure_surfaces(self, server, db, monkeypatch):
        from tools import delegate_tool_registry as dtr

        key = _create_row(db, _new_key())
        _save_goal(key)

        def boom():
            raise RuntimeError("subagent registry locked")

        monkeypatch.setattr(dtr, "list_active_subagents", boom)
        out = plugin_api.action_center_summary()
        assert out["badge"] == "red"
        assert any("subagent enumeration failed" in e for e in out["coverage"]["errors"])
        assert not any("subagent registry locked" in e for e in out["coverage"]["errors"])
        item = out["items"][0]
        assert item["subagent_count"] == 0
        assert item["subagent_count_unavailable"] is True

    def test_missing_subagent_registry_module_is_a_coverage_error_not_500(self, server, db, monkeypatch):
        """A gateway without tools.delegate_tool_registry is a degraded source: the
        summary still renders with a coverage error and a red badge (never a 500)."""
        key = _create_row(db, _new_key())
        _save_goal(key)
        monkeypatch.setitem(sys.modules, "tools.delegate_tool_registry", None)
        out = plugin_api.action_center_summary()
        assert out["badge"] == "red"
        assert any("subagent enumeration failed" in e for e in out["coverage"]["errors"])
        item = out["items"][0]
        assert item["subagent_count"] == 0
        assert item["subagent_count_unavailable"] is True

    def test_missing_process_registry_module_is_a_coverage_error_not_500(self, server, db, monkeypatch):
        """Same fail-tolerant treatment for the process registry import."""
        key = _create_row(db, _new_key())
        _save_goal(key)
        monkeypatch.setitem(sys.modules, "tools.process_registry", None)
        out = plugin_api.action_center_summary()
        assert out["badge"] == "red"
        assert any("bg-process enumeration failed" in e for e in out["coverage"]["errors"])
        item = out["items"][0]
        assert item["background_task_count"] == 0
        assert item["background_task_count_unavailable"] is True

    def test_unexpected_summary_failure_is_a_named_503_not_500(self, server, db, monkeypatch):
        """Parity with the core's outer inbox.list handler: an unhandled failure becomes
        the named 5031 error (HTTP 503), never a bare 500 and never partial data."""
        key = _create_row(db, _new_key())
        _save_goal(key)

        def boom(items, errors=()):
            raise RuntimeError("super-secret-internal-XYZ")

        monkeypatch.setattr(plugin_api, "badge_state", boom)
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_summary()
        assert getattr(excinfo.value, "status_code", None) == 503
        assert str(excinfo.value.detail) == "inbox.list failed"
        assert "super-secret-internal-XYZ" not in str(excinfo.value.detail)

    def test_unexpected_details_failure_is_a_named_503_not_500(self, server, db, monkeypatch):
        """Same outer-handler parity for the /details route."""
        key = _create_row(db, _new_key())
        _open_session(server, key)
        db.append_message(key, "user", "hello")

        def boom(dbh, session_key):
            raise RuntimeError("super-secret-internal-XYZ")

        monkeypatch.setattr(plugin_api, "_build_context_excerpt", boom)
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_details(session_key=key)
        assert getattr(excinfo.value, "status_code", None) == 503
        assert str(excinfo.value.detail) == "inbox.requests failed"
        assert "super-secret-internal-XYZ" not in str(excinfo.value.detail)

    def test_unknown_profile_is_a_named_error_not_a_fallback(self, server, db):
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_summary(profile="nonexistent-profile-xyz")
        assert getattr(excinfo.value, "status_code", None) == 404
        assert "does not exist" in str(getattr(excinfo.value, "detail", ""))

    def test_expired_record_counts_toward_needs_you(self, server, db):
        key = _create_row(db, _new_key())
        _record_expired(db, key)
        out = plugin_api.action_center_summary()
        item = next(i for i in out["items"] if i["session_key"] == key)
        assert item["expired_request_count"] == 1
        assert "needs_you" in item["lanes"]
        assert item["needs_you_count"] == 1
        assert out["badge"] == "amber"

    def test_failed_expired_scan_is_never_an_all_clear(self, server, db, monkeypatch):
        """A failed store-wide expired-request scan is NOT a valid empty store:
        the summary declares partial coverage with a red badge instead of an
        all-clear, and a clean read with a valid empty store stays clean."""
        _create_row(db, _new_key())
        real_load = plugin_api.load_expired_request_counts

        def failing_counts(dbh):
            return {}, "RuntimeError"  # the failed-scan shape, class name only

        monkeypatch.setattr(plugin_api, "load_expired_request_counts", failing_counts)
        out = plugin_api.action_center_summary()
        assert out["badge"] == "red"
        assert any("expired-request scan failed" in e for e in out["coverage"]["errors"])
        assert not any("canary" in e.lower() for e in out["coverage"]["errors"])
        # the clean path is untouched: a valid empty store is NOT an error
        monkeypatch.setattr(plugin_api, "load_expired_request_counts", real_load)
        clean = plugin_api.action_center_summary()
        assert clean["badge"] == "none"
        assert clean["coverage"]["errors"] == []
        assert out["items"][0]["expired_request_count"] == 0  # unknown ≠ fabricated count

    def test_deny_list_matches_canonical_listing_sources(self):
        from hermes_state_sessions import INTERNAL_LISTING_SOURCES

        assert plugin_api._deny_sources() == frozenset(INTERNAL_LISTING_SOURCES)
        assert "oneshot" in plugin_api._deny_sources()


# ── pure classification (ported verbatim) ──────────────────────────────────────
class TestLaneClassification:
    def test_empty_control_and_no_pending_yields_no_lanes(self):
        assert plugin_api.classify_lanes(None, False, False) == []
        assert plugin_api.classify_lanes({"goal": None, "loop": None, "heartbeat": None}, False, False) == []

    def test_pending_request_short_circuits_to_needs_you(self):
        assert plugin_api.classify_lanes(None, True, False) == ["needs_you"]
        assert plugin_api.classify_lanes(None, False, True) == ["needs_you"]
        assert plugin_api.classify_lanes(None, False, False, pending_expired=True) == ["needs_you"]

    def test_active_goal_without_barrier_is_running(self):
        control = {"goal": {"status": "active"}, "loop": None, "heartbeat": None}
        assert plugin_api.classify_lanes(control, False, False) == ["running"]

    def test_paused_goal_is_waiting_and_active_heartbeat_is_scheduled(self):
        assert plugin_api.classify_lanes({"goal": {"status": "paused"}, "loop": None, "heartbeat": None},
                                         False, False) == ["waiting"]
        assert plugin_api.classify_lanes({"heartbeat": {"status": "active"}, "goal": None, "loop": None},
                                         False, False) == ["scheduled"]

    def test_loop_scheduled_only_when_future_due(self):
        now = time.time()
        future = {"loop": {"status": "active", "awaiting_response": False, "deferred_by_goal": False,
                           "next_due_at": now + 60}, "goal": None, "heartbeat": None}
        past = {"loop": {"status": "active", "awaiting_response": False, "deferred_by_goal": False,
                         "next_due_at": now - 60}, "goal": None, "heartbeat": None}
        assert plugin_api.classify_lanes(future, False, False) == ["scheduled"]
        assert plugin_api.classify_lanes(past, False, False) == []

    def test_lanes_are_ordered_and_deduplicated(self):
        control = {
            "goal": {"status": "active"},
            "loop": {"status": "active", "awaiting_response": True, "deferred_by_goal": False, "next_due_at": 0},
            "heartbeat": {"status": "active"},
        }
        assert plugin_api.classify_lanes(control, True, False) == ["needs_you", "running", "scheduled"]

    def test_categories_overlap(self):
        control = {"goal": {"status": "active"}, "loop": {"status": "active"}, "heartbeat": None}
        assert plugin_api.classify_categories(control) == ["goals", "loops"]
        assert plugin_api.classify_categories(None) == ["other"]
        assert plugin_api.classify_categories({"goal": {"status": "cleared"}, "loop": None,
                                               "heartbeat": None}) == ["other"]

    def test_badge_states(self):
        assert plugin_api.badge_state([]) == "none"
        assert plugin_api.badge_state([{"lanes": ["running"]}]) == "none"
        assert plugin_api.badge_state([{"lanes": ["needs_you"]}]) == "amber"
        assert plugin_api.badge_state([{"lanes": ["needs_you"]}], errors=["x"]) == "red"
        assert plugin_api.badge_state([], errors=["x"]) == "red"


# ── /details: scoped request details ──────────────────────────────────────────
class TestDetails:
    def test_empty_session_key_is_400(self, server, db):
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_details(session_key="")
        assert getattr(excinfo.value, "status_code", None) == 400

    def test_missing_session_is_404(self, server, db):
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_details(session_key="nonexistent-key")
        assert getattr(excinfo.value, "status_code", None) == 404
        assert "Session not found" in str(excinfo.value.detail)

    def test_denied_persisted_source_cannot_read_live_requests(self, server, db):
        key = _create_row(db, _new_key(), source="tool")
        _open_session(server, key)
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_details(session_key=key)
        assert getattr(excinfo.value, "status_code", None) == 404

    def test_session_not_live_returns_no_live_session(self, server, db):
        key = _create_row(db, _new_key())
        result = plugin_api.action_center_details(session_key=key)
        assert result["sessions"][0]["live_session_ids"] == []
        assert result["sessions"][0]["approvals"] == []
        assert result["sessions"][0]["clarifications"] == []

    def test_live_session_found_and_launch_home_none_visible(self, server, db):
        key = _create_row(db, _new_key())
        sid = _open_session(server, key, profile_home=None)
        rid = _queue_approval(server, key)
        result = plugin_api.action_center_details(session_key=key)
        assert sid in result["sessions"][0]["live_session_ids"]
        approvals = result["sessions"][0]["approvals"]
        assert len(approvals) == 1
        assert approvals[0]["request_id"] == rid
        assert "deny" in approvals[0]["choices"] and "once" in approvals[0]["choices"]

    def test_wrong_profile_cannot_read_requests(self, server, db):
        key = _create_row(db, _new_key())
        _open_session(server, key)
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_details(session_key=key, profile="nonexistent-foreign")
        assert getattr(excinfo.value, "status_code", None) == 404

    def test_duplicate_durable_keys_across_profiles_dont_mix(self, server, db):
        key = _create_row(db, _new_key())
        launch_home = str(server._hermes_home)
        sid_launch = _open_session(server, key, profile_home=launch_home)
        _open_session(server, key, profile_home="/other/profile/home")
        result = plugin_api.action_center_details(session_key=key)
        assert sid_launch in result["sessions"][0]["live_session_ids"]
        assert len(result["sessions"][0]["live_session_ids"]) == 1

    def test_approval_redaction_and_choices(self, server, db):
        key = _create_row(db, _new_key())
        _open_session(server, key)
        _queue_approval(server, key,
                        command="curl -H 'Authorization: Bearer SECRET_KEY_12345' https://api.example.com",
                        smart_denied=True)
        result = plugin_api.action_center_details(session_key=key)
        serialized = json.dumps(result)
        assert "SECRET_KEY_12345" not in serialized
        approval = result["sessions"][0]["approvals"][0]
        assert "***" in approval["command"]  # redacted, not stripped
        assert "session" not in approval["choices"]
        assert "once" in approval["choices"] and "deny" in approval["choices"]
        assert approval["smart_denied"] is True

    def test_allow_permanent_false_excludes_always(self, server, db):
        key = _create_row(db, _new_key())
        _open_session(server, key)
        _queue_approval(server, key, allow_permanent=False)
        choices = plugin_api.action_center_details(session_key=key)["sessions"][0]["approvals"][0]["choices"]
        assert "always" not in choices
        assert "session" in choices and "deny" in choices

    def test_all_pending_approvals_included_and_queue_never_consumed(self, server, db):
        from tools import approval

        key = _create_row(db, _new_key())
        _open_session(server, key)
        for _ in range(3):
            _queue_approval(server, key, command=f"command-{_}")
        with approval._lock:
            before = sum(len(q) for q in approval._gateway_queues.values())
        result = plugin_api.action_center_details(session_key=key)
        with approval._lock:
            after = sum(len(q) for q in approval._gateway_queues.values())
        assert len(result["sessions"][0]["approvals"]) == 3
        assert before == after == 3

    def test_single_clarify_params_exposed(self, server, db):
        key = _create_row(db, _new_key())
        _sid, req_id = _queue_clarify(server, key, question="Which backend?",
                                      choices=["local", "docker", "ssh"])
        result = plugin_api.action_center_details(session_key=key)
        clarify = result["sessions"][0]["clarifications"][0]
        assert clarify["request_id"] == req_id
        assert clarify["kind"] == "single"
        params = clarify["params"]
        assert params["question"] == "Which backend?"
        assert params["choices"] == ["local", "docker", "ssh"]

    def test_multi_select_and_batch_clarify(self, server, db):
        key = _create_row(db, _new_key())
        _sid, req_id = _queue_clarify(server, key, question="Pick features",
                                      choices=["auth", "cache"], multi_select=True)
        result = plugin_api.action_center_details(session_key=key)
        assert result["sessions"][0]["clarifications"][0]["params"]["multi_select"] is True

        questions = [
            {"qid": "q1", "question": "Project name?", "choices": ["proj-a", "proj-b"]},
            {"qid": "q2", "question": "Language?", "choices": ["python", "rust"]},
        ]
        _sid2, batch_id = _queue_batch_clarify(server, key, questions=questions)
        server._server_requests.lock_answer(batch_id, "q1", "proj-a")
        result = plugin_api.action_center_details(session_key=key)
        clarifications = result["sessions"][0]["clarifications"]
        batch = next(c for c in clarifications if c["request_id"] == batch_id)
        assert batch["kind"] == "batch"
        assert len(batch["params"]["questions"]) == 2
        assert batch["params"]["answers"]["q1"] == "proj-a"

    def test_non_allowed_request_types_excluded(self, server, db):
        from tui_gateway.server_requests import ServerRequest

        key = _create_row(db, _new_key())
        sid = _open_session(server, key)
        for method, params in (
            ("secret", {"env_var": "API_KEY", "prompt": "Enter API key"}),
            ("vault.unlock_prompt", {"backend": "1password", "display_name": "Master"}),
            ("sudo", {"command": "rm -rf /"}),
        ):
            req = ServerRequest(sid, method, params)
            with server._server_requests._lock:
                server._server_requests._open[req.id] = req
        result = plugin_api.action_center_details(session_key=key)
        assert result["sessions"][0]["approvals"] == []
        assert result["sessions"][0]["clarifications"] == []

    def test_stale_session_key_not_live(self, server, db):
        key = _create_row(db, _new_key())
        sid = _open_session(server, key)
        server._sessions[sid]["_finalized"] = True
        result = plugin_api.action_center_details(session_key=key)
        assert sid not in result["sessions"][0]["live_session_ids"]

    def test_context_excerpt_bounded_redacted_oldest_first(self, server, db):
        key = _create_row(db, _new_key())
        _open_session(server, key)
        for index in range(6):
            db.append_message(key, "user", f"user turn {index}")
            db.append_message(key, "assistant", f"assistant turn {index}")
        context = plugin_api.action_center_details(session_key=key)["sessions"][0]["context"]
        assert context["available"] is True
        assert context["reason"] is None
        texts = [m["text"] for m in context["messages"]]
        assert texts == ["user turn 4", "assistant turn 4", "user turn 5", "assistant turn 5"]

    def test_context_skips_tool_rows_and_textless_turns(self, server, db):
        key = _create_row(db, _new_key())
        _open_session(server, key)
        db.append_message(key, "user", "clean the folder")
        db.append_message(key, "tool", '{"raw": "tool-payload-should-not-surface"}', tool_name="terminal")
        db.append_message(key, "assistant", "")
        db.append_message(key, "assistant", "Done, the folder is empty.")
        context = plugin_api.action_center_details(session_key=key)["sessions"][0]["context"]
        texts = [m["text"] for m in context["messages"]]
        assert texts == ["clean the folder", "Done, the folder is empty."]
        assert "tool-payload-should-not-surface" not in json.dumps(context)

    def test_context_bounds_each_message(self, server, db):
        key = _create_row(db, _new_key())
        _open_session(server, key)
        db.append_message(key, "user", "x" * 5000)
        context = plugin_api.action_center_details(session_key=key)["sessions"][0]["context"]
        text = context["messages"][-1]["text"]
        assert len(text) <= 321
        assert text.endswith("…")

    def test_context_read_failure_is_named(self, server, db, monkeypatch):
        from hermes_state import SessionDB

        def boom(self, *args, **kwargs):
            raise RuntimeError("super-secret-internal-XYZ")

        monkeypatch.setattr(SessionDB, "get_messages", boom)
        key = _create_row(db, _new_key())
        _open_session(server, key)
        result = plugin_api.action_center_details(session_key=key)
        context = result["sessions"][0]["context"]
        assert context["available"] is False
        assert context["messages"] == []
        assert context["reason"].startswith("transcript read failed:")
        assert "super-secret-internal-XYZ" not in json.dumps(result)

    def test_context_redacts_before_egress(self, server, db, monkeypatch):
        import agent.redact as redact_module

        monkeypatch.setattr(redact_module, "redact_sensitive_text", lambda text, force=False: "[SCRUBBED]")
        key = _create_row(db, _new_key())
        _open_session(server, key)
        db.append_message(key, "user", "my key is «redacted:sk-…»")
        context = plugin_api.action_center_details(session_key=key)["sessions"][0]["context"]
        assert context["messages"][-1]["text"] == "[SCRUBBED]"

    def test_no_live_session_reports_unavailable_anchor(self, server, db):
        key = _create_row(db, _new_key())
        result = plugin_api.action_center_details(session_key=key)
        assert result["coverage"]["context_anchor"].startswith("unavailable:")
        assert result["sessions"][0]["context"] == {
            "available": False,
            "reason": "no displayable messages in this session",
            "messages": [],
        }

    def test_live_session_with_messages_reports_available_anchor(self, server, db):
        key = _create_row(db, _new_key())
        _open_session(server, key)
        db.append_message(key, "user", "please tidy the temp folder")
        result = plugin_api.action_center_details(session_key=key)
        assert result["coverage"]["context_anchor"].startswith("available:")
        assert result["sessions"][0]["context"]["messages"][-1]["text"] == "please tidy the temp folder"

    def test_read_never_resumes_or_mutates_the_session(self, server, db):
        key = _create_row(db, _new_key())
        sid = _open_session(server, key)
        db.append_message(key, "user", "hello")
        plugin_api.action_center_details(session_key=key)
        assert server._sessions[sid]["history"] == []
        assert server._sessions[sid]["running"] is False


# ── expired requests: redo / dismiss ──────────────────────────────────────────
class TestExpiredRequests:
    def test_recorded_request_listed_in_details(self, server, db):
        key = _create_row(db, _new_key())
        _record_expired(db, key)
        detail = plugin_api.action_center_details(session_key=key)["sessions"][0]
        assert [e["request_id"] for e in detail["expired_requests"]] == ["exp-1"]
        entry = detail["expired_requests"][0]
        assert entry["command"] == "rm -rf /tmp/probe"
        assert entry["outcome"] == "timeout"
        assert entry["ended_at"] > 0

    def test_newest_first_and_pruned_to_the_kept_window(self, server, db):
        key = _create_row(db, _new_key())
        for index in range(12):
            _record_expired(db, key, request_id=f"exp-{index:02d}")
        detail = plugin_api.action_center_details(session_key=key)["sessions"][0]
        ids = [e["request_id"] for e in detail["expired_requests"]]
        assert len(ids) == 10, ids
        assert ids[0] == "exp-11" and "exp-00" not in ids

    def test_record_without_request_id_is_not_written(self, db, server):
        key = _create_row(db, _new_key())
        assert plugin_api.record_expired_request(db, key, {"command": "x"}, "timeout") is False
        detail = plugin_api.action_center_details(session_key=key)["sessions"][0]
        assert detail["expired_requests"] == []

    def test_dismiss_removes_the_record(self, server, db):
        key = _create_row(db, _new_key())
        _record_expired(db, key)
        result = plugin_api.action_center_dismiss(
            plugin_api.DismissBody(request_id="exp-1", session_key=key))
        assert result["dismissed"] is True
        detail = plugin_api.action_center_details(session_key=key)["sessions"][0]
        assert detail["expired_requests"] == []

    def test_dismiss_unknown_request_is_404(self, server, db):
        key = _create_row(db, _new_key())
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_dismiss(
                plugin_api.DismissBody(request_id="missing", session_key=key))
        assert getattr(excinfo.value, "status_code", None) == 404

    def test_dismiss_on_a_gateway_without_delete_meta_still_works(self, server, db, monkeypatch):
        """The runtime tree's SessionDB predates the PR's delete_meta; the write
        must go through the public _write_sql path instead."""
        key = _create_row(db, _new_key())
        _record_expired(db, key)
        original = type(db).delete_meta if hasattr(type(db), "delete_meta") else None
        if original is not None:
            monkeypatch.delattr(type(db), "delete_meta")
        result = plugin_api.action_center_dismiss(
            plugin_api.DismissBody(request_id="exp-1", session_key=key))
        assert result["dismissed"] is True
        assert plugin_api.action_center_details(session_key=key)["sessions"][0]["expired_requests"] == []

    def test_redo_requires_a_live_session(self, server, db):
        key = _create_row(db, _new_key())
        _record_expired(db, key)
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_redo(plugin_api.RedoBody(request_id="exp-1", session_key=key))
        assert getattr(excinfo.value, "status_code", None) == 409  # nothing is resumed on the operator's behalf
        assert "not running" in str(excinfo.value.detail)

    def test_redo_unknown_record_is_404(self, server, db):
        key = _create_row(db, _new_key())
        _open_session(server, key)
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_redo(plugin_api.RedoBody(request_id="missing", session_key=key))
        assert getattr(excinfo.value, "status_code", None) == 404

    def test_redo_submits_a_prompt_and_clears_the_record(self, server, db, monkeypatch):
        key = _create_row(db, _new_key())
        sid = _open_session(server, key)
        _record_expired(db, key)
        submitted: list[dict] = []

        def _fake_submit(rid, params):
            submitted.append(params)
            return {"result": {"status": "queued"}}

        monkeypatch.setitem(server._methods, "prompt.submit", _fake_submit)
        result = plugin_api.action_center_redo(plugin_api.RedoBody(request_id="exp-1", session_key=key))
        assert result["redone"] is True and result["session_id"] == sid
        assert submitted and submitted[0]["session_id"] == sid
        assert "rm -rf /tmp/probe" in submitted[0]["text"]
        assert submitted[0]["display_kind"] == "hidden"
        assert submitted[0]["queued"] is True
        detail = plugin_api.action_center_details(session_key=key)["sessions"][0]
        assert detail["expired_requests"] == []

    def test_redo_keeps_the_record_when_the_submit_fails(self, server, db, monkeypatch):
        key = _create_row(db, _new_key())
        _open_session(server, key)
        _record_expired(db, key)

        def _busy_submit(rid, params):
            return {"error": {"code": 4009, "message": "session busy"}}

        monkeypatch.setitem(server._methods, "prompt.submit", _busy_submit)
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_redo(plugin_api.RedoBody(request_id="exp-1", session_key=key))
        assert getattr(excinfo.value, "status_code", None) == 409
        assert "session busy" in str(excinfo.value.detail)
        detail = plugin_api.action_center_details(session_key=key)["sessions"][0]
        assert [e["request_id"] for e in detail["expired_requests"]] == ["exp-1"]

    def test_redo_clear_failure_is_503_not_500(self, server, db, monkeypatch):
        """A storage failure while clearing the record after an accepted submit becomes
        the named 503 error — never an unhandled exception (HTTP 500)."""
        key = _create_row(db, _new_key())
        _open_session(server, key)
        _record_expired(db, key)
        monkeypatch.setitem(server._methods, "prompt.submit",
                            lambda rid, params: {"result": {"status": "queued"}})
        monkeypatch.setattr(plugin_api, "clear_expired_request",
                            lambda db, session_key, request_id: (_ for _ in ()).throw(
                                RuntimeError("read-only foreign handle")))
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_redo(plugin_api.RedoBody(request_id="exp-1", session_key=key))
        assert getattr(excinfo.value, "status_code", None) == 503
        assert "redo failed" in str(excinfo.value.detail)
        assert "read-only foreign handle" not in str(excinfo.value.detail)  # sanitized

    def test_redo_uses_one_handle_and_keeps_the_record_when_submit_fails_before_clear(self, server, db, monkeypatch):
        """Lookup and clear share one handle; a submit refusal never reaches the clear."""
        key = _create_row(db, _new_key())
        _open_session(server, key)
        _record_expired(db, key)
        opens: list[str] = []
        real_profile_db = server._profile_db

        def counting_profile_db(params=None, *, writer=False):
            opens.append(str((params or {}).get("profile")))
            return real_profile_db(params, writer=writer)

        monkeypatch.setattr(server, "_profile_db", counting_profile_db)
        monkeypatch.setitem(server._methods, "prompt.submit",
                            lambda rid, params: {"error": {"code": 4009, "message": "session busy"}})
        with pytest.raises(Exception):  # noqa: PT011 - 409 submit refusal
            plugin_api.action_center_redo(plugin_api.RedoBody(request_id="exp-1", session_key=key))
        assert opens == ["None"]  # one handle, launch profile, lookup and clear shared
        detail = plugin_api.action_center_details(session_key=key)["sessions"][0]
        assert [e["request_id"] for e in detail["expired_requests"]] == ["exp-1"]

    def test_named_profile_redo_reads_and_clears_in_its_own_store(self, server, db, tmp_path, monkeypatch):
        """A named profile's redo resolves the record from THAT profile's store — the
        same store its /details listed it from — and the profile scoping holds."""
        profile_name = f"prof{uuid.uuid4().hex[:8]}"
        profile_home = tmp_path / "profiles" / profile_name
        profile_home.mkdir(parents=True)
        monkeypatch.setattr(
            "hermes_cli.profiles._get_profiles_root", lambda: tmp_path / "profiles")
        key = _new_key("prof")
        # Seed THAT profile's store with the record via the same seam the route uses.
        with server._profile_db({"profile": profile_name}, writer=True) as pdb:
            assert pdb is not None
            assert plugin_api.record_expired_request(pdb, key,
                                                     {"request_id": "exp-prof", "command": "cmd-x"},
                                                     "timeout")
        _open_session(server, key, profile_home=str(profile_home))
        with server._profile_db({"profile": profile_name}, writer=True) as pdb:
            pdb.create_session(key, source="cli")  # the human-facing row the gate requires
        monkeypatch.setitem(server._methods, "prompt.submit",
                            lambda rid, params: {"result": {"status": "queued"}})
        result = plugin_api.action_center_redo(
            plugin_api.RedoBody(request_id="exp-prof", session_key=key, profile=profile_name))
        assert result["redone"] is True and result["record_cleared"] is True
        with server._profile_db({"profile": profile_name}, writer=True) as pdb:
            assert plugin_api.load_expired_requests(pdb, key) == []
            assert pdb.get_session(key) is not None  # the human-facing row gate held

    def test_redo_submission_output_is_not_echoed(self, server, db, monkeypatch):
        """Only the enqueue verdict is reported; the submit envelope's own fields are not."""
        key = _create_row(db, _new_key())
        sid = _open_session(server, key)
        _record_expired(db, key)
        monkeypatch.setitem(server._methods, "prompt.submit",
                            lambda rid, params: {"result": {"status": "queued", "notice": "internal detail"}})
        result = plugin_api.action_center_redo(plugin_api.RedoBody(request_id="exp-1", session_key=key))
        assert result == {"redone": True, "session_id": sid, "record_cleared": True}
        assert "internal detail" not in json.dumps(result)


# ── /respond: approvals ────────────────────────────────────────────────────────
class TestRespond:
    def test_missing_params_are_400(self, server, db):
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_respond(plugin_api.RespondBody(request_id="", choice="once",
                                                                    session_key=""))
        assert getattr(excinfo.value, "status_code", None) == 400

    def test_invalid_choice_is_400(self, server, db):
        key = _create_row(db, _new_key())
        _open_session(server, key)
        rid = _queue_approval(server, key)
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_respond(
                plugin_api.RespondBody(request_id=rid, choice="sometimes", session_key=key))
        assert getattr(excinfo.value, "status_code", None) == 400

    def test_not_live_session_is_404_nothing_resolved(self, server, db):
        from tools import approval

        key = _create_row(db, _new_key())
        rid = _queue_approval(server, key)
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_respond(
                plugin_api.RespondBody(request_id=rid, choice="once", session_key=key))
        assert getattr(excinfo.value, "status_code", None) == 404
        with approval._lock:
            assert len(approval._gateway_queues.get(key, [])) == 1  # untouched

    def test_approve_once_resolves_only_that_request(self, server, db):
        from tools import approval

        key = _create_row(db, _new_key())
        _open_session(server, key)
        rid = _queue_approval(server, key)
        second = _queue_approval(server, key)
        result = plugin_api.action_center_respond(
            plugin_api.RespondBody(request_id=rid, choice="once", session_key=key))
        assert result == {"resolved": 1}
        with approval._lock:
            remaining = [e.data.get("request_id") for e in approval._gateway_queues.get(key, [])]
        assert remaining == [second]

    def test_deny_choice_resolves_with_deny(self, server, db):
        from tools import approval

        key = _create_row(db, _new_key())
        _open_session(server, key)
        rid = _queue_approval(server, key)
        result = plugin_api.action_center_respond(
            plugin_api.RespondBody(request_id=rid, choice="deny", session_key=key))
        assert result == {"resolved": 1}
        with approval._lock:
            assert approval._gateway_queues.get(key) in (None, [])

    def test_stale_runtime_session_still_resolves_by_durable_key(self, server, db):
        """Live presence is matched by profile-normalized session_key, so a session
        whose runtime record was minted before the panel opened still resolves."""
        from tools import approval

        key = _create_row(db, _new_key())
        _open_session(server, key, profile_home=str(server._hermes_home).upper())
        rid = _queue_approval(server, key)
        result = plugin_api.action_center_respond(
            plugin_api.RespondBody(request_id=rid, choice="session", session_key=key))
        assert result == {"resolved": 1}
        with approval._lock:
            assert approval._gateway_queues.get(key) in (None, [])

    def test_choice_not_offered_by_the_request_is_refused(self, server, db):
        """A choice the approval payload does not offer can never be submitted: an
        ``always`` on an allow_permanent=False approval must not mint a permanent
        rule from the panel (the payload's own choices are the gate)."""
        from tools import approval

        key = _create_row(db, _new_key())
        _open_session(server, key)
        rid = _queue_approval(server, key, allow_permanent=False)
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_respond(
                plugin_api.RespondBody(request_id=rid, choice="always", session_key=key))
        assert getattr(excinfo.value, "status_code", None) == 400
        assert "always" in str(excinfo.value.detail)  # names what was refused
        with approval._lock:
            assert len(approval._gateway_queues.get(key, [])) == 1  # untouched
        # A choice the card actually offers still resolves.
        assert plugin_api.action_center_respond(
            plugin_api.RespondBody(request_id=rid, choice="once", session_key=key)) == {"resolved": 1}

    def test_smart_denied_request_refuses_session_and_always(self, server, db):
        """A smart-denied approval offers only once/deny; session/always are refused."""
        from tools import approval

        key = _create_row(db, _new_key())
        _open_session(server, key)
        rid = _queue_approval(server, key, smart_denied=True)
        for refused in ("session", "always"):
            with pytest.raises(Exception) as excinfo:  # noqa: PT011
                plugin_api.action_center_respond(
                    plugin_api.RespondBody(request_id=rid, choice=refused, session_key=key))
            assert getattr(excinfo.value, "status_code", None) == 400, refused
        with approval._lock:
            assert len(approval._gateway_queues.get(key, [])) == 1  # untouched
        assert plugin_api.action_center_respond(
            plugin_api.RespondBody(request_id=rid, choice="deny", session_key=key)) == {"resolved": 1}

    def test_unknown_request_id_resolves_to_an_honest_zero(self, server, db):
        """A live session with no such pending request: nothing is fabricated."""
        key = _create_row(db, _new_key())
        _open_session(server, key)
        result = plugin_api.action_center_respond(
            plugin_api.RespondBody(request_id="rid-never-existed", choice="once", session_key=key))
        assert result == {"resolved": 0}


# ── /answer: clarifications ────────────────────────────────────────────────────
class TestAnswer:
    def test_missing_request_id_is_400(self, server, db):
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_answer(plugin_api.AnswerBody(request_id="", answer="x", session_key="k"))
        assert getattr(excinfo.value, "status_code", None) == 400

    def test_missing_session_key_is_400(self, server, db):
        """session_key is mandatory: an answer can never be resolved without the
        durable identity it must be bound to."""
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_answer(plugin_api.AnswerBody(request_id="srq-x", answer="x"))
        assert getattr(excinfo.value, "status_code", None) == 400

    def test_single_answer_resolves_the_request(self, server, db):
        key = _create_row(db, _new_key())
        sid, req_id = _queue_clarify(server, key, question="Which backend?")
        result = plugin_api.action_center_answer(
            plugin_api.AnswerBody(request_id=req_id, answer="docker", session_key=key))
        assert result["status"] == "ok"
        sr = server._server_requests
        with sr._lock:
            assert req_id not in sr._open

    def test_single_answer_non_string_json_encoded(self, server, db):
        """Multi-select answers travel as a JSON array in the response frame."""
        key = _create_row(db, _new_key())
        received: list = []
        _sid, req_id = _queue_clarify(server, key, question="Pick features", multi_select=True,
                                      on_result=lambda result: received.append(result))
        # The panel posts a JSON array; the agent-side result must carry it verbatim
        # so the multi-select answer parses (core response-frame semantics).
        result = plugin_api.action_center_answer(
            plugin_api.AnswerBody(request_id=req_id, answer=["auth", "cache"], session_key=key))
        assert result["status"] == "ok"
        assert received == [{"answer": ["auth", "cache"]}]
        sr = server._server_requests
        with sr._lock:
            assert req_id not in sr._open

    def test_single_answer_expired_request_reports_expired(self, server, db):
        """A genuinely unknown id for a VALID, LIVE, human-facing owned session keeps
        the honest ``expired`` semantics — no fabricated success, no error."""
        key = _create_row(db, _new_key())
        _queue_clarify(server, key, question="open so the session is live")
        result = plugin_api.action_center_answer(
            plugin_api.AnswerBody(request_id="srq-not-open-anywhere", answer="x", session_key=key))
        assert result["status"] == "expired"

    def test_batch_locks_one_call_per_question_last_lock_resolves(self, server, db):
        key = _create_row(db, _new_key())
        questions = [
            {"qid": "q1", "question": "Project name?", "choices": ["proj-a", "proj-b"]},
            {"qid": "q2", "question": "Language?", "choices": ["python", "rust"]},
        ]
        _sid, req_id = _queue_batch_clarify(server, key, questions=questions)
        first = plugin_api.action_center_answer(
            plugin_api.AnswerBody(request_id=req_id, answer="proj-a", question_id="q1", session_key=key))
        assert first == {"status": "ok", "remaining": ["q2"]}
        last = plugin_api.action_center_answer(
            plugin_api.AnswerBody(request_id=req_id, answer="rust", question_id="q2", session_key=key))
        assert last == {"status": "ok", "remaining": []}
        sr = server._server_requests
        with sr._lock:
            assert req_id not in sr._open  # the last lock resolved the request

    def test_batch_lock_unknown_question_is_400(self, server, db):
        key = _create_row(db, _new_key())
        questions = [{"qid": "q1", "question": "Project name?", "choices": ["proj-a"]}]
        _sid, req_id = _queue_batch_clarify(server, key, questions=questions)
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_answer(
                plugin_api.AnswerBody(request_id=req_id, answer="x", question_id="nope", session_key=key))
        assert getattr(excinfo.value, "status_code", None) == 400

    def test_batch_lock_expired_reports_expired(self, server, db):
        key = _create_row(db, _new_key())
        _queue_clarify(server, key, question="open so the session is live")
        result = plugin_api.action_center_answer(
            plugin_api.AnswerBody(request_id="srq-gone", answer="x", question_id="q1", session_key=key))
        assert result["status"] == "expired"

    def test_batch_non_string_answer_json_encoded(self, server, db):
        key = _create_row(db, _new_key())
        questions = [{"qid": "q1", "question": "Pick features", "choices": ["a", "b"]}]
        _sid, req_id = _queue_batch_clarify(server, key, questions=questions)
        result = plugin_api.action_center_answer(
            plugin_api.AnswerBody(request_id=req_id, answer=["a"], question_id="q1", session_key=key))
        # Single-question batch: the last lock resolves the request.
        assert result == {"status": "ok", "remaining": []}
        # The queue's locked answer is the JSON-encoded array string (core semantics).
        details = plugin_api.action_center_details(session_key=key)["sessions"][0]
        assert details["clarifications"] == []  # resolved, so no longer open


# ── trust boundary: request ownership & human-facing gates ────────────────────
class TestTrustBoundary:
    """Every mutation binds to a human-facing DB session row and to requests the
    addressed session provably owns — a known foreign id is never resolved."""

    def test_foreign_request_id_is_not_resolved_cross_session(self, server, db):
        """An answer naming ANOTHER session's clarify id (same profile) resolves
        nothing: the id is not owned by the addressed session."""
        key_owner = _create_row(db, _new_key())
        _sid, foreign_id = _queue_clarify(server, key_owner, question="owner's question")
        key_attacker = _create_row(db, _new_key())
        _queue_clarify(server, key_attacker, question="attacker's own open request")
        result = plugin_api.action_center_answer(
            plugin_api.AnswerBody(request_id=foreign_id, answer="HIJACKED", session_key=key_attacker))
        assert result == {"status": "expired"}  # honest expiry, never a resolution
        sr = server._server_requests
        with sr._lock:
            assert foreign_id in sr._open  # the owner's request was untouched
        snapshots = sr.open_requests(_sid)
        assert snapshots and snapshots[0]["id"] == foreign_id

    def test_global_resolution_without_ownership_is_denied(self, server, db):
        """A clarify id from a session NOT addressed must not be globally resolved by
        a known request id alone — even when the addressed session is live here."""
        key_live = _create_row(db, _new_key())
        _sid, foreign_id = _queue_clarify(server, key_live, question="other session's question")
        key_row_only = _create_row(db, _new_key())
        _queue_clarify(server, key_row_only, question="addressed session's own open request")
        result = plugin_api.action_center_answer(
            plugin_api.AnswerBody(request_id=foreign_id, answer="HIJACKED", session_key=key_row_only))
        assert result == {"status": "expired"}
        sr = server._server_requests
        with sr._lock:
            assert foreign_id in sr._open

    def test_answer_on_row_without_live_runtime_is_404(self, server, db):
        """A durable row with NO live runtime cannot be answered (nothing is waiting
        anywhere reachable): the same 404 the not-live /respond path returns."""
        key = _create_row(db, _new_key())
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_answer(
                plugin_api.AnswerBody(request_id="srq-whatever", answer="x", session_key=key))
        assert getattr(excinfo.value, "status_code", None) == 404

    def test_batch_lock_on_a_foreign_request_id_is_denied(self, server, db):
        """The batch path binds the same way: a lock on another session's id is expired,
        and the foreign batch keeps its locked answers untouched."""
        key_owner = _create_row(db, _new_key())
        questions = [
            {"qid": "q1", "question": "Owner q?", "choices": ["a", "b"]},
            {"qid": "q2", "question": "Owner q2?", "choices": ["c", "d"]},
        ]
        _sid, foreign_id = _queue_batch_clarify(server, key_owner, questions=questions)
        server._server_requests.lock_answer(foreign_id, "q1", "owner-answer")
        key_attacker = _create_row(db, _new_key())
        _queue_clarify(server, key_attacker, question="attacker open request")
        result = plugin_api.action_center_answer(
            plugin_api.AnswerBody(request_id=foreign_id, answer="HIJACKED",
                                  question_id="q1", session_key=key_attacker))
        assert result == {"status": "expired"}
        snaps = server._server_requests.open_requests(_sid)
        batch = next(s for s in snaps if s["id"] == foreign_id)
        assert batch["params"]["answers"] == {"q1": "owner-answer"}  # untouched

    def test_answer_on_unlisted_session_row_is_404(self, server, db):
        """An addressable-but-unlisted session (no durable row) can never be mutated,
        and a request minted onto it stays open."""
        key = _new_key("no-row")
        _sid, req_id = _queue_clarify(server, key, question="orphan session's question")
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_answer(
                plugin_api.AnswerBody(request_id=req_id, answer="x", session_key=key))
        assert getattr(excinfo.value, "status_code", None) == 404
        assert "session not found" in str(excinfo.value.detail)
        with server._server_requests._lock:
            assert req_id in server._server_requests._open

    def test_answer_on_deny_listed_session_is_404(self, server, db):
        """A deny-listed (kanban) session is not human-facing: /answer refuses with
        the core's session-not-found refusal and never touches its live request."""
        key = _create_row(db, _new_key(), source="kanban")
        _sid, req_id = _queue_clarify(server, key, question="kanban question")
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_answer(
                plugin_api.AnswerBody(request_id=req_id, answer="x", session_key=key))
        assert getattr(excinfo.value, "status_code", None) == 404
        with server._server_requests._lock:
            assert req_id in server._server_requests._open

    def test_respond_on_deny_listed_session_does_not_consume_the_queue(self, server, db):
        """A deny-listed session's pending approval can never be approved from the
        panel: 404 and the queue entry survives untouched."""
        from tools import approval

        hidden = _create_row(db, _new_key(), source="kanban")
        _open_session(server, hidden)
        _queue_approval(server, hidden)
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_respond(
                plugin_api.RespondBody(request_id="rid-hidden", choice="once", session_key=hidden))
        assert getattr(excinfo.value, "status_code", None) == 404
        with approval._lock:
            assert len(approval._gateway_queues.get(hidden, [])) == 1  # untouched

    def test_dismiss_on_unlisted_session_is_404_no_side_effect(self, server, db):
        """Dismiss refuses a session with no durable row, and the expired record
        is untouched (no side effect)."""
        key = _new_key("ghost")
        db2 = server._get_db()
        plugin_api.record_expired_request(db2, key, {"request_id": "g-1", "command": "c"}, "timeout")
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_dismiss(plugin_api.DismissBody(request_id="g-1", session_key=key))
        assert getattr(excinfo.value, "status_code", None) == 404
        assert "session not found" in str(excinfo.value.detail)
        assert [e["request_id"] for e in plugin_api.load_expired_requests(db2, key)] == ["g-1"]

    def test_dismiss_on_deny_listed_session_is_404_no_side_effect(self, server, db):
        hidden = _create_row(db, _new_key(), source="kanban")
        _record_expired(db, hidden)
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_dismiss(
                plugin_api.DismissBody(request_id="exp-1", session_key=hidden))
        assert getattr(excinfo.value, "status_code", None) == 404
        assert [e["request_id"] for e in plugin_api.load_expired_requests(db, hidden)] == ["exp-1"]

    def test_redo_on_unlisted_session_is_404_no_prompt(self, server, db, monkeypatch):
        """Redo refuses an unlisted session BEFORE any prompt.submit can fire."""
        key = _new_key("ghost")
        db2 = server._get_db()
        plugin_api.record_expired_request(db2, key, {"request_id": "g-1", "command": "c"}, "timeout")
        _open_session(server, key)  # live, so the old gate order would have reached submit
        submitted: list = []

        def _spy_submit(rid, params):
            submitted.append(params)
            return {"result": {"status": "queued"}}

        monkeypatch.setitem(server._methods, "prompt.submit", _spy_submit)
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_redo(plugin_api.RedoBody(request_id="g-1", session_key=key))
        assert getattr(excinfo.value, "status_code", None) == 404
        assert submitted == []  # no prompt was ever raised

    def test_redo_on_deny_listed_session_is_404(self, server, db):
        hidden = _create_row(db, _new_key(), source="kanban")
        _record_expired(db, hidden)
        _open_session(server, hidden)
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_redo(plugin_api.RedoBody(request_id="exp-1", session_key=hidden))
        assert getattr(excinfo.value, "status_code", None) == 404
        assert "session not found" in str(excinfo.value.detail)

    def test_control_live_path_on_unlisted_session_never_dispatches(self, server, db, monkeypatch):
        """A live runtime for a session with NO durable row must not be driven:
        neither the live session.control path nor the direct-state path runs."""
        ghost_key = _new_key("ghost")
        sid = _open_session(server, ghost_key)
        from hermes_cli.goals import save_goal, GoalState

        save_goal(ghost_key, GoalState(goal="sneaky", status="active", turns_used=1,
                                       max_turns=6, created_at=100.0, last_turn_at=200.0))
        calls: list = []

        def spy(rid, params):
            calls.append(dict(params))
            return {"result": {}}

        monkeypatch.setitem(server._methods, "session.control", spy)
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_control(
                plugin_api.ControlBody(action="goal.pause", session_key=ghost_key, live_session_id=sid))
        assert getattr(excinfo.value, "status_code", None) == 404
        assert calls == []  # the live runtime was never dispatched

    def test_control_live_path_on_deny_listed_session_never_dispatches(self, server, db, monkeypatch):
        hidden = _create_row(db, _new_key(), source="kanban")
        sid = _open_session(server, hidden)
        _save_goal(hidden, status="active")
        calls: list = []

        def spy(rid, params):
            calls.append(dict(params))
            return {"result": {}}

        monkeypatch.setitem(server._methods, "session.control", spy)
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_control(
                plugin_api.ControlBody(action="goal.pause", session_key=hidden, live_session_id=sid))
        assert getattr(excinfo.value, "status_code", None) == 404
        assert calls == []

    def test_answer_bound_to_compute_host_relay_owned_by_session(self, server, db, monkeypatch):
        """The positive remote path: a clarify request mirrored onto THIS session's
        live runtime (compute-host ownership) is answerable from the panel; the
        answer is relayed to the child that owns the request."""
        key = _create_row(db, _new_key())
        sid = _open_session(server, key)
        session = server._sessions[sid]
        session["_compute_host_active"] = True
        session["_compute_host_open_request"] = {
            "id": "srq-host-1", "method": "clarify", "params": {"session_id": sid, "question": "host q"}}
        relays: list = []

        def fake_relay(frame):
            relays.append(dict(frame))
            # Consume the mirror exactly as the real bridge does on a relay.
            with session["history_lock"]:
                session.pop("_compute_host_open_request", None)
            return True

        monkeypatch.setattr(plugin_api._server(), "_relay_compute_host_response", fake_relay)
        result = plugin_api.action_center_answer(
            plugin_api.AnswerBody(request_id="srq-host-1", answer="host-answer", session_key=key))
        assert result == {"status": "ok"}
        assert relays == [{"jsonrpc": "2.0", "id": "srq-host-1", "result": {"answer": "host-answer"}}]
        # ...and the mirror is consumed exactly as the real bridge does on relay.
        assert "_compute_host_open_request" not in session or session.get("_compute_host_open_request") is None

    def test_compute_host_relay_refused_for_a_foreign_session(self, server, db):
        """A mirrored host request owned by ANOTHER session is not relayable through
        THIS session's answer: ownership is per-session, fail closed."""
        key_owner = _create_row(db, _new_key())
        sid_owner = _open_session(server, key_owner)
        server._sessions[sid_owner]["_compute_host_active"] = True
        server._sessions[sid_owner]["_compute_host_open_request"] = {
            "id": "srq-host-foreign", "method": "clarify", "params": {"session_id": sid_owner}}
        key_attacker = _create_row(db, _new_key())
        sid_attacker = _open_session(server, key_attacker)
        server._sessions[sid_attacker]["_compute_host_active"] = True
        result = plugin_api.action_center_answer(
            plugin_api.AnswerBody(request_id="srq-host-foreign", answer="HIJACKED",
                                  session_key=key_attacker))
        assert result == {"status": "expired"}
        # The owner's mirror is untouched.
        with server._sessions[sid_owner]["history_lock"]:
            assert server._sessions[sid_owner]["_compute_host_open_request"]["id"] == "srq-host-foreign"


# ── /control: automation pause/resume ─────────────────────────────────────────
class TestControl:
    def test_invalid_actions_are_400(self, server, db):
        key = _create_row(db, _new_key())
        for action in ("", "not.allowed", "goal.gate.add", "goal.clear", "loop.stop"):
            with pytest.raises(Exception) as excinfo:  # noqa: PT011
                plugin_api.action_center_control(
                    plugin_api.ControlBody(action=action, session_key=key))
            assert getattr(excinfo.value, "status_code", None) == 400, action

    def test_missing_session_key_is_400(self, server, db):
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_control(plugin_api.ControlBody(action="goal.pause", session_key=""))
        assert getattr(excinfo.value, "status_code", None) == 400

    def test_stored_session_goal_pause_resume_without_live_runtime(self, server, db, monkeypatch):
        from hermes_cli.goals import load_goal

        key = _create_row(db, f"stored-{uuid.uuid4().hex[:12]}")
        _save_goal(key, status="active", turns_used=4)

        def forbidden(_rid, _params):
            raise AssertionError("direct state path must not call command.dispatch")

        monkeypatch.setitem(server._methods, "command.dispatch", forbidden)

        paused = plugin_api.action_center_control(
            plugin_api.ControlBody(action="goal.pause", session_key=key))
        assert paused["control"]["goal"]["status"] == "paused"
        assert paused["dispatch"]["output"] == "⏸ Goal paused: Finish the inbox slice"
        assert load_goal(key).status == "paused"

        resumed = plugin_api.action_center_control(
            plugin_api.ControlBody(action="goal.resume", session_key=key))
        assert resumed["control"]["goal"]["status"] == "active"
        assert resumed["dispatch"]["output"].startswith("▶ Goal resumed:")
        assert load_goal(key).status == "active"

    def test_stored_loop_and_heartbeat_pause_resume(self, server, db):
        key = _create_row(db, f"stored-{uuid.uuid4().hex[:12]}")
        _save_loop(key)
        _save_heartbeat(key)

        paused = plugin_api.action_center_control(
            plugin_api.ControlBody(action="loop.pause", session_key=key))
        assert paused["control"]["loop"]["status"] == "paused"
        assert paused["dispatch"]["output"] == "⏸ Loop paused: Check the deployment"

        resumed = plugin_api.action_center_control(
            plugin_api.ControlBody(action="loop.resume", session_key=key))
        assert resumed["control"]["loop"]["status"] == "active"

        beat = plugin_api.action_center_control(
            plugin_api.ControlBody(action="heartbeat.pause", session_key=key))
        assert beat["control"]["heartbeat"]["status"] == "paused"
        assert beat["dispatch"]["output"] == "⏸ Heartbeat paused: Check the deployment"

        beat_resumed = plugin_api.action_center_control(
            plugin_api.ControlBody(action="heartbeat.resume", session_key=key))
        assert beat_resumed["control"]["heartbeat"]["status"] == "active"
        assert beat_resumed["dispatch"]["output"] == "▶ Heartbeat resumed (every 10m): Check the deployment"

    def test_stored_path_refuses_status_mismatches_with_core_messages(self, server, db):
        key = _create_row(db, f"stored-{uuid.uuid4().hex[:12]}")
        _save_goal(key, status="done")

        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_control(plugin_api.ControlBody(action="goal.pause", session_key=key))
        assert getattr(excinfo.value, "status_code", None) == 400
        assert "done" in str(excinfo.value.detail)

        _save_goal(key, status="active")
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_control(plugin_api.ControlBody(action="goal.resume", session_key=key))
        assert "not paused" in str(excinfo.value.detail)

        _save_loop(key, status="stopped")
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_control(plugin_api.ControlBody(action="loop.pause", session_key=key))
        assert "stopped" in str(excinfo.value.detail)

        _save_loop(key, status="active")
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_control(plugin_api.ControlBody(action="loop.resume", session_key=key))
        assert "not paused" in str(excinfo.value.detail)

    def test_no_goal_set_refusal(self, server, db):
        key = _create_row(db, f"stored-{uuid.uuid4().hex[:12]}")
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_control(plugin_api.ControlBody(action="goal.pause", session_key=key))
        assert "No goal set for this session." in str(excinfo.value.detail)

    def test_unknown_session_row_is_404(self, server, db):
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_control(
                plugin_api.ControlBody(action="goal.pause", session_key="never-existed"))
        assert getattr(excinfo.value, "status_code", None) == 404
        assert "session not found" in str(excinfo.value.detail)

    def test_deny_listed_session_row_is_404(self, server, db):
        hidden = _create_row(db, f"stored-{uuid.uuid4().hex[:12]}", source="kanban")
        _save_goal(hidden)
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_control(plugin_api.ControlBody(action="goal.pause", session_key=hidden))
        assert getattr(excinfo.value, "status_code", None) == 404

    def test_stale_runtime_id_refreshes_the_live_runtime(self, server, db, monkeypatch):
        sid, key = _open_session(server, _new_key()), None
        key = server._sessions[sid]["session_key"]
        _create_row(db, key)
        _save_goal(key, status="active")
        emitted = []
        monkeypatch.setattr(server, "_emit",
                            lambda event, s, payload=None: emitted.append((event, s, payload)))
        result = plugin_api.action_center_control(
            plugin_api.ControlBody(action="goal.pause", session_key=key, live_session_id="stale-runtime-id"))
        assert result["control"]["goal"]["status"] == "paused"
        assert [(event, s) for event, s, _ in emitted] == [("session.control.update", sid)]
        assert emitted[0][2]["control"]["goal"]["status"] == "paused"

    def test_live_session_routes_through_gateway_session_control(self, server, db, monkeypatch):
        sid = _open_session(server, _new_key())
        key = server._sessions[sid]["session_key"]
        _create_row(db, key)
        _save_goal(key, status="active")
        calls = []
        real = server._methods["session.control"]

        def observe(rid, params):
            calls.append(dict(params))
            return real(rid, params)

        monkeypatch.setitem(server._methods, "session.control", observe)
        result = plugin_api.action_center_control(
            plugin_api.ControlBody(action="goal.pause", session_key=key, live_session_id=sid))
        assert result["control"]["goal"]["status"] == "paused"
        assert calls and calls[0]["session_id"] == sid and calls[0]["action"] == "goal.pause"

    def test_live_session_no_goal_reports_the_command_output(self, server, db):
        """The live path delegates to the composer's /goal command: a missing goal is a
        command result ("No goal set."), not an error — the same words the chat shows."""
        sid = _open_session(server, _new_key())
        key = server._sessions[sid]["session_key"]
        _create_row(db, key)
        result = plugin_api.action_center_control(
            plugin_api.ControlBody(action="goal.pause", session_key=key, live_session_id=sid))
        assert result["dispatch"]["output"] == "No goal set."

    def test_live_id_of_another_session_never_drives_that_session(self, server, db, monkeypatch):
        """A live_session_id pointing at a DIFFERENT session must not be dispatched:
        the live path only runs when the id provably binds to session_key + profile."""
        sid_other = _open_session(server, _new_key())
        key_other = server._sessions[sid_other]["session_key"]
        _create_row(db, key_other)
        _save_goal(key_other)  # a live goal the buggy path would have paused
        key = _create_row(db, _new_key())
        _save_goal(key, status="active")
        calls: list[dict] = []

        def spy(rid, params):
            calls.append(dict(params))
            raise AssertionError("foreign live id must not reach session.control")

        monkeypatch.setitem(server._methods, "session.control", spy)
        result = plugin_api.action_center_control(
            plugin_api.ControlBody(action="goal.pause", session_key=key, live_session_id=sid_other))
        assert calls == []
        assert result["control"]["goal"]["status"] == "paused"  # gated direct path ran for OUR key
        from hermes_cli.goals import load_goal
        assert load_goal(key).status == "paused"
        assert load_goal(key_other).status == "active"  # the other session's goal was never touched

    def test_live_id_from_a_foreign_profile_is_not_dispatched(self, server, db, monkeypatch):
        """A runtime record owned by another profile home must not take the live path."""
        key = _create_row(db, _new_key())
        sid = _open_session(server, key, profile_home="/completely/different/home")
        _save_goal(key, status="active")
        calls: list[dict] = []

        def spy(rid, params):
            calls.append(dict(params))
            raise AssertionError("foreign-profile live id must not reach session.control")

        monkeypatch.setitem(server._methods, "session.control", spy)
        result = plugin_api.action_center_control(
            plugin_api.ControlBody(action="goal.pause", session_key=key, live_session_id=sid))
        assert calls == []
        assert result["control"]["goal"]["status"] == "paused"
        # ...and the refresh emit never targets the foreign runtime either.
        emitted: list[tuple] = []
        monkeypatch.setattr(server, "_emit",
                            lambda event, s, payload=None: emitted.append((event, s, payload)))
        plugin_api.action_center_control(
            plugin_api.ControlBody(action="goal.resume", session_key=key, live_session_id=sid))
        assert emitted == []

    def test_finalized_runtime_id_falls_back_to_the_direct_path(self, server, db, monkeypatch):
        """A reaped/finalized runtime id is stale: the gated direct-state path applies."""
        key = _create_row(db, _new_key())
        sid = _open_session(server, key)
        server._sessions[sid]["_finalized"] = True
        _save_goal(key, status="active")
        calls: list[dict] = []

        def spy(rid, params):
            calls.append(dict(params))
            raise AssertionError("finalized live id must not reach session.control")

        monkeypatch.setitem(server._methods, "session.control", spy)
        result = plugin_api.action_center_control(
            plugin_api.ControlBody(action="goal.pause", session_key=key, live_session_id=sid))
        assert calls == []
        assert result["control"]["goal"]["status"] == "paused"

    def test_live_id_still_takes_the_live_path_when_bound(self, server, db, monkeypatch):
        """The gate is identity-only: a correctly bound id still reaches session.control."""
        sid = _open_session(server, _new_key())
        key = server._sessions[sid]["session_key"]
        _create_row(db, key)
        _save_goal(key, status="active")
        calls: list[dict] = []
        real = server._methods["session.control"]

        def observe(rid, params):
            calls.append(dict(params))
            return real(rid, params)

        monkeypatch.setitem(server._methods, "session.control", observe)
        result = plugin_api.action_center_control(
            plugin_api.ControlBody(action="goal.pause", session_key=key, live_session_id=sid))
        assert result["control"]["goal"]["status"] == "paused"
        assert calls and calls[0]["session_id"] == sid and calls[0]["action"] == "goal.pause"


# ── FastAPI REST contract ─────────────────────────────────────────────────────
class TestRestContract:
    """The mounted router over the real HTTP stack: shapes and error mapping."""

    @pytest.fixture()
    def client(self, server, db):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(plugin_api.router, prefix="/api/plugins/action-center")
        return TestClient(app, raise_server_exceptions=False)

    def test_summary_shape_over_http(self, client, db):
        key = _create_row(db, _new_key())
        _save_goal(key)
        response = client.get("/api/plugins/action-center/summary")
        assert response.status_code == 200
        body = response.json()
        assert body["badge"] == "none"
        assert body["items"][0]["session_key"] == key
        assert {"session_key", "profile", "title", "source", "cwd", "lanes", "categories",
                "updated_at", "needs_you_count", "pending_count", "expired_request_count",
                "goal", "loop", "heartbeat"} <= set(body["items"][0])

    def test_error_mapping(self, client, db):
        # 4002 → 400
        assert client.get("/api/plugins/action-center/details", params={"session_key": ""}).status_code == 400
        # 4001 → 404 with the core's message in detail
        response = client.get("/api/plugins/action-center/details", params={"session_key": "nope"})
        assert response.status_code == 404
        assert response.json()["detail"] == "Session not found"
        # Unknown profile → 404 (never a silent fallback)
        response = client.get("/api/plugins/action-center/summary", params={"profile": "no-such-profile"})
        assert response.status_code == 404
        assert "does not exist" in response.json()["detail"]

    def test_control_action_validation_over_http(self, client, db):
        key = _create_row(db, _new_key())
        response = client.post("/api/plugins/action-center/control",
                               json={"action": "goal.gate.add", "session_key": key})
        assert response.status_code == 400
        assert "gate actions" in response.json()["detail"]
        response = client.post("/api/plugins/action-center/control",
                               json={"action": "goal.pause", "session_key": "never-existed"})
        assert response.status_code == 404

    def test_respond_and_answer_over_http(self, client, db):
        import tui_gateway.server as mod

        key = _create_row(db, _new_key())
        response = client.post("/api/plugins/action-center/respond",
                               json={"request_id": "rid-x", "choice": "once", "session_key": key})
        assert response.status_code == 404  # not live

        _open_session(mod, key)
        rid = _queue_approval(mod, key)
        response = client.post("/api/plugins/action-center/respond",
                               json={"request_id": rid, "choice": "once", "session_key": key})
        assert response.status_code == 200
        assert response.json() == {"resolved": 1}

        _sid, clarify_id = _queue_clarify(mod, key, question="Which?")
        response = client.post("/api/plugins/action-center/answer",
                               json={"request_id": clarify_id, "answer": "docker", "session_key": key})
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_dismiss_over_http(self, client, db):
        key = _create_row(db, _new_key())
        _record_expired(db, key, request_id="exp-http")
        response = client.post("/api/plugins/action-center/dismiss",
                               json={"request_id": "exp-http", "session_key": key})
        assert response.status_code == 200
        assert response.json() == {"dismissed": True}
        # Second dismiss: the record is gone → 404.
        response = client.post("/api/plugins/action-center/dismiss",
                               json={"request_id": "exp-http", "session_key": key})
        assert response.status_code == 404

    def test_dismiss_on_a_named_profile_clears_that_store(self, client, server, db, tmp_path, monkeypatch):
        """Dismiss on a named profile deletes from THAT profile's store — the route
        writes through the core's cross-profile writer seam (read-only handles cannot)."""
        profile_name = f"prof{uuid.uuid4().hex[:8]}"
        profile_home = tmp_path / "profiles" / profile_name
        profile_home.mkdir(parents=True)
        monkeypatch.setattr(
            "hermes_cli.profiles._get_profiles_root", lambda: tmp_path / "profiles")
        key = _new_key("prof")
        with server._profile_db({"profile": profile_name}, writer=True) as pdb:
            assert plugin_api.record_expired_request(pdb, key,
                                                     {"request_id": "exp-prof", "command": "cmd-x"},
                                                     "timeout")
            pdb.create_session(key, source="cli")  # the human-facing row the gate requires
        response = client.post("/api/plugins/action-center/dismiss",
                               json={"request_id": "exp-prof", "session_key": key, "profile": profile_name})
        assert response.status_code == 200
        assert response.json() == {"dismissed": True}
        with server._profile_db({"profile": profile_name}, writer=True) as pdb:
            assert plugin_api.load_expired_requests(pdb, key) == []

    def test_respond_choice_gate_over_http(self, client, db):
        """The offered-choices gate holds over the REST surface: 400 with the core-style
        detail, and the queue entry survives untouched."""
        from tools import approval

        key = _create_row(db, _new_key())
        _open_session(_server_module(), key)
        rid = _queue_approval(_server_module(), key, allow_permanent=False)
        response = client.post("/api/plugins/action-center/respond",
                               json={"request_id": rid, "choice": "always", "session_key": key})
        assert response.status_code == 400
        assert "always" in response.json()["detail"]
        with approval._lock:
            assert len(approval._gateway_queues.get(key, [])) == 1


def test_answer_ownership_read_failure_is_not_expiry(server, db, monkeypatch):
    from fastapi import HTTPException
    key = _create_row(db, _new_key())
    _open_session(server, key)
    def broken(_sid):
        raise RuntimeError("secret-canary-must-not-leak")
    monkeypatch.setattr(server, "_open_requests", broken)
    with pytest.raises(HTTPException) as caught:
        plugin_api.action_center_answer(plugin_api.AnswerBody(
            request_id="unknown", session_key=key, answer="test"))
    assert caught.value.status_code == 503
    assert caught.value.detail == "request ownership read failed: RuntimeError"


def test_answer_refuses_non_clarify_request_in_owned_session(server, db, monkeypatch):
    key = _create_row(db, _new_key())
    _open_session(server, key)
    monkeypatch.setattr(server, "_open_requests", lambda sid: [
        {"id": "not-a-question", "method": "secret", "params": {}}])
    from tui_gateway import server_requests
    resolve = MagicMock()
    relay = MagicMock()
    monkeypatch.setattr(server_requests, "resolve_response", resolve)
    monkeypatch.setattr(server, "_relay_compute_host_response", relay)
    result = plugin_api.action_center_answer(plugin_api.AnswerBody(
        request_id="not-a-question", session_key=key, answer="test"))
    assert result == {"status": "expired"}
    resolve.assert_not_called()
    relay.assert_not_called()


def _server_module():
    import tui_gateway.server as mod

    return mod


# ── observed requests: the standalone producer (TDD RED → GREEN) ──────────────
class TestObservedRequestsProducer:
    """The plugin's own producer for requests that ended unanswered.

    The core's expired-request WRITER is the gateway's approval-settle hook
    (``server._emit_approval_request``), which a plugin must not patch — and
    ``register_gateway_settle`` REPLACES the single settle callback, so touching
    it would steal the TUI's request withdrawal. The supported production seam is
    the observer-only plugin hook ``post_approval_response`` (a ``VALID_HOOK`` in
    ``hermes_cli.plugins``), fired by ``tools.approval_gateway_wait`` on EVERY
    settlement path with an authoritative outcome: an answered choice, or the
    fail-closed non-answers ``timeout`` / ``notify_failed`` / ``cancelled`` —
    never an inference from a request disappearing from the queue.
    """

    def _hook_kwargs(self, *, choice="timeout", session_key=None, command="rm -rf /tmp/probe"):
        return {
            "command": command,
            "description": "run removal",
            "pattern_key": "rm:-rf",
            "pattern_keys": ["rm:-rf"],
            "session_key": session_key if session_key is not None else _new_key(),
            "surface": "gateway",
            "choice": choice,
        }

    _fired: list = []

    def test_timeout_settlement_is_recorded(self, server, db, monkeypatch):
        key = _create_row(db, _new_key())
        session = self._hook_kwargs(session_key=key)
        self._register_hook(monkeypatch, session, server)
        try:
            entry = self._settle_via_queue(server, db, key)
            records = plugin_api.load_expired_requests(db, key)
            assert [r["request_id"] for r in records] == [entry.data["request_id"]]
            record = records[0]
            assert record["outcome"] == "timeout"
            assert record["command"] == entry.data["command"]
            assert record["description"] == entry.data["description"]
            assert record["pattern_keys"] == [str(k) for k in (entry.data.get("pattern_keys") or ["rm:-rf"])]
        finally:
            plugin_api.unregister_observed_request_producer()

    def test_notify_failed_settlement_is_recorded(self, server, db, monkeypatch):
        key = _create_row(db, _new_key())
        self._register_hook(monkeypatch, self._hook_kwargs(choice="notify_failed", session_key=key), server)
        try:
            from tools import approval as _approval

            entry = self._settle_via_queue_directly(db, key, _approval=_approval)
            plugin_api._observe_pre_approval_request(
                choice="pre", session_key=key, command=entry.data["command"],
                request_id=entry.data["request_id"], surface="gateway")
            plugin_api._on_post_approval_response(self._hook_kwargs(
                choice="notify_failed", session_key=key, command=entry.data["command"]))
            records = plugin_api.load_expired_requests(db, key)
            assert [r["request_id"] for r in records] == [entry.data["request_id"]]
            assert records[0]["outcome"] == "notify_failed"
        finally:
            plugin_api.unregister_observed_request_producer()

    def test_session_closed_settlement_is_recorded(self, server, db, monkeypatch):
        key = _create_row(db, _new_key())
        self._register_hook(monkeypatch, self._hook_kwargs(choice="cancelled", session_key=key), server)
        try:
            entry = self._settle_via_queue_directly(db, key)
            plugin_api._observe_pre_approval_request(
                choice="pre", session_key=key, command=entry.data["command"],
                request_id=entry.data["request_id"], surface="gateway")
            plugin_api._on_post_approval_response(self._hook_kwargs(
                choice="cancelled", session_key=key, command=entry.data["command"]))
            records = plugin_api.load_expired_requests(db, key)
            assert [r["request_id"] for r in records] == [entry.data["request_id"]]
            assert records[0]["outcome"] == "session_closed"
        finally:
            plugin_api.unregister_observed_request_producer()

    def test_answered_choices_are_not_recorded(self, server, db, monkeypatch):
        for choice in ("once", "session", "always", "deny"):
            key = _create_row(db, _new_key())
            self._register_hook(monkeypatch, self._hook_kwargs(choice=choice, session_key=key), server)
            try:
                plugin_api._on_post_approval_response(self._hook_kwargs(choice=choice, session_key=key))
                assert plugin_api.load_expired_requests(db, key) == []
            finally:
                plugin_api.unregister_observed_request_producer()

    def test_unregistered_producer_records_nothing(self, server, db, monkeypatch):
        key = _create_row(db, _new_key())
        # The producer is never registered here: the callback alone must not record.
        plugin_api.unregister_observed_request_producer()
        plugin_api._on_post_approval_response(self._hook_kwargs(session_key=key))
        assert plugin_api.load_expired_requests(db, key) == []
        assert plugin_api.load_expired_request_counts(db) == ({}, None)

    def test_unknown_session_is_never_recorded(self, server, db, monkeypatch):
        self._register_hook(monkeypatch, self._hook_kwargs(session_key=_new_key()), server)
        try:
            plugin_api._on_post_approval_response(self._hook_kwargs(session_key=_new_key("ghost")))
            assert plugin_api.load_expired_request_counts(db) == ({}, None)
        finally:
            plugin_api.unregister_observed_request_producer()

    def test_allowlisted_session_scope_enforced(self, server, db, monkeypatch):
        """A session key belonging to a deny-listed source must never be recorded."""
        key = _create_row(db, _new_key(), source="kanban")
        self._register_hook(monkeypatch, self._hook_kwargs(session_key=key), server)
        try:
            plugin_api._on_post_approval_response(self._hook_kwargs(session_key=key))
            assert plugin_api.load_expired_request_counts(db) == ({}, None)
        finally:
            plugin_api.unregister_observed_request_producer()

    def test_cancelled_cause_records_session_closed_even_as_deny(self, server, db, monkeypatch):
        """The core fires ``choice='deny'`` WITH ``cancelled=<cause>`` when the
        wait was interrupted: a withdrawal nobody made — recorded as
        session_closed, never mislabeled as a user deny."""
        key = _create_row(db, _new_key())
        self._register_hook(monkeypatch, self._hook_kwargs(session_key=key), server)
        try:
            plugin_api._on_post_approval_response(self._hook_kwargs(
                choice="deny", session_key=key))
            assert plugin_api.load_expired_requests(db, key) == []  # no cause: a real deny
            kw = self._hook_kwargs(choice="deny", session_key=key)
            kw["cancelled"] = "turn interrupted"
            plugin_api._on_post_approval_response(kw)
            records = plugin_api.load_expired_requests(db, key)
            assert len(records) == 1
            assert records[0]["outcome"] == "session_closed"
        finally:
            plugin_api.unregister_observed_request_producer()

    def test_payload_is_redacted_before_persistence(self, server, db, monkeypatch):
        """A credential-shaped command never reaches the store, whichever gate
        fired the hook (the CLI gate passes unredacted copies)."""
        key = _create_row(db, _new_key())
        self._register_hook(monkeypatch, self._hook_kwargs(session_key=key), server)
        try:
            plugin_api._on_post_approval_response(self._hook_kwargs(
                choice="timeout", session_key=key,
                command="echo api_key=sk-supersecret-token-value run"))
            record = plugin_api.load_expired_requests(db, key)[0]
            assert "sk-supersecret-token-value" not in json.dumps(record)
        finally:
            plugin_api.unregister_observed_request_producer()

    def test_allowlist_gate_blocks_when_probe_fails(self, server, db, monkeypatch):
        """Fail closed: when the durable-row probe itself raises, nothing is recorded."""
        key = _create_row(db, _new_key())
        self._register_hook(monkeypatch, self._hook_kwargs(session_key=key), server)
        try:
            def boom(_params):
                raise RuntimeError("canary-secret-probe")
            monkeypatch.setattr(server, "_profile_db", boom)
            plugin_api._on_post_approval_response(self._hook_kwargs(session_key=key))
            assert plugin_api.load_expired_request_counts(db) == ({}, None)
        finally:
            plugin_api.unregister_observed_request_producer()

    def test_bounded_redacted_payload(self, server, db, monkeypatch):
        """Persisted fields are hard-bounded and carry the redacted display copy."""
        key = _create_row(db, _new_key())
        self._register_hook(monkeypatch, self._hook_kwargs(session_key=key), server)
        try:
            huge_command = "rm " + "x" * 4000
            plugin_api._on_post_approval_response(self._hook_kwargs(
                choice="timeout", session_key=key, command=huge_command))
            record = plugin_api.load_expired_requests(db, key)[0]
            assert len(record["command"]) <= 500
            assert len(record["description"]) <= 300
            assert record["command"].startswith("rm ")
            assert json.dumps(record)  # JSON-serializable durable row
        finally:
            plugin_api.unregister_observed_request_producer()

    def test_registration_is_idempotent_and_unregisters_cleanly(self, server, db, monkeypatch):
        key = _create_row(db, _new_key())
        self._register_hook(monkeypatch, self._hook_kwargs(session_key=key), server)
        assert plugin_api.register_observed_request_producer(server) is False  # no double hook
        try:
            entry = self._settle_via_queue_directly(db, key)
            plugin_api._observe_pre_approval_request(
                choice="pre", session_key=key, command=entry.data["command"],
                request_id=entry.data["request_id"], surface="gateway")
            plugin_api._on_post_approval_response(self._hook_kwargs(
                choice="timeout", session_key=key, command=entry.data["command"]))
            records = plugin_api.load_expired_requests(db, key)
            assert len(records) == 1  # one producer, one record — no duplicates
        finally:
            plugin_api.unregister_observed_request_producer()

    def test_no_ownership_claim_without_recorded_settlement(self, server, db, monkeypatch):
        """An approval that is merely queued (not settled) leaves no record: the
        producer never infers expiry from a request that has not ended."""
        key = _create_row(db, _new_key())
        self._register_hook(monkeypatch, self._hook_kwargs(session_key=key), server)
        try:
            self._settle_via_queue_directly(db, key, settle=False)
            assert plugin_api.load_expired_requests(db, key) == []
        finally:
            plugin_api.unregister_observed_request_producer()

    def test_read_route_registers_producer_and_survives_failure(self, server, db, monkeypatch):
        """The first read lazily registers the producer (import time is too
        early: plugin discovery has not run) and a failing registration never
        fails the read."""
        key = _create_row(db, _new_key())
        plugin_api.unregister_observed_request_producer()
        calls = []

        def real_register(srv=None):
            calls.append(srv)
            return plugin_api.register_observed_request_producer.__wrapped__(srv) if False else True

        # Simulate the LIVE production behavior without the real plugin system:
        # patch the register seam to a no-op True so the read proceeds.
        monkeypatch.setattr(plugin_api, "register_observed_request_producer",
                            lambda srv=None: calls.append(srv) or True)
        out = plugin_api.action_center_summary()
        assert out["badge"] in ("none", "amber", "red")
        assert len(calls) == 1  # exactly one lazy attempt per read
        plugin_api.unregister_observed_request_producer()

        # And when registration raises, the read still completes.
        def boom(srv=None):
            raise RuntimeError("canary-register")
        monkeypatch.setattr(plugin_api, "register_observed_request_producer", boom)
        out2 = plugin_api.action_center_summary()
        assert out2["badge"] in ("none", "amber", "red")

    # -- helpers -------------------------------------------------------------

    def _settle_via_queue(self, server, db, key):
        """Queue ONE entry, fire the PRE hook (captures request_id), then the
        POST hook with the authoritative settlement — the exact sequence
        ``tools.approval_gateway_wait`` produces for a timeout."""
        rid = _queue_approval(server, key)
        with approval._lock:
            entry = approval._gateway_queues[key][0]
        plugin_api._observe_pre_approval_request(
            choice="pre", session_key=key,
            command=entry.data["command"],
            description=entry.data["description"],
            pattern_keys=[str(k) for k in (entry.data.get("pattern_keys") or [])],
            request_id=entry.data["request_id"], surface="gateway")
        with approval._lock:
            approval._gateway_queues[key].remove(entry)
            if not approval._gateway_queues.get(key):
                approval._gateway_queues.pop(key, None)
        self._fired.append({
            "choice": "timeout",
            "session_key": key,
            "command": entry.data["command"],
            "description": entry.data["description"],
            "pattern_keys": [str(k) for k in (entry.data.get("pattern_keys") or ["rm:-rf"])],
        })
        plugin_api._on_post_approval_response(self._fired[-1])
        return entry

    def _settle_via_queue_directly(self, db, key, *, settle=True, _approval=None):
        """Queue one entry WITHOUT arming a settle hook (the producer path); the
        test fires the hook callback itself with the authoritative outcome."""
        _approval = _approval or approval
        rid = _queue_approval(_server_module(), key)
        with _approval._lock:
            entry = _approval._gateway_queues[key][0]
            _approval._gateway_queues[key].remove(entry)
            if not _approval._gateway_queues.get(key):
                _approval._gateway_queues.pop(key, None)
        return entry

    def _register_hook(self, monkeypatch, kwargs, server, *, ensure_unregistered_first=True):
        """Register the producer with its OWN honest identity (source='user' —
        what the dashboard host mounts a user-installed plugin as) and wire
        lifecycle.invoke_hook → the plugin's own callback (the SUPPORTED hook
        seam the core documents for plugins)."""
        from hermes_cli.plugins_manifest import PluginManifest

        if ensure_unregistered_first:
            plugin_api.unregister_observed_request_producer()
        registered = plugin_api.register_observed_request_producer(server)
        assert registered is True
        from hermes_cli import lifecycle

        monkeypatch.setattr(lifecycle, "_observe", lambda *a, **k: None)

        def fake_invoke(hook_name, **kw):
            if hook_name == "post_approval_response":
                plugin_api._on_post_approval_response(kw)
            elif hook_name == "pre_approval_request":
                plugin_api._observe_pre_approval_request(**kw)
            return []

        monkeypatch.setattr(lifecycle, "_plugin_hooks", fake_invoke)


class TestProducerReleaseGates:
    def test_identity_must_match_loaded_module_directory(self, server, monkeypatch):
        monkeypatch.setattr(plugin_api, '_dashboard_discovery', lambda: ([{
            'name': 'action-center', 'source': 'bundled', '_dir': '/unrelated/dashboard',
        }], None))
        assert plugin_api.register_observed_request_producer(server) is False
        assert plugin_api._observed_request_producer is None

    def test_answered_settlement_consumes_correlation(self, server):
        assert plugin_api.register_observed_request_producer(server)
        plugin_api._observe_pre_approval_request(session_key='test-key', request_id='real-rid', command='echo test')
        assert plugin_api._observed_pending_ids
        plugin_api._on_post_approval_response(session_key='test-key', choice='once', command='echo test')
        assert not plugin_api._observed_pending_ids

    def test_partial_registration_disposes_first_hook(self, server, monkeypatch):
        from hermes_cli.plugins import PluginContext, get_plugin_manager
        original = PluginContext.register_hook
        def fail_second(ctx, name, callback):
            if name == 'pre_approval_request':
                raise RuntimeError('synthetic registration failure')
            return original(ctx, name, callback)
        monkeypatch.setattr(PluginContext, 'register_hook', fail_second)
        assert not plugin_api.register_observed_request_producer(server)
        assert plugin_api._on_post_approval_response not in get_plugin_manager()._hooks.get('post_approval_response', [])

    def test_disable_stops_capture_and_read_disposes_hooks(self, server, db, hermes_home):
        key = _create_row(db, _new_key())
        assert plugin_api.register_observed_request_producer(server)
        (hermes_home / 'config.yaml').write_text('plugins:\n  disabled: [action-center]\n', encoding='utf-8')
        plugin_api._on_post_approval_response(session_key=key, choice='timeout', command='echo test')
        assert not plugin_api.load_expired_requests(db, key)
        plugin_api._ensure_producer_for_reads()
        assert plugin_api._observed_request_producer is None

    def test_unavailable_producer_is_never_all_clear(self, server, db, monkeypatch):
        monkeypatch.setattr(plugin_api, '_dashboard_discovery', lambda: ([], None))
        out = plugin_api.action_center_summary()
        assert out['badge'] == 'red'
        assert out['coverage']['expired_requests']['observed'] is False
        assert any('observed requests unavailable' in e for e in out['coverage']['errors'])

    def test_stale_manager_registration_is_recovered(self, server, db):
        assert plugin_api.register_observed_request_producer(server)
        old = plugin_api._observed_request_producer
        old.post_handle.dispose()
        plugin_api._ensure_producer_for_reads()
        assert plugin_api._observed_request_producer is not old
        assert plugin_api._producer_coverage(None)['observed'] is True

    def test_concurrent_registration_has_one_owner(self, server, monkeypatch):
        from concurrent.futures import ThreadPoolExecutor
        from hermes_cli.plugins import PluginContext
        entered = threading.Event()
        release = threading.Event()
        original = PluginContext.register_hook
        calls = []
        def delayed(ctx, name, callback):
            calls.append(name)
            if name == 'post_approval_response':
                entered.set()
                assert release.wait(3)
            return original(ctx, name, callback)
        monkeypatch.setattr(PluginContext, 'register_hook', delayed)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(plugin_api.register_observed_request_producer, server)
            assert entered.wait(2)
            second = pool.submit(plugin_api.register_observed_request_producer, server)
            time.sleep(0.1)
            release.set()
            assert sorted([first.result(3), second.result(3)]) == [False, True]
        assert calls.count('post_approval_response') == 1

    def test_capture_write_failure_is_visible_in_coverage(self, server, db, monkeypatch):
        key = _create_row(db, _new_key())
        assert plugin_api.register_observed_request_producer(server)
        monkeypatch.setattr(plugin_api, 'record_expired_request', lambda *a: False)
        plugin_api._on_post_approval_response(session_key=key, choice='timeout', command='echo test')
        result = plugin_api.action_center_summary()
        assert result['badge'] == 'red'
        assert any('capture' in e for e in result['coverage']['errors'])
