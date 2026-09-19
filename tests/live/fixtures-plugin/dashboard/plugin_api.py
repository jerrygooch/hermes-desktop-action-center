"""TEST ONLY: seed real gateway queues; never bundled in the release archive.

Every endpoint refuses outside an explicitly marked, disposable test home.
No LLM is created and no command is executed. Production routes are untouched.
"""
import os
import threading
import time
from pathlib import Path
from fastapi import APIRouter, HTTPException

router = APIRouter()
_refs = {}

def guard():
    home = Path(os.environ.get('HERMES_HOME', '')).resolve()
    root = Path(os.environ.get('AC_TEST_ROOT', '')).resolve()
    if not os.environ.get('AC_TEST_ROOT') or not home.is_relative_to(root) or not (home / 'ACTION_CENTER_SYNTHETIC_TEST').is_file():
        raise HTTPException(403, 'Synthetic fixture home required')
    from tui_gateway import server
    return server

@router.post('/seed')
def seed(body: dict):
    s = guard()
    kind = body['kind']
    key = 'ac-live-' + kind
    db = s._get_db()
    if not db.get_session(key):
        db.create_session(key, source='cli')
        db.set_session_title(key, 'SYNTHETIC ' + kind)
    sid = 'ac-runtime-' + kind
    if kind in ('once', 'deny', 'restricted', 'single', 'multi', 'batch', 'redo'):
        s._sessions[sid] = dict(session_key=key, history=[], history_lock=threading.Lock(), history_version=0, running=False, attached_images=[], cols=120, agent=None, created_at=time.time(), profile_home=None)
    if kind in ('once', 'deny', 'restricted'):
        from tools import approval
        from tools.approval_gateway_wait import _ApprovalEntry
        data = dict(request_id='ac-request-' + kind, command='echo SYNTHETIC_NEVER_EXECUTED', description='Synthetic approval: no tool execution')
        if kind == 'restricted':
            data.update(allow_session=False, allow_permanent=False)
        entry = _ApprovalEntry(data)
        with approval._lock:
            approval._gateway_queues.setdefault(key, []).append(entry)
        _refs[kind] = entry
        return dict(key=key, sid=sid, request_id=data['request_id'])
    if kind in ('single', 'multi', 'batch'):
        from tui_gateway.server_requests import ServerRequest
        params = dict(question='Synthetic choice?', choices=['Red', 'Blue'])
        qids = None
        if kind == 'multi':
            params['multi_select'] = True
        if kind == 'batch':
            params = dict(questions=[dict(qid='color', question='Synthetic color?', choices=['Red', 'Blue']), dict(qid='size', question='Synthetic size?', choices=['Small', 'Large'])])
            qids = ['color', 'size']
        req = ServerRequest(sid, 'clarify', params, qids=qids)
        with s._server_requests._lock:
            s._server_requests._open[req.id] = req
        _refs[kind] = req
        return dict(key=key, sid=sid, request_id=req.id)
    if kind in ('goal', 'loop', 'heartbeat'):
        if kind == 'goal':
            from hermes_cli.goals import GoalState, save_goal
            save_goal(key, GoalState(goal='Synthetic goal, never executed', status='active', turns_used=0, max_turns=6, created_at=time.time(), last_turn_at=time.time()))
        elif kind == 'loop':
            from hermes_cli.loops import LoopState, save_loop
            save_loop(key, LoopState(prompt='Synthetic loop, never executed', status='active', mode='interval', interval_seconds=300, current_delay=300, created_at=time.time()))
        else:
            from hermes_cli.heartbeat import HeartbeatState, save_heartbeat
            save_heartbeat(key, HeartbeatState(prompt='Synthetic heartbeat, never executed', status='active', interval_seconds=600, created_at=time.time(), last_fired_at=time.time(), fire_count=0))
        return dict(key=key)
    if kind == 'capture':
        from tools.approval_gateway_wait import _await_gateway_decision
        def unavailable(_):
            raise RuntimeError('Synthetic notification delivery failure')
        outcome = _await_gateway_decision(key, unavailable, dict(request_id='ac-captured', command='echo SYNTHETIC_CAPTURE', description='Real notification-failure lifecycle; no execution'))
        return dict(key=key, outcome=outcome)
    if kind in ('expired', 'redo'):
        if kind == 'redo':
            original = s._methods['prompt.submit']
            _refs[kind] = []
            def record_only(rid, params):
                if params.get('session_id') != sid:
                    return original(rid, params)
                _refs[kind].append(params)
                return {'result': {'status': 'queued'}}
            s._methods['prompt.submit'] = record_only
        import sys
        plugin = next(m for m in list(sys.modules.values()) if str(getattr(m, '__file__', '')).replace('\\', '/').endswith('/plugins/action-center/dashboard/plugin_api.py'))
        plugin.record_expired_request(db, key, dict(request_id='ac-expired', command='echo SYNTHETIC', description='Pre-seeded expiry, not capture proof'), 'timeout')
        return dict(key=key, request_id='ac-expired')
    raise HTTPException(400, 'Unknown fixture kind')

@router.get('/inspect')
def inspect(kind: str):
    s = guard()
    from tools import approval
    key = 'ac-live-' + kind
    ref = _refs.get(kind)
    result = dict(key=key, core_file=s.__file__, approvals=approval.list_gateway_approvals(key))
    if kind in ('once','deny','restricted'):
        result['result'] = ref.result
        result['settled'] = ref.event.is_set()
    if kind in ('single','multi','batch'):
        result.update(result=ref.result, settled=ref.event.is_set())
    if kind == 'redo':
        result['submitted'] = ref
    if kind == 'goal':
        from hermes_cli.goals import load_goal
        result['status'] = load_goal(key).status
    if kind == 'loop':
        from hermes_cli.loops import load_loop
        result['status'] = load_loop(key).status
    if kind == 'heartbeat':
        from hermes_cli.heartbeat import load_heartbeat
        result['status'] = load_heartbeat(key).status
    return result
