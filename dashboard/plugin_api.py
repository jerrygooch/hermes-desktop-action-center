"""Action Center — backend for the ``action-center`` desktop plugin.

Mounted by the gateway at ``/api/plugins/action-center/`` (FastAPI ``APIRouter``
named ``router``; ``dashboard/manifest.json`` points at this file).

This runs INSIDE the Hermes gateway process, so it reads the same runtime state
the core TUI gateway methods read: ``tui_gateway.server`` globals (``_sessions``
under ``_sessions_lock``, the shared SessionDB via ``_profile_db``/``_get_db``),
the live approval queue (``tools.approval``), the server→client request queue
(``tui_gateway.server_requests``) and the persisted automation managers
(``hermes_cli.goals`` / ``hermes_cli.loops`` / ``hermes_cli.heartbeat``).
Every hermes/tui_gateway import happens LAZILY inside the handlers — never at
module import — so plugin import order can never break the backend, and state is
always read through the server module object so the gateway's live globals (and
test patches of them) are honoured.

Ported near-verbatim from the closed core PR's ``tui_gateway/methods_inbox.py``,
``tui_gateway/methods_inbox_requests.py`` and the direct-state pause/resume path
of ``tui_gateway/methods_session_control.py``: the canonical
``INTERNAL_LISTING_SOURCES`` deny-list, ``_inbox_home_key`` profile normalization
(a launch-profile session stores ``profile_home=None`` and must still match),
metadata-only approval/clarify egress, bounded + redacted transcript excerpts,
the durable expired-request store (Redo/Dismiss), the session-row gate for
direct state control, tolerant coverage errors instead of 500s, and the exact
refusal messages.

Routes (all JSON):
  GET  /summary?profile=&limit=   cross-session aggregation (the core
                                  ``inbox.list`` result's ``inbox`` object)
  GET  /details?session_key=&profile=   scoped request details (core
                                  ``inbox.requests`` result)
  POST /respond {request_id, choice, session_key, profile}   approve/deny
  POST /answer  {request_id, answer, session_key[, question_id], profile}
                                  clarify answer (string or JSON array;
                                  batch = one call per question, proxied
                                  through ``clarify.lock``; the request id
                                  must be owned by the addressed session)
  POST /control {action, session_key[, live_session_id], profile}   pause/resume
                                  for goal|loop|heartbeat — live runtime when
                                  one is attached, persisted state otherwise
  POST /redo    {request_id, session_key, profile}   re-raise an expired request
  POST /dismiss {request_id, session_key, profile}   clear an expired record

Error mapping — the core's named JSON-RPC errors become HTTP statuses and the
detail string carries the core's exact message:
    4001 not found → 404 · 4002 missing/invalid param → 400 ·
    4004 invalid action/state → 400 · 4009 conflict (not live) → 409 ·
    4064 unknown profile → 404 · 5031/5036 storage/enumeration → 503.
``ProfileUnavailableError`` propagates as 404 — never a silent fallback to the
launch profile.

One deliberate boundary: the expired-request WRITER in the core PR is the
gateway's approval-settle hook (``server._emit_approval_request``), which a
plugin must not patch — the app stays unpatched. This module ports the whole
store (``record_expired_request`` / load / prune / clear) verbatim, so records
written by a gateway carrying the core change (or by any caller of the same
API) are listed, redone and dismissed here; nothing in this plugin mutates
gateway behavior at import time.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import json
import logging
import os
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

log = logging.getLogger(__name__)
router = APIRouter()

# rid used for the few gateway calls that still want a JSON-RPC id (they only
# echo it back into error envelopes, which we translate to HTTP errors).
_RID = "action-center"

_DEFAULT_LIMIT = 200
_MAX_LIMIT = 1000

# Only these two server→client request types are surfaced; password, secret,
# vault, sudo and other credential-shaped types are excluded (core allowlist).
_ALLOWED_REQUEST_METHODS = frozenset({"approval", "clarify"})

# Bounded transcript context for the request-detail view. The scan window is
# wider than what we return so tool-only and empty rows cannot starve the excerpt.
_CONTEXT_SCAN_LIMIT = 24
_CONTEXT_KEPT_MESSAGES = 4
_CONTEXT_MESSAGE_CHARS = 320
_CONTEXT_TOTAL_CHARS = 1200
_CONTEXT_ROLES = frozenset({"user", "assistant"})

# ── expired requests (durable) ────────────────────────────────────────────────
# A request that ends without an answer (timeout, withdrawal, session teardown)
# is recorded in the owning session's store (state_meta — survives the turn, the
# session close and an app restart), pruned to a bounded window, and rendered by
# the panel with a Redo.
_EXPIRED_PREFIX = "inbox.expired."
_EXPIRED_KEEP_PER_SESSION = 10
_EXPIRED_MAX_AGE_S = 7 * 24 * 3600

_REDO_PROMPT = (
    "[Action Center] An approval request expired before it was answered. "
    "The user asked to redo it — attempt this action again now so they can approve or deny it.\n\n"
    "Command: {command}"
)

# Pause/resume are pure persisted-state writes: the Action Center may run them
# for a stored session with no live runtime. Everything else still requires the
# live session (the panel only ever offers these six).
_DIRECT_STATE_ACTIONS = frozenset({
    "goal.pause",
    "goal.resume",
    "loop.pause",
    "loop.resume",
    "heartbeat.pause",
    "heartbeat.resume",
})

# The choices an approval card offers (once|session|always|deny as offered in
# the approval payload's own ``choices``).
_ALLOWED_CHOICES = frozenset({"once", "session", "always", "deny"})

# Core JSON-RPC error code → HTTP status (detail keeps the core's message).
_STATUS_BY_CODE = {
    4001: 404,
    4002: 400,
    4004: 400,
    4009: 409,
    4064: 404,
    5019: 502,
    5031: 503,
    5036: 503,
}


# ── small helpers ─────────────────────────────────────────────────────────────
def _quiet(fn, default):
    """Defensive probe: an optional internal must never 500 a read."""
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _server():
    """The live gateway server module — imported lazily, read through always."""
    from tui_gateway import server

    return server


def _deny_sources() -> frozenset:
    """The canonical session-sidebar deny-list (INTERNAL_LISTING_SOURCES).

    Imported lazily from the canonical tuple — a hand-rolled copy here would
    silently drift from the sidebar's set (the exact defect the core fixed).
    """
    from hermes_state_sessions import INTERNAL_LISTING_SOURCES

    return frozenset(INTERNAL_LISTING_SOURCES)


def _denied_source(row) -> bool:
    return (row.get("source") or "").strip().lower() in _deny_sources()


def _denied_live_record(record) -> bool:
    """A live runtime record whose source is deny-listed is not human-facing (same
    deny-list the session sidebar applies to the durable rows)."""
    return _denied_source(record)


def _safe_error_message(exc: Exception) -> str:
    """Sanitize an exception for egress: safe code only, never raw internal text."""
    return f"{type(exc).__name__}"


def _route_guard(message: str):
    """The core's outer handler discipline for the read routes: an unexpected failure
    becomes the named error (HTTP 503, the core's own message) — never a bare 500 and
    never a fabricated partial result. Named HTTP errors pass through untouched."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001 - fail closed, never fabricate a partial badge
                # Log hygiene: exception CLASS only. Raw exc text / tracebacks can carry
                # credential-shaped values (command lines, tokens, request ids) into logs.
                log.debug("%s failed: %s", fn.__name__, _safe_error_message(exc))
                raise _http_error(5031, message) from exc
        return wrapper
    return deco


def _err_payload(code: int, message: str) -> dict:
    """Internal error envelope in the core handlers' shape."""
    return {"error": {"code": code, "message": message}}


def _http_error(code: int, message: str) -> HTTPException:
    return HTTPException(status_code=_STATUS_BY_CODE.get(code, 500), detail=message)


def _raise_from_envelope(response: dict, default_code: int = 5031) -> None:
    error = response.get("error")
    if error:
        code = int(error.get("code") or default_code)
        raise _http_error(code, str(error.get("message") or "request failed"))


def _offered_choices(data: dict) -> set[str]:
    """The choices THIS approval actually offers (the core's own computation)."""
    try:
        return {str(c) for c in (_server()._approval_request_payload(data or {}).get("choices") or [])}
    except Exception:  # noqa: BLE001 - a queue read failure must not widen the gate
        return {"deny"}


def _validate_choice_offered(session_key: str, request_id: str, choice: str) -> None:
    """Refuse a choice the pending approval never offered.

    The approval payload's own ``choices`` are the authority (mirrors the core's
    ``_approval_request_payload``: smart-denied approvals drop ``session``/``always``,
    ``allow_permanent=False`` drops ``always``). ``resolve_gateway_approval`` itself
    accepts any string, so without this gate an Action Center ``always`` could grant a
    permanent rule for an approval that only offered once/deny.
    """
    try:
        from tools import approval as _approval

        pending = {str(p.get("request_id") or ""): p for p in _approval.list_gateway_approvals(session_key)}
    except Exception as exc:  # noqa: BLE001 - fail closed: never resolve on an unreadable queue
        raise _http_error(5031, f"approval resolve failed: {_safe_error_message(exc)}") from exc
    payload = pending.get(request_id)
    if payload is None:
        return  # nothing pending by that id: the resolve below reports it honestly
    offered = _offered_choices(payload)
    if choice not in offered:
        raise _http_error(
            4004, f"choice '{choice}' is not offered by this request (offered: {', '.join(sorted(offered))})")


def _storage_unavailable() -> HTTPException:
    """The shared SessionDB is unopenable — the core's fail-closed storage error."""
    response = _quiet(lambda: _server()._db_unavailable_error(_RID, code=5031), None)
    message = "Session storage is unavailable."
    if isinstance(response, dict):
        message = str((response.get("error") or {}).get("message") or message)
    return HTTPException(status_code=503, detail=message)


@contextlib.contextmanager
def _profile_scope(profile: str | None):
    """Resolve *profile* and bind its runtime scope exactly like the core's
    ``@_profile_scoped`` wrapper: ``_profile_home`` resolves the home (None =
    launch profile), then the session profile runtime scope binds config/secrets
    to it. ``ProfileUnavailableError`` becomes 404 — never a fallback; any other
    resolution failure becomes 503."""
    server = _server()
    name = (profile or "").strip() or None
    try:
        home = server._profile_home(name)
    except server.ProfileUnavailableError as exc:
        raise _http_error(4064, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - never fall back to the wrong user's data
        raise _http_error(5031, f"profile resolution failed: {_safe_error_message(exc)}") from exc
    scope = getattr(server, "_session_profile_runtime_scope", None)
    if scope is None:
        yield home
        return
    with scope({"profile_home": str(home) if home is not None else None}):
        yield home


@contextlib.contextmanager
def _profile_db(profile: str | None):
    """The SessionDB for *profile* — the core's own ``_profile_db`` handle.

    Kept as the module's single DB-door helper so tests can patch one seam;
    handlers open the same core context manager directly (same semantics:
    launch profile → the shared handle, a named profile → a dedicated read-only
    handle, None when the store is unopenable → fail closed).
    """
    name = (profile or "").strip() or None
    with _server()._profile_db({"profile": name}):
        yield


def _profile_display_name(profile: str | None) -> str:
    """Best-effort profile name for coverage metadata."""
    try:
        return _server()._response_profile_name((profile or "").strip() or None) or "default"
    except Exception:  # noqa: BLE001
        return "default"


def _inbox_home_key(home) -> str:
    """Normalized comparison key for "which profile home owns this runtime session".

    A session created under the launch profile stores ``profile_home = None``,
    and ``_profile_home()`` returns None for that same profile — comparing raw
    values against a home path never matched and dropped every launch-profile
    session from the live joins. Both sides resolve through here; a foreign
    profile still matches only its own normalized path.
    """
    return os.path.normcase(str(home) if home is not None else str(_server()._hermes_home))


def _listing_rows(db, limit: int) -> list:
    """Human-facing ``list_sessions_rich`` rows (most recent first), deny-list applied."""
    rows = db.list_sessions_rich(source=None, limit=limit, order_by_last_active=True, compact_rows=True)
    return [row for row in rows if not _denied_source(row)]


def _live_sids_for_key(profile_home, session_key: str) -> list[str]:
    """Runtime ids of every live, human-facing session owning *session_key* in this profile."""
    server = _server()
    want_home = _inbox_home_key(profile_home)
    try:
        with server._sessions_lock:
            snapshot = list(server._sessions.items())
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for sid, record in snapshot:
        if not isinstance(record, dict) or record.get("_finalized"):
            continue
        if _inbox_home_key(record.get("profile_home")) != want_home:
            continue
        if str(record.get("session_key") or "") != session_key:
            continue
        if _denied_live_record(record):
            continue
        out.append(sid)
    return out


def _live_session_for_key(profile_home, session_key: str) -> str | None:
    """Runtime id of the live, human-facing session owning *session_key* in this profile, else None."""
    for sid in _live_sids_for_key(profile_home, session_key):
        return sid
    return None


def _owned_request_ids(server, live_sids: list[str]) -> set[str]:
    """Server→client request ids the listed live sessions PROVABLY own.

    Read through the gateway's own aggregate reader (``server._open_requests``):
    the local queue for the session's runtime id, plus the compute-host mirror
    the parent keeps for host-owned requests. Nothing outside this session in
    this profile is ever in the set, so an answer can only bind to a request the
    addressed session actually owns — never a globally-resolved foreign id.
    """
    owned: set[str] = set()
    for sid in live_sids:
        try:
            snaps = server._open_requests(sid)
        except Exception as exc:  # noqa: BLE001 - failed ownership lookup is not expiry
            raise _http_error(5031, f"request ownership read failed: {_safe_error_message(exc)}") from exc
        for snap in snaps:
            if isinstance(snap, dict) and snap.get("method") == "clarify" and snap.get("id"):
                owned.add(str(snap["id"]))
    return owned


def _require_session_row(server, session_key: str, profile: str | None) -> dict:
    """The durable session row must exist in the addressed profile and be human-facing.

    The trust anchor for every mutation: a session the Action Center would never
    list (unlisted row, deny-listed source) can never be acted on — fail closed
    with the core's ``session not found`` refusal.
    """
    with server._profile_db({"profile": (profile or "").strip() or None}) as db:
        if db is None:
            raise _storage_unavailable()
        return _session_row_from(db, session_key)


def _session_row_from(db, session_key: str) -> dict:
    """``_require_session_row`` against an ALREADY-OPEN handle (redo/dismiss share
    one writable handle for lookup + clear; the gate must not open a second one)."""
    row = db.get_session(session_key) if db is not None else None
    if row is None or _denied_source(row):
        raise _http_error(4001, "session not found")
    return row


def _live_runtime_binding(live_session_id: str, profile_home, session_key: str,
                          profile: str | None = None) -> dict | None:
    """The live runtime record for *live_session_id* when it provably belongs to
    *session_key* in this profile, else None.

    Binds the client-supplied runtime id to the durable identity before the live
    dispatch path may use it: a stale/reaped id, an id from another profile, or an id
    pointing at a DIFFERENT session must never route another session's runtime through
    the gateway's ``session.control``. Mirrors the core's profile-normalized matching.
    The session row must ALSO exist and be human-facing (the same gate the direct-state
    path applies), so the live path can never reach a session the Action Center would
    never list (deny-listed source, internal automation, unlisted row).
    """
    server = _server()
    try:
        with server._profile_db({"profile": (profile or "").strip() or None}) as db:
            row = db.get_session(session_key) if db is not None else None
    except Exception:  # noqa: BLE001 - fail closed: an unreadable store never widens the gate
        return None
    if row is None or _denied_source(row):
        return None
    try:
        with server._sessions_lock:
            record = server._sessions.get(live_session_id)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(record, dict) or record.get("_finalized"):
        return None
    if _inbox_home_key(record.get("profile_home")) != _inbox_home_key(profile_home):
        return None
    if str(record.get("session_key") or "") != session_key:
        return None
    if _denied_live_record(record):
        return None
    return record


def _pending_approval(session_key: str):
    """Oldest unresolved approval payload for *session_key*, strict when supported.

    The core's strict reader propagates queue-read errors so callers can surface
    them as coverage errors instead of a false all-clear; older gateways only
    have the tolerant reader.
    """
    fn = _server()._pending_approval_request_payload
    if "strict" in inspect.signature(fn).parameters:
        return fn(session_key, strict=True)
    return fn(session_key)


# ── pure lane/category classification (ported verbatim) ───────────────────────
def classify_lanes(
    control: dict | None, pending_approval: bool, pending_clarify: bool, now: float | None = None,
    pending_expired: bool = False,
) -> list[str]:
    """Ordered, deduplicated lanes for one session from its allowed control snapshot.

    Honest rules — no inference from turn counts:
      needs_you  - a pending approval or clarify prompt for this session, or an expired
                   request still awaiting the operator's redo/dismiss decision.
      running    - an ACTIVE goal with no wait barrier, or a loop awaiting its response.
      waiting    - a paused goal / wait barrier, or a loop deferred by a goal or paused.
      scheduled  - an active heartbeat, or an active loop not awaiting a response whose
                   next due time is in the future.
    ``control`` is the ``session.control.read`` snapshot shape; absent state returns [].
    """
    lanes: list[str] = []
    if pending_approval or pending_clarify or pending_expired:
        lanes.append("needs_you")
    control = control or {}
    goal = control.get("goal")
    loop = control.get("loop")
    heartbeat = control.get("heartbeat")
    if now is None:
        now = time.time()

    if bool(goal and goal.get("status") == "active" and not goal.get("wait_barrier")) or bool(
        loop and loop.get("status") == "active" and loop.get("awaiting_response")
    ):
        lanes.append("running")
    if bool(goal and (goal.get("status") == "paused" or goal.get("wait_barrier"))) or bool(
        loop and (loop.get("status") == "paused" or loop.get("deferred_by_goal"))
    ):
        lanes.append("waiting")
    if bool(heartbeat and heartbeat.get("status") == "active") or bool(
        loop
        and loop.get("status") == "active"
        and not loop.get("awaiting_response")
        and (loop.get("next_due_at") or 0) > now
    ):
        lanes.append("scheduled")
    return lanes


def _count_lanes(items: list[dict]) -> dict:
    counts = {lane: 0 for lane in ("needs_you", "running", "waiting", "scheduled", "total")}
    for item in items:
        counts["total"] += 1
        for lane in item.get("lanes", []):
            if lane in counts:
                counts[lane] += 1
    return counts


def classify_categories(control: dict | None) -> list[str]:
    """Categories for the left navigation from a control snapshot.

    Returns ALL matching categories (overlapping membership).  A session with
    both a goal and a loop appears in both ``goals`` and ``loops``.
    Sessions with no recognized automation state return ``["other"]``.
    Completed/done automation types are included when persisted state exists,
    with separate activity state indicated by the goal/loop/heartbeat status.
    """
    control = control or {}
    goal = control.get("goal")
    loop = control.get("loop")
    heartbeat = control.get("heartbeat")
    cats: list[str] = []
    if goal and goal.get("status") in ("active", "paused", "done"):
        cats.append("goals")
    if loop and loop.get("status") in ("active", "paused", "done"):
        cats.append("loops")
    if heartbeat and heartbeat.get("status") in ("active", "paused", "done"):
        cats.append("heartbeats")
    if not cats:
        cats.append("other")
    return cats


def _count_categories(items: list[dict]) -> dict:
    counts = {cat: 0 for cat in ("goals", "loops", "heartbeats", "subagents", "background_tasks", "other")}
    for item in items:
        for cat in item.get("categories", []):
            if cat in counts:
                counts[cat] += 1
    return counts


def badge_state(items: list[dict], errors: list = ()) -> str:
    """Amber when anything needs the operator; red on any data-source failure/error state
    (so an unsupported/error view is never labelled all-clear); muted otherwise."""
    if errors:
        return "red"
    if any("needs_you" in item.get("lanes", []) for item in items):
        return "amber"
    return "none"


# ── live sources ──────────────────────────────────────────────────────────────
def _live_clarify_by_session_key(profile_home) -> tuple[dict[str, int], list[str]]:
    """session_key → pending clarify count for OPEN sessions owned by this profile.

    Uses the public ``server_requests.open_requests(sid)`` reader for pending
    clarify detection.  Only the count is surfaced — never the question text —
    so credential-shaped clarify payloads are never egressed.  Returns
    (counts, errors) so enumeration/query failures surface as coverage errors
    and a red badge instead of being silently swallowed.
    """
    server = _server()
    out: dict[str, int] = {}
    errors: list[str] = []
    try:
        from tui_gateway import server_requests

        with server._sessions_lock:
            snapshot = list(server._sessions.items())
    except Exception as exc:  # noqa: BLE001
        log.debug("action-center live-session enumeration failed: %s", _safe_error_message(exc))
        errors.append(f"live-session enumeration failed: {_safe_error_message(exc)}")
        return out, errors

    # Both sides normalize through _inbox_home_key: None means the launch profile's home.
    want_home = _inbox_home_key(profile_home)
    for sid, record in snapshot:
        if not isinstance(record, dict):
            continue
        if _inbox_home_key(record.get("profile_home")) != want_home:
            continue
        key = str(record.get("session_key") or "")
        if not key:
            continue
        try:
            reqs = server_requests.open_requests(sid)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{key}: clarify query failed: {_safe_error_message(exc)}")
            continue
        clarify_count = sum(1 for r in reqs if r.get("method") == "clarify")
        if clarify_count > 0:
            out[key] = clarify_count
    return out, errors


def _subagent_counts_by_owner() -> tuple[dict[str, int], str | None]:
    """session_key → active subagent count from the live registry.

    Only the count is surfaced — never the goal text or transcript content.
    Returns ``(counts, error)`` where *error* is None on success or a safe error
    message on failure — never ``{}`` misinterpreted as "zero subagents".
    The import is inside the guarded block: a gateway without the registry module
    is a degraded data source (a coverage error), never a 500.
    """
    try:
        from tools.delegate_tool_registry import list_active_subagents

        records = list_active_subagents()
    except Exception as exc:  # noqa: BLE001
        return {}, f"subagent enumeration failed: {_safe_error_message(exc)}"
    counts: dict[str, int] = {}
    for r in records:
        owner_key = str(r.get("owner_agent_session_id") or "")
        if owner_key:
            counts[owner_key] = counts.get(owner_key, 0) + 1
    return counts, None


def _background_task_counts_by_session(session_keys: list[str]) -> tuple[dict[str, int], str | None]:
    """session_key → running background-process count from the process registry.

    Uses ``process_registry.list_sessions(session_key=key)`` for each bounded
    allowed key to respect the PUBLIC scoped API.  Only the count is surfaced —
    never command text or output content.  The import is inside the guarded
    block: a gateway without the registry module is a degraded data source (a
    coverage error), never a 500.
    """
    counts: dict[str, int] = {}
    try:
        from tools.process_registry import process_registry

        for key in session_keys:
            if not key:
                continue
            try:
                sessions = process_registry.list_sessions(session_key=key)
            except Exception as exc:  # noqa: BLE001
                return counts, f"bg-process query failed for {_safe_error_message(exc)}"
            running = sum(1 for s in sessions if s.get("status") == "running")
            if running > 0:
                counts[key] = running
    except Exception as exc:  # noqa: BLE001
        return counts, f"bg-process enumeration failed: {_safe_error_message(exc)}"
    return counts, None


# ── expired requests: the durable store (ported verbatim) ─────────────────────
def _expired_record_key(session_key: str, request_id: str) -> str:
    return f"{_EXPIRED_PREFIX}{session_key}.{request_id}"


def _parse_expired_record(raw) -> dict | None:
    try:
        entry = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(entry, dict) or not entry.get("request_id"):
        return None
    return entry


def _expired_read_failed(entries) -> bool:
    """True when a ``load_expired_requests`` result is the named scan failure
    (``[{"error": ...}]``) rather than clean data. A failed DB scan must never
    masquerade as "nothing expired"."""
    return bool(entries) and len(entries) == 1 and isinstance(entries[0], dict) and (
        "error" in entries[0] and "request_id" not in entries[0])


def _delete_meta(db, key: str) -> None:
    """Drop one ``state_meta`` row.

    Prefers the SessionDB's own ``delete_meta`` (the core PR added it); a
    gateway that predates it gets the same write through the public
    ``_write_sql`` path, so Dismiss works on both.
    """
    delete = getattr(db, "delete_meta", None)
    if callable(delete):
        delete(key)
        return
    db._write_sql("DELETE FROM state_meta WHERE key = ?", (key,))


def record_expired_request(db, session_key: str, payload: dict, outcome: str) -> bool:
    """Persist one request that ended without an answer; True when written.

    Only what the panel needs to say *what it was for* is kept, each field bounded. The
    command text is whatever the surface already redacted for display, so a
    credential-shaped value never reaches this store.
    """
    if db is None or not session_key:
        return False
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        return False
    entry = {
        "request_id": request_id,
        "session_key": session_key,
        "kind": "approval",
        "command": str(payload.get("command") or "")[:500],
        "description": str(payload.get("description") or "")[:300],
        "pattern_keys": [str(k) for k in (payload.get("pattern_keys") or [])][:8],
        "ended_at": time.time(),
        "outcome": str(outcome or "unknown")[:40],
    }
    try:
        db.set_meta(_expired_record_key(session_key, request_id), json.dumps(entry))
        _prune_expired_requests(db, session_key)
    except Exception:  # noqa: BLE001 - request_id is caller-controlled; class-only log
        log.warning("failed to record expired request")
        return False
    return True


def _prune_expired_requests(db, session_key: str) -> None:
    """Keep the newest N per session and drop anything past the age window.

    A failed read classifies nothing: pruning is skipped (records may
    over-retain, but a failed scan never drives deletes).
    """
    entries = load_expired_requests(db, session_key)
    if _expired_read_failed(entries):
        return
    cutoff = time.time() - _EXPIRED_MAX_AGE_S
    for index, entry in enumerate(entries):  # newest first
        too_old = float(entry.get("ended_at") or 0) < cutoff
        if too_old or index >= _EXPIRED_KEEP_PER_SESSION:
            _delete_meta(db, _expired_record_key(session_key, str(entry.get("request_id") or "")))


def load_expired_requests(db, session_key: str) -> list[dict]:
    """Expired requests for one session, newest first.

    A failed prefix scan returns the named failure ``[{"error": <class>}]`` — never
    ``[]``, which callers would read as "nothing expired" (a false all-clear). A
    clean read with genuinely no rows still returns ``[]``.
    """
    if db is None or not session_key:
        return []
    try:
        rows = db.list_meta_prefix(f"{_EXPIRED_PREFIX}{session_key}.")
    except Exception as exc:  # noqa: BLE001
        log.debug("expired-request read failed: %s", _safe_error_message(exc))
        return [{"error": _safe_error_message(exc)}]
    entries = [entry for _key, raw in rows if (entry := _parse_expired_record(raw)) is not None]
    entries.sort(key=lambda e: float(e.get("ended_at") or 0), reverse=True)
    return entries


def load_expired_request_counts(db) -> tuple[dict[str, int], str | None]:
    """session_key → expired-request count for every session, from ONE prefix scan.

    Returns ``(counts, error)``: *error* is None on a clean scan (including a
    valid empty store → ``{}``), or the exception class name when the scan
    failed — so callers can surface partial coverage instead of an all-clear.
    """
    counts: dict[str, int] = {}
    if db is None:
        return counts, None
    try:
        rows = db.list_meta_prefix(_EXPIRED_PREFIX)
    except Exception as exc:  # noqa: BLE001
        log.debug("expired-request scan failed: %s", _safe_error_message(exc))
        return counts, _safe_error_message(exc)
    for _key, raw in rows:
        entry = _parse_expired_record(raw)
        if entry is None:
            continue
        key = str(entry.get("session_key") or "")
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts, None


def clear_expired_request(db, session_key: str, request_id: str) -> bool:
    """Drop one expired-request record (Dismiss). False when it was not there."""
    if db is None or not session_key or not request_id:
        return False
    key = _expired_record_key(session_key, request_id)
    if db.get_meta(key) is None:
        return False
    _delete_meta(db, key)
    return True


# ── request-detail build blocks (ported verbatim) ─────────────────────────────
def _build_approval_payload(data: dict) -> dict:
    """Redacted approval payload with computed choices, matching the core."""
    # Use chat's authority for redaction and choice calculation; do not fork it.
    server = _server()
    payload = server._approval_request_payload(data)
    return {
        "request_id": payload.get("request_id", ""),
        "command": payload.get("command", ""),
        "description": payload.get("description", ""),
        "choices": payload.get("choices", ["deny"]),
        "allow_permanent": payload.get("allow_permanent"),
        "allow_session": payload.get("allow_session"),
        "smart_denied": payload.get("smart_denied"),
        "tool_name": payload.get("tool_name"),
    }


def _build_clarify_detail(snapshot: dict) -> dict:
    """Extract clarification details from a server request snapshot."""
    params = dict(snapshot.get("params", {}))
    qids = params.pop("questions", None)
    answers = params.pop("answers", None)
    if qids is not None:
        return {
            "request_id": snapshot.get("id", ""),
            "kind": "batch",
            "params": {
                "questions": qids,
                "answers": answers,
            },
        }
    return {
        "request_id": snapshot.get("id", ""),
        "kind": "single",
        "params": {
            "question": params.get("question"),
            "choices": params.get("choices"),
            "multi_select": params.get("multi_select"),
            "answers": answers,
        },
    }


def _redact_context_text(text: str) -> str:
    """Redact credential-shaped content before any transcript text leaves the gateway."""
    try:
        from agent.redact import redact_sensitive_text

        return str(redact_sensitive_text(str(text), force=True) or "")
    except Exception:  # noqa: BLE001
        return ""


def _build_context_excerpt(db, session_key: str) -> dict:
    """Bounded, redacted excerpt of the owning session's recent turns.

    Read-only and side-effect free: it reads PERSISTED messages directly and never
    resumes, hydrates or mutates the session, so opening the panel cannot change the
    state of the work it describes.

    Only user/assistant text rows are surfaced. Tool rows are raw JSON payloads that
    cost context without informing a decision, and tool-call-only assistant rows carry
    no text to show. Content is redacted and hard-capped per message and in total, so
    an unbounded transcript can never be egressed through this path.
    """
    try:
        rows = db.get_messages(session_key, limit=_CONTEXT_SCAN_LIMIT, latest=True)
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "reason": f"transcript read failed: {_safe_error_message(exc)}",
            "messages": [],
        }

    kept: list[dict] = []
    total = 0
    for row in reversed(rows or []):  # newest first while selecting, displayed oldest first
        role = str(row.get("role") or "")
        if role not in _CONTEXT_ROLES:
            continue
        text = _redact_context_text(row.get("content") or "").strip()
        if not text:
            continue
        if len(text) > _CONTEXT_MESSAGE_CHARS:
            text = text[:_CONTEXT_MESSAGE_CHARS].rstrip() + "…"
        kept.append({
            "role": role,
            "text": text,
            "timestamp": row.get("timestamp"),
        })
        total += len(text)
        if len(kept) >= _CONTEXT_KEPT_MESSAGES or total >= _CONTEXT_TOTAL_CHARS:
            break

    if not kept:
        return {"available": False, "reason": "no displayable messages in this session", "messages": []}
    kept.reverse()  # chronological for display
    return {"available": True, "reason": None, "messages": kept}


def _gather_requests_for_session(
    sid: str, session: dict, profile_home
) -> tuple[list[dict], list[dict], list[str]]:
    """Gather approvals and clarifications for one live session.

    Returns (approvals, clarifications, errors).
    """
    from tui_gateway import server_requests

    approvals: list[dict] = []
    clarifications: list[dict] = []
    errors: list[str] = []

    session_key = str(session.get("session_key") or "")
    if not session_key:
        return approvals, clarifications, errors

    # Gather approvals from the gateway queue (read-only replay-safe snapshots).
    try:
        from tools import approval as _approval

        raw_approvals = _approval.list_gateway_approvals(session_key)
        for raw in raw_approvals:
            approvals.append(_build_approval_payload(raw))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"approval read failed: {_safe_error_message(exc)}")

    # Gather clarifications from server_requests.
    try:
        from tui_gateway import server_requests

        snapshots = server_requests.open_requests(sid)
        for snap in snapshots:
            method_name = snap.get("method", "")
            if method_name not in _ALLOWED_REQUEST_METHODS:
                continue
            if method_name == "clarify":
                clarifications.append(_build_clarify_detail(snap))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"clarify read failed: {_safe_error_message(exc)}")

    return approvals, clarifications, errors


