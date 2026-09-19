export function jsx(type, props, key) {
  return { __el: true, type, props: props || {}, key }
}
export function jsxs(type, props, key) {
  return { __el: true, type, props: props || {}, key }
}
export const Fragment = Symbol.for('react.fragment')