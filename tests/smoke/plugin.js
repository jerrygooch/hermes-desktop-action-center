// Action Center — Hermes Desktop plugin (single uncompiled ESM file).
//
// One panel for everything the operator must answer without opening a session:
// pending approvals (approve/deny + session/always variants), clarify questions
// (lettered choices A/B/C… + a type-your-own row, multi-select, batch),
// recent-message context with "Open full chat", cross-session search with live
// rail counts, goal/loop/heartbeat facts with pause/resume (stored sessions
// included), and expired requests that persist with Redo/Dismiss.
//
// Talks ONLY to its own backend namespace via ctx.rest:
//   GET  /summary?profile=                   — inbox aggregation (core inbox.list `inbox` shape)
//   GET  /details?session_key=&profile=      — per-session requests (core inbox.requests shape)
//   POST /respond  { request_id, choice, session_key, profile }
//   POST /answer   { request_id, answer, session_key, profile }  (+ question_id for batch, one call per question)
//   POST /control  { action, session_key, live_session_id?, profile }
//   POST /redo     { request_id, session_key, profile }
//   POST /dismiss  { request_id, session_key, profile }
//
// Surfaces: status chip (statusBar.right), full page (/action-center), sidebar
// nav row, palette commands (open/refresh). Data via React Query: 5s
// refetchInterval (never faster), gateway-event invalidation, invalidate after
// every mutation. No hand-rolled poll loops.

