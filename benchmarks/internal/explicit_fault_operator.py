"""Isolated runtime-only fault boundaries; no scenario authoring or live installer.

Importing does nothing. A later approved bootstrap must explicitly attach wrappers.
Faults target SQL-bound job identity, never raw payloads or arbitrary chat wording.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ControlError(RuntimeError):
    pass


class PublicationRefused(ConnectionError):
    pass


class Selector(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    run_id: UUID
    case_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,80}$')
    source_head: str = Field(pattern=r'^[a-f0-9]{40}$')
    tenant_id: UUID
    owner_id: UUID
    request_key_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


def utc():
    return datetime.now(UTC).isoformat()


def write_new(path, data):
    with Path(path).open('x', encoding='utf-8') as handle:
        json.dump(data, handle, indent=2)


def read_control(path, selector):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        raise ControlError('Invalid control file')
    try:
        raw = path.read_bytes()
        if len(raw) > 16384:
            raise ValueError('Too large')
        data = json.loads(raw)
        allowed = {'selector', 'mode', 'state', 'armed_at', 'armed_monotonic', 'expires_monotonic',
                   'removed_at', 'removed_monotonic', 'valid', 'removal_reason'}
        if set(data) != allowed or Selector.model_validate(data['selector']) != selector:
            raise ValueError('Binding mismatch')
        if data['mode'] not in {'dispatch', 'status_read'} or data['state'] not in {'prepared', 'armed', 'removed'}:
            raise ValueError('Unknown state')
        if data['state'] == 'prepared':
            if any(data[k] is not None for k in ('armed_at', 'armed_monotonic', 'expires_monotonic',
                    'removed_at', 'removed_monotonic', 'removal_reason')) or data['valid'] is not True:
                raise ValueError('Invalid prepared state')
            return data
        start, end = data['armed_monotonic'], data['expires_monotonic']
        if isinstance(start, bool) or isinstance(end, bool) or not math.isfinite(start) or not math.isfinite(end) or not 0 < end-start <= 10:
            raise ValueError('Unbounded duration')
        if start > time.monotonic() or not isinstance(data['valid'], bool):
            raise ValueError('Invalid clock/state')
        if datetime.fromisoformat(data['armed_at']).tzinfo is None:
            raise ValueError('Missing timezone')
        if data['state'] == 'removed':
            removed = datetime.fromisoformat(data['removed_at'])
            monotonic_removed = data['removed_monotonic']
            if (removed.tzinfo is None or removed > datetime.now(UTC) or isinstance(monotonic_removed, bool)
                    or not math.isfinite(monotonic_removed) or monotonic_removed < start
                    or (data['valid'] and monotonic_removed >= end)):
                raise ValueError('Invalid removal clock')
        return data
    except Exception as exc:
        raise ControlError('Control state invalid; stop experiment') from exc


class Controller:
    """One writer; automatic expiry restores pass-through but invalidates the trial."""
    def __init__(self, directory, selector, mode, hold_seconds=5, *, start_armed=True):
        if mode not in {'dispatch', 'status_read'} or not 0 < hold_seconds <= 10:
            raise ControlError('Unknown mode or unbounded duration')
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.path = self.directory/'control.json'
        self.selector = selector
        self.lock = threading.Lock()
        self.hold_seconds = hold_seconds
        self.timer = None
        self.state = {'selector': selector.model_dump(mode='json'), 'mode': mode, 'state': 'prepared',
            'armed_at': None, 'armed_monotonic': None, 'expires_monotonic': None,
            'removed_at': None, 'removed_monotonic': None, 'valid': True, 'removal_reason': None}
        write_new(self.path, self.state)
        if start_armed:
            self.arm()

    def arm(self):
        with self.lock:
            state = read_control(self.path, self.selector)
            if state['state'] != 'prepared':
                raise ControlError('Control already armed/removed; no rerun')
            started = time.monotonic()
            state.update(state='armed', armed_at=utc(), armed_monotonic=started,
                         expires_monotonic=started+self.hold_seconds)
            temporary = self.directory/f'.control-{uuid4().hex}.tmp'
            write_new(temporary, state)
            temporary.replace(self.path)
            self.state = state
            self.timer = threading.Timer(self.hold_seconds, self._expire)
            self.timer.daemon = True
            self.timer.start()

    def _remove(self, automatic):
        with self.lock:
            # Refuse corrupt/rebound state, never silently repair the control file.
            current = read_control(self.path, self.selector)
            if current['state'] in {'prepared', 'removed'}:
                return current
            now = time.monotonic()
            expired = automatic or now >= current['expires_monotonic']
            current.update(state='removed', removed_at=utc(), removed_monotonic=now,
                valid=not expired, removal_reason='automatic_expiry' if expired else 'operator_release')
            temporary = self.directory/f'.control-{uuid4().hex}.tmp'
            write_new(temporary, current)
            temporary.replace(self.path)
            self.state = current
            return current

    def _expire(self):
        try:
            self._remove(True)
        except ControlError:
            # Gates independently cease injection at the pinned expiry even if collection fails.
            pass

    def release(self):
        try:
            return self._remove(False)
        finally:
            if self.timer:
                self.timer.cancel()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.release()

    def removal_receipt(self):
        state = read_control(self.path, self.selector)
        if state['state'] != 'removed' or not state['valid'] or state['removal_reason'] != 'operator_release':
            raise ControlError('No valid observed operator removal; diagnostic only')
        paths = list((self.directory/'observations').glob('*.json'))
        if len(paths) > 200:
            raise ControlError('Observation bound exceeded')
        events = []
        for path in paths:
            if path.stat().st_size > 16384:
                raise ControlError('Observation too large')
            event = json.loads(path.read_bytes())
            if event.get('selector') != self.selector.model_dump(mode='json') or event.get('experiment_valid') is False:
                raise ControlError('Invalid experiment observation')
            events.append(event)
        roles = {'api', 'worker'} if state['mode'] == 'dispatch' else {'api'}
        acknowledged = {e['role'] for e in events if e.get('event') == 'removal_observed' and e.get('removed_at') == state['removed_at']}
        if not roles <= acknowledged:
            raise ControlError('Required process removal acknowledgements unavailable')
        if not roles <= {e['role'] for e in events if e.get('event') == 'fault_refused'}:
            raise ControlError('Required selected refusal observations unavailable')
        artifact = {'control': state, 'observations': events, 'dependency_health_verified': False}
        raw = json.dumps(artifact, sort_keys=True).encode()
        with (self.directory/'removal-artifact.json').open('xb') as handle:
            handle.write(raw)
        return {'run_id': str(self.selector.run_id), 'case_id': self.selector.case_id,
            'source_head': self.selector.source_head, 'kind': 'fault_injection',
            'artifact_sha256': hashlib.sha256(raw).hexdigest(), 'owner_adjudicated': False,
            'recorded_at': utc(), 'fault_removed_at': state['removed_at']}


def sql_lookup(engine, job_id, *, schema):
    """Fixed parameterized, bounded, read-only lookup; no raw payload projection."""
    from sqlalchemy import text
    url = engine.url
    if (url.host != '127.0.0.1' or url.port != 55471 or url.database != 'replay_test'
            or not url.drivername.startswith('postgresql') or not re.fullmatch(r'replay_[a-f0-9]{32}', schema)):
        raise ControlError('SQL lookup must use isolated database/schema')
    job_id = str(UUID(str(job_id)))
    with engine.connect() as connection:
        with connection.begin():
            connection.execute(text('SET TRANSACTION READ ONLY'))
            connection.execute(text("SET LOCAL statement_timeout='1s'"))
            connection.execute(text('SET LOCAL search_path="'+schema+'"'))
            return connection.execute(text('SELECT id, tenant_id, proxy_user_id, request_key_sha256, '
                'celery_task_id, queue_name FROM extraction_jobs WHERE id=:job_id'), {'job_id': job_id}).mappings().one_or_none()


class Gate:
    def __init__(self, control_path, selector, lookup, role, *, schema, broker_url, isolated_verified):
        parsed = urlsplit(broker_url)
        if (isolated_verified is not True or not re.fullmatch(r'replay_[a-f0-9]{32}', schema)
                or parsed.scheme != 'redis' or parsed.hostname != '127.0.0.1' or parsed.port != 55472
                or parsed.path != '/0' or parsed.username or parsed.password or parsed.query or parsed.fragment
                or role not in {'api', 'worker'}):
            raise ControlError('Only explicitly verified isolated runtime accepted')
        self.path, self.selector, self.lookup, self.role = Path(control_path), selector, lookup, role
        self.events = self.path.parent/'observations'
        self.events.mkdir(exist_ok=True)
        # Pin expiry independently so corrupt/missing state cannot leave a fault on forever.
        self.pin = read_control(self.path, selector)
        self.expiry = self.pin['expires_monotonic']
        self.collection_failed = False

    def record(self, event, **details):
        try:
            write_new(self.events/f'{self.role}-{uuid4().hex}.json', {'event': event, 'role': self.role,
                'selector': self.selector.model_dump(mode='json'), 'observed_at': utc(),
                'observed_monotonic': time.monotonic(), **details})
            return True
        except OSError:
            self.collection_failed = True
            return False

    def active(self, mode, job_id, task_id=None, queue_name=None, advertised_tenant=None):
        # Expiry is a safety escape, not an acceptance pass or a new recovery origin.
        if self.expiry is not None and time.monotonic() >= self.expiry:
            self.record('expiry_pass_through', experiment_valid=False)
            return False
        state = read_control(self.path, self.selector)
        if state['mode'] != self.pin['mode']:
            raise ControlError('Pinned fault mode changed')
        if state['state'] == 'prepared':
            return False
        if self.expiry is None:
            self.pin = state
            self.expiry = state['expires_monotonic']
        if any(state[k] != self.pin[k] for k in ('mode', 'armed_at', 'armed_monotonic', 'expires_monotonic')):
            raise ControlError('Pinned fault configuration changed')
        if state['state'] == 'removed' or state['mode'] != mode:
            self.record('control_pass_through', state=state['state'], mode=state['mode'])
            return False
        try:
            row = self.lookup(str(UUID(str(job_id))))
        except ValueError:
            return False
        if row is None:
            return False
        matches = (str(row['id']) == str(job_id) and str(row['tenant_id']) == str(self.selector.tenant_id)
            and str(row['proxy_user_id']) == str(self.selector.owner_id)
            and row['request_key_sha256'] == self.selector.request_key_sha256)
        if not matches:
            return False
        if mode == 'dispatch' and (not task_id or str(row['celery_task_id']) != str(task_id)
                or queue_name != row['queue_name'] or str(advertised_tenant) != str(self.selector.tenant_id)):
            raise ControlError('Selected durable delivery identity differs')
        if not self.record('fault_refused', mode=mode, job_id=str(job_id), task_id=task_id):
            raise ControlError('Fault evidence collection failed; stop experiment')
        return True

    def acknowledge_removal(self):
        state = read_control(self.path, self.selector)
        if state['state'] != 'removed':
            raise ControlError('Removal not observed')
        if self.collection_failed or not self.record('removal_observed', removed_at=state['removed_at'], experiment_valid=state['valid']):
            raise ControlError('Removal evidence collection incomplete')
        # This acknowledgement proves control observation, not dependency/network health.
        return state['removed_at']


def wrap_initial_dispatch(original, gate):
    def dispatch(name, *args, **kwargs):
        if name == 'api.tasks.extraction_tasks.process_extraction_job':
            payloads = kwargs.get('args', args[0] if args else [])
            if isinstance(payloads, (list, tuple)) and len(payloads) == 1 and isinstance(payloads[0], dict):
                job_id = payloads[0].get('job_id')
                if gate.active('dispatch', job_id, kwargs.get('task_id'), kwargs.get('queue'), payloads[0].get('tenant_id')):
                    raise PublicationRefused('Selected synthetic publication refused')
        return original(name, *args, **kwargs)
    return dispatch


def wrap_recovery_dispatch(original, gate):
    def dispatch(payload, task_id, queue_name):
        if gate.active('dispatch', payload.get('job_id'), task_id, queue_name, payload.get('tenant_id')):
            raise PublicationRefused('Selected synthetic recovery publication refused')
        return original(payload, task_id, queue_name)
    return dispatch


def wrap_status_read(original, gate):
    async def read(service, *, job_id):
        if await asyncio.to_thread(gate.active, 'status_read', job_id):
            from api.errors import APIError
            raise APIError(status_code=503, code='LOCAL_FAULT_503', error='synthetic_status_unavailable')
        return await original(service, job_id=job_id)
    return read


def queue_snapshot(client, selector, job_id, queue_name):
    connection = client.connection_pool.connection_kwargs
    if connection.get('host') != '127.0.0.1' or connection.get('port') != 55472 or connection.get('db', 0) != 0:
        raise ControlError('Queue accounting must use isolated Redis')
    job_id = str(UUID(str(job_id)))
    if queue_name not in {'free-extraction', 'starter-extraction', 'growth-extraction', 'scale-extraction', 'enterprise-extraction'}:
        raise ControlError('Unknown extraction queue')
    # Atomic read-only Redis command batch; errors propagate, never fall back to zero.
    with client.pipeline(transaction=True) as batch:
        batch.get(f'tenant_queue_depth:{selector.tenant_id}:{queue_name}')
        batch.zcard(f'queue_depth:{queue_name}:jobs')
        batch.zscore(f'queue_depth:{queue_name}:jobs', f'{selector.tenant_id}:{job_id}')
        depth, total, score = batch.execute()
    depth = 0 if depth is None else int(depth)
    if depth < 0 or not isinstance(total, int) or total < 0:
        raise ControlError('Invalid queue accounting')
    return {'selector': selector.model_dump(mode='json'), 'job_id': job_id, 'queue_name': queue_name,
        'recorded_at': utc(), 'queue_depth': total, 'reserved_job_count': depth,
        'selected_job_reserved': score is not None}


def queue_receipt(snapshot, artifact_path):
    """Persist actual observation bytes; collection is not human adjudication."""
    selector = Selector.model_validate(snapshot['selector'])
    if any(isinstance(snapshot[name], bool) or not isinstance(snapshot[name], int) or snapshot[name] < 0
           for name in ('queue_depth', 'reserved_job_count')):
        raise ControlError('Invalid queue counts')
    raw = json.dumps(snapshot, sort_keys=True).encode()
    with Path(artifact_path).open('xb') as handle:
        handle.write(raw)
    return {'run_id': str(selector.run_id), 'case_id': selector.case_id, 'source_head': selector.source_head,
        'kind': 'queue_accounting', 'artifact_sha256': hashlib.sha256(raw).hexdigest(),
        'owner_adjudicated': False, 'recorded_at': utc(),
        'queue_depth': snapshot['queue_depth'], 'reserved_job_count': snapshot['reserved_job_count']}
