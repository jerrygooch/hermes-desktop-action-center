// React stub for the offline smoke harness.
//
// The harness walker invokes component functions directly (no reconciler), so
// hook slots are keyed by (component scope, call order within one invocation).
// The walker sets globalThis.__componentName and resets globalThis.__hookCursor
// to 0 before each component invocation, restoring both afterwards — that makes
// same-component hooks stable across the harness's sequential render passes,
// which is what simulating row-expansion toggles needs.
const slots = new Map()

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
  if (!slots.has(key)) slots.set(key, { current: init })
  return slots.get(key)
}
export function useEffect() {
  return undefined
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
}
export default {}