// Offline smoke harness for the Action Center desktop plugin.
//
// Loads desktop/plugin.js with stubbed SDK/React modules (bootstrapped from the
// tracked sources in stubs/ so a clean checkout runs without preparation), runs
// register(ctx), then walks every registered render with (a) populated sample
// data, (b) empty data, (c) an error state, (d) partial coverage, (e) an
// expanded-row interaction walk and (f) mutation scripting that asserts the
// exact REST routes the SPEC defines. Hook state is reset between steps so no
// scenario leaks into the next.
//
// Run: node tests/smoke/run.mjs        (plain node, no dependencies)
import { readFileSync, writeFileSync, mkdirSync, rmSync, existsSync, cpSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const here = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(here, '..', '..')

// ── bootstrap: tracked stubs → node_modules (fresh-checkout reproducibility) ──
// node_modules/ is gitignored; the stubs live in tests/smoke/stubs/ (tracked)
// and are copied into node_modules before the plugin import resolves them.
for (const dir of ['@hermes/plugin-sdk', 'react']) {
  if (!existsSync(path.join(here, 'stubs', dir, 'package.json'))) {
    console.error(`bootstrap failed: tests/smoke/stubs/${dir} is missing`)
    process.exit(1)
  }
}
rmSync(path.join(here, 'node_modules'), { recursive: true, force: true })
for (const dir of ['@hermes/plugin-sdk', 'react']) {
  mkdirSync(path.join(here, 'node_modules', path.dirname(dir)), { recursive: true })
  cpSync(path.join(here, 'stubs', dir), path.join(here, 'node_modules', dir), { recursive: true })
}

// Import the plugin THROUGH the stub node_modules beside this file (a copy of
// desktop/plugin.js) so `@hermes/plugin-sdk` resolves to the stubs.
writeFileSync(path.join(here, 'plugin.js'), readFileSync(path.join(repoRoot, 'desktop', 'plugin.js'), 'utf8'))

const mod = await import('./plugin.js')
const plugin = mod.default
const acResponsive = mod.acResponsive

const { renderPass, invokeComponent, clickableByText, collectByType, collectBranchClickables, flattenTexts, childText } = await import('./walker.mjs')
const {
  SUMMARY_POPULATED, SUMMARY_EMPTY, SUMMARY_PARTIAL, DETAILS_POPULATED, DETAILS_RESTRICTED,
  SUMMARY_KEY_LIVE, DETAILS_KEY_LIVE, ITEMS
} = await import('./fixtures.mjs')

let failed = 0
const checks = []
const check = (name, ok, detail) => {
  checks.push([name, ok, detail])
  if (!ok) failed++
}

// Rail-posture helper for steps 6b/6d: re-renders the page and returns the rail
// element (key 'rail', matched by the plugin's own data hook since PanelBody is
// a Stub frag), or null when the rail is not rendered (compact/too-narrow
// bands).
const pageRailEl = () => {
  const root = page.render()
  const branch = invokeComponent(root.type, root.props, 'render[page-step6b]')
  const out = []
  const walk = node => {
    if (node == null || typeof node !== 'object') return
    if (Array.isArray(node)) return node.forEach(walk)
    if (node.__frag) return walk(node.children)
    if (node.__el) {
      if (node.props?.['data-ac-rail-panel'] === '') out.push(node)
      return walk(node.props?.children)
    }
  }
  walk(branch)
  return out[0] || null
}

const renderErrors = [] // [path, error]
const restCalls = [] // [{ path, opts }]

// ── ctx wiring ───────────────────────────────────────────────────────────────

function makeCtx() {
  const registered = []
  return {
    registered,
    source: 'plugin:action-center',
    register: c => registered.push(c),
    registerMany: cs => registered.push(...cs),
    storage: { get: (_k, d) => d, set() {}, remove() {} },
    rest: async (routePath, opts) => {
      restCalls.push({ path: routePath, opts })
      if (globalThis.__AC_REST) return globalThis.__AC_REST(routePath, opts)
      return {}
    },
    socket: () => () => {},
    i18n: { register() {}, t: k => k }
  }
}

const reactStub = await import('react')
const sdkStub = await import('@hermes/plugin-sdk')

// DOM shims for the plugin's real effect contract (the walker is not a
// browser): a `window` for the effect's resize listener and a ResizeObserver
// whose latest instance the harness fires after a simulated size change.
globalThis.window = globalThis.window || { addEventListener() {}, removeEventListener() {} }
globalThis.ResizeObserver = class {
  constructor(callback) { globalThis.__AC_LAST_OBSERVER = { callback, observed: null } }
  observe(node) { if (globalThis.__AC_LAST_OBSERVER) globalThis.__AC_LAST_OBSERVER.observed = node }
  disconnect() {}
}

// Fresh scenario: wipe injected data/errors/REST scripting/observations AND the
// react stub's hook slots, so each step's renders start from initial state.
function resetGlobals() {
  for (const key of ['__AC_DATA', '__AC_ERR', '__AC_NOTES', '__AC_ROWS', '__AC_EVENTS', '__AC_REST', '__AC_SESSIONS', '__AC_WORKSPACES', '__AC_NAVS', '__AC_INVALIDATIONS', '__AC_ERRORS']) {
    delete globalThis[key]
  }
  restCalls.length = 0
  renderErrors.length = 0
  reactStub.__resetForHarness()
  // The page remounts per scenario: clear the measured-width bookkeeping so a
  // fresh instance starts unmeasured (no cross-instance state to leak).
  reactStub.__clearRefsForHarness()
  sdkStub.host.state.profile.set('default')
}

const ctx = makeCtx()
try {
  plugin.register(ctx)
} catch (e) {
  renderErrors.push(['register', e])
}

const registered = ctx.registered
check('plugin id', plugin.id === 'action-center', plugin.id)
check('plugin name', plugin.name === 'Action Center', plugin.name)
check('defaultEnabled false (opt-in)', plugin.defaultEnabled === false, String(plugin.defaultEnabled))

const page = registered.find(r => r.id === 'page')
check('page area=routes', page?.area === 'routes', page?.area)
check('page data.path=/action-center', page?.data?.path === '/action-center', page?.data?.path)
check('page has render', typeof page?.render === 'function')

const nav = registered.find(r => r.id === 'nav')
check('nav area=sidebar-nav', nav?.area === 'sidebar-nav', nav?.area)
check('nav path', nav?.data?.path === '/action-center', nav?.data?.path)
check('nav label', nav?.data?.label === 'Action Center', nav?.data?.label)
check('nav codicon', typeof nav?.data?.codicon === 'string' && nav.data.codicon.length > 0, nav?.data?.codicon)

const chip = registered.find(r => r.id === 'chip')
check('chip area=statusBar.right', chip?.area === 'statusBar.right', chip?.area)
check('chip has render', typeof chip?.render === 'function')

const palette = registered.filter(r => r.area === 'palette')
check('palette open+refresh', palette.length >= 2, palette.map(p => p.id).join(','))
const paletteOpen = palette.find(p => p.id === 'open')
check('palette open run/label', typeof paletteOpen?.data?.run === 'function' && paletteOpen?.data?.label === 'Open Action Center', paletteOpen?.data?.label)
const paletteRefresh = palette.find(p => p.id === 'refresh')
check('palette refresh run/label', typeof paletteRefresh?.data?.run === 'function' && paletteRefresh?.data?.label === 'Refresh Action Center', paletteRefresh?.data?.label)
check('one gateway event subscription (*)', (globalThis.__AC_EVENTS || []).some(e => e.type === '*'))

// ── step 2: walk every render with POPULATED data ────────────────────────────

resetGlobals()
globalThis.__AC_DATA = { [SUMMARY_KEY_LIVE]: SUMMARY_POPULATED }
renderPass(chip, 'render:chip[populated]', renderErrors, 'chip-pop')
renderPass(page, 'render:page[populated]', renderErrors, 'page-pop')
check('populated walk: no render errors', renderErrors.length === 0, renderErrors.map(([p, e]) => `${p}: ${e?.message}`).join(' | '))

// ── step 3: EMPTY data walk ──────────────────────────────────────────────────

resetGlobals()
globalThis.__AC_DATA = { [SUMMARY_KEY_LIVE]: SUMMARY_EMPTY }
renderPass(chip, 'render:chip[empty]', renderErrors, 'chip-empty')
renderPass(page, 'render:page[empty]', renderErrors, 'page-empty')
check('empty walk: no render errors', renderErrors.length === 0, renderErrors.map(([p, e]) => `${p}: ${e?.message}`).join(' | '))

// ── step 4: ERROR state walk (summary fails) ─────────────────────────────────

resetGlobals()
globalThis.__AC_ERR = { [SUMMARY_KEY_LIVE]: { detail: 'inbox aggregation unavailable' } }
renderPass(chip, 'render:chip[error]', renderErrors, 'chip-err')
renderPass(page, 'render:page[error]', renderErrors, 'page-err')
check('error walk: no render errors', renderErrors.length === 0, renderErrors.map(([p, e]) => `${p}: ${e?.message}`).join(' | '))

// ── step 5: PARTIAL coverage walk (error banner) ─────────────────────────────

resetGlobals()
globalThis.__AC_DATA = { [SUMMARY_KEY_LIVE]: SUMMARY_PARTIAL }
renderPass(page, 'render:page[partial]', renderErrors, 'page-partial')
check('partial walk: no render errors', renderErrors.length === 0, renderErrors.map(([p, e]) => `${p}: ${e?.message}`).join(' | '))

// ── step 6: expanded-row walk (details populated) ────────────────────────────

resetGlobals()
globalThis.__AC_DATA = {
  [SUMMARY_KEY_LIVE]: SUMMARY_POPULATED,
  [DETAILS_KEY_LIVE('gallery-goals')]: DETAILS_POPULATED,
  [DETAILS_KEY_LIVE('gallery-background_tasks')]: DETAILS_RESTRICTED
}
const collapsed = renderPass(page, 'render:page[collapsed]', renderErrors, 'page-step6')
const rowsCollapsed = collapsed.rows
const goalsRow = rowsCollapsed.find(r => r.rowKey === 'gallery-goals')
check('row list renders goals row', Boolean(goalsRow))
check('row meta line', Boolean(goalsRow && String(goalsRow.meta).includes('Needs you')), goalsRow?.meta)
check('row title', goalsRow?.title === 'Deploy pipeline', goalsRow?.title)
check('row list carries every session key', ['gallery-goals', 'gallery-loops', 'gallery-heartbeats', 'gallery-subagents', 'gallery-background_tasks', 'gallery-other']
  .every(key => rowsCollapsed.some(r => r.rowKey === key)), rowsCollapsed.map(r => r.rowKey).join(','))

if (goalsRow) {
  await goalsRow.onSelect()
  renderPass(page, 'render:page[expanded-goals]', renderErrors, 'page-step6')
}
check('expanded walk: no render errors', renderErrors.length === 0, renderErrors.map(([p, e]) => `${p}: ${e?.message}`).join(' | '))
{
  // The expanded detail shows approval + clarify + expired cards and the
  // context excerpt; assert through a fresh pass over the same scenario.
  const expanded = renderPass(page, 'render:page[expanded-goals]', renderErrors, 'page-step6')
  const labels = expanded.clickables.map(b => childText(b.props.children)).filter(Boolean)
  check('approval card offers Approve once', labels.includes('Approve once'), labels.join('|'))
  check('approval card offers Approve for session', labels.includes('Approve for session'), labels.join('|'))
  check('approval card offers Always allow', labels.includes('Always allow'), labels.join('|'))
  check('approval card offers Deny', labels.includes('Deny'), labels.join('|'))
  check('clarify single offers Submit', labels.includes('Submit'), labels.join('|'))
  check('clarify batch offers Submit answers', labels.includes('Submit answers'), labels.join('|'))
  check('expired card offers Redo + Dismiss', labels.includes('Redo') && labels.includes('Dismiss'), labels.join('|'))
  check('open full chat button present', labels.includes('Open full chat'), labels.join('|'))
  check('automation Pause/Resume present', labels.some(l => /^(Pause|Resume) (goal|loop|heartbeat)$/.test(l)), labels.join('|'))
}

// Batch staging progress surfaces (polish regression): scan page text.
{
  const expanded = renderPass(page, 'render:page[expanded-goals]', renderErrors, 'page-step6')
  check('batch staging progress label', expanded.texts.some(t => /^\d+ of \d+ answered$/.test(t)), expanded.texts.filter(t => t.includes('answered')).join(' | '))
}

// ── step 6b (NEW regression): responsive posture at MEASURED CONTAINER widths ─
// The docked panel is a container, not the viewport: CSS viewport 1024 →
// container ~678px, 560 → ~214px, 400 → ~88px (observed live). PanelBody's own
// `min-[47.5rem]:flex-row` split reads the VIEWPORT, so a wide viewport with a
// narrow container would still split — and vice versa. The plugin therefore
// measures its own body with a ResizeObserver (useMeasuredContainer returns
// [ref, width]; null width until the real effect measures) and controls the
// ACTUAL body direction itself via a measured split wrapper: row only when the
// measured width reaches RAIL_STACK_MIN_PX. Contract (acResponsive):
//   ≤RAIL_COMPACT_MAX (≤420):    rail becomes a horizontal compact strip
//                                (collapsible via the SDK Button); the full
//                                11rem rail must NEVER render — it would
//                                starve the detail.
//   RAIL_STACK_MIN (420 < w < 760): stacked rail above the list.
//   ≥RAIL_STACK_MIN (≥760):      11rem side rail beside the list.
//   <RAIL_MIN (min-width):       honest "too narrow to use" affordance with an
//                                expand action instead of silently clipping at
//                                absurd widths (live showed 88px).
// The harness simulates the real DOM contract: the measured node gets
// offsetWidth from a stub node; the ref reaches the effect; the effect's
// measure callback re-renders with the new width.

resetGlobals()
globalThis.__AC_DATA = { [SUMMARY_KEY_LIVE]: SUMMARY_POPULATED }
{
  // Real-effect driver: render → flush the mount effect (registers the RO and
  // takes the first measure), assign the measured node to the body ref the way
  // a real commit would, then fire the ResizeObserver — the same path a real
  // browser takes (effect → RO fires → setState). No test-only production
  // hooks: this drives the plugin's actual useRef/useEffect/ResizeObserver.
  const mountMeasure = async width => {
    renderPass(page, 'render:page[posture]', renderErrors, 'page-step6b')
    reactStub.__flushEffectsForHarness()
    const slot = reactStub.__lastRefForHarness('render[page-step6b]>ActionCenterPage#0')
    if (slot == null) return null
    slot.current = { offsetWidth: width }
    reactStub.__runObserverForHarness()
    reactStub.__flushEffectsForHarness()
    return renderPass(page, 'render:page[posture]', renderErrors, 'page-step6b')
  }

  // Split-wrapper helper: re-renders the page and returns the measured split
  // wrapper element (key 'ac-split', matched by data-ac-split), or null.
  const splitEl = () => {
    const root = page.render()
    const branch = invokeComponent(root.type, root.props, 'render[page-step6b]')
    const out = []
    const walk = node => {
      if (node == null || typeof node !== 'object') return
      if (Array.isArray(node)) return node.forEach(walk)
      if (node.__frag) return walk(node.children)
      if (node.__el) {
        if (node.props?.['data-ac-split'] === '') out.push(node)
        return walk(node.props?.children)
      }
    }
    walk(branch)
    return out[0] || null
  }

  const WIDE = { width: 1280, height: 800, narrow: false }
  const NARROW = { width: 560, height: 700, narrow: true } // live: ~214px container
  const TINY = { width: 400, height: 700, narrow: true } // live: ~88px container
  // The viewport is passed EXPLICITLY per band so the discriminating cases are
  // real: e.g. measured 678px under a WIDE viewport is the case PanelBody's
  // own min-[47.5rem] media query gets wrong.
  const measure = async (width, vp) => {
    sdkStub.host.state.viewport.set(vp)
    const pass = await mountMeasure(width)
    return pass
  }

  // WIDE: 1024px measured container (wide viewport) → 11rem side rail, row
  // split, rows live.
  {
    const wide = await measure(1024, WIDE)
    const wideRail = splitEl()?.props?.children?.find(c => c?.props?.['data-ac-rail-panel'] === '')
    check('wide container: split wrapper renders in row direction', splitEl()?.props?.style?.flexDirection === 'row', JSON.stringify(splitEl()?.props?.style))
    check('wide container renders the section rail', wide.texts.includes('All sessions'), wide.texts.join(' | '))
    check('wide container keeps the 11rem side rail', wideRail?.props?.style?.width === '11rem', JSON.stringify(wideRail?.props?.style))
    check('page body carries the measured-container contract', Boolean(splitEl()), 'split wrapper found')
    check('wide container: PanelBody is NOT the split owner', Boolean(splitEl()?.props?.style?.flexDirection === 'row' && splitEl()?.props?.style?.width === '100%'), JSON.stringify(splitEl()?.props?.style))
  }

  // The 678px container under a WIDE VIEWPORT (live 1024px CSS viewport): this
  // is the case a viewport-driven posture gets wrong — the page body must
  // still STACK (measured 678 < 760) even though PanelBody's own min-[47.5rem]
  // media query would split. 678px container stacks the rail, rows stay live.
  {
    const at678 = await measure(678, WIDE) // viewport stays WIDE on purpose
    const rail = splitEl()?.props?.children?.find(c => c?.props?.['data-ac-rail-panel'] === '')
    check('stacked band: container 678px under wide viewport stacks the rail (measured, not viewport)', splitEl()?.props?.style?.flexDirection === 'column' && rail?.props?.style?.width === '100%', JSON.stringify({ split: splitEl()?.props?.style, rail: rail?.props?.style }))
    check('stacked band: container 678px under wide viewport still renders rows', at678.rows.some(r => r.rowKey === 'gallery-goals' && r.title === 'Deploy pipeline'), JSON.stringify(at678.rows.map(r => r.rowKey)))
    check('stacked band: 700px measured container stacks too', (await measure(700, WIDE)) && splitEl()?.props?.style?.flexDirection === 'column', JSON.stringify(splitEl()?.props?.style))
  }

  // Compact band: 214px container (narrow viewport). The full 11rem rail column
  // MUST NOT render (it would starve the detail); instead a collapsible
  // compact strip (SDK Button toggle + icon rows) shares the row with the
  // list. Rows stay reachable.
  {
    const compact = await measure(214, NARROW)
    const rail = splitEl()?.props?.children?.find(c => c?.props?.['data-ac-rail-panel'] === '')
    check('compact container drops the full rail column (214px)', rail === undefined || rail?.props?.style?.width === 'auto', JSON.stringify(rail?.props?.style))
    check('compact container never reserves the 11rem column', rail?.props?.style?.width !== '11rem', JSON.stringify(rail?.props?.style))
    check('compact container offers an accessible rail toggle', compact.clickables.some(b => b.props?.['data-ac-rail-toggle'] != null && typeof b.props?.['aria-expanded'] === 'boolean' && typeof b.props?.['aria-label'] === 'string' && b.props['aria-label'].includes('section')), compact.clickables.map(b => b.props?.['aria-label']).join(' | '))
    {
      // The toggle must actually flip: collapsed default → expanded → collapsed.
      const toggle = compact.clickables.find(b => b.props?.['data-ac-rail-toggle'] != null)
      if (toggle) {
        await toggle.props.onClick()
        const opened = renderPass(page, 'render:page[posture]', renderErrors, 'page-step6b')
        const openToggle = opened.clickables.find(b => b.props?.['data-ac-rail-toggle'] != null)
        check('compact rail toggle expands (aria-expanded → true, rows appear)', openToggle?.props?.['aria-expanded'] === true && opened.clickables.some(b => b.props?.['data-ac-rail'] != null), JSON.stringify({ expanded: openToggle?.props?.['aria-expanded'] }))
        if (openToggle) {
          await openToggle.props.onClick()
          const closed = renderPass(page, 'render:page[posture]', renderErrors, 'page-step6b')
          const closedToggle = closed.clickables.find(b => b.props?.['data-ac-rail-toggle'] != null)
          check('compact rail toggle collapses again (aria-expanded → false)', closedToggle?.props?.['aria-expanded'] === false && !closed.clickables.some(b => b.props?.['data-ac-rail'] != null), JSON.stringify({ expanded: closedToggle?.props?.['aria-expanded'] }))
        }
      } else {
        check('compact rail toggle expands (aria-expanded → true, rows appear)', false, 'no toggle found')
      }
      // Reset the strip to collapsed for the bands below.
      const resetToggle = renderPass(page, 'render:page[posture]', renderErrors, 'page-step6b').clickables.find(b => b.props?.['data-ac-rail-toggle'] != null)
      if (resetToggle && resetToggle.props?.['aria-expanded'] === true) await resetToggle.props.onClick()
      renderPass(page, 'render:page[posture]', renderErrors, 'page-step6b')
    }
    check('compact container keeps session rows reachable', compact.rows.some(r => r.rowKey === 'gallery-goals' && r.title === 'Deploy pipeline'), JSON.stringify(compact.rows.map(r => r.rowKey)))
    {
      const compactRow = compact.rows.find(r => r.rowKey === 'gallery-goals')
      if (compactRow) await compactRow.onSelect()
      const afterExpand = renderPass(page, 'render:page[posture]', renderErrors, 'page-step6b')
      check('compact container keeps detail content after expansion', Boolean(compactRow) && afterExpand.texts.some(t => String(t).includes('Open full chat')), 'row expansion walk')
      if (compactRow) await compactRow.onSelect() // collapse again for the next band
      renderPass(page, 'render:page[posture]', renderErrors, 'page-step6b')
    }
  }

  // Absurd width (88px, tiny viewport): no silently clipped UI — an honest
  // minimum-width notice with an expand affordance instead.
  {
    const tiny = await measure(88, TINY)
    check('88px container shows the too-narrow notice (honest, not clipped)', tiny.texts.some(t => String(t).includes('narrow to use')), tiny.texts.join(' | '))
    check('88px container offers an expand affordance', tiny.clickables.some(b => String(b.props?.['aria-label'] || childText(b.props?.children)).includes('expand')), tiny.clickables.map(b => b.props?.['aria-label'] || childText(b.props?.children)).join(' | '))
    check('tiny expand button has visible text', tiny.clickables.some(b => childText(b.props?.children) === 'Expand'))
    check('88px container does not render the full rail or rows', !tiny.texts.includes('All sessions') && tiny.rows.length === 0, JSON.stringify(tiny.rows.map(r => r.rowKey)))
  }

  // Container-width plumbing sanity: the exported constants.
  check('acResponsive constants exported', acResponsive && typeof acResponsive === 'object'
    && typeof acResponsive.RAIL_STACK_MIN_PX === 'number'
    && typeof acResponsive.RAIL_COMPACT_MAX_PX === 'number'
    && typeof acResponsive.RAIL_MIN_PX === 'number', String(acResponsive && Object.keys(acResponsive)))
  check('acResponsive band order is coherent (compact < stack < sane min)', Boolean(acResponsive) && acResponsive.RAIL_COMPACT_MAX_PX < acResponsive.RAIL_STACK_MIN_PX && acResponsive.RAIL_MIN_PX <= acResponsive.RAIL_COMPACT_MAX_PX, JSON.stringify(acResponsive ? { compact: acResponsive.RAIL_COMPACT_MAX_PX, stack: acResponsive.RAIL_STACK_MIN_PX, min: acResponsive.RAIL_MIN_PX } : acResponsive))
  check('openWorkspace minWidth is reasonable (not viewport-derived)', Boolean(acResponsive) && acResponsive.OPEN_MIN_WIDTH_PX >= 320 && acResponsive.OPEN_MIN_WIDTH_PX <= 560, String(acResponsive?.OPEN_MIN_WIDTH_PX))

  // The measured posture must survive a scenario reset (no cross-instance
  // state): a fresh page instance starts UNMEASURED (stacked), then remeasures.
  {
    resetGlobals()
    globalThis.__AC_DATA = { [SUMMARY_KEY_LIVE]: SUMMARY_POPULATED }
    const fresh = await measure(1024, WIDE)
    check('fresh instance re-measures after reset (no stale shared width)', splitEl()?.props?.style?.flexDirection === 'row', JSON.stringify(splitEl()?.props?.style))
    void fresh
  }

  // Keyboard/focus hygiene on the rail rows (they are raw buttons).
  await measure(1024, WIDE)
  const railRoot = splitEl()?.props?.children?.find(c => c?.props?.['data-ac-rail-panel'] === '')
  const wideRailRows = []
  const walkRows = node => {
    if (node == null || typeof node !== 'object') return
    if (Array.isArray(node)) return node.forEach(walkRows)
    if (node.__frag) return walkRows(node.children)
    if (node.__el) {
      if (node.type === 'button') { wideRailRows.push(node); return }
      if (typeof node.type === 'function' && node.type.name !== 'Stub') {
        let inner = null
        try { inner = invokeComponent(node.type, node.props, 'render[page-step6b]') } catch { return }
        return walkRows(inner)
      }
      return walkRows(node.props?.children)
    }
  }
  walkRows(railRoot)
  check('rail rows are real buttons (keyboard reachable)', wideRailRows.length > 0 && wideRailRows.every(b => b.props?.type === 'button'), String(wideRailRows.length))
  check('rail rows keep the session-key data hook for live tests', wideRailRows.every(b => typeof b.props?.['data-ac-rail'] === 'string'), wideRailRows.map(b => b.props?.['data-ac-rail']).join(','))

  await measure(1024, WIDE)
}

// ── step 6c (NEW regression): long-value wrapping, no horizontal blowouts ────
// Live narrow failures included text that refused to wrap (keys, commands,
// cwd). Every long-value surface must carry overflowWrap so the detail column
// never scrolls sideways at 214px.

resetGlobals()
// Long session_key + cwd make the wrap contract non-vacuous: PanelMeta renders
// plain cells, so any ≥20-char raw string value would blow the 214px column out.
const LONG_KEY = `session-key-${'k'.repeat(64)}`
globalThis.__AC_DATA = {
  [SUMMARY_KEY_LIVE]: { ...SUMMARY_POPULATED, items: [{ ...ITEMS[0], session_key: LONG_KEY, cwd: `C:/w/very/deep/${'d'.repeat(64)}` }] },
  [DETAILS_KEY_LIVE(LONG_KEY)]: DETAILS_POPULATED
}
{
  const offenders = []
  const passX = renderPass(page, 'render:page[wrap]', renderErrors, 'page-step6c')
  const row = passX.rows.find(r => (r.rowKey || '').startsWith('session-key-'))
  if (row) {
    await row.onSelect()
    renderPass(page, 'render:page[wrap]', renderErrors, 'page-step6c')
  }
  check('expanded detail renders without render errors', renderErrors.length === 0, renderErrors.map(([p, e]) => `${p}: ${e?.message}`).join(' | '))
  // PanelMeta rows are the only long-value surface the harness can see from the
  // outside; the plugin must wrap them itself (PanelMeta renders plain cells).
  const root = page.render()
  const branch = invokeComponent(root.type, root.props, 'render[page-step6c]')
  const walkMeta = node => {
    if (node == null || typeof node !== 'object') return
    if (Array.isArray(node)) return node.forEach(walkMeta)
    if (node.__frag) return walkMeta(node.children)
    if (node.__el) {
      if (typeof node.type === 'function' && node.type.name === 'PanelMeta') {
        for (const r of node.props?.rows ?? []) {
          const valueText = typeof r.value === 'string' ? r.value : flattenTexts(r.value, []).join('')
          if (valueText.length >= 20) offenders.push(valueText)
        }
        return
      }
      walkMeta(node.props?.children)
    }
  }
  walkMeta(branch)
  check('session meta values wrap long keys (no ≥20-char unwrapped meta value)', offenders.length === 0, offenders.slice(0, 3).join(' | '))
}

// ── step 7: interaction scripting (mutations hit the exact REST routes) ──────

resetGlobals()
globalThis.__AC_DATA = {
  [SUMMARY_KEY_LIVE]: SUMMARY_POPULATED,
  [DETAILS_KEY_LIVE('gallery-goals')]: DETAILS_POPULATED
}
globalThis.__AC_REST = async path => {
  if (path.startsWith('/respond')) return { resolved: 1 }
  if (path.startsWith('/answer')) return { status: 'ok' }
  if (path.startsWith('/control')) return { ok: true }
  if (path.startsWith('/redo')) return { redone: true, record_cleared: true }
  if (path.startsWith('/dismiss')) return { dismissed: true }
  return {}
}
let pass = renderPass(page, 'render:page[interact]', renderErrors, 'page-step7')
const row7 = pass.rows.find(r => r.rowKey === 'gallery-goals')
if (row7) {
  await row7.onSelect()
  pass = renderPass(page, 'render:page[interact]', renderErrors, 'page-step7')
}

// Expand a clarify option + stage batch answers, then click submit. The option
// rows are raw buttons; the batch choices are Button stubs. Drive them through
// their onClick before asserting the REST calls. Each click is followed by a
// re-render so staged state shows on the next pick. The trailing task flush
// mirrors a real UI: a click's awaited post settles (button re-enables) before
// the next interaction — raw back-to-back onClick calls would interleave
// closures mid-await, something a disabled-while-busy button makes impossible.
const flushInflight = () => new Promise(resolve => setTimeout(resolve, 0))
const clickText = async name => {
  const btn = clickableByText(pass.clickables, name)
  if (!btn) return false
  await btn.props.onClick()
  await flushInflight()
  pass = renderPass(page, 'render:page[interact]', renderErrors, 'page-step7')
  return true
}

// Pick clarify options so Submit/Submit answers have pending answers.
await clickText('TypeScript')
// multi-select: pick two, expect a JSON array answer
await clickText('Error handling')
await clickText('Performance')
// batch: one per question
await clickText('High')
await clickText('Production')
await clickText('Lint')
await clickText('Typecheck')

const responded = await clickText('Approve once')
// The expanded row carries TWO single clarify cards (single + multi-select):
// each Submit click resolves the FIRST unresolved card in DOM order, so click
// until both have fired their answer POSTs (max 3 passes, stop when both ids
// are present).
let answered = false
for (let i = 0; i < 3; i++) {
  const again = clickableByText(pass.clickables, 'Submit')
  if (!again) break
  await again.props.onClick()
  pass = renderPass(page, 'render:page[interact]', renderErrors, 'page-step7')
  const haveSingle = restCalls.some(c => c.path.startsWith('/answer') && c.opts?.body?.request_id === 'req-clarify-1')
  const haveMulti = restCalls.some(c => c.path.startsWith('/answer') && c.opts?.body?.request_id === 'req-clarify-multi')
  if (haveSingle && haveMulti) { answered = true; break }
}
const batchSubmitted = await clickText('Submit answers')
const redone = await clickText('Redo')
const dismissed = await clickText('Dismiss')
const openedChat = await clickText('Open full chat')
const controlled = await clickText('Pause goal') || await clickText('Resume goal')
  || await clickText('Pause loop') || await clickText('Resume loop')
  || await clickText('Pause heartbeat') || await clickText('Resume heartbeat')

const paths = restCalls.map(c => c.path)
check('respond POST hit', responded && paths.some(p => p.startsWith('/respond')), paths.join(','))
const respondCall = restCalls.find(c => c.path.startsWith('/respond'))
check('respond body fields', respondCall && respondCall.opts?.method === 'POST' && respondCall.opts?.body?.request_id === 'req-approval-1'
  && ['once', 'session', 'always', 'deny'].includes(respondCall.opts?.body?.choice)
  && respondCall.opts?.body?.session_key === 'gallery-goals'
  && respondCall.opts?.body?.profile === 'default', JSON.stringify(respondCall?.opts?.body ?? {}))
check('answer POST hit (single clarify)', answered && paths.some(p => p.startsWith('/answer')))
const answerCalls = restCalls.filter(c => c.path.startsWith('/answer'))
const singleAnswer = answerCalls.find(c => c.opts?.body?.request_id === 'req-clarify-1')
check('single clarify answer body', singleAnswer && typeof singleAnswer.opts?.body?.answer === 'string' && singleAnswer.opts?.body?.session_key === 'gallery-goals', JSON.stringify(singleAnswer?.opts?.body ?? {}))
const multiAnswer = answerCalls.find(c => c.opts?.body?.request_id === 'req-clarify-multi')
check('multi-select answer is a JSON array', multiAnswer && (() => { try { const v = JSON.parse(multiAnswer.opts.body.answer); return Array.isArray(v) && v.includes('Error handling') && v.includes('Performance') } catch { return false } })(), JSON.stringify(multiAnswer?.opts?.body ?? {}))
check('batch staged per-question (one POST per question)', batchSubmitted && answerCalls.filter(c => c.opts?.body?.question_id).length >= 3, String(answerCalls.filter(c => c.opts?.body?.question_id).length))
check('batch carries question_id + answer', ['q1', 'q2', 'q3'].every(qid => answerCalls.some(c => c.opts?.body?.question_id === qid && typeof c.opts?.body?.answer === 'string')), JSON.stringify(answerCalls.map(c => [c.opts?.body?.question_id, c.opts?.body?.answer])))
check('redo POST hit', redone && paths.some(p => p.startsWith('/redo')))
const redoCall = restCalls.find(c => c.path.startsWith('/redo'))
check('redo body fields', redoCall && redoCall.opts?.body?.request_id === 'req-expired-1' && redoCall.opts?.body?.session_key === 'gallery-goals', JSON.stringify(redoCall?.opts?.body ?? {}))
check('dismiss POST hit', dismissed && paths.some(p => p.startsWith('/dismiss')))
check('control POST hit', controlled && paths.some(p => p.startsWith('/control')))
const controlCall = restCalls.find(c => c.path.startsWith('/control'))
check('control body fields', controlCall
  && ['goal.pause', 'goal.resume', 'loop.pause', 'loop.resume', 'heartbeat.pause', 'heartbeat.resume'].includes(controlCall.opts?.body?.action)
  && controlCall.opts?.body?.session_key === 'gallery-goals'
  && controlCall.opts?.body?.live_session_id === 'live-1', JSON.stringify(controlCall?.opts?.body ?? {}))
check('open full chat routes via host.openSession', openedChat && (globalThis.__AC_SESSIONS || []).some(s => s.id === 'gallery-goals'), JSON.stringify(globalThis.__AC_SESSIONS || []))
check('mutations invalidate the query cache', (globalThis.__AC_INVALIDATIONS || 0) > 0, String(globalThis.__AC_INVALIDATIONS || 0))
check('all POST bodies carry profile', ['respond', 'answer', 'control', 'redo', 'dismiss']
  .every(seg => { const call = restCalls.find(c => c.path.startsWith(`/${seg}`)); return call && call.opts?.body?.profile === 'default' }))
check('all mutation bodies use POST method', ['respond', 'answer', 'control', 'redo', 'dismiss']
  .every(seg => { const call = restCalls.find(c => c.path.startsWith(`/${seg}`)); return call && call.opts?.method === 'POST' }))

// ── step 8: restricted approval (allow_session=false, allow_permanent=false) ─

resetGlobals()
globalThis.__AC_DATA = {
  [SUMMARY_KEY_LIVE]: SUMMARY_POPULATED,
  [DETAILS_KEY_LIVE('gallery-background_tasks')]: DETAILS_RESTRICTED
}
const restrictedCollapsed = renderPass(page, 'render:page[restricted]', renderErrors, 'page-step8')
const restrictedRow = restrictedCollapsed.rows.find(r => r.rowKey === 'gallery-background_tasks')
if (restrictedRow) {
  await restrictedRow.onSelect()
  renderPass(page, 'render:page[restricted]', renderErrors, 'page-step8')
}
check('restricted walk: no render errors', renderErrors.length === 0, renderErrors.map(([p, e]) => `${p}: ${e?.message}`).join(' | '))
{
  const restricted = renderPass(page, 'render:page[restricted]', renderErrors, 'page-step8')
  const labels = restricted.clickables.map(b => childText(b.props.children))
  check('restricted approval hides Approve for session', !labels.includes('Approve for session'), labels.join('|'))
  check('restricted approval hides Always allow', !labels.includes('Always allow'), labels.join('|'))
  check('restricted approval still offers Approve once + Deny', labels.includes('Approve once') && labels.includes('Deny'), labels.join('|'))
}

// ── step 9: chip content + palette/chip interactions ─────────────────────────

resetGlobals()
globalThis.__AC_DATA = { [SUMMARY_KEY_LIVE]: { ...SUMMARY_EMPTY, coverage: { ...SUMMARY_EMPTY.coverage, expired_requests: { observed: true, partial: true } } } }
{
  const root = page.render()
  const tree = invokeComponent(root.type, root.props, 'coverage-scope')
  check('header declares observed-only expiry scope', String(tree.props.children[0].props.subtitle).includes('observed expiry history'))
  const node = chip.render()
  const rendered = invokeComponent(node.type, node.props, 'coverage-chip')
  check('zero chip makes no unqualified all-clear claim', !rendered.props.title.includes('all quiet') && rendered.props.title.includes('observed'))
}
resetGlobals()
globalThis.__AC_DATA = { [SUMMARY_KEY_LIVE]: SUMMARY_POPULATED }
{
  const tree = chip.render()
  const texts = flattenTexts(tree)
  check('chip label text', texts.includes('Action Center'), texts.join('|'))
  check('chip need-attention count text', texts.includes('1'), texts.join('|'))
}

try {
  paletteOpen.data.run()
  const opened = (globalThis.__AC_WORKSPACES || []).length > 0 || (globalThis.__AC_NAVS || []).includes('/action-center')
  check('palette open reaches the panel (workspace dock or route)', opened,
    JSON.stringify({ navs: globalThis.__AC_NAVS || [], workspaces: (globalThis.__AC_WORKSPACES || []).map(w => w.id) }))
  const docked = (globalThis.__AC_WORKSPACES || []).find(w => w.id === 'action-center')
  check('openWorkspace dock uses render + title', Boolean(docked) && typeof docked?.options?.render === 'function' && docked?.options?.title === 'Action Center')
} catch (e) {
  check('palette open reaches the panel', false, String(e))
}
try {
  paletteRefresh.data.run()
  check('palette refresh invalidates queries', (globalThis.__AC_INVALIDATIONS || 0) > 0, String(globalThis.__AC_INVALIDATIONS || 0))
} catch (e) {
  check('palette refresh runs', false, String(e))
}

// ── step 10 (NEW regression): mutation refusal when the profile changes ──────
// A card pins the profile at mount. The page collapses the expanded row when
// the profile switches (core parity — queries re-key), but a card closure is
// still mounted in-flight: capture the card element + hook scope BEFORE the
// switch, then click the captured closure AFTER the atom swap. The guard must
// refuse with the named message, never post under the new profile.

resetGlobals()
globalThis.__AC_DATA = {
  [SUMMARY_KEY_LIVE]: SUMMARY_POPULATED,
  [DETAILS_KEY_LIVE('gallery-goals')]: DETAILS_POPULATED
}
globalThis.__AC_REST = async () => ({ resolved: 1, status: 'ok' })
{
  const passA = renderPass(page, 'render:page[guard]', renderErrors, 'page-step10')
  const row = passA.rows.find(r => r.rowKey === 'gallery-goals')
  if (row) {
    await row.onSelect()
    renderPass(page, 'render:page[guard]', renderErrors, 'page-step10')
  }
  // Capture the mounted card (element + walk scope) BEFORE the switch.
  const treePre = page.render()
  const foundPre = []
  collectByType(treePre, 'ApprovalCard', foundPre, renderErrors, 'render[page-step10]')
  const cardEl = foundPre[foundPre.length - 1]?.el
  const cardScope = foundPre[foundPre.length - 1]?.scope
  check('profile guard scenario mounts an approval card', Boolean(cardEl && cardScope))
  // Swap the host profile atom (the race window). The page re-render then
  // collapses the row (correct scope-reset behavior) — the captured closure
  // stands in for the still-mounted card.
  sdkStub.host.state.profile.set('other-profile')
  renderPass(page, 'render:page[guard]', renderErrors, 'page-step10')
  check('page collapses the expanded row on profile switch', renderPass(page, 'render:page[guard]', renderErrors, 'page-step10').texts.includes('Approve once') === false)
  // Drive the captured closure: pin must refuse, never post.
  const branch = cardEl && invokeComponent(cardEl.type, cardEl.props, cardScope)
  const buttons = branch ? collectBranchClickables(branch) : []
  const approve = buttons.find(b => childText(b.props?.children) === 'Approve once')
  if (approve) await approve.props.onClick()
  const respondCalls = restCalls.filter(c => c.path.startsWith('/respond'))
  check('profile switch blocks approve (no POST fired under the new profile)', respondCalls.length === 0, JSON.stringify(respondCalls.map(c => c.opts?.body)))
  // The refusal is visible on the re-invoked card.
  const again = cardEl && invokeComponent(cardEl.type, cardEl.props, cardScope)
  const refusalTexts = again ? flattenTexts(again, [], cardScope) : []
  check('profile switch refusal message shown on the card', refusalTexts.some(t => String(t).includes('Profile changed — re-open to act')), refusalTexts.join(' | '))
  sdkStub.host.state.profile.set('default')
}

// ── step 10b (NEW regression): batch answers fail closed mid-loop ─────────────
// The batch card answers question-by-question across awaited posts. A profile
// switch landing BETWEEN posts must stop the loop: later questions never land
// on the new backend under the pinned (stale) profile, the refusal shows, and
// the card does NOT flip to answered as if everything committed. The switch is
// injected from inside the REST stub the moment q1's post resolves — the real
// race window (the host atom flips while the loop is mid-flight).

resetGlobals()
globalThis.__AC_DATA = {
  [SUMMARY_KEY_LIVE]: SUMMARY_POPULATED,
  [DETAILS_KEY_LIVE('gallery-goals')]: DETAILS_POPULATED
}
{
  let q1Answered = false
  globalThis.__AC_REST = async () => {
    // The host profile atom flips while the batch loop awaits q2's post: the
    // first resolution triggers the swap, so q2+ must never fire.
    if (!q1Answered) {
      q1Answered = true
      sdkStub.host.state.profile.set('other-profile')
    }
    return { status: 'ok' }
  }
  const passB = renderPass(page, 'render:page[guard-batch]', renderErrors, 'page-step10b')
  const row = passB.rows.find(r => r.rowKey === 'gallery-goals')
  if (row) {
    await row.onSelect()
    renderPass(page, 'render:page[guard-batch]', renderErrors, 'page-step10b')
  }
  // Capture the batch card element + scope BEFORE the switch, then stage one
  // answer per question so the submit closure is live.
  const foundBatch = []
  collectByType(page.render(), 'BatchClarifyCard', foundBatch, renderErrors, 'render[page-step10b]')
  const batchEl = foundBatch[foundBatch.length - 1]?.el
  const batchScope = foundBatch[foundBatch.length - 1]?.scope
  check('batch guard scenario mounts the batch card', Boolean(batchEl && batchScope))
  let branch = batchEl && invokeComponent(batchEl.type, batchEl.props, batchScope)
  let preClickables = branch ? collectBranchClickables(branch) : []
  for (const label of ['High', 'Production', 'Lint']) {
    const chip = preClickables.find(b => childText(b.props?.children).endsWith(label))
    if (chip) {
      await chip.props.onClick()
      renderPass(page, 'render:page[guard-batch]', renderErrors, 'page-step10b')
      branch = invokeComponent(batchEl.type, batchEl.props, batchScope)
      preClickables = collectBranchClickables(branch)
    }
  }
  const submitBtn = preClickables.find(b => childText(b.props?.children) === 'Submit answers')
  check('batch guard scenario stages all three answers', Boolean(submitBtn))
  if (submitBtn) {
    await submitBtn.props.onClick()
    await new Promise(resolve => setTimeout(resolve, 0))
    const submitCalls = restCalls.filter(c => c.path.startsWith('/answer') && c.opts?.body?.question_id)
    check('batch guard: only q1 posted under the pinned profile', submitCalls.length === 1 && submitCalls[0]?.opts?.body?.profile === 'default' && submitCalls[0]?.opts?.body?.question_id === 'q1', JSON.stringify(submitCalls.map(c => c.opts?.body)))
    check('batch guard: q2/q3 never posted', !submitCalls.some(c => c.opts?.body?.question_id !== 'q1'), JSON.stringify(submitCalls.map(c => c.opts?.body?.question_id)))
    // Re-invoke the card: the guard error must show and the card must NOT be
    // in the answered terminal state.
    const after = invokeComponent(batchEl.type, batchEl.props, batchScope)
    const afterTexts = flattenTexts(after, [], batchScope)
    check('batch guard: refusal message shown on the batch card', afterTexts.some(t => String(t).includes('Profile changed — re-open to act')), afterTexts.join(' | '))
    check('batch guard: card did not flip to answered', !afterTexts.includes('Answered'), afterTexts.join(' | '))
  }
  sdkStub.host.state.profile.set('default')
}

// ── step 10c (NEW regression): sync submit locks ─────────────────────────────
// AutomationControls.run and ExpiredRequestCard.act are async: a second click
// before the awaited post settles must not fire a second /control or
// /redo//dismiss. Drive the captured closure twice without letting the first
// post settle; only one POST may fire. The react stub keeps useState slots, so
// `busy` still reads null on the second immediate invocation — the ref is what
// actually gates.

resetGlobals()
globalThis.__AC_DATA = {
  [SUMMARY_KEY_LIVE]: SUMMARY_POPULATED,
  [DETAILS_KEY_LIVE('gallery-goals')]: DETAILS_POPULATED
}
globalThis.__AC_REST = async () => ({ ok: true, redone: true, dismissed: true })
{
  const passC = renderPass(page, 'render:page[locks]', renderErrors, 'page-step10c')
  const row = passC.rows.find(r => r.rowKey === 'gallery-goals')
  if (row) {
    await row.onSelect()
    renderPass(page, 'render:page[locks]', renderErrors, 'page-step10c')
  }
  // Double-click Redo: capture the ExpiredRequestCard and drive the closure
  // twice with NO await between them — the ref lock must refuse the second.
  const foundCard = []
  collectByType(page.render(), 'ExpiredRequestCard', foundCard, renderErrors, 'render[page-step10c]')
  const expiredEl = foundCard[foundCard.length - 1]?.el
  const expiredScope = foundCard[foundCard.length - 1]?.scope
  check('lock scenario mounts the expired-request card', Boolean(expiredEl && expiredScope))
  if (expiredEl) {
    const branch = invokeComponent(expiredEl.type, expiredEl.props, expiredScope)
    const redo = collectBranchClickables(branch).find(b => childText(b.props?.children) === 'Redo')
    check('lock scenario offers the Redo button', Boolean(redo))
    if (redo) {
      const p1 = redo.props.onClick()
      await redo.props.onClick() // second click while the first is mid-await
      await p1
      await new Promise(resolve => setTimeout(resolve, 0))
      const redoCalls = restCalls.filter(c => c.path.startsWith('/redo'))
      check('sync lock: double-clicked Redo posts exactly once', redoCalls.length === 1, JSON.stringify(redoCalls.map(c => c.opts?.body)))
    }
  }
  // Double-click Pause goal the same way.
  const foundCtl = []
  collectByType(page.render(), 'AutomationControls', foundCtl, renderErrors, 'render[page-step10c]')
  const ctlEl = foundCtl[foundCtl.length - 1]?.el
  const ctlScope = foundCtl[foundCtl.length - 1]?.scope
  check('lock scenario mounts an automation control', Boolean(ctlEl && ctlScope))
  if (ctlEl) {
    const branch = invokeComponent(ctlEl.type, ctlEl.props, ctlScope)
    const pause = collectBranchClickables(branch).find(b => /^(Pause|Resume) /.test(childText(b.props?.children)))
    check('lock scenario offers a pause/resume button', Boolean(pause))
    if (pause) {
      const p1 = pause.props.onClick()
      await pause.props.onClick()
      await p1
      await new Promise(resolve => setTimeout(resolve, 0))
      const ctlCalls = restCalls.filter(c => c.path.startsWith('/control'))
      check('sync lock: double-clicked pause/resume posts exactly once', ctlCalls.length === 1, JSON.stringify(ctlCalls.map(c => c.opts?.body)))
    }
  }
}

// ── step 11 (NEW regression): queries are keyed per profile ──────────────────
// After a switch the chip reads the NEW profile's cache entry, so a stale count
// from the old backend never survives on screen.

resetGlobals()
globalThis.__AC_DATA = {
  'action-center|summary|default': SUMMARY_POPULATED,
  'action-center|summary|other-profile': { ...SUMMARY_EMPTY, coverage: { ...SUMMARY_EMPTY.coverage, profile: 'other-profile' } }
}
{
  renderPass(chip, 'render:chip[profiles]', renderErrors, 'chip-step11')
  sdkStub.host.state.profile.set('other-profile')
  renderPass(chip, 'render:chip[profiles]', renderErrors, 'chip-step11')
  const texts = flattenTexts(chip.render())
  check('chip profile switch: count comes from the new profile', !texts.includes('1'), texts.join('|'))
  sdkStub.host.state.profile.set('default')
}

// ── REST route coverage: only SPEC routes ever called ────────────────────────

const ALLOWED = ['/summary', '/details', '/respond', '/answer', '/control', '/redo', '/dismiss']
const offenders = restCalls.filter(c => !ALLOWED.some(a => c.path.startsWith(a)))
check('only SPEC REST routes called', offenders.length === 0, offenders.map(c => c.path).join(','))

// ── report ───────────────────────────────────────────────────────────────────

console.log('registered:', registered.map(r => `${r.id}@${r.area}`).join(', '))
console.log('rest calls observed:', restCalls.length)
let reported = 0
for (const [name, ok, detail] of checks) {
  console.log(`${ok ? 'PASS' : 'FAIL'} - ${name}${ok || !detail ? '' : ` :: ${detail}`}`)
  if (!ok) reported++
}
if (renderErrors.length) {
  console.log('RENDER ERRORS:')
  for (const [p, e] of renderErrors) {
    console.log(' -', p, '::', String((e && e.stack) || e).split('\n').slice(0, 4).join(' | '))
  }
  failed++
}
if (reported || failed) {
  console.log('SMOKE FAIL')
  process.exit(1)
}
console.log('SMOKE PASS')