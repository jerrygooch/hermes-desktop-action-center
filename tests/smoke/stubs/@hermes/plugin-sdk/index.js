// Minimal stub of @hermes/plugin-sdk for the offline smoke harness.
// Kit components render their children through a __frag marker so the harness
// walker reaches nested component bodies. Data/error injection happens through
// globalThis.__AC_DATA / __AC_ERR keyed by "<plugin-id>|<query-key-joined>".

export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar-nav'
export const PALETTE_AREA = 'palette'
export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }
export const WORKSPACE_PAGE_HEADER_AREA = 'workspace-page-header'

export const atom = init => {
  let value = init
  return {
    get: () => value,
    set: next => { value = typeof next === 'function' ? next(value) : next }
  }
}
export const computed = fn => {
  const a = atom(undefined)
  a.get = fn
  return a
}
export const useValue = a => (a && typeof a.get === 'function' ? a.get() : a)

export const queryClient = {
  invalidateCalls: [],
  invalidateQueries(opts) {
    this.invalidateCalls.push(opts)
    globalThis.__AC_INVALIDATIONS = (globalThis.__AC_INVALIDATIONS || 0) + 1
  },
  refetchQueries() {}
}
export const useQueryClient = () => queryClient

const dataFor = key => (globalThis.__AC_DATA ? globalThis.__AC_DATA[key] : undefined)
const errFor = key => ((globalThis.__AC_ERR && globalThis.__AC_ERR[key]) || null)

export const useQuery = ({ queryKey }) => {
  const key = Array.isArray(queryKey) ? queryKey.join('|') : String(queryKey)
  const data = dataFor(key)
  const error = errFor(key)
  return {
    data,
    error,
    isPending: data === undefined && !error,
    refetch() {}
  }
}
export const useMutation = options => ({ mutate() {}, ...options })

export const haptic = () => {}
export const icons = new Proxy({}, { get: () => function IconStub() { return null } })

export const host = {
  state: {
    profile: atom('default'),
    busy: atom(false),
    activeSessionId: atom(null),
    // Live window posture (mirrors the SDK): { width, height, narrow }.
    viewport: atom({ width: 1280, height: 800, narrow: false })
  },
  notify: m => { (globalThis.__AC_NOTES ||= []).push(m) },
  navigate: p => { (globalThis.__AC_NAVS ||= []).push(p) },
  openWorkspace: (id, options) => {
    (globalThis.__AC_WORKSPACES ||= []).push({ id, options })
    // Exercise the render closure so the docked panel body is walked too.
    if (options && typeof options.render === 'function') {
      try { options.render() } catch (e) { (globalThis.__AC_ERRORS ||= []).push(['openWorkspace.render', e]) }
    }
    return () => {}
  },
  openSession: (id, options) => { (globalThis.__AC_SESSIONS ||= []).push({ id, options }) },
  request: async () => ({}),
  onEvent: (type, fn) => {
    (globalThis.__AC_EVENTS ||= []).push({ type, fn })
    return () => {}
  },
  logs: () => {},
  status: () => ({})
}

const Stub = props => (props && props.children !== undefined ? { __frag: true, children: props.children } : null)
export const Button = Stub
export const Codicon = Stub
export const Loader = Stub
export const SearchField = Stub
export const Input = Stub
export const StatusDot = Stub
export const Tip = Stub
export const Popover = Stub
export const PopoverContent = Stub
export const PopoverTrigger = Stub
export const EmptyState = Stub
export const ErrorState = Stub

// Panel kit (PanelHeader/PanelBody/PanelEmpty/PanelMeta/PanelPill/PanelSectionLabel)
// behave like Stub; PanelListRow additionally surfaces rowKey + title so the
// harness can assert the row list's shape.
export const PanelHeader = Stub
export const PanelBody = Stub
export const PanelEmpty = Stub
export const PanelSectionLabel = Stub
export const PanelPill = Stub
export function PanelMeta({ rows }) {
  return { __frag: true, children: (rows || []).map(r => ({ __el: true, type: 'meta-row', props: r, key: r.label })) }
}
export function PanelListRow(props) {
  const rows = globalThis.__AC_ROWS ||= []
  rows.push(props)
  return Stub(props)
}
export const relativeTime = ms => {
  const diff = ms - Date.now()
  const abs = Math.abs(diff)
  const min = Math.round(abs / 60000)
  if (abs < 3600000) return diff < 0 ? `${min} min ago` : `in ${min} min`
  const hours = Math.round(abs / 3600000)
  return diff < 0 ? `${hours} hr ago` : `in ${hours} hr`
}
export const fmtDateTime = () => ''
export const coarseElapsed = () => ({ unit: 'min', value: 1 })
export const cn = (...parts) => parts.filter(Boolean).join(' ')
export const useI18n = () => ({ t: k => k })
export const Contribute = Stub