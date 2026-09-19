# Action Center review and repair report

## Verdict

**Reviewed and materially hardened; automated suites pass. Not full product acceptance.** The remaining release limitations are automatic expired-request capture on an unpatched gateway, narrow/docked layout clipping, and the absence of a live end-to-end pending-request interaction run. Nothing was pushed.

Baseline: `02be0c4` (initial port preserved before edits). Work branch: `astra/review-polish`.

## Prioritized findings and repairs

### P1 — request and session authority

- **Clarify answers could resolve a globally known request ID without proving session ownership.** `/answer` now requires `session_key`, a human-facing persisted row in the addressed profile, and a request visible through that session's local/compute-host request snapshots. Wrong-session/foreign requests are not resolved. Only `clarify` methods qualify: the answer route cannot respond to a secret/sudo/other server request. Failed ownership reads return a sanitized 503, not a misleading expiry result. Evidence: `TestTrustBoundary`, `test_answer_refuses_non_clarify_request_in_owned_session`, and `test_answer_ownership_read_failure_is_not_expiry`.
- **Live mutations bypassed the persisted-row/internal-source gate.** All five mutation routes now require a human-facing durable session row. Live runtime IDs are bound to both the durable session and profile; stale/foreign IDs cannot dispatch another session's control action. Evidence: `TestTrustBoundary` covers unlisted/deny-listed routes, foreign request IDs, batch locks, and compute-host mirrors; existing control regression tests cover stale/finalized/cross-profile bindings and valid dispatch.
- **Approval responses accepted choices the request did not offer.** The backend now validates the core-computed offered choices before resolving the approval queue, including smart-denied and permanent-approval restrictions. Queue read failure is fail-closed. Tests assert refusals leave the queue intact, including HTTP transport.

### P1 — persistence and truthful coverage

- **Named-profile Redo/Dismiss used read-only handles.** They now use the existing gateway `writer=True` seam, sharing one handle for lookup and clear. Named-profile tests prove only the addressed store changes. Storage failure becomes a named 503.
- **Failed expiry scans looked like empty data.** Summary/details now expose these failures in coverage; summary shows an error badge. Redo returns a named 503 on a failed scan rather than claiming no record exists. Clean empty data remains a valid empty result. Evidence: `TestExpiredScanFailure`.
- **Exception logging could reveal sensitive exception text.** Backend logs now retain exception class only, without traceback or caller-controlled request IDs. A secret-canary regression exercises all logging paths and asserts the canary and `exc_info` are absent.

### P2 — robustness and UI

- Guarded lazy imports/read-route errors now degrade through coverage or sanitized named errors, not unhandled import failures.
- Approval/clarify mutations now invalidate summary/details through a single owner. Mutation cards pin their profile; batch submission rechecks between awaited question posts and stops on a switch.
- Automation and expired-request controls now have synchronous ref locks, preventing two same-tick clicks from submitting twice.
- Clarify fields use the SDK `Input`, with accessible labels and focus treatment. Rail buttons expose pressed state; batch cards show answer progress.
- Rail width switches at the SDK PanelBody's 760 CSS-pixel breakpoint instead of the different sidebar-collapse breakpoint. Live checks confirmed row/column switching; this is not a guarantee against severe host-pane squeezing.
- The original smoke harness could not reach the interaction buttons and leaked hook state between scenarios/sibling cards. It now walks components using path-scoped hook state, stable refs, and rerendered interactions. Tracked stubs bootstrap the ignored `node_modules`, so a clean checkout can run offline.

## Files and ownership

- `dashboard/plugin_api.py`: authority gates, offered-choice validation, writable expiry handling, truthful coverage, sanitized failures/logs.
- `tests/test_plugin_api.py`: backend regression suite, now 138 tests.
- `desktop/plugin.js`: mutation/profile/submit guards, SDK input/accessibility polish, responsive rail, cache invalidation, stable live-probe page marker. Remains one uncompiled ESM file importing only the SDK, React, and React JSX runtime.
- `tests/smoke/run.mjs`, `walker.mjs`, `fixtures.mjs`, `stubs/**`: reproducible behavioral smoke harness; `plugin.js` is its generated copy. Removed the obsolete debug harness.
- `tests/live/probe.cjs` and screenshots: isolated packaged-app probe and real empty-search captures. This machine-specific development probe enables Action Center only in the isolated test desktop state, resizes the test window, and closes its own app. It does not submit session actions.
- `README.md`: replaced the unsupported blanket-green status and documented the expiry capture/distribution limitations.

## Exact commands and observed results

### Baseline

From the plugin repo:

