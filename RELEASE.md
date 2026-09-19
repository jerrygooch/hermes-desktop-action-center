# Action Center 0.1.0 release verification

## Verdict

Ready for a local release with the scope documented in README. The earlier review's three open gates have been addressed: standalone observed-expiry capture, container-aware layout, and packaged-app interaction verification. Nothing has been pushed, published, or installed into the daily Hermes home.

Branch: `astra/review-polish`. Original baseline: `02be0c4`; previous review: `f8f64d6`.

## Changes since the first review

- `dashboard/plugin_api.py`: observes the supported pre/post approval hooks without replacing notification or settlement callbacks. Registration uses the dashboard-discovered identity and real enable/disable gates; the module directory must match. Registration is serialized, partially registered hooks are disposed on failure, and stale registrations recover on the next read. Answered settlements clear correlation state. Redacted records remain bounded. Capture failures and missing capability appear in coverage.
- `desktop/plugin.js`: each mounted panel measures its own container with ResizeObserver. A wrapper inside SDK PanelBody owns row/column layout, avoiding the SDK's viewport breakpoint trap. Compact panes get a collapsible section rail and wrapping header. Extremely narrow panes show a labeled Expand button. The header describes observed-only expiry history, and the zero-state chip no longer says “all quiet.”
- `tests/test_plugin_api.py`: observer and release-gate regressions, with an enabled user-plugin fixture rather than fabricated bundled trust. New regressions caught mismatched identity, leaked correlation, stale/disabled observers, concurrent registration, and silent capture-write failure.
- `tests/smoke/**`: measurements exercise the effect/ref/ResizeObserver path. No invented SDK state or production test-only width override remains. Tests cover visible recovery-button text and scoped history wording.
- `tests/live/e2e.cjs`, `tests/live/fixtures-plugin/**`: repeatable packaged-app tests using fresh synthetic sessions, real gateway queues and production routes. The test-only plugin cannot run outside its marked test home and is excluded from the release archive.
- `scripts/package_release.py`, `tests/test_release_package.py`: deterministic allowlisted ZIP, CRC/source-byte verification, SHA-256 inventory, and tests that reject invalid/missing inputs.
- `README.md`: local installation/removal instructions, accurate capture scope, and removal of the unpublished install link and stale screenshot reference. `.gitignore` excludes release output.

The first observer hardening attempt was not accepted: parent verification caught a syntax error and failing regressions. Those defects were repaired before the final runs below.

## Test receipts

### Backend

Command, run separately from each checkout listed below:

```text
PYTHONDONTWRITEBYTECODE=1 C:/w/hermes-agent-inbox/.inbox-work/.venv/Scripts/python.exe -m pytest -p no:cacheprovider C:/w/hermes-desktop-action-center/tests/test_plugin_api.py -q --tb=short
```

| Checkout | Revision | Observed result |
| --- | --- | --- |
| `C:/w/baseline-main` | `b1968795b2569d2f7a2fc541328399c952679c25` | 160 passed, 1 warning, 20.13s |
| `C:/w/action-center-pr` | `11e39ebe5f0f4e09c7e12f11927f40e35bf6000c` | 160 passed, 1 warning, 22.58s |

The warning is Starlette TestClient's deprecated `anyio.abc.BlockingPortal` alias. It was not suppressed. The live receipt also verifies that `tui_gateway.server.__file__` belongs to baseline-main, rather than relying on the interpreter's editable-install configuration.

### UI, packaging, whitespace

```text
node tests/smoke/run.mjs
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p test_release_package.py -v
git diff --check
```

Results: 107 smoke assertions passed, zero failures; 3 packaging tests passed; whitespace check exited 0. A disposable clean copy excluding smoke `node_modules` and its generated plugin copy also passed all 107 assertions. Git's LF/CRLF notices are normalization warnings, not test failures.

### Packaged-app acceptance

```text
node tests/live/e2e.cjs
```

Final runner: exit 0, 22 named checks, no uncaught renderer errors. Packaged renderer: `50503c8fb9`; backend: unpatched baseline-main.

Receipt and full-window screenshots:

```text
C:/Users/jerry/AppData/Local/HermesInlineMainTestBuild/ac-acceptance-1789844975735/
```

The receipt includes SHA-256 hashes of the deployed UI and backend. The runner verifies actual queue/DB state after UI actions: approve once, deny, restricted choices, single/multi/batch clarification, stored goal/loop/heartbeat pause and resume, Dismiss, Redo refusal, Redo dispatch, and automatic capture through a real notification-failure lifecycle. It also checks unknown-profile refusal.

Native window sizes were exercised separately from controlled container widths of 900, 678, 214 and 88 CSS pixels. The controlled tests change only the real mounted panel's CSS width to exercise ResizeObserver. Compact rail expansion/collapse, zero horizontal root overflow and the visible Expand button are asserted.

Full-window images confirm readable wrapping at 214px and an intact notice/button at 88px. Early element-only screenshots falsely clipped the left edge under Electron's 90% zoom; they were rejected as visual evidence. The final runner retains full-window captures instead. Matching shipping bytes were visually reviewed in the preceding run `ac-acceptance-1789844628653`.

## Boundaries and limitations

- Capture begins on the first summary/detail read and covers observed settlements in the gateway's launch profile. It cannot recover history from before activation, process crashes, or another gateway. A foreign-profile view reports capture unavailable for that profile instead of implying full coverage. This is an explicit product boundary, not complete historical capture.
- All live-test sessions and commands are synthetic. No LLM or shell command was executed. Successful Redo is verified through real UI/HTTP/persistence with `prompt.submit` intercepted only for its synthetic session. Real agent execution after the dispatch boundary was not exercised.
- Timeout/cancellation outcomes are covered by backend tests; the packaged-app producer test uses an actual notification failure. These are distinct evidence tiers.
- Compatibility was tested on the revisions above, not every Hermes release or operating system.
- No tracked files in either core checkout changed. Untracked scratch/cache directories were present at final inspection, including a Windows `%SystemDrive%` cache folder in baseline-main. The early test environment may have caused that cache; it was left untouched rather than deleting files outside the authorized write scope. The final runner preserves the Windows system-directory environment variables while excluding credentials.
- Test apps close normally. A process scan after the live checks found no Hermes/Python processes carrying the isolated test home's `HERMES_HOME`. The daily app was not stopped or reconfigured.

## Release artifact

Build with `python scripts/package_release.py`. The archive contains five allowlisted shipping files and `CONTENTS.sha256`; a separate `.zip.sha256` verifies the whole archive. Tests and fixture endpoints are absent. The archive is prepared locally; publication still requires Jerry's approval.