# ── direct state control (ported verbatim from the core) ──────────────────────
def _execute_heartbeat_action(session_key: str, action: str) -> dict:
    from hermes_cli.heartbeat import HeartbeatManager, format_interval

    manager = HeartbeatManager(session_id=session_key)
    if action == "heartbeat.pause":
        state = manager.pause()
        output = f"⏸ Heartbeat paused: {state.prompt}" if state else "No heartbeat set."
    elif action == "heartbeat.resume":
        state = manager.resume()
        output = (
            f"▶ Heartbeat resumed (every {format_interval(state.interval_seconds)}): {state.prompt}"
            if state else "No heartbeat to resume."
        )
    elif action == "heartbeat.clear":
        output = "✓ Heartbeat cleared." if manager.clear() else "No heartbeat set."
    else:
        return _err_payload(4004, f"unknown heartbeat action: {action}")
    return {"result": {"type": "exec", "output": output}}


def _execute_direct_state_action(session_key: str, action: str) -> dict:
    """Run a persisted pause/resume through the public manager APIs (no live session).

    Mirrors the slash-command semantics so a control applied from the Action Center lands in
    the same state the typed command leaves (pause reasons, turn budget, cadence untouched).
    """
    kind, verb = action.split(".", 1)

    if kind == "goal":
        from hermes_cli.goals import GoalManager, load_goal

        state = load_goal(session_key)
        if state is None or state.status == "cleared":
            return _err_payload(4004, "No goal set for this session.")
        if verb == "pause" and state.status not in ("active", "paused"):
            return _err_payload(4004, f"Goal is {state.status}; only an active goal can be paused.")
        if verb == "resume" and state.status != "paused":
            return _err_payload(4004, f"Goal is {state.status}, not paused.")
        manager = GoalManager(session_id=session_key)
        updated = manager.pause() if verb == "pause" else manager.resume()
        if updated is None:
            return _err_payload(4004, "Goal update failed.")
        output = (
            f"⏸ Goal paused: {updated.goal}"
            if verb == "pause"
            else f"▶ Goal resumed: {updated.goal}"
        )
    elif kind == "loop":
        from hermes_cli.loops import LoopManager, load_loop

        state = load_loop(session_key)
        if state is None or state.status == "cleared":
            return _err_payload(4004, "No loop set for this session.")
        if verb == "pause" and state.status not in ("active", "paused"):
            return _err_payload(4004, f"Loop is {state.status}; only a running loop can be paused.")
        if verb == "resume" and state.status != "paused":
            return _err_payload(4004, f"Loop is {state.status}, not paused.")
        manager = LoopManager(session_id=session_key)
        updated = manager.pause() if verb == "pause" else manager.resume()
        if updated is None:
            return _err_payload(4004, f"Loop cannot be {'paused' if verb == 'pause' else 'resumed'}.")
        output = (
            f"⏸ Loop paused: {updated.prompt}"
            if verb == "pause"
            else f"▶ Loop resumed ({updated.cadence_label()}): {updated.prompt}"
        )
    elif kind == "heartbeat":
        return _execute_heartbeat_action(session_key, action)
    else:
        return _err_payload(4004, f"action requires a live session: {action}")
    return {"result": {"type": "exec", "output": output}}


