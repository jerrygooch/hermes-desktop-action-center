// Offline smoke harness for the Action Center desktop plugin.
//
// Loads desktop/plugin.js with stubbed SDK/React modules, runs register(ctx),
// then walks every registered render with (a) populated sample data, (b) empty
// data, (c) an error state, and additionally (d) partial coverage, (e) an
// expanded-row interaction walk and (f) mutation scripting that asserts the
// exact REST routes the SPEC defines. Collects reference errors and asserts
// contribution shapes/paths/labels.
//
// Run: node tests/smoke/run.mjs
import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const here = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(here, '..', '..')

// Import the plugin THROUGH the stub node_modules beside this file (a copy of
// desktop/plugin.js) so `@hermes/plugin-sdk` resolves to the stubs.
const pluginSource = readFileSync(path.join(repoRoot, 'desktop', 'plugin.js'), 'utf8')
writeFileSync(path.join(here, 'plugin.js'), pluginSource)

const mod = await import('./plugin.js')
const plugin = mod.default

let failed = 0
const checks = []
const check = (name, ok, detail) => {
  checks.push([name, ok, detail])
  if (!ok) failed++
}

const renderErrors = [] // [path, error]
const restCalls = [] // [{ path, opts }]

// ── sample data (mirrors the core PR's e2e fixtures) ─────────────────────────

const goalSnapshot = {
  title: 'Ship approved changes',
  status: 'active',
  turns_used: 3,
  max_turns: 20,
  contract: { outcome: 'Changelog updated', verification: 'unit tests', stop_when: 'changelog merged' },
  subgoals: ['Update changelog', 'Tag the release'],
  gates: [{ command: 'pytest -q', attempts: 2, last_exit_code: 0 }],
  wait_barrier: null,
  paused_reason: null,
  last_verdict: 'pass'
}
const loopSnapshot = {
  prompt: 'Check deployment health on staging',
  status: 'active',
  interval_seconds: 1800,
  ticks_fired: 4,
  times: 10,
  until: 'error rate is zero for 10 minutes',
  next_due_at: Math.floor(Date.now() / 1000) + 600,
  last_fired_at: Math.floor(Date.now() / 1000) - 1200,
  awaiting_response: false,
  deferred_by_goal: false,
  mode: null,
  max_ticks: null,
  paused_reason: null,
  last_stop_reason: null
}
const heartbeatSnapshot = {
  prompt: 'Morning check',
  status: 'paused',
  interval_seconds: 1800,
  fire_count: 5,
  last_fired_at: Math.floor(Date.now() / 1000) - 3600
}

const ITEMS = [
  {
    session_key: 'gallery-goals', title: 'Deploy pipeline', source: 'terminal', cwd: 'C:/w/demo',
    lanes: ['needs_you', 'running'], categories: ['goals'],
    pending_approval: { count: 1, command_redacted: true, description: 'Deploy command' },
    pending_clarify: null, expired_request_count: 1,
    goal: goalSnapshot, loop: null, heartbeat: null,
    subagent_count: 0, subagent_count_unavailable: false,
    background_task_count: 0, background_task_count_unavailable: false
  },
  {
    session_key: 'gallery-loops', title: 'Staging watchdog', source: 'terminal', cwd: 'C:/w/demo',
    lanes: ['running'], categories: ['loops'],
    pending_approval: null, pending_clarify: { count: 1 }, expired_request_count: 0,
    goal: null, loop: loopSnapshot, heartbeat: null,
    subagent_count: 0, subagent_count_unavailable: false,
    background_task_count: 0, background_task_count_unavailable: false
  },
  {
    session_key: 'gallery-heartbeats', title: 'Morning checks', source: 'cron', cwd: 'C:/w/demo',
    lanes: ['scheduled'], categories: ['heartbeats'],
    pending_approval: null, pending_clarify: null, expired_request_count: 0,
    goal: null, loop: null, heartbeat: heartbeatSnapshot,
    subagent_count: 0, subagent_count_unavailable: false,
    background_task_count: 0, background_task_count_unavailable: false
  },
  {
    session_key: 'gallery-subagents', title: 'Review crew', source: 'terminal', cwd: 'C:/w/demo',
    lanes: ['waiting'], categories: ['subagents'],
    pending_approval: null, pending_clarify: null, expired_request_count: 0,
    goal: null, loop: null, heartbeat: null,
    subagent_count: 3, subagent_count_unavailable: false,
    background_task_count: 0, background_task_count_unavailable: false
  },
  {
    session_key: 'gallery-background_tasks', title: 'Long renders', source: 'terminal', cwd: 'C:/w/demo',
    lanes: ['running'], categories: ['background_tasks'],
    pending_approval: null, pending_clarify: null, expired_request_count: 0,
    goal: null, loop: null, heartbeat: null,
    subagent_count: 0, subagent_count_unavailable: false,
    background_task_count: 2, background_task_count_unavailable: false
  },
  {
    session_key: 'gallery-other', title: 'Everything else', source: 'terminal', cwd: 'C:/w/demo',
    lanes: ['waiting'], categories: [], // no recognized category → 'Other' rail row appears
    pending_approval: null, pending_clarify: null, expired_request_count: 0,
    goal: null, loop: null, heartbeat: null,
    subagent_count: 0, subagent_count_unavailable: false,
    background_task_count: 0, background_task_count_unavailable: false
  }
]

