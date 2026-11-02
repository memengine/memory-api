"""Local single-purpose dispatcher. No Beat/lifecycle/reconciliation schedules."""
import json
import os
import time
import uuid
from api.celery_app import celery_app

tenant=str(uuid.UUID(os.environ['LOCAL_VECTOR_TENANT_ID']))
if os.environ.get('QDRANT_URL')!='http://qdrant:6333' or os.environ.get('CELERY_BROKER_URL')!='redis://redis:6379/2':
    raise RuntimeError('Refuse nonlocal resources')
while True:
    receipt=celery_app.send_task('api.tasks.vector_sync_tasks.process_outbox',
        kwargs={'tenant_id':tenant},queue='celery',expires=10)
    print(json.dumps({'task':'process_outbox','tenant_id':tenant,'task_id':receipt.id}),flush=True)
    time.sleep(5)