def _manager_error_message(action: str, exc: Exception) -> str:
    prefixes = {
        "subgoal.add": "/subgoal",
        "subgoal.remove": "/subgoal remove",
        "subgoal.clear": "/subgoal clear",
    }
    return f"{prefixes.get(action, action)}: {exc}"


def _dispatch_envelope(response: dict) -> dict:
    """Keep the command result's user-visible envelope without adding model-facing data."""
    result = response.get("result") or {}
    return {
        "type": result.get("type"),
        "output": result.get("output"),
        "notice": result.get("notice"),
        "message": result.get("message"),
        "display": result.get("display"),
    }


def _direct_state_control(session_key: str, action: str, profile: str | None) -> dict:
    """Pause/resume persisted automation for a stored session with no live runtime.

    Port of the core's ``_direct_state_control``: the session row must exist in the
    addressed profile and be operator-facing (the same deny-list the summary applies),
    so the direct path cannot reach sessions the Action Center would never show.
    Success returns the same payload shape as the live path.
    """
    server = _server()
    row = None
    with server._profile_db({"profile": (profile or "").strip() or None}) as db:
        row = db.get_session(session_key) if db is not None else None
    if row is None or _denied_source(row):
        raise _http_error(4001, "session not found")

    try:
        action_result = _execute_direct_state_action(session_key, action)
    except (RuntimeError, ValueError, IndexError) as exc:
        raise _http_error(4004, _manager_error_message(action, exc)) from exc
    error = action_result.get("error")
    if error:
        raise _http_error(int(error.get("code") or 4004), str(error.get("message") or action))

    try:
        control = server._snapshot_control(session_key)
    except Exception as exc:  # noqa: BLE001
        log.debug("session.control snapshot after direct %s failed: %s", action, _safe_error_message(exc))
        raise _http_error(5031, f"session.control snapshot failed: {_safe_error_message(exc)}") from exc

    # A runtime for this key may exist after all (the client's live id was stale): refresh it
    # so any open chat surface follows the change the same way the live path keeps it in sync.
    try:
        home = server._profile_home((profile or "").strip() or None)
        live_sid = _live_session_for_key(home, session_key)
        if live_sid:
            event_control = {key: value for key, value in control.items() if key != "loop_min_interval_seconds"}
            server._emit("session.control.update", live_sid, {"control": event_control})
    except Exception as exc:  # noqa: BLE001
        log.debug("session.control.update emit after direct %s failed (best-effort): %s", action, _safe_error_message(exc))

    return {"control": control, "dispatch": _dispatch_envelope(action_result)}