import {
  Button,
  Codicon,
  PALETTE_AREA,
  PanelBody,
  PanelEmpty,
  PanelHeader,
  PanelListRow,
  PanelMeta,
  PanelPill,
  PanelSectionLabel,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  SearchField,
  haptic,
  host,
  icons,
  queryClient,
  relativeTime,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

// ── constants ────────────────────────────────────────────────────────────────

const PAGE_ROUTE = '/action-center'
const QUERY_ROOT = ['action-center']
const SUMMARY_KEY = ['action-center', 'summary']
const DETAILS_KEY = ['action-center', 'details']
const REFETCH_MS = 5000
const UNKNOWN = '—'
const OTHER_PLACEHOLDER = 'Other (type your answer)'
const RECOMMENDED_LABEL = '(Recommended)'

const HAIRLINE = '1px solid var(--ui-stroke-tertiary)'
const textPrimary = { color: 'var(--ui-text-primary)' }
const textSecondary = { color: 'var(--ui-text-secondary)' }
const textTertiary = { color: 'var(--ui-text-tertiary)' }
const textQuaternary = { color: 'var(--ui-text-quaternary)' }
const errColor = { color: 'var(--ui-red)' }
const cardBox = { border: HAIRLINE, borderRadius: 6, background: 'var(--ui-bg-tertiary)' }
const resolvedBox = { ...cardBox, padding: '8px 12px', ...textTertiary, fontSize: 12 }

// Gateway events that mean request/automation state moved and the panel must
// re-read. Stream deltas (message.delta / reasoning.*) are deliberately NOT
// here — invalidating on every token would refetch continuously.
const INVALIDATING_EVENTS = new Set([
  'message.start',
  'message.complete',
  'session.control.update',
  'session.reclaimed',
  'session.title',
  'sessions.changed',
  'request.cancel',
  'background.complete',
  'subagent.complete'
])

// The core panel's category rail. 'Other' only appears when something is in it.
const CATEGORY_NAV = [
  { id: 'all', label: 'All sessions', icon: 'inbox' },
  { id: 'goals', label: 'Goals', icon: 'target' },
  { id: 'loops', label: 'Loops', icon: 'sync' },
  { id: 'heartbeats', label: 'Heartbeats', icon: 'pulse' },
  { id: 'background_tasks', label: 'Background tasks', icon: 'tools' },
  { id: 'subagents', label: 'Subagents', icon: 'account' },
  { id: 'other', label: 'Other', icon: 'folder' }
]

const LANE_LABEL = {
  needs_you: 'Needs you',
  running: 'Running',
  waiting: 'Waiting',
  scheduled: 'Scheduled'
}

const RECOGNIZED_CATEGORIES = new Set(['goals', 'loops', 'heartbeats', 'subagents', 'background_tasks'])

const ALLOWED_CHOICES = new Set(['once', 'session', 'always', 'deny'])

const EXPIRY_OUTCOME_TEXT = {
  notify_failed: 'never reached a surface that could answer it',
  session_closed: 'the session closed before an answer',
  timeout: 'timed out without an answer'
}

// ── tolerant payload helpers ─────────────────────────────────────────────────

function payloadFromResponse(response) {
  return response?.payload || response?.result?.payload || response?.result || response?.data || response || {}
}

function errMessage(error) {
  const payload = payloadFromResponse(error)
  const detail = payload?.detail ?? payload?.error ?? payload?.message ?? error?.message
  const text = typeof detail === 'string' && detail.trim() ? detail : String(error ?? '')
  return text.trim().slice(0, 240) || 'Something went wrong'
}

function currentProfile() {
  return String(host?.state?.profile?.get?.() ?? '')
}

async function postAction(ctx, path, body) {
  return payloadFromResponse(await ctx.rest(path, { method: 'POST', body }))
}

function invalidateActionCenter(qc) {
  const client = qc || queryClient
  void client.invalidateQueries({ queryKey: QUERY_ROOT })
}

// ── pure selectors (ported from the core store) ─────────────────────────────

function filterInboxItems(items, query) {
  const needle = query.trim().toLowerCase()
  if (!needle) return items
  return items.filter(item => {
    const haystack = `${item?.title ?? ''}\n${item?.session_key ?? ''}\n${item?.cwd ?? ''}`.toLowerCase()
    return haystack.includes(needle)
  })
}

function filterByCategory(items, category) {
  if (category === 'all') return items
  if (category === 'other') {
    return items.filter(item => !item.categories.some(c => RECOGNIZED_CATEGORIES.has(c)))
  }
  return items.filter(item => item.categories.includes(category))
}

function filterNeedsAttention(items) {
  return items.filter(item => item.lanes.includes('needs_you'))
}

function countByCategory(items, category) {
  return filterByCategory(items, category).length
}

// ── formatting (ported from the core panel) ──────────────────────────────────

function asText(value) {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

function asNumber(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function asRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value : null
}

function asStringList(value) {
  return Array.isArray(value)
    ? value.filter(item => typeof item === 'string' && item.trim().length > 0)
    : []
}

/** 300 → "5m", 5400 → "1.5h", 45 → "45s". */
function formatEvery(seconds) {
  if (seconds === null || seconds <= 0) return null
  if (seconds >= 3600) {
    const hours = seconds / 3600
    return Number.isInteger(hours) ? `${hours}h` : `${Math.round(hours * 10) / 10}h`
  }
  if (seconds >= 60) {
    const minutes = seconds / 60
    return Number.isInteger(minutes) ? `${minutes}m` : `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`
  }
  return `${Math.round(seconds)}s`
}

/** Epoch seconds → "in 4 min" / "12 min ago" (the SDK's canonical formatter). */
function formatAt(value) {
  return value !== null && value > 0 ? relativeTime(value * 1000) : null
}

function statusTone(status) {
  if (status === 'active') return 'good'
  if (status === 'paused') return 'warn'
  return 'muted'
}

function expiryLine(entry) {
  const seconds = Math.max(0, Date.now() / 1000 - entry.ended_at)
  const ago =
    seconds < 90 ? 'just now'
      : seconds < 3600 ? `${Math.round(seconds / 60)}m ago`
        : seconds < 86400 ? `${Math.round(seconds / 3600)}h ago`
          : `${Math.round(seconds / 86400)}d ago`
  return `Expired ${ago} — ${EXPIRY_OUTCOME_TEXT[entry.outcome] ?? entry.outcome}`
}

function laneDotStyle(item) {
  const lanes = Array.isArray(item.lanes) ? item.lanes : []
  if (lanes.includes('needs_you')) return { background: 'var(--ui-red)' }
  if (lanes.includes('running')) return { background: 'var(--ui-green)' }
  if (lanes.includes('waiting')) return { background: 'var(--ui-yellow)' }
  return { background: 'var(--ui-text-quaternary)' }
}

function formatCounts(item) {
  const parts = []
  if (item.subagent_count > 0) {
    parts.push(`${item.subagent_count} ${item.subagent_count === 1 ? 'subagent' : 'subagents'}`)
  }
  if (item.background_task_count > 0) {
    parts.push(`${item.background_task_count} ${item.background_task_count === 1 ? 'background task' : 'background tasks'}`)
  }
  if (item.subagent_count_unavailable) parts.push('Subagents unavailable')
  if (item.background_task_count_unavailable) parts.push('Background tasks unavailable')
  return parts.join(' · ')
}

function metaLine(item) {
  const lanes = Array.isArray(item.lanes) ? item.lanes : []
  const lane = lanes.map(l => LANE_LABEL[l] ?? l).join(' · ')
  const counts = formatCounts(item)
  return counts ? `${lane} · ${counts}` : lane
}

function chipStateText(badge, count, errors) {
  if (badge === 'red') {
    return errors > 0 ? `partial — ${errors} read ${errors === 1 ? 'error' : 'errors'}` : 'unsupported / could not read'
  }
  if (badge === 'amber') return `${count} ${count === 1 ? 'request' : 'requests'} need you`
  return 'all quiet'
}

// ── data plumbing ────────────────────────────────────────────────────────────

function summaryPath(profile) {
  return profile ? `/summary?profile=${encodeURIComponent(profile)}` : '/summary'
}

function detailsPath(sessionKey, profile) {
  const key = encodeURIComponent(sessionKey)
  return profile ? `/details?session_key=${key}&profile=${encodeURIComponent(profile)}` : `/details?session_key=${key}`
}

function useSummaryQuery(ctx, profile) {
  return useQuery({
    queryKey: [...SUMMARY_KEY, profile],
    queryFn: () => ctx.rest(summaryPath(profile)).then(payloadFromResponse),
    refetchInterval: REFETCH_MS,
    retry: false
  })
}

function useDetailsQuery(ctx, sessionKey, profile) {
  return useQuery({
    queryKey: [...DETAILS_KEY, sessionKey, profile],
    queryFn: () => ctx.rest(detailsPath(sessionKey, profile)).then(payloadFromResponse),
    enabled: Boolean(sessionKey),
    refetchInterval: REFETCH_MS,
    retry: false
  })
}

/** Chip click → dock the panel into the main workspace when the host can, else the page route. */
function openPanel(ctx) {
  haptic('tap')
  if (typeof host?.openWorkspace === 'function') {
    host.openWorkspace('action-center', {
      title: 'Action Center',
      render: () => jsx(ActionCenterPage, { ctx })
    })
  } else if (typeof host?.navigate === 'function') {
    host.navigate(PAGE_ROUTE)
  }
}

// ── shared bits ──────────────────────────────────────────────────────────────

function MetaPill({ status }) {
  return status
    ? jsx(PanelPill, { tone: statusTone(status), children: status }, 'pill')
    : UNKNOWN
}

function RailRow({ active, codicon, count, dim, dotStyle, label, onClick }) {
  return jsxs('button', {
    type: 'button',
    onClick,
    style: {
      display: 'flex',
      width: '100%',
      height: 28,
      alignItems: 'center',
      gap: 8,
      borderRadius: 6,
      border: 'none',
      cursor: 'pointer',
      padding: '0 8px',
      fontSize: 12,
      textAlign: 'left',
      background: active ? 'var(--ui-row-active-background)' : 'transparent',
      color: active ? textPrimary.color : textSecondary.color,
      opacity: dim ? 0.45 : undefined
    },
    children: [
      dotStyle
        ? jsx('span', { 'aria-hidden': true, style: { width: 6, height: 6, borderRadius: 9999, flexShrink: 0, ...dotStyle } }, 'dot')
        : jsx(Codicon, { name: codicon, size: '0.85rem', 'aria-hidden': true, style: { color: 'var(--ui-text-tertiary)', flexShrink: 0 } }, 'icon'),
      jsx('span', { style: { flex: 1, textAlign: 'left', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: label }, 'label'),
      (count > 0 || dim)
        ? jsx('span', { style: { fontVariantNumeric: 'tabular-nums', fontSize: 10, color: 'var(--ui-text-tertiary)' }, children: String(count) }, 'count')
        : null
    ]
  }, `rail-${label}`)
}

// ── approval card ────────────────────────────────────────────────────────────

function choiceLabel(choice, approval) {
  switch (choice) {
    case 'once': return 'Approve once'
    case 'session': return approval?.allow_session === false ? 'Approve once' : 'Approve for session'
    case 'always': return approval?.allow_permanent === false ? 'Approve once' : 'Always allow'
    case 'deny': return 'Deny'
    default: return choice
  }
}

function availableChoices(approval) {
  const choices = Array.isArray(approval?.choices) ? approval.choices : []
  return choices.filter(choice => {
    if (!ALLOWED_CHOICES.has(choice)) return false
    if (choice === 'session' && approval.allow_session === false) return false
    if (choice === 'always' && approval.allow_permanent === false) return false
    return true
  })
}

function ApprovalCard({ ctx, approval, sessionKey, onResolved }) {
  const qc = useQueryClient()
  const [submitting, setSubmitting] = useState(null)
  const [error, setError] = useState(null)
  const [resolved, setResolved] = useState(false)
  const submittingRef = useRef(false)

  const choices = availableChoices(approval)
  const hasRequestId = Boolean(approval?.request_id)

  const respond = async choice => {
    if (submittingRef.current || resolved || !hasRequestId) return
    submittingRef.current = true
    setSubmitting(choice)
    setError(null)
    try {
      const result = await postAction(ctx, '/respond', {
        request_id: approval.request_id,
        choice,
        session_key: sessionKey,
        profile: currentProfile()
      })
      const resolvedCount = typeof result?.resolved === 'number' ? result.resolved : (result?.ok ? 1 : 0)
      if (resolvedCount > 0) {
        setResolved(true)
        onResolved?.()
      } else {
        setError('Request may have been resolved already')
      }
    } catch (err) {
      setError(errMessage(err) || 'Failed to respond')
    } finally {
      submittingRef.current = false
      setSubmitting(null)
    }
  }

  if (resolved) {
    return jsx('div', { style: resolvedBox, children: 'Resolved' }, 'resolved')
  }

  return jsxs('div', {
    style: cardBox,
    'data-approval-request': '',
    children: [
      jsxs('div', { style: { padding: '8px 12px', display: 'grid', gap: 4 }, children: [
        jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: 8, ...textSecondary, fontSize: 12, minWidth: 0 }, children: [
          jsx(Codicon, { name: 'terminal', size: '0.8rem', 'aria-hidden': true, style: { flexShrink: 0 } }, 'icon'),
          jsx('span', { style: { fontWeight: 500, overflowWrap: 'anywhere' }, children: approval?.command || 'Pending approval' }, 'command')
        ] }, 'title-row'),
        approval?.description
          ? jsx('p', { style: { ...textTertiary, fontSize: 11, margin: 0, overflowWrap: 'anywhere' }, children: approval.description }, 'description')
          : null
      ] }, 'head'),
      error ? jsx('div', { style: { padding: '0 12px 4px', ...errColor, fontSize: 10 }, role: 'alert', children: error }, 'error') : null,
      jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: 6, padding: '4px 12px 8px', flexWrap: 'wrap' }, children: [
        choices.length === 0
          ? jsx('span', { style: { ...textTertiary, fontSize: 10 }, children: 'No supported actions available' }, 'none')
          : null,
        ...choices.map(choice => jsx(Button, {
          type: 'button',
          size: 'xs',
          variant: choice === 'deny' ? 'text' : choice === 'once' ? 'default' : 'text',
          disabled: submitting !== null || !hasRequestId,
          onClick: () => { void respond(choice) },
          children: submitting === choice ? '…' : choiceLabel(choice, approval)
        }, choice))
      ] }, 'actions')
    ]
  }, 'card')
}

