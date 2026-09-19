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
    yield mod
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


# ── /answer: clarifications ────────────────────────────────────────────────────
class TestAnswer:
    def test_missing_request_id_is_400(self, server, db):
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            plugin_api.action_center_answer(plugin_api.AnswerBody(request_id="", answer="x"))
        assert getattr(excinfo.value, "status_code", None) == 400

    def test_single_answer_resolves_the_request(self, server, db):
        key = _create_row(db, _new_key())
        sid, req_id = _queue_clarify(server, key, question="Which backend?")
        result = plugin_api.action_center_answer(plugin_api.AnswerBody(request_id=req_id, answer="docker"))
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
            plugin_api.AnswerBody(request_id=req_id, answer=["auth", "cache"]))
        assert result["status"] == "ok"
        assert received == [{"answer": ["auth", "cache"]}]
        sr = server._server_requests
        with sr._lock:
            assert req_id not in sr._open

    def test_single_answer_expired_request_reports_expired(self, server, db):
        result = plugin_api.action_center_answer(
            plugin_api.AnswerBody(request_id="srq-not-open-anywhere", answer="x"))
        assert result["status"] == "expired"

    def test_batch_locks_one_call_per_question_last_lock_resolves(self, server, db):
        key = _create_row(db, _new_key())
        questions = [
            {"qid": "q1", "question": "Project name?", "choices": ["proj-a", "proj-b"]},
            {"qid": "q2", "question": "Language?", "choices": ["python", "rust"]},
        ]
        _sid, req_id = _queue_batch_clarify(server, key, questions=questions)
        first = plugin_api.action_center_answer(
            plugin_api.AnswerBody(request_id=req_id, answer="proj-a", question_id="q1"))
        assert first == {"status": "ok", "remaining": ["q2"]}
        last = plugin_api.action_center_answer(
            plugin_api.AnswerBody(request_id=req_id, answer="rust", question_id="q2"))
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
                plugin_api.AnswerBody(request_id=req_id, answer="x", question_id="nope"))
        assert getattr(excinfo.value, "status_code", None) == 400

    def test_batch_lock_expired_reports_expired(self, server, db):
        result = plugin_api.action_center_answer(
            plugin_api.AnswerBody(request_id="srq-gone", answer="x", question_id="q1"))
        assert result["status"] == "expired"

    def test_batch_non_string_answer_json_encoded(self, server, db):
        key = _create_row(db, _new_key())
        questions = [{"qid": "q1", "question": "Pick features", "choices": ["a", "b"]}]
        _sid, req_id = _queue_batch_clarify(server, key, questions=questions)
        result = plugin_api.action_center_answer(
            plugin_api.AnswerBody(request_id=req_id, answer=["a"], question_id="q1"))
        # Single-question batch: the last lock resolves the request.
        assert result == {"status": "ok", "remaining": []}
        # The queue's locked answer is the JSON-encoded array string (core semantics).
        details = plugin_api.action_center_details(session_key=key)["sessions"][0]
        assert details["clarifications"] == []  # resolved, so no longer open


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
        result = plugin_api.action_center_control(
            plugin_api.ControlBody(action="goal.pause", session_key=key, live_session_id=sid))
        assert result["dispatch"]["output"] == "No goal set."


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
                               json={"request_id": clarify_id, "answer": "docker"})
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


def _server_module():
    import tui_gateway.server as mod

    return mod