def _dispatch_live_control(body: "ControlBody", action: str) -> dict:
    """Run one allowlisted control through the gateway's own ``session.control``.

    Same allowlist the composer's status cards go through, so the panel can never
    exceed the chat's authority; the live session's command handlers arbitrate
    against the running turn.
    """
    server = _server()
    handler = server._methods.get("session.control")
    if handler is None:
        raise _http_error(5031, "session.control unavailable")
    params: dict[str, Any] = {
        "session_id": body.live_session_id,
        "action": action,
        "args": {},
    }
    if (body.profile or "").strip():
        params["profile"] = body.profile.strip()
    result = handler(_RID, params)
    _raise_from_envelope(result)
    return result.get("result") or {}


# ── request bodies ────────────────────────────────────────────────────────────
class RespondBody(BaseModel):
    request_id: str
    choice: str
    session_key: str
    profile: str | None = None


class AnswerBody(BaseModel):
    request_id: str
    answer: "str | list[Any] | dict[Any, Any]" = ""   # string, or a JSON array for multi-select
    question_id: str | None = None   # batch clarify: one call per question
    session_key: str | None = None
    profile: str | None = None


class ControlBody(BaseModel):
    action: str
    session_key: str
    live_session_id: str | None = None
    profile: str | None = None


class RedoBody(BaseModel):
    request_id: str
    session_key: str
    profile: str | None = None