// ── clarify cards ────────────────────────────────────────────────────────────

/** Bare text plus the backend's `(Recommended)` tag in tertiary text. */
function ChoiceLabel({ choice }) {
  const bare = typeof choice === 'string' && choice.endsWith(RECOMMENDED_LABEL)
    ? choice.slice(0, -RECOMMENDED_LABEL.length).trim()
    : choice
  if (bare === choice) return jsx('span', { children: choice }, 'bare')
  return jsxs('span', { children: [
    jsx('span', { children: bare }, 'text'),
    jsx('span', { style: textTertiary, children: ` ${RECOMMENDED_LABEL}` }, 'tag')
  ] }, 'labeled')
}

/** A/B/C… option letters — the trailing "Other" row takes the letter after the last choice. */
const letterFor = index => String.fromCharCode(65 + index)

function OptionRow({ char, children, onSelect, selected }) {
  return jsxs('button', {
    type: 'button',
    onClick: onSelect,
    style: {
      display: 'flex',
      width: '100%',
      alignItems: 'flex-start',
      gap: 8,
      borderRadius: 4,
      border: 'none',
      cursor: 'pointer',
      padding: '4px 8px',
      fontSize: 11,
      textAlign: 'left',
      background: selected ? 'var(--ui-row-active-background)' : 'transparent',
      color: selected ? textPrimary.color : textSecondary.color
    },
    children: [
      jsx('span', {
        'aria-hidden': true,
        style: {
          display: 'grid',
          placeItems: 'center',
          width: 16,
          height: 16,
          flexShrink: 0,
          borderRadius: 3,
          border: selected ? '1px solid transparent' : HAIRLINE,
          background: selected ? 'var(--ui-control-active-background)' : 'transparent',
          fontSize: 9,
          fontWeight: 500,
          color: selected ? textPrimary.color : textTertiary.color
        },
        children: char
      }, 'letter'),
      jsx('span', { style: { minWidth: 0, flex: 1, overflowWrap: 'anywhere' }, children }, 'text')
    ]
  }, `opt-${char}`)
}

function ClarifyResolved({ resolvedState }) {
  return jsx('div', { style: resolvedBox, children: resolvedState === 'expired' ? 'Expired' : 'Answered' }, 'resolved')
}

function SingleClarifyCard({ ctx, clarification, sessionKey, onResolved }) {
  const qc = useQueryClient()
  const [selectedChoices, setSelectedChoices] = useState([])
  const [freeText, setFreeText] = useState('')
  const [otherText, setOtherText] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)
  const [resolvedState, setResolvedState] = useState(null)
  const submitLock = useRef(false)

  const params = clarification?.params ?? {}
  const choices = Array.isArray(params.choices) ? params.choices : []
  const hasChoices = choices.length > 0
  const isMultiSelect = params.multi_select === true
  const question = params.question ?? 'The agent has a question'

  // Picking a choice and typing are mutually exclusive answers: the answer is a
  // picked choice, else the typed text. Multi-select sends a JSON array.
  const trimmedOther = otherText.trim()
  const selectedAnswer = isMultiSelect
    ? (selectedChoices.length > 0 ? JSON.stringify(selectedChoices) : null)
    : (selectedChoices[0] ?? null)
  const pendingAnswer = selectedAnswer ?? (trimmedOther || null) ?? (freeText || null)

  const submit = async answer => {
    if (submitting || resolvedState || !answer) return
    if (submitLock.current) return
    submitLock.current = true
    setSubmitting(true)
    setError(null)
    try {
      const result = await postAction(ctx, '/answer', {
        request_id: clarification.request_id,
        answer,
        session_key: sessionKey,
        profile: currentProfile()
      })
      if (result?.status === 'ok') {
        setResolvedState('answered')
        onResolved?.()
      } else if (result?.status === 'expired') {
        setResolvedState('expired')
      } else {
        setError('Unexpected response')
      }
    } catch (err) {
      setError(errMessage(err) || 'Failed to answer')
    } finally {
      submitLock.current = false
      setSubmitting(false)
    }
  }

  if (resolvedState === 'answered' || resolvedState === 'expired') {
    return jsx(ClarifyResolved, { resolvedState }, 'resolved')
  }

  const handleSelectChoice = choice => {
    setFreeText('')
    setOtherText('')
    if (isMultiSelect) {
      setSelectedChoices(prev => (prev.includes(choice) ? prev.filter(c => c !== choice) : [...prev, choice]))
    } else {
      setSelectedChoices([choice])
    }
  }

  const otherLetter = letterFor(choices.length)

  return jsxs('div', {
    style: cardBox,
    'data-clarify-request': '',
    children: [
      jsxs('div', { style: { padding: '8px 12px', display: 'grid', gap: 4 }, children: [
        jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: 8, ...textSecondary, fontSize: 12 }, children: [
          jsx(Codicon, { name: 'question', size: '0.8rem', 'aria-hidden': true }, 'icon'),
          jsx('span', { style: { fontWeight: 500 }, children: 'Question' }, 'title')
        ] }, 'title-row'),
        jsx('p', { style: { ...textPrimary, fontSize: 11, margin: 0, overflowWrap: 'anywhere' }, children: question }, 'question')
      ] }, 'head'),
      hasChoices
        ? jsxs('div', { style: { display: 'flex', flexDirection: 'column', gap: 1, padding: '0 12px 8px' }, children: [
            ...choices.map((choice, index) => jsx(OptionRow, {
              char: letterFor(index),
              selected: selectedChoices.includes(choice),
              onSelect: () => handleSelectChoice(choice),
              children: jsx(ChoiceLabel, { choice }, 'label')
            }, choice)),
            // The type-your-own row takes the letter after the last choice (A–C → D).
            jsxs('label', {
              style: {
                display: 'flex',
                width: '100%',
                alignItems: 'center',
                gap: 8,
                borderRadius: 4,
                padding: '4px 8px',
                fontSize: 11,
                background: trimmedOther ? 'var(--ui-row-active-background)' : 'transparent',
                color: trimmedOther ? textPrimary.color : textSecondary.color
              },
              children: [
                jsx('span', {
                  'aria-hidden': true,
                  style: {
                    display: 'grid',
                    placeItems: 'center',
                    width: 16,
                    height: 16,
                    flexShrink: 0,
                    borderRadius: 3,
                    border: trimmedOther ? '1px solid transparent' : HAIRLINE,
                    background: trimmedOther ? 'var(--ui-control-active-background)' : 'transparent',
                    fontSize: 9,
                    fontWeight: 500,
                    color: trimmedOther ? textPrimary.color : textTertiary.color
                  },
                  children: otherLetter
                }, 'letter'),
                jsx('input', {
                  value: otherText,
                  placeholder: OTHER_PLACEHOLDER,
                  onChange: e => {
                    setOtherText(e.target.value)
                    if (e.target.value) setSelectedChoices([])
                  },
                  style: { minWidth: 0, flex: 1, background: 'transparent', border: 'none', outline: 'none', fontSize: 11, color: textPrimary.color }
                }, 'input')
              ]
            }, 'other-row')
          ] }, 'options')
        : null,
      !hasChoices
        ? jsx('div', { style: { padding: '0 12px 8px' }, children: jsx('input', {
            value: freeText,
            placeholder: 'Type your answer…',
            onChange: e => setFreeText(e.target.value),
            style: { width: '100%', borderRadius: 4, border: HAIRLINE, background: 'transparent', padding: '4px 8px', fontSize: 11, color: textPrimary.color, outline: 'none' }
          }, 'input') }, 'free-row')
        : null,
      error ? jsx('div', { style: { padding: '0 12px 4px', ...errColor, fontSize: 10 }, role: 'alert', children: error }, 'error') : null,
      jsx('div', { style: { display: 'flex', alignItems: 'center', gap: 6, padding: '4px 12px 8px' }, children: jsx(Button, {
        type: 'button',
        size: 'xs',
        disabled: submitting || !pendingAnswer,
        onClick: () => { const answer = pendingAnswer; if (answer) void submit(answer) },
        children: submitting ? '…' : 'Submit'
      }, 'submit') }, 'actions')
    ]
  }, 'card')
}