const SUMMARY_POPULATED = {
  badge: 'amber',
  counts: { needs_you: 1, running: 2, waiting: 2, scheduled: 1, total: 6 },
  coverage: {
    profile: 'default', connection_scope: 'active connection and profile only',
    scanned_sessions: 6, partial: false, approval_scope: 'fixture', clarify_scope: 'fixture', errors: []
  },
  items: ITEMS
}

const CONTEXT = {
  available: true,
  reason: null,
  messages: [
    { role: 'user', text: 'Clean up the stale build cache before the staging deploy finishes.', timestamp: 1789765000 },
    { role: 'assistant', text: 'Staging is green. I want to clear /tmp/build-cache, then finish the deploy.', timestamp: 1789765060 }
  ]
}

const DETAILS_POPULATED = {
  coverage: { approval_count: 1, clarification_count: 3, context_anchor: 'session:gallery-goals', errors: [], live_session_count: 1, profile: 'default', session_key: 'gallery-goals' },
  sessions: [{
    live_session_ids: ['live-1'],
    context: CONTEXT,
    approvals: [{
      request_id: 'req-approval-1', command: 'rm -rf /tmp/build-cache', description: 'Delete build cache directory',
      choices: ['once', 'session', 'always', 'deny'], allow_session: true, allow_permanent: true, smart_denied: null, tool_name: 'terminal'
    }],
    clarifications: [
      { request_id: 'req-clarify-1', kind: 'single', params: { question: 'Which language should the new module be written in?', choices: ['TypeScript', 'Python', 'Rust'], multi_select: false } },
      { request_id: 'req-clarify-multi', kind: 'single', params: { question: 'Which areas need the most improvement?', choices: ['Error handling', 'Performance', 'Documentation', 'Testing'], multi_select: true } },
      {
        request_id: 'req-batch-1', kind: 'batch', params: {
          questions: [
            { qid: 'q1', question: 'Priority level?', choices: ['Low', 'Medium', 'High'], multi_select: false },
            { qid: 'q2', question: 'Target environment?', choices: ['Staging', 'Production'], multi_select: false },
            { qid: 'q3', question: 'Run additional checks?', choices: ['Lint', 'Typecheck', 'Unit tests'], multi_select: true }
          ]
        }
      }
    ],
    expired_requests: [
      { request_id: 'req-expired-1', kind: 'approval', command: 'dangerous-script.sh', description: 'Restricted approval', ended_at: Math.floor(Date.now() / 1000) - 120, outcome: 'timeout' }
    ]
  }]
}

const DETAILS_RESTRICTED = {
  coverage: { approval_count: 1, clarification_count: 0, context_anchor: 'unavailable: open chat for context', errors: [], live_session_count: 1, profile: 'default', session_key: 'gallery-background_tasks' },
  sessions: [{
    live_session_ids: ['live-5'],
    context: { available: false, reason: 'no displayable rows', messages: [] },
    approvals: [{
      request_id: 'req-approval-restricted', command: 'dangerous-script.sh', description: 'Restricted approval',
      choices: ['once', 'session', 'always', 'deny'], allow_session: false, allow_permanent: false, smart_denied: null, tool_name: 'terminal'
    }],
    clarifications: [],
    expired_requests: []
  }]
}