class DismissBody(BaseModel):
    request_id: str
    session_key: str
    profile: str | None = None


# ── routes ────────────────────────────────────────────────────────────────────
@router.get("/summary")
@_route_guard("inbox.list failed")
def action_center_summary(profile: str = "", limit: int = _DEFAULT_LIMIT) -> dict:
    """Read-only cross-session aggregation for the active profile.

    Same shape as the core ``inbox.list`` result's ``inbox`` object.  Sources:
    persisted automation via the SAME snapshots ``session.control.read`` returns
    (so sessions that are NOT open are still covered — no resume, no transcript
    hydration), the live pending approval (redacted on egress), live pending
    clarify for OPEN sessions (count only), and durably recorded expired
    requests.  Coverage is declared outright and tolerant: a failed source
    surfaces as a coverage error and a red badge, never a 500 and never an
    all-clear.
    """
    server = _server()
    name = (profile or "").strip() or None
    with _profile_scope(name) as profile_home:
        try:
            cap = max(1, min(int(limit), _MAX_LIMIT))
        except (TypeError, ValueError):
            cap = _DEFAULT_LIMIT
        # Fetch cap+1 rows so truncation can be detected from the raw row count
        # before deny-list filtering reduces the scan.  The extra row is never processed.
        fetch_limit = min(cap + 1, _MAX_LIMIT + 1)
        with server._profile_db({"profile": name}) as db:
            if db is None:
                raise _storage_unavailable()
            try:
                rows = _listing_rows(db, fetch_limit)
            except Exception as exc:  # noqa: BLE001
                log.warning("action-center summary scan failed: %s", _safe_error_message(exc))
                raise _http_error(5031, "inbox.list scan failed") from exc
            # One prefix scan for every session's expired-request count (state_meta), so a
            # request that died without an answer stays visible after its turn, session
            # close and app restart.  Read on the SAME open handle — the core read it
            # after the with-block, which silently no-ops on a closed foreign-profile
            # handle; here the handle is still open, foreign profiles included.
            expired_by_key, expired_scan_error = load_expired_request_counts(db)

        clarify_by_key, clarify_errors = _live_clarify_by_session_key(profile_home)
        subagent_by_key, subagent_error = _subagent_counts_by_owner()

        # Collect session keys from the allowed rows for bounded bg-process queries
        session_keys = []
        for row in rows[:cap]:
            if _denied_source(row):
                continue
            key = str(row.get("id") or "")
            if key:
                session_keys.append(key)
        bg_task_by_key, bg_task_error = _background_task_counts_by_session(session_keys)

        items: list[dict] = []
        errors: list[str] = []
        if expired_scan_error is not None:
            # A failed store-wide scan is NOT a valid empty store: declare partial
            # coverage (red badge) instead of an all-clear.
            errors.append(f"expired-request scan failed: {expired_scan_error}")
        if subagent_error:
            errors.append(subagent_error)
        if bg_task_error:
            errors.append(bg_task_error)
        scanned = 0
        for row in rows[:cap]:
            if _denied_source(row):
                continue
            key = str(row.get("id") or "")
            if not key:
                continue
            scanned += 1
            try:
                control = server._snapshot_control(key)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{key}: snapshot read failed: {_safe_error_message(exc)}")
                control = {}
            approval = None
            try:
                pending = _pending_approval(key)
                if isinstance(pending, dict):
                    # Metadata-only approval — never copy arbitrary description text
                    # that could contain credential-shaped values or raw exception text.
                    approval = {
                        "count": 1,
                        "description": "pending approval",
                        "command_redacted": True,
                    }
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{key}: approval read failed: {_safe_error_message(exc)}")
            clarify_count = clarify_by_key.get(key, 0)
            clarify = {"count": clarify_count} if clarify_count > 0 else None
            expired_count = expired_by_key.get(key, 0)
            lanes = classify_lanes(control, approval is not None, clarify is not None,
                                   pending_expired=expired_count > 0)
            cats = classify_categories(control)
            subagent_count = subagent_by_key.get(key, 0)
            bg_task_count = bg_task_by_key.get(key, 0)
            # Sessions with active subagents / background processes appear in their
            # categories (overlapping membership)
            if subagent_count > 0 and "subagents" not in cats:
                cats.append("subagents")
            if bg_task_count > 0 and "background_tasks" not in cats:
                cats.append("background_tasks")
            items.append({
                "session_key": key,
                "profile": name or _profile_display_name(name),
                "title": str(row.get("title") or ""),
                "source": str(row.get("source") or ""),
                "cwd": str(row.get("cwd") or ""),
                "lanes": lanes,
                "categories": cats,
                "updated_at": control.get("updated_at", 0),
                "needs_you_count": 1 if "needs_you" in lanes else 0,
                "pending_count": (1 if approval is not None else 0) + clarify_count,
                "expired_request_count": expired_count,
                "goal": control.get("goal"),
                "loop": control.get("loop"),
                "heartbeat": control.get("heartbeat"),
                "pending_approval": approval,
                "pending_clarify": clarify,
                "subagent_count": subagent_count,
                "subagent_count_unavailable": subagent_error is not None,
                "background_task_count": bg_task_count,
                "background_task_count_unavailable": bg_task_error is not None,
            })

        # Honest truncation: we fetched cap+1 rows; if MORE than cap rows survived
        # deny-list filtering, the listing has more human-facing rows than the page
        # shows. (Parity with the core: denied-source rows in the DB never count toward
        # truncation — _listing_rows filters at Python level before this comparison.)
        raw_truncated = len(rows) > cap
        errors.extend(clarify_errors)
        coverage = {
            "profile": name or _profile_display_name(name),
            "connection_scope": "active connection and profile only",
            "scanned_sessions": min(scanned, cap),
            "partial": raw_truncated,
            "approval_scope": "live gateway approval queue",
            "clarify_scope": "live open sessions only",
            "errors": errors[:20],
        }
        return {
            "badge": badge_state(items, errors),
            "counts": _count_lanes(items),
            "categories": _count_categories(items),
            "coverage": coverage,
            "items": items,
        }


