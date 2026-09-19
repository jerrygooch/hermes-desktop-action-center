// Shared walker/invoker for the Action Center smoke harness.
//
// The plugin is loaded uncompiled (no reconciler), so "rendering" means
// invoking component functions directly. The react stub keys useState slots by
// (component scope, hook index); the scope is the component's RENDER PATH —
// root label + parent scope + type name + array position — so sibling
// instances of the same component (two SingleClarifyCards in one detail view)
// keep separate state, exactly as React would with its own instance identity.
//
// Two more rules make hook state correct:
// 1. Every component invocation must be scope-managed (set scope, reset
//    cursor, restore afterwards).
// 2. A pass walks the tree twice. The stub is not a reconciler: slots are
//    initialized on the first pass and hold values on the second, which is
//    what surfaces state-dependent subtrees (expanded details, selected
//    options). The root scope derives from the contribution label, so repeated
//    renderPass calls for the same contribution address the same slots.

// Invoke one function component with the react stub's hook scope pointed at it.
// `scope` is the walker's render path; `null` scope = anonymous invocation
// (used by childText, which must not disturb pathed slot state).
export function invokeComponent(type, props, scope = null) {
  const name = type.name || 'anon'
  const prevName = globalThis.__componentName
  const prevCursor = globalThis.__hookCursor
  if (name !== 'Stub') {
    globalThis.__componentName = scope ? `${scope}>${name}` : name
    globalThis.__hookCursor = 0
  }
  try {
    return type({ ...props })
  } finally {
    globalThis.__componentName = prevName
    globalThis.__hookCursor = prevCursor
  }
}

// Depth-first walk. Every element whose props carry onClick is collected —
// including Button-stub card actions (ApprovalCard buttons, ExpiredRequestCard
// Redo/Dismiss, batch choice chips). `scope` carries the render path.
export function collectClickables(node, out, errors, scope = '') {
  if (node == null || typeof node === 'boolean' || typeof node === 'string' || typeof node === 'number') return out
  if (Array.isArray(node)) {
    node.forEach((child, i) => collectClickables(child, out, errors, `${scope}[${i}]`))
    return out
  }
  if (typeof node === 'object' && node.__frag) {
    collectClickables(node.children, out, errors, scope)
    return out
  }
  if (typeof node === 'object' && node.__el) {
    if (node.props && typeof node.props.onClick === 'function') out.push(node)
    if (typeof node.type === 'function') {
      let branch = null
      try {
        branch = invokeComponent(node.type, node.props, scope)
      } catch (e) {
        errors.push([`collect>${node.type.name || 'anon'}`, e])
        return out
      }
      collectClickables(branch, out, errors, `${scope}>${node.type.name || 'anon'}`)
      return out
    }
    collectClickables(node.props && node.props.children, out, errors, scope)
    return out
  }
  return out
}

// Depth-first walk collecting the props of every PanelListRow element
// (rowKey/title/meta/onSelect) — the row list's harness-facing shape. Matched
// by function name so the SDK stub's identity stays internal to run.mjs.
export function collectRows(node, rowsSink, errors, scope = '') {
  if (node == null || typeof node === 'boolean' || typeof node === 'string' || typeof node === 'number') return
  if (Array.isArray(node)) {
    node.forEach((child, i) => collectRows(child, rowsSink, errors, `${scope}[${i}]`))
    return
  }
  if (typeof node === 'object' && node.__frag) {
    collectRows(node.children, rowsSink, errors, scope)
    return
  }
  if (typeof node === 'object' && node.__el) {
    if (typeof node.type === 'function') {
      if (node.type.name === 'PanelListRow') {
        rowsSink.push(node.props)
        return
      }
      let branch = null
      try {
        branch = invokeComponent(node.type, node.props, scope)
      } catch (e) {
        errors.push([`rows>${node.type.name || 'anon'}`, e])
        return
      }
      collectRows(branch, rowsSink, errors, `${scope}>${node.type.name || 'anon'}`)
      return
    }
    collectRows(node.props && node.props.children, rowsSink, errors, scope)
    return
  }
}

// Flatten an element's visible text (frag/component-aware) for label matching.
// Stub-kit components pass their children through; other function components
// are invoked (scope-managed, anonymous scope so pathed slots aren't disturbed).
export function childText(c) {
  if (typeof c === 'string' || typeof c === 'number') return String(c)
  if (Array.isArray(c)) return c.map(childText).join('')
  if (c && typeof c === 'object') {
    if (c.__frag) return childText(c.children)
    if (c.__el && typeof c.type === 'function') {
      const name = c.type.name || 'anon'
      if (name === 'Stub') return childText(c.props?.children)
      let branch = null
      try { branch = invokeComponent(c.type, c.props, null) } catch { return '' }
      return childText(branch?.props?.children)
    }
    if (c.props?.children !== undefined) return childText(c.props.children)
  }
  return ''
}

