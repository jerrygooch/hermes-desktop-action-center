# Action Center — Desktop plugin build spec

Port of the closed core PR (NousResearch/hermes-agent#116123) into a standalone Hermes
Desktop plugin, living in this repo. The core PR's implementation is the reference;
this repo re-packages it so nothing patches the app.

## What it is

One panel that shows what every agent session needs from you and lets you act without
opening the session: pending approvals (approve/deny + session/always variants),
clarify questions (lettered choices A/B/C… + a type-your-own row, multi-select, batch),
recent-message context with "Open full chat", search across sessions with live rail
counts, goal/loop/heartbeat facts with pause/resume (works for stored sessions too),
and expired requests that persist with Redo/Dismiss.

## Repo layout (this folder = repo root)

```
plugin id: action-center   (folder names must match)
dashboard/
  manifest.json      { "name": "action-center", "label": "Action Center", "description": ..., "version": "0.1.0", "api": "plugin_api.py" }
  plugin_api.py      FastAPI router, mounted at /api/plugins/action-center/
desktop/
  plugin.js          single uncompiled ESM plugin file (no build step)
tests/
  smoke/             offline plugin.js harness (stubbed SDK/react)   — frontend agent
  test_plugin_api.py backend pytest                                  — backend agent
README.md, LICENSE   (owner: orchestrator)
```

## Reference sources (READ-ONLY, do not modify)

The closed PR lives at `C:/w/action-center-pr/` (branch `feat/action-center`, based on
today's upstream main). Port LOGIC from these, adapted to this repo's contracts:

- Backend logic to port: `tui_gateway/methods_inbox.py`,
  `tui_gateway/methods_inbox_requests.py`, and the direct-state pause/resume path in
  `tui_gateway/methods_session_control.py` (`_execute_direct_state_action`,
  `_direct_state_control`, `_DIRECT_STATE_ACTIONS`).
- Payload shapes (authoritative field names): port the same dicts those files return.
  Desktop-side TypeScript types mirror them in
  `apps/shared/src/gateway-contract.generated.ts` (search `inbox.`).
- Frontend logic/UI to port: `apps/desktop/src/app/shell/inbox/*` (`inbox-panel.tsx`,
  `approval-card.tsx`, `clarify-card.tsx`, `automation-controls.tsx`,
  `inbox-statusbar-chip.tsx`, `use-inbox.ts`) and `apps/desktop/src/store/inbox.ts`.
- Behavior tests to learn from (do not copy): `tests/tui_gateway/test_inbox.py`,
  `tests/tui_gateway/test_inbox_requests.py`, `tests/tui_gateway/test_session_control.py`
  and `apps/desktop/e2e/inbox-*.spec.ts`.
- Proven plugin patterns: `~/AppData/Local/hermes/plugins/local-engines/` (backend
  `dashboard/plugin_api.py` style) and
  `~/AppData/Local/hermes/desktop-plugins/local-engines/plugin.js` +
  `~/AppData/Local/hermes/plugins/local-engines/tests/smoke/` (smoke harness style).
- SDK contract: `C:/w/baseline-main/website/docs/developer-guide/desktop-plugin-sdk.md`
  (the ONLY allowed APIs).

## REST contract (frontend and backend must agree exactly)

All routes are under the plugin namespace (frontend calls via `ctx.rest('/route')`).

- `GET /summary?profile=` → same shape as the core `inbox.list` result's `inbox` object:
  `{ badge, counts {needs_you, running, waiting, scheduled, total}, coverage {profile,
  scanned_sessions, partial, errors[], connection_scope?}, items[] }` — items carry
  `session_key, profile, title, source, cwd, lanes[], categories[], updated_at,
  needs_you_count, pending_count, expired_request_count` and nullable `goal`, `loop`,
  `heartbeat` snapshot objects (see core for exact snapshot fields).
- `GET /details?session_key=&profile=` → same shape as core `inbox.requests`:
  `{ coverage{...}, sessions: [ { approvals[], clarifications[], expired_requests[],
  live_session_ids[], context {excerpt | named-absence} } ] }`. Approval entries carry
  `request_id, command, description, choices[], allow_session?, allow_permanent?`;
  clarify entries carry `request_id, question, choices[], multi_select?, batch info`.
- `POST /respond` `{ request_id, choice, session_key, profile }` → approve/deny
  (choice ∈ once|session|always|deny as offered). Mirrors core `inbox.respond`.
- `POST /answer` `{ request_id, answer, session_key, profile }` → clarify answer
  (string or JSON array for multi-select; batch = one call per question, mirroring the
  core batch flow incl. any lock call the core makes).
- `POST /control` `{ action, session_key, live_session_id?, profile }` → pause/resume
  for goal|loop|heartbeat, stored sessions included (port the direct-state path; keep
  the deny-list + session-row gate and the same refusal messages).
- `POST /redo` `{ request_id, session_key, profile }` → re-raise an expired request.
- `POST /dismiss` `{ request_id, session_key, profile }` → clear an expired record.
- Errors: mirror the core's named errors (`{detail: "..."}`-style for FastAPI) with the
  same guard semantics; never invent an all-clear.

## Backend rules (plugin_api.py)

- Runs inside the gateway process; importing hermes-agent code is expected and documented.
  Import `tui_gateway` internals lazily inside handlers so import order never breaks the
  backend, and read runtime state the same way the core methods do (same locks:
  `_sessions_lock`, same managers `hermes_cli.goals/loops/heartbeat`).
- Port the LOGIC near-verbatim (guards, deny-list via the canonical
  `INTERNAL_LISTING_SOURCES`, `_inbox_home_key` profile normalization, expired-record
  persistence, tolerant coverage errors instead of 500s).
- Thread-safety: same discipline as core (no mutation while iterating; use the locks).
- Testability: `tests/test_plugin_api.py` imports the router and calls route functions
  directly with monkeypatched gateway state, mirroring the core tests' fixtures
  (temp HERMES_HOME, a real `state.db` row, fake `_sessions` entries). Python:
  `C:/w/hermes-agent-inbox/.inbox-work/.venv/Scripts/python.exe -m pytest`.

## Frontend rules (desktop/plugin.js)

- ONE uncompiled ESM file. Allowed imports ONLY: `@hermes/plugin-sdk`, `react`,
  `react/jsx-runtime`. No JSX syntax — `jsx()`/`jsxs()` calls. No other specifiers.
- Register (all contributions): status chip (`statusBar.right`) showing "Action Center"
  + need-attention count; a full page at route `/action-center` (the panel, master-detail
  like core); sidebar nav row; palette commands (open, refresh); optional keybind.
  `defaultEnabled: false` (opt-in).
- Chip click opens the panel: use `host.openWorkspace` when available (docks a tab in the
  main workspace), else `host.navigate('/action-center')`. Feature-detect.
- Data: React Query against `ctx.rest` (`refetchInterval` ≈ 5s, never faster; plus
  `host.onEvent('*')`-driven invalidation for message/session events). Invalidate after
  every mutation. No hand-rolled poll loops.
- UI: use ONLY the SDK's exported kit + theme vars (`var(--ui-*)`, spacing via classes)
  — never hardcoded colors. Native look and feel. Keep the core's information hierarchy:
  rail (sections + live counts + search), row list, detail with context excerpt,
  approval card (disabled-while-sending, error surface, restricted states), clarify card
  (A/B/C… lettering, type-your-own row, multi-select, batch staging), automation sections
  (facts + Pause/Resume), expired card (outcome + Redo/Dismiss), loading/retry/empty states.
- Helpers exist in the SDK for time formatting (`relativeTime` etc.) — use them.
- Smoke harness: `tests/smoke/` with stubbed `@hermes/plugin-sdk` + `react` +
  `react/jsx-runtime` node_modules (mirror local-engines' smoke), a `run.mjs` that loads
  the plugin, runs `register(ctx)`, walks every render with sample data (success AND
  empty AND error states), and asserts contribution shapes. Run with plain `node`.

## Acceptance

- Backend: pytest green (aggregation, guards, respond, answer, control incl. stored
  sessions, redo/dismiss, error paths).
- Frontend: smoke harness green (no render errors; contributions shaped; both data
  states walk cleanly).
- Nothing outside this repo is modified.
- Report: files written, exact commands run, observed results, anything unverified.