@router.get("/details")
@_route_guard("inbox.requests failed")
def action_center_details(session_key: str = "", profile: str = "") -> dict:
    """Scoped request details for one session (the core ``inbox.requests`` result).

    Read-only: it never resolves approvals or clarifications, never resumes or
    hydrates sessions, and never has side effects on the server request queue or
    approval queue.  Only ``approval`` and ``clarify`` request types are
    surfaced; password, secret, vault, sudo and other credential-shaped types
    are excluded.
    """
    server = _server()
    key = (session_key or "").strip()
    if not key:
        raise _http_error(4002, "session_key is required")
    name = (profile or "").strip() or None
    with _profile_scope(name) as profile_home:
        # Durable identity is sessions.id; never trust only a runtime record's source.
        with server._profile_db({"profile": name}) as db:
            if db is None:
                raise _storage_unavailable()
            row = db.get_session(key)
            if row is None or _denied_source(row):
                raise _http_error(4001, "Session not found")
            # Same connection as the identity check: one read, no second open, no side
            # effects on the session itself.
            context = _build_context_excerpt(db, key)
            # Expired requests survive the turn, the session close and an app restart;
            # they are read here so the panel can still say what died unanswered — and
            # offer a Redo. A FAILED read is not a valid empty store: the record of what
            # died unanswered becomes unknown, so name it in coverage (red badge).
            expired_requests = load_expired_requests(db, key)
            pre_errors: list[str] = []
            if _expired_read_failed(expired_requests):
                # A failed read is not a valid empty store: the record of what died
                # unanswered becomes unknown — name it in coverage (red badge), and
                # never render the failure sentinel as a record row.
                pre_errors.append(f"expired-request read failed: {expired_requests[0]['error']}")
                expired_requests = []

        # Thread-safe snapshot of live sessions
        try:
            with server._sessions_lock:
                snapshot = list(server._sessions.items())
        except Exception as exc:  # noqa: BLE001
            raise _http_error(5036, f"could not enumerate active sessions: {_safe_error_message(exc)}") from exc

        want_home = _inbox_home_key(profile_home)
        live_sessions: list[tuple[str, dict]] = []
        for sid, record in snapshot:
            if not isinstance(record, dict):
                continue
            if record.get("_finalized"):
                continue
            if _inbox_home_key(record.get("profile_home")) != want_home:
                continue
            if str(record.get("session_key") or "") != key:
                continue
            # Deny-listed sources are not human-facing
            source = str(record.get("source") or "").strip().lower()
            if source in _deny_sources():
                continue
            live_sessions.append((sid, record))

        all_approvals: list[dict] = []
        all_clarifications: list[dict] = []
        all_errors: list[str] = list(pre_errors)
        live_ids: list[str] = []
        for sid, record in live_sessions:
            live_ids.append(sid)
            try:
                apps, cls, errs = _gather_requests_for_session(sid, record, profile_home)
                all_approvals.extend(apps)
                all_clarifications.extend(cls)
                all_errors.extend(errs)
            except Exception as exc:  # noqa: BLE001
                all_errors.append(f"{sid}: request gathering failed: {_safe_error_message(exc)}")

        # The anchor reports what the excerpt actually carries. "Unavailable" stays a
        # real, named state (read failure vs. nothing displayable) so the panel never
        # renders an empty transcript as if it were the whole story.
        if context.get("available"):
            context_anchor = f"available: last {len(context['messages'])} message(s)"
        else:
            context_anchor = f"unavailable: {context.get('reason') or 'no context'}"

        coverage = {
            "profile": name or _profile_display_name(name),
            "session_key": key,
            "live_session_count": len(live_ids),
            "approval_count": len(all_approvals),
            "clarification_count": len(all_clarifications),
            "context_anchor": context_anchor,
            "errors": all_errors[:20],
        }
        return {
            "coverage": coverage,
            "sessions": [{
                "live_session_ids": live_ids,
                "approvals": all_approvals,
                "clarifications": all_clarifications,
                "context": context,
                "expired_requests": expired_requests,
            }],
        }