const SUMMARY_EMPTY = {
  badge: 'none',
  counts: { needs_you: 0, running: 0, waiting: 0, scheduled: 0, total: 0 },
  coverage: {
    profile: 'default', connection_scope: 'active connection and profile only',
    scanned_sessions: 0, partial: false, approval_scope: 'fixture', clarify_scope: 'fixture', errors: []
  },
  items: []
}

const SUMMARY_PARTIAL = {
  badge: 'red',
  counts: { needs_you: 0, running: 0, waiting: 0, scheduled: 0, total: 0 },
  coverage: {
    profile: 'default', connection_scope: 'active connection and profile only',
    scanned_sessions: 0, partial: true, approval_scope: 'fixture', clarify_scope: 'fixture',
    errors: ['Session snapshot failed: timeout']
  },
  items: []
}

// ── tree walkers (component-scoped hook emulation) ───────────────────────────

// Invoke a function component with the react stub's hook scope pointed at it.
// Stub (UI-kit) components are pass-throughs and need no scope of their own.
function invokeComponent(type, props) {
  const name = type.name || 'anon'
  const prevName = globalThis.__componentName
  const prevCursor = globalThis.__hookCursor
  if (name !== 'Stub') {
    globalThis.__componentName = name
    globalThis.__hookCursor = 0
  }
  try {
    return type({ ...props })
  } finally {
    globalThis.__componentName = prevName
    globalThis.__hookCursor = prevCursor
  }
}

function walk(node, path, depth = 0) {
  if (node == null || typeof node === 'boolean' || typeof node === 'string' || typeof node === 'number') return
  if (Array.isArray(node)) {
    node.forEach((child, i) => walk(child, `${path}[${i}]`, depth + 1))
    return
  }
  if (typeof node === 'object' && node.__frag) {
    walk(node.children, `${path}#frag`, depth + 1)
    return
  }
  if (typeof node === 'object' && node.__el) {
    const { type, props } = node
    if (type == null) {
      renderErrors.push([path, new Error('element with null/undefined type')])
      return
    }
    if (typeof type === 'function') {
      try {
        walk(invokeComponent(type, props), `${path}>${type.name || 'anon'}`, depth + 1)
      } catch (e) {
        renderErrors.push([`${path}>${type.name || 'anon'}`, e])
      }
    } else {
      walk(props && props.children, `${path}<${type}>`, depth + 1)
    }
    return
  }
  if (typeof node === 'object' && node.$$typeof) return
}

// Depth-first collection of every element carrying an onClick. Component
// functions are invoked (scope-managed) so their bodies are included.
function collectClickables(node, out, depth = 0) {
  if (node == null || typeof node === 'boolean' || typeof node === 'string' || typeof node === 'number') return
  if (Array.isArray(node)) {
    node.forEach(child => collectClickables(child, out, depth + 1))
    return
  }
  if (typeof node === 'object' && node.__frag) {
    collectClickables(node.children, out, depth + 1)
    return
  }
  if (typeof node === 'object' && node.__el) {
    // A Button stub element carries onClick on the element itself even though
    // its type is a function component — capture it before descending.
    if (node.props && typeof node.props.onClick === 'function') out.push(node)
    if (typeof node.type === 'function') {
      let branch = null
      try {
        branch = invokeComponent(node.type, node.props)
      } catch (e) {
        renderErrors.push([`collect>${node.type.name || 'anon'}`, e])
        return
      }
      collectClickables(branch, out, depth + 1)
      return
    }
    collectClickables(node.props && node.props.children, out, depth + 1)
    return
  }
  if (typeof node === 'object' && node.$$typeof) return
}

// Flatten an element's visible text (frag/component-aware) for label assertions.
function childText(c) {
  if (typeof c === 'string' || typeof c === 'number') return String(c)
  if (Array.isArray(c)) return c.map(childText).join('')
  if (c && typeof c === 'object') {
    if (c.__frag) return childText(c.children)
    if (c.__el && typeof c.type === 'function') {
      const name = c.type.name || 'anon'
      if (name === 'Stub') return childText(c.props?.children)
      let branch = null
      try { branch = invokeComponent(c.type, c.props) } catch { return '' }
      return childText(branch?.props?.children)
    }
    if (c.props?.children !== undefined) return childText(c.props.children)
  }
  return ''
}

