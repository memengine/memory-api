"""Socket-free mechanism checks; no acceptance inputs or backend fixtures."""
import asyncio
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from benchmarks.internal.explicit_operations_operator import DisplayOperator, review_receipt
from benchmarks.internal.explicit_operations_pilot import OperatorEvidence


def operator(tmp_path):
    manifest = SimpleNamespace(run_id='00000000-0000-0000-0000-000000000001', source_head='a'*40,
        display=SimpleNamespace(port=55199, owner_bindings={'UNIT_KEY': 'mechanism-owner'}))
    return DisplayOperator(manifest, tmp_path)


def context():
    return SimpleNamespace(case_id='mechanism-only'), SimpleNamespace(name='inspect', operation='job',
        principal_env='UNIT_KEY', fresh_client=False)


def test_page_csp_and_requests_require_capability_and_origin(tmp_path):
    op = operator(tmp_path)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(op.app), base_url=op.origin) as client:
            page = await client.get('/')
            assert 'frame-ancestors' in page.headers['content-security-policy']
            assert op.token not in page.text
            assert (await client.get('/pending')).status_code == 403
            assert (await client.get('/pending', headers={'X-Operator-Token':op.token,
                'Origin':'http://foreign.invalid'})).status_code == 403
            assert (await client.get('/pending', headers={'X-Operator-Token':op.token})).status_code == 200
    asyncio.run(run())


def test_action_paint_binding_and_offline_review(tmp_path):
    op = operator(tmp_path); case, step = context()
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(op.app), base_url=op.origin,
                headers={'X-Operator-Token':op.token}) as client:
            begin = asyncio.create_task(op.begin(case,step,{'external_user_id':'requested-other'},time.perf_counter()+1))
            await asyncio.sleep(0)
            pending = (await client.get('/pending')).json()
            assert pending['bound_owner']=='mechanism-owner' and 'principal_env' not in pending
            body={'id':pending['id'],'document_id':'doc-one'}
            assert (await client.post('/action',json={**body,'external_user_id':'override'})).status_code==409
            assert (await client.post('/action',json=body)).status_code==200
            assert (await client.post('/action',json=body)).status_code==409
            identity=await begin
            assert identity['requested_owner']=='requested-other'  # never rewrite negative requests
            result={'status':200,'body':{'data':{'status':'completed'}},'elapsed_ms':2}
            finish=asyncio.create_task(op.finish(case,step,result,identity,time.perf_counter()+1))
            await asyncio.sleep(0)
            assert (await client.get('/pending')).json()['result']==result
            assert (await client.post('/paint',json={**body,'document_id':'different','elapsed_ms':1})).status_code==409
            assert (await client.post('/paint',json={**body,'elapsed_ms':1})).status_code==200
            await finish
    asyncio.run(run())
    artifact=tmp_path/'display-mechanism-only-inspect.json'
    receipt=op.receipt(case,'customer_rendering')
    assert receipt['owner_adjudicated'] is False
    assert OperatorEvidence.model_validate(receipt).owner_adjudicated is False
    artifact_data=json.loads(artifact.read_text())
    assert artifact_data['paint']['measured_elapsed_ms']>=artifact_data['paint']['server_elapsed_ms']
    assert 'applied' not in artifact_data['response']['body']['data']
    raw=(tmp_path/'receipt-mechanism-only-customer_rendering.json').read_bytes()
    output=tmp_path/'reviewed.json'
    with pytest.raises(ValueError): review_receipt(tmp_path/'receipt-mechanism-only-customer_rendering.json',artifact,output,False)
    review_receipt(tmp_path/'receipt-mechanism-only-customer_rendering.json',artifact,output,True)
    assert json.loads(output.read_text())['owner_adjudicated'] is True
    assert raw==(tmp_path/'receipt-mechanism-only-customer_rendering.json').read_bytes()
    with pytest.raises(FileExistsError): review_receipt(tmp_path/'receipt-mechanism-only-customer_rendering.json',artifact,output,True)


def test_missing_paint_preserves_response_without_success_receipt(tmp_path):
    op=operator(tmp_path); case,step=context()
    op.pending={'id':'operation'}; op.action.set(); op.action_at=time.perf_counter()
    response={'status':503,'body':{'error':'mechanism-only'}}
    with pytest.raises(TimeoutError):
        asyncio.run(op.finish(case,step,response,{},time.perf_counter()+.01))
    artifact=json.loads((tmp_path/'display-mechanism-only-inspect.json').read_text())
    assert artifact['response']==response and artifact['paint'] is None
    with pytest.raises(KeyError): op.receipt(case,'customer_rendering')


def test_action_timeout_sends_nothing_and_unbound_principal_refused(tmp_path):
    op=operator(tmp_path); case,step=context()
    with pytest.raises(TimeoutError): asyncio.run(op.begin(case,step,None,time.perf_counter()+.01))
    step.principal_env='UNBOUND'
    with pytest.raises(ValueError): asyncio.run(op.begin(case,step,None,time.perf_counter()+1))


def test_artifact_tampering_refuses_review(tmp_path):
    op=operator(tmp_path); case,step=context()
    async def run():
        op.pending={'id':'operation','paint':{'measured_elapsed_ms':4}};op.paint.set()
        await op.finish(case,step,{'status':200},{},time.perf_counter()+1)
    asyncio.run(run()); op.receipt(case,'customer_rendering')
    artifact=tmp_path/'display-mechanism-only-inspect.json'
    artifact.write_text('{}')
    with pytest.raises(ValueError): review_receipt(tmp_path/'receipt-mechanism-only-customer_rendering.json',artifact,tmp_path/'reviewed.json',True)


def test_display_has_no_backend_client_or_fault_routes(tmp_path):
    op=operator(tmp_path)
    assert {r.path for r in op.app.routes}=={'/','/pending','/action','/paint'}
    assert not hasattr(op,'backend_client')


def test_fresh_document_cannot_reuse_previous_document(tmp_path):
    op=operator(tmp_path); case,step=context(); step.fresh_client=True
    op.document_history['UNIT_KEY']='old-document'
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(op.app),base_url=op.origin,
                headers={'X-Operator-Token':op.token}) as client:
            task=asyncio.create_task(op.begin(case,step,None,time.perf_counter()+1))
            await asyncio.sleep(0)
            pending=(await client.get('/pending')).json()
            assert pending['previous_document']=='old-document'
            body={'id':pending['id'],'document_id':'old-document'}
            assert (await client.post('/action',json=body)).status_code==409
            assert (await client.post('/action',json={**body,'document_id':'new-document'})).status_code==200
            identity=await task
            assert identity['document_id']=='new-document'
    asyncio.run(run())