@router.post("/respond")
def action_center_respond(body: RespondBody) -> dict:
    """Approve/deny one pending approval (choice ∈ once|session|always|deny).

    Mirrors the core's ``approval.respond`` for a specific ``request_id``, resolved
    by durable session identity: the session must have a human-facing row in this
    profile AND be live here (an approval prompt belongs to a live session's
    queue; nothing is resolved remotely otherwise).  Reading the queue never
    consumes it — only the operator's choice does.
    """
    request_id = (body.request_id or "").strip()
    session_key = (body.session_key or "").strip()
    if not request_id or not session_key:
        raise _http_error(4002, "request_id and session_key are required")
    choice = (body.choice or "").strip().lower()
    if choice not in _ALLOWED_CHOICES:
        raise _http_error(4004, "choice must be one of once, session, always, deny")
    with _profile_scope(body.profile) as profile_home:
        server = _server()
        # Trust boundary: the durable row must exist and be human-facing (a
        # deny-listed/unlisted session can never be approved from the panel).
        _require_session_row(server, session_key, body.profile)
        if _live_session_for_key(profile_home, session_key) is None:
            raise _http_error(4001, "session not found")
        _validate_choice_offered(session_key, request_id, choice)
        try:
            from tools import approval as _approval

            resolved = _approval.resolve_gateway_approval(
                session_key, choice, resolve_all=False, request_id=request_id)
        except Exception as exc:  # noqa: BLE001
            raise _http_error(5031, f"approval resolve failed: {_safe_error_message(exc)}") from exc
    return {"resolved": resolved}


