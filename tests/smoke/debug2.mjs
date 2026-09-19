import { readFileSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(here, '..', '..')
writeFileSync(path.join(here, 'plugin.js'), readFileSync(path.join(repoRoot, 'desktop', 'plugin.js'), 'utf8'))

const mod = await import('./plugin.js')
const plugin = mod.default

const registered = []
const restCalls = []
const ctx = {
  register: c => registered.push(c),
  registerMany: cs => registered.push(...cs),
  storage: { get: (_k, d) => d, set() {}, remove() {} },
  rest: async (p, o) => { restCalls.push({ p, o }); return { resolved: 1, status: 'ok', ok: true, redone: true, dismissed: true } },
  socket: () => () => {},
  i18n: { register() {}, t: k => k }
}
plugin.register(ctx)
const page = registered.find(r => r.id === 'page')

function invoke(type, props) {
  const name = type.name || 'anon'
  const prevName = globalThis.__componentName
  const prevCursor = globalThis.__hookCursor
  if (name !== 'Stub') { globalThis.__componentName = name; globalThis.__hookCursor = 0 }
  try { return type({ ...props }) } finally { globalThis.__componentName = prevName; globalThis.__hookCursor = prevCursor }
}
function collectClickables(node, out) {
  if (node == null || typeof node === 'boolean' || typeof node === 'string' || typeof node === 'number') return
  if (Array.isArray(node)) { node.forEach(c => collectClickables(c, out)); return }
  if (node.__frag) { collectClickables(node.children, out); return }
  if (node.__el) {
    if (node.props?.onClick) out.push(node)
    if (typeof node.type === 'function') { collectClickables(invoke(node.type, node.props), out); return }
    collectClickables(node.props?.children, out)
    return
  }
  if (node.$$typeof) return
}
function childText(c) {
  if (typeof c === 'string' || typeof c === 'number') return String(c)
  if (Array.isArray(c)) return c.map(childText).join('')
  if (c && typeof c === 'object') {
    if (c.__frag) return childText(c.children)
    if (c.__el && typeof c.type === 'function') {
      if ((c.type.name || 'anon') === 'Stub') return childText(c.props?.children)
      return childText(invoke(c.type, c.props)?.props?.children)
    }
    if (c.props?.children !== undefined) return childText(c.props.children)
  }
  return ''
}
function walk(node, path) {
  if (node == null || typeof node === 'boolean' || typeof node === 'string' || typeof node === 'number') return
  if (Array.isArray(node)) { node.forEach((c, i) => walk(c, `${path}[${i}]`)); return }
  if (node.__frag) { walk(node.children, `${path}#frag`); return }
  if (node.__el) {
    if (typeof node.type === 'function') { walk(invoke(node.type, node.props), `${path}>${node.type.name}`); return }
    walk(node.props?.children, `${path}<${node.type}>`)
    return
  }
  if (node.$$typeof) return
}

const ITEMS = [{
  session_key: 'gallery-goals', title: 'Deploy pipeline', source: 'terminal', cwd: 'C:/w/demo',
  lanes: ['needs_you', 'running'], categories: ['goals'],
  pending_approval: { count: 1, command_redacted: true, description: 'Deploy command' },
  pending_clarify: null, expired_request_count: 0,
  goal: { title: 'Ship it', status: 'active', turns_used: 1, max_turns: 5, contract: {}, subgoals: [], gates: [] },
  loop: null, heartbeat: null,
  subagent_count: 0, subagent_count_unavailable: false,
  background_task_count: 0, background_task_count_unavailable: false
}]
const SUMMARY = {
  badge: 'amber',
  counts: { needs_you: 1, running: 0, waiting: 0, scheduled: 0, total: 1 },
  coverage: { profile: 'default', connection_scope: 'x', scanned_sessions: 1, partial: false, errors: [] },
  items: ITEMS
}
const DETAILS = {
  coverage: { approval_count: 1, clarification_count: 3, context_anchor: '', errors: [], live_session_count: 1, profile: 'default', session_key: 'gallery-goals' },
  sessions: [{
    live_session_ids: ['live-1'],
    context: { available: true, reason: null, messages: [{ role: 'user', text: 'hi', timestamp: 1 }] },
    approvals: [{ request_id: 'req-approval-1', command: 'rm', description: 'd', choices: ['once', 'session', 'always', 'deny'], allow_session: true, allow_permanent: true }],
    clarifications: [
      { request_id: 'req-clarify-1', kind: 'single', params: { question: 'q1?', choices: ['TypeScript', 'Python', 'Rust'], multi_select: false } },
      { request_id: 'req-clarify-multi', kind: 'single', params: { question: 'q2?', choices: ['Error handling', 'Performance'], multi_select: true } },
      { request_id: 'req-batch-1', kind: 'batch', params: { questions: [
        { qid: 'q1', question: 'Priority level?', choices: ['Low', 'Medium', 'High'], multi_select: false },
        { qid: 'q3', question: 'Checks?', choices: ['Lint', 'Typecheck'], multi_select: true }
      ] } }
    ],
    expired_requests: [{ request_id: 'req-expired-1', kind: 'approval', command: 'x.sh', description: 'd', ended_at: Date.now() / 1000 - 120, outcome: 'timeout' }]
  }]
}
globalThis.__AC_DATA = {
  'action-center|summary|default': SUMMARY,
  'action-center|details|gallery-goals|default': DETAILS
}
globalThis.__componentName = 'ActionCenterPage'
globalThis.__hookCursor = 0
globalThis.__AC_ROWS = []
walk(page.render(), 'r1')
const row = (globalThis.__AC_ROWS || []).find(r => r.rowKey === 'gallery-goals')
row?.onSelect()

globalThis.__componentName = 'ActionCenterPage'
globalThis.__hookCursor = 0
const buttons = []
collectClickables(page.render(), buttons)
const click = async name => {
  const b = buttons.find(x => childText(x.props.children) === name)
  if (!b) return false
  await b.props.onClick()
  return true
}

// Isolate the clarify submit: skip approval entirely.
console.log('TS:', await click('ATypeScript'))
console.log('submit:', await click('Submit'))
await new Promise(r => setTimeout(r, 30))
console.log('rest:', restCalls.map(c => `${c.p} ${JSON.stringify(c.o?.body ?? {})}`).join('\n'))