function BatchClarifyCard({ ctx, clarification, sessionKey, onResolved }) {
  const qc = useQueryClient()
  const params = clarification?.params ?? {}
  const questions = Array.isArray(params.questions) ? params.questions : []
  const [stagedAnswers, setStagedAnswers] = useState({})
  const [stagedDrafts, setStagedDrafts] = useState({})
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)
  const [resolvedState, setResolvedState] = useState(null)
  const submitLock = useRef(false)

  const stagedAnswer = q => {
    const selected = stagedAnswers[q.qid] ?? []
    if (selected.length > 0) {
      return q.multi_select ? JSON.stringify(selected) : (selected[0] ?? null)
    }
    const draft = (stagedDrafts[q.qid] ?? '').trim()
    return draft || null
  }

  const answeredCount = questions.filter(q => stagedAnswer(q) !== null).length

  // Batch mirrors the core flow: stage per-question answers, then one call per
  // question (the plugin backend forwards each as the core's clarify.lock).
  const submit = async () => {
    if (submitting || resolvedState) return
    if (submitLock.current || answeredCount === 0) return
    submitLock.current = true
    setSubmitting(true)
    setError(null)
    try {
      let lastResult = { status: 'ok' }
      for (const q of questions) {
        const answer = stagedAnswer(q)
        if (answer === null) continue
        lastResult = await postAction(ctx, '/answer', {
          request_id: clarification.request_id,
          question_id: q.qid,
          answer,
          session_key: sessionKey,
          profile: currentProfile()
        })
        if (lastResult?.status === 'expired') break
      }
      if (lastResult?.status === 'ok') {
        setResolvedState('answered')
        onResolved?.()
      } else if (lastResult?.status === 'expired') {
        setResolvedState('expired')
      } else {
        setError('Unexpected response')
      }
    } catch (err) {
      setError(errMessage(err) || 'Failed to answer')
    } finally {
      submitLock.current = false
      setSubmitting(false)
    }
  }

  if (resolvedState === 'answered' || resolvedState === 'expired') {
    return jsx(ClarifyResolved, { resolvedState }, 'resolved')
  }

  const clearDraft = qid => {
    setStagedDrafts(prev => {
      if (!(qid in prev)) return prev
      const next = { ...prev }
      delete next[qid]
      return next
    })
  }

  return jsxs('div', {
    style: cardBox,
    'data-clarify-request': '',
    children: [
      jsx('div', { style: { padding: '8px 12px' }, children: jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: 8, ...textSecondary, fontSize: 12 }, children: [
        jsx(Codicon, { name: 'list-flat', size: '0.8rem', 'aria-hidden': true }, 'icon'),
        jsx('span', { style: { fontWeight: 500 }, children: `${questions.length} questions` }, 'title')
      ] }, 'title-row') }, 'head'),
      jsx('div', { style: { display: 'flex', flexDirection: 'column', gap: 8, padding: '0 12px 8px' }, children: questions.map(q => {
        const hasChoices = Array.isArray(q.choices) && q.choices.length > 0
        const isMultiSelect = q.multi_select === true
        const effectiveChoices = Array.isArray(q.choices) ? q.choices : []
        const selected = stagedAnswers[q.qid] ?? []
        const draft = stagedDrafts[q.qid] ?? ''
        return jsxs('div', { style: { borderRadius: 4, background: 'var(--ui-bg-quaternary)', padding: '6px 8px', display: 'grid', gap: 4 }, children: [
          jsx('p', { style: { ...textPrimary, fontSize: 11, margin: 0, overflowWrap: 'anywhere' }, children: q.question }, 'question'),
          hasChoices
            ? jsx('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 4 }, children: effectiveChoices.map((choice, index) => jsx(Button, {
                type: 'button',
                variant: 'ghost',
                size: 'micro',
                onClick: () => {
                  // A picked choice and a typed answer are mutually exclusive.
                  clearDraft(q.qid)
                  if (isMultiSelect) {
                    setStagedAnswers(prev => {
                      const current = prev[q.qid] ?? []
                      const next = current.includes(choice) ? current.filter(c => c !== choice) : [...current, choice]
                      return { ...prev, [q.qid]: next }
                    })
                  } else {
                    setStagedAnswers(prev => ({ ...prev, [q.qid]: [choice] }))
                  }
                },
                style: selected.includes(choice)
                  ? { background: 'var(--ui-row-active-background)', color: textPrimary.color }
                  : undefined,
                children: jsxs('span', { style: { display: 'inline-flex', alignItems: 'center', gap: 4 }, children: [
                  jsx('span', { style: { fontSize: 9, fontWeight: 600, opacity: 0.7 }, children: letterFor(index) }, 'letter'),
                  jsx(ChoiceLabel, { choice }, 'label')
                ] }, 'chip')
              }, choice)) }, 'choices')
            : null,
          hasChoices
            ? jsxs('label', { style: { display: 'flex', alignItems: 'center', gap: 6, fontSize: 10, ...textTertiary }, children: [
                jsx('span', {
                  'aria-hidden': true,
                  style: {
                    display: 'grid',
                    placeItems: 'center',
                    width: 14,
                    height: 14,
                    flexShrink: 0,
                    borderRadius: 3,
                    border: HAIRLINE,
                    fontSize: 8,
                    fontWeight: 600
                  },
                  children: letterFor(effectiveChoices.length)
                }, 'letter'),
                jsx('input', {
                  value: draft,
                  placeholder: OTHER_PLACEHOLDER,
                  onChange: e => {
                    const value = e.target.value
                    setStagedDrafts(prev => ({ ...prev, [q.qid]: value }))
                    if (value) setStagedAnswers(prev => ({ ...prev, [q.qid]: [] }))
                  },
                  style: { minWidth: 0, flex: 1, borderRadius: 4, border: HAIRLINE, background: 'transparent', padding: '2px 6px', fontSize: 10, color: textPrimary.color, outline: 'none' }
                }, 'input')
              ] }, 'other-row')
            : null,
          !hasChoices
            ? jsx('input', {
                value: stagedDrafts[q.qid] ?? '',
                placeholder: 'Answer…',
                onChange: e => {
                  const value = e.target.value
                  if (value) {
                    setStagedDrafts(prev => ({ ...prev, [q.qid]: value }))
                  } else {
                    setStagedDrafts(prev => {
                      const next = { ...prev }
                      delete next[q.qid]
                      return next
                    })
                  }
                },
                style: { width: '100%', borderRadius: 4, border: HAIRLINE, background: 'transparent', padding: '2px 6px', fontSize: 10, color: textPrimary.color, outline: 'none' }
              }, 'free-input')
            : null
        ] }, q.qid)
      }) }, 'questions'),
      error ? jsx('div', { style: { padding: '0 12px 4px', ...errColor, fontSize: 10 }, role: 'alert', children: error }, 'error') : null,
      jsx('div', { style: { display: 'flex', alignItems: 'center', gap: 6, padding: '4px 12px 8px' }, children: jsx(Button, {
        type: 'button',
        size: 'xs',
        disabled: submitting || answeredCount === 0,
        onClick: () => void submit(),
        children: submitting ? '…' : 'Submit answers'
      }, 'submit') }, 'actions')
    ]
  }, 'card')
}

function ClarifyCard({ ctx, clarification, sessionKey, onResolved }) {
  if (clarification?.kind === 'batch') {
    return jsx(BatchClarifyCard, { ctx, clarification, sessionKey, onResolved }, 'batch')
  }
  return jsx(SingleClarifyCard, { ctx, clarification, sessionKey, onResolved }, 'single')
}

// ── automation controls (goal / loop / heartbeat) ────────────────────────────

