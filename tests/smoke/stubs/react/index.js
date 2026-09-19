// React stub for the offline smoke harness.
//
// The harness walker invokes component functions directly (no reconciler), so
// hook slots are keyed by (component scope, call order within one invocation).
// The walker sets globalThis.__componentName and resets globalThis.__hookCursor
// to 0 before each component invocation, restoring both afterwards — that makes
// same-component hooks stable across the harness's sequential render passes,
// which is what simulating row-expansion toggles needs.
//
// Effects and refs follow the REAL react contract the harness must drive:
// useRef returns a fresh slot object (the component writes it into the tree),
// and useEffect REGISTERS its closure (the harness flushes them, mirroring a
// real mount). No test-only hooks exist in production code; the harness pokes
// the stub itself, exactly as a browser would poke real refs.
const slots = new Map()
const effectQueue = []

export function useState(init) {
  const owner = String(globalThis.__componentName ?? 'root')
  const index = Number(globalThis.__hookCursor ?? 0)
  globalThis.__hookCursor = index + 1
  const key = `${owner}#${index}`
  if (!slots.has(key)) slots.set(key, typeof init === 'function' ? init() : init)
  const setter = next => {
    slots.set(key, typeof next === 'function' ? next(slots.get(key)) : next)
  }
  return [slots.get(key), setter]
}
// Like real useRef: the SAME object across re-renders of one component
// instance, so mount-time pins (e.g. the profile a card was listed by) hold.
export function useRef(init) {
  const owner = String(globalThis.__componentName ?? 'root')
  const index = Number(globalThis.__hookCursor ?? 0)
  globalThis.__hookCursor = index + 1
  const key = `${owner}#${index}`
  if (!slots.has(key)) {
    slots.set(key, { current: init })
    // Bookkeeping for the harness: which ref SLOT landed on which component
    // instance. The harness attaches a measurement node to the page body's ref
    // and flushes effects — the same contract a real DOM satisfies.
    const refs = (globalThis.__AC_REF_SLOTS ||= [])
    refs.push({ owner: key, ref: slots.get(key) })
  }
  return slots.get(key)
}
// Real useEffect runs after commit; this stub cannot commit, so it queues the
// closure. The harness flushes the queue (mount) and can flush again after a
// width change — mirroring the browser's effect → ResizeObserver → setState
// loop without inventing a production hook.
export function useEffect(fn) {
  effectQueue.push(fn)
}
// Harness-only: run every queued effect closure once (mount semantics).
export function __flushEffectsForHarness() {
  while (effectQueue.length > 0) {
    const fn = effectQueue.shift()
    try { fn() } catch (e) { (globalThis.__AC_ERRORS ||= []).push(['effect', e]) }
  }
}
// Harness-only: fire the most recently registered ResizeObserver for each
// observed node (the real RO fires the callback after a size change).
export function __runObserverForHarness() {
  const observer = globalThis.__AC_LAST_OBSERVER
  if (observer && typeof observer.callback === 'function') observer.callback()
}
// Harness-only: drop the ref bookkeeping between scenarios (the page "unmounts").
export function __clearRefsForHarness() {
  ;(globalThis.__AC_REF_SLOTS ||= []).length = 0
}
// The page body's ref slot: the LAST ref created under the given scope prefix
// (the page component is walked last, so its ref is the newest under its scope).
export function __lastRefForHarness(ownerPrefix) {
  const refs = globalThis.__AC_REF_SLOTS || []
  for (let i = refs.length - 1; i >= 0; i--) {
    if (refs[i].owner.startsWith(ownerPrefix)) return refs[i].ref
  }
  return null
}
export function useCallback(fn) {
  return fn
}
export function useMemo(fn) {
  return fn()
}
// Harness-only: wipe every hook slot between scenario steps so each step's
// renders start from the component's own initial state (no leakage of resolved
// cards or staged answers from a previous step).
export function __resetForHarness() {
  slots.clear()
  effectQueue.length = 0
  ;(globalThis.__AC_REF_SLOTS ||= []).length = 0
  globalThis.__AC_LAST_OBSERVER = null
}
export default {}