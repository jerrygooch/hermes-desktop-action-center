// Sample data mirroring the core PR's e2e fixtures (apps/desktop/e2e/inbox-*.spec.ts).
// Shapes follow SPEC.md's REST contract — the same dicts the plugin backend returns.

export const goalSnapshot = {
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

export const loopSnapshot = {
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

export const heartbeatSnapshot = {
  prompt: 'Morning check',
  status: 'paused',
  interval_seconds: 1800,
  fire_count: 5,
  last_fired_at: Math.floor(Date.now() / 1000) - 3600
}

export const ITEMS = [
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

export const SUMMARY_POPULATED = {
  badge: 'amber',
  counts: { needs_you: 1, running: 2, waiting: 2, scheduled: 1, total: 6 },
  coverage: {
    profile: 'default', connection_scope: 'active connection and profile only',
    scanned_sessions: 6, partial: false, approval_scope: 'fixture', clarify_scope: 'fixture', errors: []
  },
  items: ITEMS
}

export const CONTEXT = {
  available: true,
  reason: null,
  messages: [
    { role: 'user', text: 'Clean up the stale build cache before the staging deploy finishes.', timestamp: 1789765000 },
    { role: 'assistant', text: 'Staging is green. I want to clear /tmp/build-cache, then finish the deploy.', timestamp: 1789765060 }
  ]
}

export const DETAILS_POPULATED = {
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

export const DETAILS_RESTRICTED = {
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

export const SUMMARY_EMPTY = {
  badge: 'none',
  counts: { needs_you: 0, running: 0, waiting: 0, scheduled: 0, total: 0 },
  coverage: {
    profile: 'default', connection_scope: 'active connection and profile only',
    scanned_sessions: 0, partial: false, approval_scope: 'fixture', clarify_scope: 'fixture', errors: []
  },
  items: []
}

export const SUMMARY_PARTIAL = {
  badge: 'red',
  counts: { needs_you: 0, running: 0, waiting: 0, scheduled: 0, total: 0 },
  coverage: {
    profile: 'default', connection_scope: 'active connection and profile only',
    scanned_sessions: 0, partial: true, approval_scope: 'fixture', clarify_scope: 'fixture',
    errors: ['Session snapshot failed: timeout']
  },
  items: []
}

// Query-cache keys the SDK stub resolves against: "<plugin-id>|<key joined by |>".
export const SUMMARY_KEY_LIVE = 'action-center|summary|default'
export const DETAILS_KEY_LIVE = key => `action-center|details|${key}|default`
export const DETAILS_KEY_LIVE_PROFILE = (key, profile) => `action-center|details|${key}|${profile}`