function AutomationControls({ ctx, kind, status, liveSessionId, sessionKey, sessionLive }) {
  const qc = useQueryClient()
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)

  const canPause = status === 'active'
  const canResume = status === 'paused'
  if (!canPause && !canResume) return null

  const action = `${kind}.${canPause ? 'pause' : 'resume'}`

  const run = async () => {
    setBusy(action)
    setError(null)
    try {
      await postAction(ctx, '/control', {
        action,
        session_key: sessionKey,
        // Live runtime when we have one; the stored key always travels so the
        // backend can pause/resume persisted automation for a session that is
        // not running (the core direct-state path).
        ...(liveSessionId ? { live_session_id: liveSessionId } : {}),
        profile: currentProfile()
      })
      invalidateActionCenter(qc)
    } catch (err) {
      setError(errMessage(err) || 'Control action failed')
    } finally {
      setBusy(null)
    }
  }

  return jsxs('div', { style: { marginTop: 6, display: 'grid', gap: 4 }, children: [
    jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }, children: [
      jsx(Button, {
        type: 'button',
        variant: 'secondary',
        size: 'xs',
        disabled: busy !== null,
        onClick: () => { void run() },
        children: busy ? 'Working…' : canPause ? `Pause ${kind}` : `Resume ${kind}`
      }, 'btn'),
      sessionLive === false
        ? jsx('span', { style: { ...textQuaternary, fontSize: 10 }, children: "session isn't running — applies to stored state" }, 'note')
        : null
    ] }, 'row'),
    error ? jsx('p', { role: 'alert', style: { ...errColor, fontSize: 10, margin: 0 }, children: error }, 'error') : null
  ] }, 'controls')
}

// ── automation fact sections ─────────────────────────────────────────────────

function GoalSection({ ctx, goal, liveSessionId, sessionKey, sessionLive }) {
  const contract = asRecord(goal.contract)
  const contractRows = [
    ['Outcome', asText(contract?.outcome)],
    ['Verification', asText(contract?.verification)],
    ['Constraints', asText(contract?.constraints)],
    ['Boundaries', asText(contract?.boundaries)],
    ['Stop when', asText(contract?.stop_when)]
  ].flatMap(([label, value]) => (value ? [{ label, value }] : []))

  const criteria = asStringList(goal.subgoals)
  const gates = Array.isArray(goal.gates)
    ? goal.gates.map(asRecord).filter(gate => gate !== null)
    : []
  const status = asText(goal.status) ?? ''
  const turnsUsed = asNumber(goal.turns_used)
  const maxTurns = asNumber(goal.max_turns)
  const turnText = turnsUsed === null ? null : maxTurns !== null && maxTurns > 0 ? `${turnsUsed} of ${maxTurns}` : String(turnsUsed)
  const waitBarrier = asText(goal.wait_barrier)
  const pausedReason = asText(goal.paused_reason)
  const lastVerdict = asText(goal.last_verdict)

  return jsxs('div', { style: { display: 'grid', gap: 6 }, children: [
    jsx(PanelSectionLabel, { children: 'Goal' }, 'label'),
    jsx('p', { style: { ...textSecondary, fontSize: 12, fontWeight: 500, margin: 0, overflowWrap: 'anywhere' }, children: asText(goal.title) ?? UNKNOWN }, 'title'),
    jsx(PanelMeta, {
      rows: [
        { label: 'Status', value: jsx(MetaPill, { status }, 'status') },
        ...(turnText ? [{ label: 'Turn', value: turnText }] : []),
        ...(waitBarrier ? [{ label: 'Waiting on', value: waitBarrier }] : []),
        ...(pausedReason ? [{ label: 'Paused', value: pausedReason }] : []),
        ...contractRows,
        ...(lastVerdict ? [{ label: 'Last verdict', value: lastVerdict }] : [])
      ]
    }, 'meta'),
    criteria.length > 0
      ? jsxs('div', { style: { display: 'grid', gap: 3 }, children: [
          jsx('div', { style: { ...textQuaternary, fontSize: 10 }, children: `Criteria (${criteria.length})` }, 'label'),
          ...criteria.map((criterion, index) => jsxs('div', { style: { display: 'flex', gap: 6, fontSize: 11, ...textSecondary }, children: [
            jsx('span', { 'aria-hidden': true, style: textQuaternary, children: '•' }, 'dot'),
            jsx('span', { style: { minWidth: 0, overflowWrap: 'anywhere' }, children: criterion }, 'text')
          ] }, `crit-${index}`))
        ] }, 'criteria')
      : null,
    gates.length > 0
      ? jsxs('div', { style: { display: 'grid', gap: 3 }, children: [
          jsx('div', { style: { ...textQuaternary, fontSize: 10 }, children: `Verify gates (${gates.length})` }, 'label'),
          ...gates.map((gate, index) => {
            const command = asText(gate.command) ?? UNKNOWN
            const attempts = asNumber(gate.attempts)
            const exitCode = asNumber(gate.last_exit_code)
            const detail = [
              attempts !== null ? `${attempts} ${attempts === 1 ? 'attempt' : 'attempts'}` : null,
              exitCode !== null ? `last exit ${exitCode}` : null
            ].filter(part => part !== null)
            return jsxs('div', { style: { display: 'flex', gap: 6, fontSize: 11, ...textSecondary }, children: [
              jsx('span', { 'aria-hidden': true, style: textQuaternary, children: '•' }, 'dot'),
              jsxs('span', { style: { minWidth: 0, overflowWrap: 'anywhere' }, children: [
                jsx('span', { style: { fontFamily: 'var(--ui-font-mono, ui-monospace, monospace)', fontSize: 10.5 }, children: command }, 'cmd'),
                detail.length > 0 ? jsx('span', { style: textTertiary, children: ` — ${detail.join(', ')}` }, 'detail') : null
              ] }, 'text')
            ] }, `gate-${index}`)
          })
        ] }, 'gates')
      : null,
    jsx(AutomationControls, { ctx, kind: 'goal', status, liveSessionId, sessionKey, sessionLive }, 'controls')
  ] }, 'goal')
}

function LoopSection({ ctx, loop, liveSessionId, sessionKey, sessionLive }) {
  const status = asText(loop.status) ?? ''
  const prompt = asText(loop.prompt)
  const mode = asText(loop.mode)
  const every = formatEvery(asNumber(loop.interval_seconds))
  const ticks = asNumber(loop.ticks_fired)
  const times = asNumber(loop.times)
  const maxTicks = asNumber(loop.max_ticks)
  const until = asText(loop.until)
  const nextDue = formatAt(asNumber(loop.next_due_at))
  const lastFired = formatAt(asNumber(loop.last_fired_at))
  const awaiting = loop.awaiting_response === true
  const deferred = loop.deferred_by_goal === true
  const pausedReason = asText(loop.paused_reason)
  const stopReason = asText(loop.last_stop_reason)
  const firedText = ticks === null ? null : times !== null && times > 0 ? `${ticks} of ${times}` : String(ticks)

  // What will actually end this loop: the judged condition, the user cap, or the backstop.
  const stopRow = until
    ? { label: 'Stops when', value: until }
    : times !== null && times > 0
      ? { label: 'Stops', value: `after ${times} ${times === 1 ? 'run' : 'runs'}` }
      : maxTicks !== null && maxTicks > 0
        ? { label: 'Backstop', value: `pauses after ${maxTicks} ticks` }
        : null

  return jsxs('div', { style: { display: 'grid', gap: 6 }, children: [
    jsx(PanelSectionLabel, { children: 'Loop' }, 'label'),
    prompt
      ? jsx('p', { title: prompt, style: { ...textSecondary, fontSize: 12, fontWeight: 500, margin: 0, overflowWrap: 'anywhere' }, children: prompt }, 'prompt')
      : null,
    jsx(PanelMeta, {
      rows: [
        { label: 'Status', value: jsx(MetaPill, { status }, 'status') },
        ...(every ? [{ label: 'Every', value: every }] : mode === 'self_paced' ? [{ label: 'Every', value: 'self-paced' }] : []),
        ...(firedText ? [{ label: 'Fired', value: firedText }] : []),
        ...(stopRow ? [stopRow] : []),
        ...(nextDue && !awaiting ? [{ label: 'Next run', value: nextDue }] : []),
        ...(lastFired ? [{ label: 'Last run', value: lastFired }] : []),
        ...(awaiting ? [{ label: 'Activity', value: 'a run is in progress' }] : []),
        ...(deferred ? [{ label: 'Deferred', value: 'while the goal runs' }] : []),
        ...(pausedReason ? [{ label: 'Paused', value: pausedReason }] : []),
        ...(stopReason ? [{ label: 'Stopped', value: stopReason }] : [])
      ]
    }, 'meta'),
    jsx(AutomationControls, { ctx, kind: 'loop', status, liveSessionId, sessionKey, sessionLive }, 'controls')
  ] }, 'loop')
}