// Flatten an element's visible text into a flat string list (each text node a
// separate entry). Function components are re-invoked against the scope of the
// element's position — pass `scope` when walking a tree whose hook slots live
// under a pathed namespace (the default '') so state-dependent subtrees render.
export function flattenTexts(node, out = [], scope = '') {
  if (node == null || typeof node === 'boolean') return out
  if (typeof node === 'string' || typeof node === 'number') { out.push(String(node)); return out }
  if (Array.isArray(node)) { node.forEach((child, i) => flattenTexts(child, out, `${scope}[${i}]`)); return out }
  if (node.__frag) return flattenTexts(node.children, out, scope)
  if (node.__el) {
    if (typeof node.type === 'function') {
      let branch = null
      try { branch = invokeComponent(node.type, node.props, scope) } catch { return out }
      return flattenTexts(branch, out, `${scope}>${node.type.name || 'anon'}`)
    }
    return flattenTexts(node.props?.children, out, scope)
  }
  return out
}

// ONE render pass over a contribution: render → walk twice. Two collect passes
// give the stub "reconciler" its second render so state-dependent subtrees
// (expanded details, staged options) show. The root scope derives from
// `scopeKey` (defaults to the label) so consecutive passes that should share
// hook state pass the same key. Pushes component throws into `errors`.
export function renderPass(contribution, label, errors, scopeKey) {
  if (!contribution || typeof contribution.render !== 'function') return { clickables: [], rows: [], texts: [] }
  let tree = null
  const rootScope = `render[${scopeKey || label}]`
  try {
    tree = contribution.render()
  } catch (e) {
    errors.push([label, e])
    return { clickables: [], rows: [], texts: [] }
  }
  const rows = []
  const clickables = []
  collectRows(tree, rows, errors, rootScope)
  collectClickables(tree, clickables, errors, rootScope)
  collectClickables(tree, clickables, errors, rootScope)
  // Texts share the walk's scope so state-dependent subtrees (expanded row,
  // resolved cards) render with the same hook slots the clickables used.
  return { clickables, rows, texts: flattenTexts(tree, [], rootScope) }
}

// Depth-first walk collecting every element whose function type has the given
// name, together with the scope path it was found at (so the caller can
// re-invoke it against the SAME hook slots the page walk used).
export function collectByType(node, typeName, out, errors, scope = '') {
  if (node == null || typeof node === 'boolean' || typeof node === 'string' || typeof node === 'number') return out
  if (Array.isArray(node)) {
    node.forEach((child, i) => collectByType(child, typeName, out, errors, `${scope}[${i}]`))
    return out
  }
  if (typeof node === 'object' && node.__frag) {
    collectByType(node.children, typeName, out, errors, scope)
    return out
  }
  if (typeof node === 'object' && node.__el) {
    if (typeof node.type === 'function') {
      if ((node.type.name || 'anon') === typeName) {
        out.push({ el: node, scope })
        return out
      }
      let branch = null
      try {
        branch = invokeComponent(node.type, node.props, scope)
      } catch (e) {
        errors.push([`byType>${node.type.name || 'anon'}`, e])
        return out
      }
      collectByType(branch, typeName, out, errors, `${scope}>${node.type.name || 'anon'}`)
      return out
    }
    collectByType(node.props && node.props.children, typeName, out, errors, scope)
    return out
  }
  return out
}

// Collect clickables from an already-rendered branch (no page re-render).
// Anonymous scope so pathed slot state is not disturbed.
export function collectBranchClickables(branch) {
  return collectClickables(branch, [], [], '')
}

// Find a clickable whose visible text equals `label`, tolerating the clarify
// option rows' leading letter chip ("A" + "TypeScript" → "ATypeScript") and
// falling back to an aria-label match. Returns the element or null.
export function clickableByText(clickables, label) {
  const el = clickables.find(b => {
    const text = childText(b.props?.children)
    if (text === label) return true
    // Lettered option row: strip a single leading A–Z chip letter.
    if (text.length === label.length + 1 && /^[A-Z]/.test(text)) {
      return text.slice(1) === label
    }
    return typeof b.props?.['aria-label'] === 'string' && b.props['aria-label'] === label
  })
  return el || null
}

// Drive one clickable: call its onClick (await if async).
export async function clickClickable(el) {
  if (!el || typeof el.props?.onClick !== 'function') return false
  await el.props.onClick()
  return true
}