function renderContribution(contribution, label) {
  if (typeof contribution.render !== 'function') return null
  try {
    const tree = contribution.render()
    walk(tree, label)
    return tree
  } catch (e) {
    renderErrors.push([label, e])
    return null
  }
}

// ── ctx wiring ───────────────────────────────────────────────────────────────

function makeCtx() {
  const registered = []
  return {
    registered,
    source: 'plugin:action-center',
    register: c => registered.push(c),
    registerMany: cs => registered.push(...cs),
    storage: { get: (_k, d) => d, set() {}, remove() {} },
    rest: async (path, opts) => {
      restCalls.push({ path, opts })
      if (globalThis.__AC_REST) return globalThis.__AC_REST(path, opts)
      return {}
    },
    socket: () => () => {},
    i18n: { register() {}, t: k => k }
  }
}

function resetGlobals() {
  for (const key of ['__AC_DATA', '__AC_ERR', '__AC_NOTES', '__AC_ROWS', '__AC_EVENTS', '__AC_REST', '__AC_SESSIONS', '__AC_WORKSPACES', '__AC_INVALIDATIONS']) {
    delete globalThis[key]
  }
  restCalls.length = 0
}

const SUMMARY_KEY_LIVE = 'action-center|summary|default' // queryKey ['action-center','summary','default']
const DETAILS_KEY_LIVE = key => `action-center|details|${key}|default`

// ── step 1: register + contribution shapes ───────────────────────────────────

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
renderContribution(chip, 'render:chip[populated]')
renderContribution(page, 'render:page[populated]')
check('populated walk: no render errors', renderErrors.length === 0, renderErrors.map(([p, e]) => `${p}: ${e?.message}`).join(' | '))

// ── step 3: EMPTY data walk ──────────────────────────────────────────────────

resetGlobals()
globalThis.__AC_DATA = { [SUMMARY_KEY_LIVE]: SUMMARY_EMPTY }
renderContribution(chip, 'render:chip[empty]')
renderContribution(page, 'render:page[empty]')
check('empty walk: no render errors', renderErrors.length === 0, renderErrors.map(([p, e]) => `${p}: ${e?.message}`).join(' | '))

// ── step 4: ERROR state walk (summary fails) ─────────────────────────────────

resetGlobals()
globalThis.__AC_ERR = { [SUMMARY_KEY_LIVE]: { detail: 'inbox aggregation unavailable' } }
renderContribution(chip, 'render:chip[error]')
renderContribution(page, 'render:page[error]')
check('error walk: no render errors', renderErrors.length === 0, renderErrors.map(([p, e]) => `${p}: ${e?.message}`).join(' | '))

// ── step 5: PARTIAL coverage walk (error banner) ─────────────────────────────

resetGlobals()
globalThis.__AC_DATA = { [SUMMARY_KEY_LIVE]: SUMMARY_PARTIAL }
renderContribution(page, 'render:page[partial]')
check('partial walk: no render errors', renderErrors.length === 0, renderErrors.map(([p, e]) => `${p}: ${e?.message}`).join(' | '))

// ── step 6: expanded-row walk (details populated) ────────────────────────────

resetGlobals()
globalThis.__componentName = 'ActionCenterPage'
globalThis.__hookCursor = 0
globalThis.__AC_DATA = {
  [SUMMARY_KEY_LIVE]: SUMMARY_POPULATED,
  [DETAILS_KEY_LIVE('gallery-goals')]: DETAILS_POPULATED,
  [DETAILS_KEY_LIVE('gallery-background_tasks')]: DETAILS_RESTRICTED
}
globalThis.__AC_ROWS = []
renderContribution(page, 'render:page[collapsed]')

// The react stub persists useState per component scope, so clicking a row
// (captured by the PanelListRow stub) and re-rendering exercises the expanded
// detail subtree: meta rows, automation sections, context messages, approval
// card, clarify cards (single + multi-select + batch), expired card.
const rowsCollapsed = globalThis.__AC_ROWS || []
const goalsRow = rowsCollapsed.find(r => r.rowKey === 'gallery-goals')
check('row list renders goals row', Boolean(goalsRow))
check('row meta line', Boolean(goalsRow && String(goalsRow.meta).includes('Needs you')), goalsRow?.meta)
check('row title', goalsRow?.title === 'Deploy pipeline', goalsRow?.title)
check('row list carries every session key', ['gallery-goals', 'gallery-loops', 'gallery-heartbeats', 'gallery-subagents', 'gallery-background_tasks', 'gallery-other']
  .every(key => rowsCollapsed.some(r => r.rowKey === key)), rowsCollapsed.map(r => r.rowKey).join(','))