function HeartbeatSection({ ctx, heartbeat, liveSessionId, sessionKey, sessionLive }) {
  const status = asText(heartbeat.status) ?? ''
  const prompt = asText(heartbeat.prompt)
  const every = formatEvery(asNumber(heartbeat.interval_seconds))
  const fires = asNumber(heartbeat.fire_count)
  const lastFired = formatAt(asNumber(heartbeat.last_fired_at))

  return jsxs('div', { style: { display: 'grid', gap: 6 }, children: [
    jsx(PanelSectionLabel, { children: 'Heartbeat' }, 'label'),
    prompt
      ? jsx('p', { title: prompt, style: { ...textSecondary, fontSize: 12, fontWeight: 500, margin: 0, overflowWrap: 'anywhere' }, children: prompt }, 'prompt')
      : null,
    jsx(PanelMeta, {
      rows: [
        { label: 'Status', value: jsx(MetaPill, { status }, 'status') },
        ...(every ? [{ label: 'Every', value: every }] : []),
        ...(fires !== null && fires > 0 ? [{ label: 'Fired', value: `${fires} ${fires === 1 ? 'time' : 'times'}` }] : []),
        ...(lastFired ? [{ label: 'Last fire', value: lastFired }] : [])
      ]
    }, 'meta'),
    jsx(AutomationControls, { ctx, kind: 'heartbeat', status, liveSessionId, sessionKey, sessionLive }, 'controls')
  ] }, 'heartbeat')
}

// ── expired request card ─────────────────────────────────────────────────────

function ExpiredRequestCard({ ctx, entry, sessionKey }) {
  const qc = useQueryClient()
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)

  const act = async kind => {
    setBusy(kind)
    setError(null)
    try {
      if (kind === 'redo') {
        await postAction(ctx, '/redo', { request_id: entry.request_id, session_key: sessionKey, profile: currentProfile() })
      } else {
        await postAction(ctx, '/dismiss', { request_id: entry.request_id, session_key: sessionKey, profile: currentProfile() })
      }
      invalidateActionCenter(qc)
    } catch (err) {
      setError(errMessage(err) || 'request failed')
    } finally {
      setBusy(null)
    }
  }

  return jsxs('div', {
    style: { ...cardBox, padding: '6px 8px', display: 'grid', gap: 4 },
    'data-expired-request': '',
    children: [
      jsx('p', { style: { margin: 0, fontFamily: 'var(--ui-font-mono, ui-monospace, monospace)', fontSize: 10.5, ...textPrimary, overflowWrap: 'anywhere' }, children: entry.command || '(command not recorded)' }, 'command'),
      entry.description
        ? jsx('p', { style: { margin: 0, ...textTertiary, fontSize: 10, overflowWrap: 'anywhere' }, children: entry.description }, 'description')
        : null,
      jsx('p', { style: { margin: 0, ...textQuaternary, fontSize: 9.5 }, children: expiryLine(entry) }, 'expiry'),
      jsxs('div', { style: { display: 'flex', gap: 8 }, children: [
        jsx(Button, { type: 'button', variant: 'secondary', size: 'xs', disabled: busy !== null, onClick: () => { void act('redo') }, children: busy === 'redo' ? 'Redoing…' : 'Redo' }, 'redo'),
        jsx(Button, { type: 'button', variant: 'text', size: 'xs', disabled: busy !== null, onClick: () => { void act('dismiss') }, children: busy === 'dismiss' ? 'Dismissing…' : 'Dismiss' }, 'dismiss')
      ] }, 'actions'),
      error ? jsx('p', { role: 'alert', style: { ...errColor, fontSize: 10, margin: 0 }, children: error }, 'error') : null
    ]
  }, 'card')
}

// ── inline detail (expanded row) ─────────────────────────────────────────────

