"""Persisted local control/receipt tests; synthetic callables, no live services."""
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

from benchmarks.internal.explicit_fault_operator import (
    ControlError, Controller, Gate, PublicationRefused, Selector,
    queue_snapshot, queue_receipt, sql_lookup, wrap_initial_dispatch, wrap_recovery_dispatch, wrap_status_read,
)
from benchmarks.internal.explicit_operations_pilot import OperatorEvidence

JOB = str(UUID(int=4))
TASK = JOB
SCHEMA = 'replay_'+'a'*32


def selector():
    return Selector(run_id=UUID(int=1), case_id='mechanism-only', source_head='a'*40,
        tenant_id=UUID(int=2), owner_id=UUID(int=3), request_key_sha256='b'*64)


def row(**updates):
    return {'id': JOB, 'tenant_id': selector().tenant_id, 'proxy_user_id': selector().owner_id,
        'request_key_sha256':'b'*64, 'celery_task_id':TASK, 'queue_name':'starter-extraction', **updates}


def gate(controller, lookup=None, role='api', **updates):
    return Gate(controller.path, selector(), lookup or (lambda job:row()),role,
        **{'schema':SCHEMA, 'broker_url':'redis://127.0.0.1:55472/0','isolated_verified':True, **updates})


def test_initial_and_recovery_selective_refusal_then_unchanged_dispatch(tmp_path):
    with Controller(tmp_path/'control',selector(),'dispatch') as control:
        api=gate(control); worker=gate(control,role='worker')
        original=Mock(return_value='unchanged')
        initial=wrap_initial_dispatch(original,api)
        recovery=wrap_recovery_dispatch(original,worker)
        payload={'job_id':JOB,'tenant_id':str(selector().tenant_id),'_payload_reference':'extraction_job'}
        options={'args':[payload],'task_id':TASK,'queue':'starter-extraction','retry':False}
        with pytest.raises(PublicationRefused):initial('api.tasks.extraction_tasks.process_extraction_job',**options)
        with pytest.raises(PublicationRefused):recovery(payload,TASK,'starter-extraction')
        assert not original.called
        assert initial('other-task',**options)=='unchanged'
        control.release(); api.acknowledge_removal(); worker.acknowledge_removal()
        assert initial('api.tasks.extraction_tasks.process_extraction_job',**options)=='unchanged'
        original.assert_called_with('api.tasks.extraction_tasks.process_extraction_job',**options)
        receipt=control.removal_receipt()
        assert OperatorEvidence.model_validate(receipt).owner_adjudicated is False
        artifact=(control.directory/'removal-artifact.json').read_bytes()
        assert hashlib.sha256(artifact).hexdigest()==receipt['artifact_sha256']
        assert not json.loads(artifact)['dependency_health_verified']


@pytest.mark.parametrize('field,value',[('tenant_id',UUID(int=9)),('proxy_user_id',UUID(int=9)),
    ('request_key_sha256','c'*64),('id',str(UUID(int=9)))])
def test_other_sql_bound_identities_are_unaffected(tmp_path,field,value):
    with Controller(tmp_path/'control',selector(),'dispatch') as control:
        g=gate(control,lookup=lambda job:row(**{field:value}))
        assert not g.active('dispatch',JOB,TASK,'starter-extraction',str(selector().tenant_id))


@pytest.mark.parametrize('task,queue,tenant',[(str(UUID(int=9)),'starter-extraction',str(UUID(int=2))),
    (TASK,'growth-extraction',str(UUID(int=2))),(TASK,'starter-extraction',str(UUID(int=9)))])
def test_selected_publication_requires_exact_delivery_binding(tmp_path,task,queue,tenant):
    with Controller(tmp_path/'control',selector(),'dispatch') as control:
        with pytest.raises(ControlError):gate(control).active('dispatch',JOB,task,queue,tenant)


def test_status_read_returns_503_then_original_result(tmp_path):
    calls=[]
    async def original(service,*,job_id):calls.append(job_id);return {'status':'completed'}
    with Controller(tmp_path/'control',selector(),'status_read') as control:
        g=gate(control);read=wrap_status_read(original,g)
        with pytest.raises(Exception) as error:asyncio.run(read(None,job_id=JOB))
        assert error.value.status_code==503 and not calls
        control.release();g.acknowledge_removal()
        assert asyncio.run(read(None,job_id=JOB))=={'status':'completed'}
        assert calls==[JOB]


def test_automatic_expiry_restores_pass_through_but_cannot_export_valid_receipt(tmp_path):
    control=Controller(tmp_path/'control',selector(),'dispatch',hold_seconds=.05)
    g=gate(control)
    control.timer.join(1)
    assert not control.timer.is_alive()
    assert not g.active('dispatch',JOB,TASK,'starter-extraction',str(selector().tenant_id))
    assert json.loads(control.path.read_text())['valid'] is False
    with pytest.raises(ControlError):control.removal_receipt()


def test_missing_or_corrupt_control_is_not_silently_repaired(tmp_path):
    control=Controller(tmp_path/'control',selector(),'dispatch')
    try:
        g=gate(control);control.path.write_text('{invalid')
        with pytest.raises(ControlError):g.active('dispatch',JOB,TASK,'starter-extraction',str(selector().tenant_id))
        with pytest.raises(ControlError):control.release()
        assert control.path.read_text()=='{invalid'
    finally:control.timer.cancel()


