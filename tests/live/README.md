# Live acceptance harness

`node tests/live/e2e.cjs` launches a separate packaged Hermes app, creates a fresh agent home and desktop preference store below the authorized test root, installs the current plugin bytes, and closes its own app in `finally`. It never opens a real session, creates an agent, or runs a command. The child environment is allowlisted; it does not inherit API keys or dashboard passwords.

## Configuration

The checked-in defaults target Jerry's isolated Windows test build. Override paths with these environment variables for another authorized test installation:

| Variable | Purpose |
| --- | --- |
| `AC_TEST_ROOT` | Parent directory for fresh `ac-acceptance-*` fixture homes and receipts |
| `AC_EXE` | Packaged Hermes executable |
| `AC_HERMES_ROOT` | Unpatched Hermes source root for its backend |
| `AC_PYTHON` | Interpreter containing Hermes dependencies |
| `AC_PLAYWRIGHT` | Path to an installed Playwright Node module |

`AC_DESKTOP_SOURCE` (file) and `AC_BACKEND_SOURCE` (dashboard directory) are optional frozen-source overrides for developing the runner while other workers edit the candidate. **Release acceptance runs must omit them** and compare the receipt's source hashes with the release candidate.

The runner waits for the credential-free onboarding option, then enables the disk plugin only in its own desktop state. The test binary also contains a reference Action Center: the runner selects the labeled disk-plugin chip and requires the plugin's unique `data-action-center="page"` marker, not just matching text.

## What is real

- Production `desktop/plugin.js`, SDK components, `ctx.rest`, Electron IPC and authenticated backend HTTP routing.
- Real gateway approval queues, `ServerRequest` objects, SQLite sessions, automation manager persistence, and plugin route handlers.
- Approve once, deny, restricted approval options; single/multi/batch question staging and submission; stored goal/loop/heartbeat pause/resume; Redo refusal and visible error; Dismiss; unknown-profile refusal.
- Expiry capture through the real gateway notification-failure lifecycle (not a pre-seeded expiry record).
- Native-window resizing plus **controlled container-width** tests. The latter changes only the mounted page root's CSS width to exercise ResizeObserver at 900/678/214/88 pixels; it is labeled separately from natural docking.

## What is synthetic or intercepted

All sessions, requests, commands and automation prompts are labeled synthetic. They are inserted by the separate `ac-fixtures` test-only plugin. Its endpoints refuse unless the home is inside `AC_TEST_ROOT` and contains the explicit synthetic marker.

Expired-record Dismiss and Redo tests begin with a pre-seeded record. The separate capture scenario does not. Redo's successful dispatch is intercepted at `prompt.submit` **only for its synthetic session**, recording the exact submission and returning queued; no LLM or real command execution is claimed. The production Action Center has no test endpoints or test-mode network behavior.

The test plugin is not included in release packages. Closing the disposable backend discards its in-memory fixtures. The fresh home is retained for diagnosis, with no credentials copied into it.

## Evidence

Each run retains `receipt.json`, source hashes, named checks, failures, and screenshots inside its fresh run directory. An exit code of zero requires all assertions, including all target container widths; partial runs are not acceptance. Screenshots contain synthetic data and are diagnostic evidence, not hand-built marketing mockups.

The older `probe.cjs` covers only empty search/layout and uses the original isolated test home. It does not substitute for this acceptance runner.