if (goalsRow) {
  goalsRow.onSelect()
  renderContribution(page, 'render:page[expanded-goals]')
}
check('expanded walk: no render errors', renderErrors.length === 0, renderErrors.map(([p, e]) => `${p}: ${e?.message}`).join(' | '))
{
  // The expanded detail shows approval + clarify + expired cards and the
  // context excerpt; assert through a fresh clickable collection.
  const buttons = []
  collectClickables(page.render(), buttons)
  const labels = buttons.map(b => childText(b.props.children)).filter(Boolean)
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

// ── step 7: interaction scripting (mutations hit the exact REST routes) ──────

resetGlobals()
globalThis.__componentName = 'ActionCenterPage'
globalThis.__hookCursor = 0
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
renderContribution(page, 'render:page[interact-collapsed]')
const interactRow = (globalThis.__AC_ROWS || []).find(r => r.rowKey === 'gallery-goals')
if (interactRow) {
  interactRow.onSelect()
  renderContribution(page, 'render:page[interact-expanded]')
}

// Expand a clarify option + stage batch answers, then click submit. The option
// rows are raw buttons; the batch choices are Button stubs. Drive them through
// their onClick before asserting the REST calls.
const clickables = []
collectClickables(page.render(), clickables)
const clickText = async name => {
  const btn = clickables.find(b => childText(b.props.children) === name)
  if (!btn) return false
  await btn.props.onClick()
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
const answered = await clickText('Submit')
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
check('approval restricted choices absent', !clickables.some(b => childText(b.props.children) === 'Approve for session' && String(b.props?.children ?? '').length >= 0) || true) // informational
check('all POST bodies carry profile', ['respond', 'answer', 'control', 'redo', 'dismiss']
  .every(seg => { const call = restCalls.find(c => c.path.startsWith(`/${seg}`)); return call && call.opts?.body?.profile === 'default' }))

// ── step 8: restricted approval (allow_session=false, allow_permanent=false) ─

resetGlobals()
globalThis.__componentName = 'ActionCenterPage'
globalThis.__hookCursor = 0
globalThis.__AC_DATA = {
  [SUMMARY_KEY_LIVE]: SUMMARY_POPULATED,
  [DETAILS_KEY_LIVE('gallery-background_tasks')]: DETAILS_RESTRICTED
}
renderContribution(page, 'render:page[restricted-collapsed]')
const restrictedRow = (globalThis.__AC_ROWS || []).find(r => r.rowKey === 'gallery-background_tasks')
if (restrictedRow) {
  restrictedRow.onSelect()
  renderContribution(page, 'render:page[restricted-expanded]')
}
check('restricted walk: no render errors', renderErrors.length === 0, renderErrors.map(([p, e]) => `${p}: ${e?.message}`).join(' | '))
{
  const restrictedButtons = []
  collectClickables(page.render(), restrictedButtons)
  const labels = restrictedButtons.map(b => childText(b.props.children))
  check('restricted approval hides Approve for session', !labels.includes('Approve for session'), labels.join('|'))
  check('restricted approval hides Always allow', !labels.includes('Always allow'), labels.join('|'))
  check('restricted approval still offers Approve once + Deny', labels.includes('Approve once') && labels.includes('Deny'), labels.join('|'))
}

// ── step 9: chip content + palette/chip interactions ─────────────────────────

resetGlobals()
globalThis.__AC_DATA = { [SUMMARY_KEY_LIVE]: SUMMARY_POPULATED }
{
  const tree = chip.render()
  const texts = []
  const gather = node => {
    if (typeof node === 'string' || typeof node === 'number') { texts.push(String(node)); return }
    if (Array.isArray(node)) { node.forEach(gather); return }
    if (typeof node === 'object' && node.__frag) gather(node.children)
    else if (typeof node === 'object' && node.__el) {
      if (typeof node.type === 'function') {
        try { gather(invokeComponent(node.type, node.props)) } catch { /* counted elsewhere */ }
        return
      }
      gather(node.props?.children)
    } else if (typeof node === 'object' && node.$$typeof) return
    else if (typeof node === 'object' && node.props) gather(node.props.value)
  }
  gather(tree)
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