function InlineDetail({ ctx, item }) {
  const qc = useQueryClient()
  const profile = useValue(host.state.profile)
  const details = useDetailsQuery(ctx, item.session_key, profile)

  const sessions = Array.isArray(details.data?.sessions) ? details.data.sessions : []
  const first = sessions.length > 0 ? sessions[0] : null
  const detailApprovals = sessions.flatMap(s => (Array.isArray(s?.approvals) ? s.approvals : []))
  const detailClarifications = sessions.flatMap(s => (Array.isArray(s?.clarifications) ? s.clarifications : []))
  const detailLiveSessionId = (first && Array.isArray(first.live_session_ids) ? first.live_session_ids[0] : '') || ''
  // null while details are loading or failed: the controls stay usable (stored-state
  // path) and no live-status claim is rendered until we actually know.
  const sessionLive = details.data ? detailLiveSessionId !== '' : null
  const detailContext = first?.context ?? { available: false, reason: null, messages: [] }
  const detailExpired = first && Array.isArray(first.expired_requests) ? first.expired_requests : []

  const retry = () => {
    haptic('tap')
    invalidateActionCenter(qc)
    void details.refetch()
  }

  const openFullChat = () => {
    haptic('tap')
    if (typeof host?.openSession === 'function') {
      void host.openSession(item.session_key, { profile: currentProfile() })
    } else {
      host.notify({ kind: 'error', message: 'Opening the session is not available in this desktop build.' })
    }
  }

  const lanes = Array.isArray(item.lanes) ? item.lanes : []
  const categories = Array.isArray(item.categories) ? item.categories : []
  const messages = Array.isArray(detailContext.messages) ? detailContext.messages : []

  const sectionLabel = (label, count) => jsx(PanelSectionLabel, { children: count != null ? `${label} (${count})` : label }, `sec-${label}`)

  return jsxs('div', {
    style: { display: 'grid', gap: 12, padding: '8px 0' },
    'data-session-detail': item.session_key,
    children: [
      jsx(PanelMeta, {
        rows: [
          { label: 'Title', value: item.title || UNKNOWN },
          { label: 'Session', value: item.session_key },
          { label: 'Lanes', value: lanes.map(lane => LANE_LABEL[lane] ?? lane).join(', ') || UNKNOWN },
          { label: 'Source', value: item.source || UNKNOWN },
          { label: 'Cwd', value: item.cwd || UNKNOWN },
          ...((item.subagent_count > 0 || item.subagent_count_unavailable)
            ? [{ label: 'Subagents', value: item.subagent_count_unavailable ? `${item.subagent_count}+ (unavailable)` : String(item.subagent_count) }]
            : []),
          ...((item.background_task_count > 0 || item.background_task_count_unavailable)
            ? [{ label: 'BG tasks', value: item.background_task_count_unavailable ? `${item.background_task_count}+ (unavailable)` : String(item.background_task_count) }]
            : []),
          ...(categories.length > 0 ? [{ label: 'Categories', value: categories.join(', ') }] : [])
        ]
      }, 'meta'),

      item.goal ? jsx(GoalSection, { ctx, goal: item.goal, liveSessionId: detailLiveSessionId, sessionKey: item.session_key, sessionLive }, 'goal') : null,
      item.loop ? jsx(LoopSection, { ctx, loop: item.loop, liveSessionId: detailLiveSessionId, sessionKey: item.session_key, sessionLive }, 'loop') : null,
      item.heartbeat ? jsx(HeartbeatSection, { ctx, heartbeat: item.heartbeat, liveSessionId: detailLiveSessionId, sessionKey: item.session_key, sessionLive }, 'heartbeat') : null,

      details.isPending && !details.data
        ? jsx('p', { role: 'status', style: { ...textTertiary, fontSize: 11, margin: 0 }, children: 'Loading request details…' }, 'loading')
        : null,
      details.error && !details.data
        ? jsxs('div', { role: 'alert', style: { display: 'grid', gap: 6 }, children: [
            jsx('p', { style: { ...errColor, fontSize: 11, margin: 0 }, children: errMessage(details.error) }, 'msg'),
            jsx(Button, { type: 'button', variant: 'secondary', size: 'xs', onClick: retry, children: 'Retry request details' }, 'retry')
          ] }, 'details-error')
        : null,

      // Context first: decide from the conversation, then act.
      jsxs('div', { style: { display: 'grid', gap: 4 }, children: [
        sectionLabel('Recent messages'),
        detailContext.available && messages.length > 0
          ? jsx('div', { style: { display: 'grid', gap: 6, marginTop: 4 }, children: messages.map((message, index) => jsxs('div', {
              style: { borderRadius: 6, border: HAIRLINE, background: 'var(--ui-bg-tertiary)', padding: '6px 8px', display: 'grid', gap: 2 },
              children: [
                jsx('span', { style: { ...textQuaternary, fontSize: 9.5, textTransform: 'uppercase', letterSpacing: '0.05em' }, children: message.role }, 'role'),
                jsx('p', { style: { margin: 0, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 11, ...textSecondary }, children: message.text }, 'text')
              ]
            }, `msg-${index}`)) }, 'messages')
          : jsx('p', { style: { ...textQuaternary, fontSize: 10, marginTop: 4 }, children: detailContext.reason ? `Unavailable: ${detailContext.reason}` : 'No recent transcript available.' }, 'empty')
      ] }, 'context'),

      detailApprovals.length > 0
        ? jsxs('div', { style: { display: 'grid', gap: 8 }, children: [
            sectionLabel('Approvals', detailApprovals.length),
            jsx('div', { style: { display: 'flex', flexDirection: 'column', gap: 8, marginTop: 4 }, children: detailApprovals.map(a => jsx(ApprovalCard, { ctx, approval: a, sessionKey: item.session_key }, a.request_id || 'approval')) }, 'list')
          ] }, 'approvals-sec')
        : null,

      detailClarifications.length > 0
        ? jsxs('div', { style: { display: 'grid', gap: 8 }, children: [
            sectionLabel('Questions', detailClarifications.length),
            jsx('div', { style: { display: 'flex', flexDirection: 'column', gap: 8, marginTop: 4 }, children: detailClarifications.map(c => jsx(ClarifyCard, { ctx, clarification: c, sessionKey: item.session_key }, c.request_id || 'clarify')) }, 'list')
          ] }, 'clarify-sec')
        : null,

      detailExpired.length > 0
        ? jsxs('div', { style: { display: 'grid', gap: 8 }, children: [
            sectionLabel('Expired requests', detailExpired.length),
            jsx('div', { style: { display: 'flex', flexDirection: 'column', gap: 8, marginTop: 4 }, children: detailExpired.map(entry => jsx(ExpiredRequestCard, { ctx, entry, sessionKey: item.session_key }, entry.request_id || 'expired')) }, 'list')
          ] }, 'expired-sec')
        : null,

      jsx('div', { children: jsx(Button, {
        type: 'button',
        variant: 'secondary',
        size: 'xs',
        onClick: openFullChat,
        children: jsxs('span', { style: { display: 'inline-flex', alignItems: 'center', gap: 6 }, children: [
          jsx(Codicon, { name: 'arrow-right', size: '0.75rem', 'aria-hidden': true }, 'icon'),
          jsx('span', { children: 'Open full chat' }, 'label')
        ] }, 'wrap')
      }, 'open-chat') }, 'open-chat-row')
    ]
  }, 'detail')
}

// ── the page ─────────────────────────────────────────────────────────────────