```text
node tests/smoke/run.mjs
```

Exit 1, `SMOKE FAIL`: rendering/contribution assertions passed, but 15 interaction assertions failed; no REST actions were observed. This was a combination of an ineffective interaction harness and actual uncovered invalidation issues—not evidence that every action route was broken.

From `C:/w/action-center-pr`:

```text
PYTHONDONTWRITEBYTECODE=1 C:/w/hermes-agent-inbox/.inbox-work/.venv/Scripts/python.exe -m pytest -p no:cacheprovider C:/w/hermes-desktop-action-center/tests/test_plugin_api.py
```

Baseline: **94 passed, 1 warning in 11.91s**.

### Final parent reruns

The same backend command, with cwd `C:/w/action-center-pr`: **138 passed, 1 warning in 15.25s**.

The same backend command, with cwd `C:/w/baseline-main`: **138 passed, 1 warning in 16.17s**. This additionally checks the suite against baseline main rather than only the reference PR checkout.

The warning in both is Starlette TestClient's deprecated `anyio.abc.BlockingPortal` alias. No test failure was suppressed. The absolute test path is necessary because cwd is a core checkout, not the plugin repo. Bytecode and pytest cache writes were disabled for these runs.

```text
node tests/smoke/run.mjs
```

**83 PASS, 0 FAIL, SMOKE PASS**. A separate disposable copy inside `tests/live/`, excluding all smoke `node_modules` and the generated plugin copy, also returned **83 PASS, 0 FAIL** and was removed afterward. This proves the tracked stub bootstrap works without the original ignored dependencies.

```text
git diff --check
```

Exit 0; no whitespace errors. Git reported Windows LF/CRLF normalization warnings only.

## Live verification — scope and observed output

Only the authorized isolated home was deployed:

`C:/Users/jerry/AppData/Local/HermesInlineMainTestBuild/agent-home`

Desktop plugin, backend module, and manifest were copied there and SHA-256 byte equality was confirmed. `plugins.enabled` already included `action-center`; no configuration edit was needed. The plugin was enabled in that test desktop's own preference store.

```text
node tests/live/probe.cjs
```

The probe launched packaged renderer `50503c8fb9` using the test source root and test interpreter, creating a fresh backend process so deployed backend edits were loaded. It then closed its own app. The renderer mounted the uniquely marked **plugin** page, not merely the similarly named built-in core panel. Search and empty-result state were visible, no alert errors were reported, and the status chip measured approximately 94.35 × 20 CSS pixels.

At requested native widths 1280/700/500, Windows scaling produced actual renderer widths **1024/560/400** CSS pixels. Measured panel widths were **678/214/88**. Body direction changed from **row** to **column**, and rail width from **176** to the full narrow panel width. Root scrollWidth equaled clientWidth in each capture, but that alone does not prove readable content: visual inspection showed clipped text and interference from surrounding host panes at tiny available widths. The retained images are diagnostic evidence, not polished marketing previews.

Early live attempts correctly failed to find the plugin marker because its opt-in desktop switch was still off. After enabling the isolated test copy, it mounted. No live approvals, answers, Redos, or automation changes were submitted to real sessions; those are verified by synthetic fixtures/HTTP tests and the UI harness, not a complete live user-request lifecycle.

## Remaining gaps / proposed next steps

1. **P1 product completeness: automatic expiry capture.** The standalone plugin does not hook approval settlement. It reads compatible records written by a gateway carrying that support; unpatched main does not automatically provide that history. Do not market this as complete standalone timeout capture. A plugin-side alternative is an explicitly incomplete history of observed requests, but polling cannot reliably distinguish an answer from expiry. No core patch was made.
2. **P2 narrow/docked layout.** Real host panes can leave only 88 CSS pixels for this workspace. Rail stacking is verified, full usability under such compression is not. Next polish should respond to the actual container width and provide a compact rail/detail mode; no claim of complete narrow-width acceptance is made.
3. **Live action lifecycle remains unverified.** The packaged test covers load, read/search, geometry, empty state, and the chip—not actual pending approval/clarify/control submissions.
4. Compute-host response relay retains the host's own success semantics; passing synthetic tests is not a real remote-host integration test.
5. The dependency warning remains. No distribution remote was created and nothing was pushed.

## Work-process caveat

One UI worker accidentally restored the baseline over its own uncommitted UI changes during a falsifiability probe. It rebuilt the changes, and the parent independently reran the resulting 83-check suite, clean-copy smoke, diff checks, and live mount. Evidence above applies to the resulting files, not to the lost intermediate state. No baseline/core checkout was reset by the parent.