def test_receipt_requires_both_dispatch_role_acknowledgements(tmp_path):
    with Controller(tmp_path/'control',selector(),'dispatch') as control:
        api=gate(control);worker=gate(control,role='worker')
        for g in (api,worker):
            assert g.active('dispatch',JOB,TASK,'starter-extraction',str(selector().tenant_id))
        control.release()
        api.acknowledge_removal()
        with pytest.raises(ControlError):control.removal_receipt()
        worker.acknowledge_removal()
        assert control.removal_receipt()['fault_removed_at']==json.loads(control.path.read_text())['removed_at']


def test_wrappers_can_attach_before_arming_without_injecting(tmp_path):
    with Controller(tmp_path/'control',selector(),'dispatch',start_armed=False) as control:
        g=gate(control)
        assert not g.active('dispatch',JOB,TASK,'starter-extraction',str(selector().tenant_id))
        assert control.timer is None
        control.arm()
        assert g.active('dispatch',JOB,TASK,'starter-extraction',str(selector().tenant_id))
        with pytest.raises(ControlError):control.arm()


def test_collection_failure_cannot_hold_fault_after_removal(tmp_path,monkeypatch):
    import benchmarks.internal.explicit_fault_operator as module
    with Controller(tmp_path/'control',selector(),'dispatch') as control:
        g=gate(control);control.release()
        def denied(*args):raise PermissionError('synthetic collection failure')
        monkeypatch.setattr(module,'write_new',denied)
        assert not g.active('dispatch',JOB,TASK,'starter-extraction',str(selector().tenant_id))
        with pytest.raises(ControlError):g.acknowledge_removal()


@pytest.mark.parametrize('updates',[{'isolated_verified':False},{'schema':'public'},
    {'broker_url':'redis://remote.invalid:6379/0'}])
def test_nonisolated_attachment_is_refused(tmp_path,updates):
    with Controller(tmp_path/'control',selector(),'dispatch') as control:
        with pytest.raises(ControlError):gate(control,**updates)


def test_queue_accounting_atomic_read_only_and_error_propagation():
    commands=[]
    class Batch:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,*args):commands.append(('get',args))
        def zcard(self,*args):commands.append(('zcard',args))
        def zscore(self,*args):commands.append(('zscore',args))
        def execute(self):return ['1',1,42]
    client=SimpleNamespace(connection_pool=SimpleNamespace(connection_kwargs={'host':'127.0.0.1','port':55472,'db':0}),
        pipeline=lambda transaction:Batch())
    result=queue_snapshot(client,selector(),JOB,'starter-extraction')
    assert result['queue_depth']==result['reserved_job_count']==1 and result['selected_job_reserved']
    assert [x[0] for x in commands]==['get','zcard','zscore']
    def failed(transaction):raise ConnectionError('synthetic unavailable')
    client.pipeline=failed
    with pytest.raises(ConnectionError):queue_snapshot(client,selector(),JOB,'starter-extraction')


def test_sql_projection_is_parameterized_bounded_read_only():
    statements=[]
    class Context:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def begin(self):return self
        def execute(self,query,params=None):
            statements.append((str(query),params))
            return SimpleNamespace(mappings=lambda:SimpleNamespace(one_or_none=lambda:row()))
    engine=SimpleNamespace(url=SimpleNamespace(host='127.0.0.1',port=55471,database='replay_test',drivername='postgresql+psycopg2'),connect=lambda:Context())
    assert sql_lookup(engine,JOB,schema=SCHEMA)==row()
    assert statements[0][0]=='SET TRANSACTION READ ONLY' and 'statement_timeout' in statements[1][0]
    assert statements[-1][1]=={'job_id':JOB}
    assert 'payload' not in statements[-1][0] and 'request_key_sha256' in statements[-1][0]
    engine.url.host='remote.invalid'
    with pytest.raises(ControlError):sql_lookup(engine,JOB,schema=SCHEMA)


def test_queue_receipt_preserves_artifact_and_requires_review(tmp_path):
    snapshot={'selector':selector().model_dump(mode='json'),'job_id':JOB,'queue_depth':1,
              'reserved_job_count':1,'selected_job_reserved':True}
    artifact=tmp_path/'queue.json'
    record=queue_receipt(snapshot,artifact)
    assert OperatorEvidence.model_validate(record).owner_adjudicated is False
    assert record['artifact_sha256']==hashlib.sha256(artifact.read_bytes()).hexdigest()
    with pytest.raises(FileExistsError):queue_receipt(snapshot,artifact)


def test_invalid_removed_clock_stops_control_observation(tmp_path):
    control=Controller(tmp_path/'control',selector(),'dispatch')
    g=gate(control);control.release()
    state=json.loads(control.path.read_text());state['removed_monotonic']=float('nan')
    control.path.write_text(json.dumps(state))
    with pytest.raises(ControlError):g.acknowledge_removal()
    with pytest.raises(ControlError):control.release()
