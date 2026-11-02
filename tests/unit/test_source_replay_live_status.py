from types import SimpleNamespace as Row
from unittest.mock import AsyncMock
import uuid
import pytest
from api.errors import APIError
from api.services.memory_service import MemoryService
from api.services.memory_add_replay import REPLAY_BINDING_FIELD
from api.services.proxy_user_service import ProxyUserService


def run(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    finally:
        coroutine.close()
    raise AssertionError('Unexpected IO')


def service_and_binding():
    tenant,owner,job_id=map(str,[uuid.uuid4(),uuid.uuid4(),uuid.uuid4()])
    binding={'version':1,'tenant_id':tenant,'owner_id':'external-owner','payload_sha256':'a'*64}
    service=object.__new__(MemoryService)
    service._check_replay_permissions=AsyncMock()
    service.session=Row(execute=AsyncMock(return_value=Row(scalar_one_or_none=lambda:None)))
    job=Row(id=job_id,tenant_id=tenant,proxy_user_id=owner)
    proxy=Row(tenant_id=tenant,is_blocked=False,
        external_user_id_hash=ProxyUserService.hash_external_user_id(tenant,'external-owner'))
    service.session.get=AsyncMock(side_effect=[job,proxy])
    service.cache_service=Row(get_idempotent_response=AsyncMock(return_value={
        'job_id':job_id,'status':'queued',REPLAY_BINDING_FIELD:binding}))
    service._job_payload_with_live_status=lambda row:{'job_id':str(row.id),'status':'completed'}
    return service,binding


def test_cached_source_retry_returns_current_status():
    service,binding=service_and_binding()
    result=run(service.get_idempotent_memory_add(tenant_id=binding['tenant_id'],
        idempotency_key='synthetic',replay_binding=binding,durable=False))
    assert result['status']=='completed'
    service._check_replay_permissions.assert_awaited_once()


def test_changed_source_request_conflicts_before_receipt_lookup():
    service,binding=service_and_binding()
    with pytest.raises(APIError) as error:
        run(service.get_idempotent_memory_add(tenant_id=binding['tenant_id'],
            idempotency_key='synthetic',replay_binding={**binding,'payload_sha256':'b'*64},durable=False))
    assert error.value.status_code==409
    service.session.get.assert_not_awaited()


def test_missing_cached_job_is_not_reported_queued():
    service,binding=service_and_binding()
    service.session.get=AsyncMock(return_value=None)
    with pytest.raises(APIError) as error:
        run(service.get_idempotent_memory_add(tenant_id=binding['tenant_id'],
            idempotency_key='synthetic',replay_binding=binding,durable=False))
    assert error.value.status_code==409