function ActionCenterPage({ ctx }) {
  const qc = useQueryClient()
  const profile = useValue(host.state.profile)
  const summary = useSummaryQuery(ctx, profile)

  const [category, setCategory] = useState('all')
  const [needsAttentionOnly, setNeedsAttentionOnly] = useState(false)
  const [query, setQuery] = useState('')
  const [narrowDuringSearch, setNarrowDuringSearch] = useState(false)
  const [expandedKey, setExpandedKey] = useState(null)

  const snapshot = summary.data ?? null
  const coverage = snapshot?.coverage ?? null
  const items = useMemo(() => (Array.isArray(snapshot?.items) ? snapshot.items : []), [snapshot])

  const trimmedQuery = query.trim()
  const isSearching = trimmedQuery.length > 0

  // Search spans every section; the rail then shows where the matches live. While a
  // search is running the list shows all matches until a section is clicked to narrow.
  const matches = useMemo(() => (isSearching ? filterInboxItems(items, trimmedQuery) : null), [isSearching, items, trimmedQuery])
  const narrowBySection = !isSearching || narrowDuringSearch
  const source = matches ?? items
  const needsAttentionCount = useMemo(() => filterNeedsAttention(source).length, [source])
  const visibleItems = useMemo(
    () => (narrowBySection ? (needsAttentionOnly ? filterNeedsAttention(source) : filterByCategory(source, category)) : source),
    [narrowBySection, needsAttentionOnly, source, category]
  )

  const navCounts = useMemo(() => ({
    all: source.length,
    goals: countByCategory(source, 'goals'),
    loops: countByCategory(source, 'loops'),
    heartbeats: countByCategory(source, 'heartbeats'),
    background_tasks: countByCategory(source, 'background_tasks'),
    subagents: countByCategory(source, 'subagents'),
    other: countByCategory(source, 'other')
  }), [source])
  const navItems = CATEGORY_NAV.filter(cat => cat.id !== 'other' || navCounts.other > 0)

  const handleRowSelect = item => {
    setExpandedKey(prev => (prev === item.session_key ? null : item.session_key))
  }

  const handleNeedsAttentionClick = () => {
    setNeedsAttentionOnly(true)
    setCategory('all')
    setNarrowDuringSearch(isSearching)
  }

  const handleCategoryClick = cat => {
    setCategory(cat)
    setNeedsAttentionOnly(false)
    // While searching, a section click narrows the matches; "All sessions" clears it.
    setNarrowDuringSearch(isSearching && cat !== 'all')
  }

  const handleQueryChange = value => {
    setQuery(value)
    if (!value.trim()) setNarrowDuringSearch(false)
  }

  const refresh = () => {
    haptic('tap')
    invalidateActionCenter(qc)
  }

  const coverageLine = coverage
    ? `Profile: ${coverage.profile} · ${coverage.scanned_sessions} sessions scanned${coverage.partial ? ' · partial' : ''}`
    : 'loading connection…'
  const hasErrors = (coverage?.errors?.length ?? 0) > 0
  const isPartial = coverage?.partial ?? false
  const isLoading = summary.isPending && !snapshot
  const isError = Boolean(summary.error) && !snapshot

  const retryButton = jsx(Button, { type: 'button', variant: 'secondary', size: 'xs', onClick: refresh, children: 'Try again' }, 'retry')

  const rail = jsxs('div', { style: { width: '11rem', flexShrink: 0, display: 'flex', flexDirection: 'column', gap: 2 }, children: [
    jsx(RailRow, {
      label: 'Needs attention',
      count: needsAttentionCount,
      dotStyle: needsAttentionCount > 0 ? { background: 'var(--ui-red)' } : { background: 'var(--ui-text-quaternary)' },
      active: needsAttentionOnly && narrowBySection,
      dim: isSearching && needsAttentionCount === 0,
      onClick: handleNeedsAttentionClick
    }, 'needs-attention'),
    jsx('div', { style: { height: 1, background: 'var(--ui-stroke-tertiary)', margin: '4px 2px' } }, 'sep'),
    ...navItems.map(cat => jsx(RailRow, {
      label: cat.label,
      codicon: cat.icon,
      count: navCounts[cat.id] ?? 0,
      active: narrowBySection ? (category === cat.id && !needsAttentionOnly) : cat.id === 'all',
      dim: isSearching && (navCounts[cat.id] ?? 0) === 0,
      onClick: () => handleCategoryClick(cat.id)
    }, cat.id))
  ] }, 'rail')

  const rowFor = item => jsxs('div', { style: { display: 'flex', flexDirection: 'column' }, children: [
    jsx(PanelListRow, {
      active: expandedKey === item.session_key,
      lead: jsx('span', {
        'aria-hidden': true,
        style: { display: 'inline-block', width: 6, height: 6, borderRadius: 9999, flexShrink: 0, ...laneDotStyle(item) }
      }, 'dot'),
      meta: metaLine(item),
      onSelect: () => handleRowSelect(item),
      rowKey: item.session_key,
      title: item.title || item.session_key
    }, 'row'),
    expandedKey === item.session_key
      ? jsx('div', { style: { marginLeft: 16, borderLeft: '2px solid var(--ui-stroke-tertiary)', paddingLeft: 12, paddingRight: 8, paddingBottom: 8 }, children: jsx(InlineDetail, { ctx, item }, 'detail') }, 'expanded')
      : null
  ] }, item.session_key)

  let listBody
  if (isLoading) {
    listBody = [jsx(PanelEmpty, { icon: 'loading~spin', title: 'Loading Action Center…', description: 'Reading the active profile…' }, 'loading')]
  } else if (isError) {
    listBody = [jsx(PanelEmpty, {
      icon: 'error',
      title: 'Action Center could not be loaded',
      description: errMessage(summary.error),
      action: retryButton
    }, 'error')]
  } else if (hasErrors) {
    // Partial read: warn loudly, then still show whatever survived.
    listBody = [
      jsx(PanelEmpty, {
        icon: 'warning',
        title: 'Partial read',
        description: `${coverage.errors.length} session snapshot${coverage.errors.length === 1 ? '' : 's'} could not be read.`,
        action: jsx(Button, { type: 'button', variant: 'secondary', size: 'xs', onClick: refresh, children: 'Refresh' }, 'refresh')
      }, 'partial'),
      ...visibleItems.map(rowFor)
    ]
  } else if (visibleItems.length === 0 && isSearching) {
    listBody = [jsx(PanelEmpty, {
      icon: 'search',
      title: 'No results',
      description: `No sessions matching "${trimmedQuery}"${narrowBySection ? ` in ${needsAttentionOnly ? 'needs attention' : category === 'all' ? 'all sessions' : category}` : ''}.`
    }, 'no-results')]
  } else if (visibleItems.length === 0) {
    listBody = [jsx(PanelEmpty, {
      icon: isPartial ? 'warning' : 'inbox',
      title: isPartial ? 'Incomplete data' : 'All clear',
      description: isPartial
        ? 'Some sessions could not be read. Counts may be incomplete.'
        : `Nothing in ${needsAttentionOnly ? 'needs attention' : category === 'all' ? 'this profile' : category}.`,
      action: isPartial ? jsx(Button, { type: 'button', variant: 'secondary', size: 'xs', onClick: refresh, children: 'Refresh' }, 'refresh') : undefined
    }, 'empty')]
  } else {
    listBody = visibleItems.map(rowFor)
  }

  return jsxs('div', {
    style: { height: '100%', minWidth: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden', color: textPrimary.color, fontSize: 12 },
    children: [
      jsx(PanelHeader, {
        title: 'Action Center',
        subtitle: coverageLine,
        actions: jsx(Button, {
          type: 'button',
          variant: 'secondary',
          size: 'xs',
          onClick: refresh,
          title: 'Refresh',
          'aria-label': 'Refresh Action Center',
          children: jsx(icons.RefreshCw, { 'aria-hidden': true, style: { width: 12, height: 12 } }, 'icon')
        }, 'refresh')
      }, 'header'),
      jsx(PanelBody, { children: [
        rail,
        jsxs('div', { style: { display: 'flex', minWidth: 0, flex: 1, flexDirection: 'column', gap: 4 }, children: [
          jsx(SearchField, {
            'aria-label': 'Search sessions',
            placeholder: 'Search title, key, or path…',
            value: query,
            onChange: handleQueryChange,
            containerClassName: 'w-full'
          }, 'search'),
          jsx('div', { style: { display: 'flex', minHeight: 0, flex: 1, flexDirection: 'column', overflowY: 'auto', overflowX: 'hidden' }, children: listBody }, 'rows')
        ] }, 'right')
      ] }, 'body')
    ]
  }, 'page')
}

// ── the status chip ──────────────────────────────────────────────────────────

function ActionCenterChip({ ctx }) {
  const profile = useValue(host.state.profile)
  const summary = useSummaryQuery(ctx, profile)

  const snapshot = summary.data ?? null
  const count = snapshot?.counts?.needs_you ?? 0
  const errors = snapshot?.coverage?.errors?.length ?? 0
  const isErr = Boolean(summary.error) && !snapshot
  const badge = isErr ? 'red' : (count > 0 ? 'amber' : (snapshot?.badge ?? 'none'))
  const stateText = !snapshot && !summary.error ? 'connecting' : chipStateText(badge, count, errors)
  const scope = snapshot?.coverage
    ? `${snapshot.coverage.profile} · ${snapshot.coverage.connection_scope ?? ''}`
    : 'connecting'
  const label = count > 0 ? `Action Center — ${count} need attention` : 'Action Center'
  const color = badge === 'amber' ? 'var(--ui-yellow)' : badge === 'red' ? 'var(--ui-red)' : 'var(--ui-text-tertiary)'

  return jsxs('button', {
    type: 'button',
    'aria-label': label,
    title: `Action Center — ${stateText}. ${scope}. Open the panel to act on requests.`,
    onClick: () => openPanel(ctx),
    style: {
      display: 'inline-flex',
      height: '100%',
      alignItems: 'center',
      gap: 4,
      padding: '0 6px',
      background: 'transparent',
      border: 'none',
      cursor: 'pointer',
      color,
      fontSize: 11
    },
    children: [
      jsx(Codicon, { name: 'inbox', size: '0.75rem', 'aria-hidden': true }, 'icon'),
      jsx('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: 'Action Center' }, 'label'),
      count > 0 ? jsx('span', { style: { fontVariantNumeric: 'tabular-nums' }, children: String(count) }, 'count') : null
    ]
  }, 'chip')
}

// ── plugin registration ──────────────────────────────────────────────────────

export default {
  id: 'action-center',
  name: 'Action Center',
  defaultEnabled: false,
  register(ctx) {
    // Gateway-event invalidation: request/automation state moves with NO user
    // action, so the 5s poll alone would leave stale controls on screen. The
    // '*' subscription is tracked by the host and retired with the plugin.
    try {
      host.onEvent('*', event => {
        const type = String(event?.type ?? '')
        if (INVALIDATING_EVENTS.has(type)) {
          void queryClient.invalidateQueries({ queryKey: QUERY_ROOT })
        }
      })
    } catch {
      // The event door is unavailable in this host — the 5s poll still refreshes.
    }

    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: PAGE_ROUTE },
        render: () => jsx(ActionCenterPage, { ctx })
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        data: { path: PAGE_ROUTE, label: 'Action Center', codicon: 'inbox' }
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'action-center.open',
          label: 'Open Action Center',
          icon: icons.Inbox,
          keywords: ['action', 'center', 'inbox', 'approvals', 'questions', 'automation'],
          run: () => openPanel(ctx)
        }
      },
      {
        id: 'refresh',
        area: PALETTE_AREA,
        data: {
          id: 'action-center.refresh',
          label: 'Refresh Action Center',
          icon: icons.RefreshCw,
          keywords: ['action', 'center', 'refresh', 'inbox'],
          run: () => {
            haptic('tap')
            invalidateActionCenter()
          }
        }
      },
      {
        id: 'chip',
        area: STATUSBAR_AREAS.right,
        order: 130,
        render: () => jsx(ActionCenterChip, { ctx })
      }
    ])
  }
}