@router.post("/answer")
def action_center_answer(body: AnswerBody) -> dict:
    """Answer a clarify request: single via the response-frame path, batch via locks.

    ``answer`` is a string, or a JSON array for multi-select.  A batch clarify is
    answered ONE call per question (``question_id`` set): each lock is proxied
    through the compute-host bridge first (exactly the core's ``clarify.lock``),
    then locks into the local queue; the LAST lock resolves the request.  A
    non-string answer is JSON-encoded for the lock, as the core does.

    Trust boundary: ``session_key`` is REQUIRED.  The durable row must exist and
    be human-facing in this profile, the session must be live here, and the
    request id must belong to one of THIS session's live runtimes (local queue or
    compute-host mirror) — a known foreign/global id is never resolved.
    """
    request_id = (body.request_id or "").strip()
    session_key = (body.session_key or "").strip()
    if not request_id:
        raise _http_error(4002, "request_id is required")
    if not session_key:
        raise _http_error(4002, "session_key is required")
    with _profile_scope(body.profile) as profile_home:
        server = _server()
        # The addressed session must exist and be human-facing in THIS profile.
        _require_session_row(server, session_key, body.profile)
        live_sids = _live_sids_for_key(profile_home, session_key)
        if not live_sids:
            raise _http_error(4001, "session not found")
        # The request id must be PROVABLY owned by one of this session's live
        # runtimes: the local queue via the same aggregate reader the details
        # route reads, plus the compute-host mirror for host-owned requests.
        owned = _owned_request_ids(server, live_sids)
        if request_id not in owned:
            # Not provably owned by an addressed session: never resolve globally —
            # the same honest ``expired`` an unknown id gets, no cross-session leak.
            return {"status": "expired"}

        from tui_gateway import server_requests

        if (body.question_id or "").strip():
            question_id = body.question_id.strip()
            answer = body.answer if isinstance(body.answer, str) else json.dumps(body.answer, ensure_ascii=False)
            if (proxied := server._lock_compute_host_clarify(_RID, request_id, question_id, answer)) is not None:
                _raise_from_envelope(proxied)
                return proxied.get("result") or {}
            try:
                remaining = server_requests.lock_answer(request_id, question_id, answer)
            except ValueError as exc:
                raise _http_error(4002, str(exc)) from exc
            if remaining is None:
                # The wait already ended (timeout / cancel) while the card was still
                # visible: not an error.
                return {"status": "expired"}
            return {"status": "ok", "remaining": remaining}

        frame = {"jsonrpc": "2.0", "id": request_id, "result": {"answer": body.answer}}
        if server_requests.resolve_response(frame) or server._relay_compute_host_response(frame):
            return {"status": "ok"}
        # The ownership pre-check bound this id to an owned session; reaching here
        # means the request settled between the two reads (genuine race): report
        # ``expired`` honestly instead of hunting a global winner.
        return {"status": "expired"}


@router.post("/control")
def action_center_control(body: ControlBody) -> dict:
    """Pause/resume goal|loop|heartbeat for one session.

    With a live runtime attached, the action runs through the gateway's own
    ``session.control`` (the same allowlist the composer's status cards go
    through).  Without one, the direct persisted-state path applies — stored
    sessions included — gated on the session row existing in the addressed
    profile and being operator-facing, with the same refusal messages as the
    chat commands.
    """
    raw_action = (body.action or "").strip()
    if not raw_action:
        raise _http_error(4004, "action is required")
    if raw_action.startswith("goal.gate"):
        raise _http_error(4004, "gate actions are not allowed through session.control")
    if raw_action not in _DIRECT_STATE_ACTIONS:
        raise _http_error(4004, f"unknown action: {raw_action}")
    session_key = (body.session_key or "").strip()
    if not session_key:
        raise _http_error(4002, "session_key is required")

    with _profile_scope(body.profile) as profile_home:
        # Every mutation anchors on a human-facing durable row in the addressed
        # profile — an unlisted/deny-listed session is 404 before any dispatch.
        server = _server()
        _require_session_row(server, session_key, body.profile)
        # Live runtime first, exactly like the core's session.control order. The id the
        # client sent must actually be THIS session in THIS profile: dispatching through
        # the gateway's own session.control on an unverified id would let a stale or
        # foreign live id drive another session's runtime. A verified binding keeps the
        # live path; anything else falls back to the gated direct-state path.
        if body.live_session_id:
            bound = _live_runtime_binding(body.live_session_id, profile_home, session_key, body.profile)
            if bound is not None:
                return _dispatch_live_control(body, raw_action)
        return _direct_state_control(session_key, raw_action, body.profile)


@router.post("/redo")
def action_center_redo(body: RedoBody) -> dict:
    """Re-raise an expired request by asking its session to attempt the action again.

    The original waiter is long gone (the agent already received its not-approved
    result and must not be told to retry from here), so re-raising means a fresh
    attempt: the session is prompted, which raises a NEW approval the operator
    can answer.  Requires the session to be live — the prompt path resumes
    nothing on its own — and the redo record is cleared only after the submit is
    accepted.
    """
    session_key = (body.session_key or "").strip()
    request_id = (body.request_id or "").strip()
    if not session_key or not request_id:
        raise _http_error(4002, "session_key and request_id are required")
    with _profile_scope(body.profile) as profile_home:
        server = _server()
        try:
            # ONE open, WRITABLE handle for the trust gate, the record lookup and
            # the later clear.
            # ``writer=True`` is the core's own cross-profile write seam: the default
            # opens a foreign profile READ-ONLY, so the clear after an accepted submit
            # would always raise (and after a refused submit a second handle would be
            # pure waste). writer is a no-op for the launch profile (shared handle).
            with server._profile_db({"profile": (body.profile or "").strip() or None}, writer=True) as db:
                if db is None:
                    raise _storage_unavailable()
                # Trust boundary: the durable row must exist and be human-facing —
                # an unlisted/deny-listed session's records are invisible to mutations.
                _session_row_from(db, session_key)
                entries = load_expired_requests(db, session_key)
                if _expired_read_failed(entries):
                    raise _http_error(
                        5031, f"expired-request read failed: {entries[0]['error']}")
                record = next(
                    (entry for entry in entries
                     if str(entry.get("request_id")) == request_id),
                    None,
                )
                if record is None:
                    raise _http_error(4001, "expired request not found")

                live_sid = _live_session_for_key(profile_home, session_key)
                if live_sid is None:
                    raise _http_error(4009, "session is not running — open it to redo this request")

                text = _REDO_PROMPT.format(command=str(record.get("command") or "(command not recorded)"))
                # Through the composer's own choke point: role alternation, persistence and
                # streaming behave exactly as a typed message.  ``queued`` never interrupts a
                # turn in flight, and ``hidden`` keeps a message the user did not type out of
                # the transcript's bubbles.
                submit = server._methods.get("prompt.submit")
                if submit is None:
                    raise _http_error(5031, "prompt.submit unavailable")
                submitted = submit(_RID, {
                    "session_id": live_sid, "text": text, "queued": True, "display_kind": "hidden",
                })
                _raise_from_envelope(submitted)

                cleared = clear_expired_request(db, session_key, request_id)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - a storage failure is 503, never a bare 500
            raise _http_error(5031, f"inbox.redo failed: {_safe_error_message(exc)}") from exc
        return {"redone": True, "session_id": live_sid, "record_cleared": cleared}


@router.post("/dismiss")
def action_center_dismiss(body: DismissBody) -> dict:
    """Drop one expired-request record (the operator's decision that it is done).

    Trust boundary: the session row must exist and be human-facing in the
    addressed profile — records of sessions the Action Center would never list
    cannot be cleared through this route (fail closed: ``session not found``).
    """
    session_key = (body.session_key or "").strip()
    request_id = (body.request_id or "").strip()
    if not session_key or not request_id:
        raise _http_error(4002, "session_key and request_id are required")
    with _profile_scope(body.profile):
        server = _server()
        try:
            # ``writer=True``: Dismiss WRITES (a delete), so a named profile's store must
            # be opened through the core's cross-profile write seam — the default opens a
            # foreign profile READ-ONLY and the delete would always fail.
            with server._profile_db({"profile": (body.profile or "").strip() or None}, writer=True) as db:
                if db is None:
                    raise _storage_unavailable()
                # Trust boundary: an unlisted/deny-listed session's records are
                # invisible to mutations (same gate as /respond /answer /control).
                _session_row_from(db, session_key)
                dismissed = clear_expired_request(db, session_key, request_id)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _http_error(5031, f"inbox.dismiss failed: {_safe_error_message(exc)}") from exc
    if not dismissed:
        raise _http_error(4001, "expired request not found")
    return {"dismissed": True}