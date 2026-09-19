# Action Center

Answer approvals and questions, inspect recent context, and pause or resume automation without opening each session. Action Center is an opt-in Hermes Desktop plugin with a single-file UI and a gateway-side Python router. No core patches or build step are required.

## Features

- Approve once, approve for the session, always allow, or deny. Only choices offered by the request are shown and accepted.
- Answer single-choice, multi-select, free-text, and batch questions. Batch answers stay local until submitted.
- Read a bounded, redacted excerpt of recent messages, with a link to the full chat.
- Search session titles, keys and paths across sections, with live counts.
- Pause or resume goals, loops and heartbeats, including stored sessions with no open runtime.
- Review persisted expired approvals. Dismiss removes a record; Redo asks the running session to try again. Redo does not grant approval or execute a saved shell command directly.
- Use a compact layout in narrow panes. Below the usable minimum, the panel shows a width notice and an Expand control instead of squeezed session cards.

## Install a local release

The repository is not published yet. There is no working public install link in this release.

Extract the release ZIP. Choose the Hermes home belonging to the desktop/backend you intend to extend, then copy these files:

```text
Archive                                 Destination below HERMES_HOME
-------------------------------------   --------------------------------------------
action-center/desktop/plugin.js          desktop-plugins/action-center/plugin.js
action-center/dashboard/manifest.json    plugins/action-center/dashboard/manifest.json
action-center/dashboard/plugin_api.py    plugins/action-center/dashboard/plugin_api.py
```

1. Back up your configuration and any existing Action Center files.
2. Add `action-center` to the existing `plugins.enabled` list in that home's `config.yaml`. Preserve its other entries. An entry in `plugins.disabled` takes precedence, so remove that entry if you intend to enable this plugin.
3. Restart that backend. Python backend changes do not hot-reload.
4. Enable Action Center in Desktop's **Capabilities → Plugins**. The desktop half starts disabled.
5. Open the labeled Action Center status chip. The panel reports which profile was scanned and any coverage errors.

Both halves must be installed. A desktop file by itself cannot read the gateway queues. To disable, turn off the desktop plugin, remove its backend allow-list entry or add it to `plugins.disabled`, and restart the backend. Remove the copied files only after disabling. Persisted history is not deleted by uninstalling.

## Expiry history and coverage

The backend registers observer hooks on its first summary/detail read. It records authoritative timeout, notification-failure and cancellation outcomes for human-facing sessions in its launch profile. It does not replace the gateway's notification or settlement callback, and it does not guess that a disappearing request expired.

History covers only settlements observed while this gateway process and its observer were active. It cannot reconstruct events before activation, a process crash, or another gateway's requests. When you view a different profile through the same gateway, existing records remain readable, but new capture belongs to that profile's own gateway process. The panel labels observed-only history; unavailable capture and read/write failures are surfaced rather than reported as an all-clear.

Compatible records already persisted by a supporting gateway remain usable. Stored-session pause/resume changes persisted automation state; it does not start a session. Open a session before using Redo. Normal approval checks still apply when the agent retries.

## Compatibility and verification

Version 0.1.0 was tested on Windows with a packaged Hermes Desktop test build and both the unpatched baseline backend and the feature-reference checkout. See `RELEASE.md` in the source repository for exact revisions, commands, results and test boundaries. This is a local release; publishing and installation into a daily home are separate actions.

The UI imports only `@hermes/plugin-sdk`, `react`, and `react/jsx-runtime`. Its API calls stay under `/api/plugins/action-center/`. Optional desktop host APIs are feature-detected. Backend internals are imported lazily; missing capabilities produce visible errors or coverage warnings.

## Development

```text
node tests/smoke/run.mjs
python -m unittest discover -s tests -p test_release_package.py -v
python scripts/package_release.py
```

Run the backend suite from a compatible Hermes checkout, using an interpreter with that checkout's dependencies and an absolute path to this repository's test file:

```text
python -m pytest -p no:cacheprovider /absolute/path/to/action-center/tests/test_plugin_api.py
```

Set `PYTHONDONTWRITEBYTECODE=1` when verifying read-only checkouts. The smoke harness rebuilds its synthetic SDK/React modules from tracked stubs; no npm install is required. `tests/live/README.md` documents the packaged-app acceptance runner and its isolated, credential-free fixtures.

The package builder includes only the three plugin files listed above, README/LICENSE, and a SHA-256 inventory. Test plugins, local receipts, credentials and Git metadata are excluded. Packaging alone does not imply test acceptance.

## License

MIT
