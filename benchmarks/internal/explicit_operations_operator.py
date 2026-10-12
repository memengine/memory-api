"""Loopback display for the observer's requests; never sends backend requests.

Artifacts are private, unadjudicated observations, not acceptance labels.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from pathlib import Path
import secrets
import time
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse


PAGE = r"""<!doctype html><meta charset="utf-8"><title>MemoryOS local observer</title>
<h1>Local operation observation</h1><p id="state">Waiting for an operation.</p>
<button id="go" disabled>Run displayed operation</button>
<pre id="request"></pre><pre id="result"></pre><script>
const token=location.hash.slice(1); history.replaceState(null,'',location.pathname);
const documentId=crypto.randomUUID(); const go=document.getElementById('go'),
 state=document.getElementById('state'),result=document.getElementById('result'),requestView=document.getElementById('request');
let current=null, started=0, busy=false;
async function call(path,body) {
 const r=await fetch(path,{method:body?'POST':'GET',cache:'no-store',
  headers:{'X-Operator-Token':token,...(body?{'Content-Type':'application/json'}:{})},
  ...(body?{body:JSON.stringify(body)}:{})});
 if(!r.ok) throw Error('Operator refused: '+r.status); return r.json();
}
go.onclick=async()=>{if(!current||busy)return; busy=true;go.disabled=true;
 started=performance.now();try{await call('/action',{id:current.id,document_id:documentId});}
 catch(e){state.textContent=e.message;busy=false;}};
async function tick(){try{const p=await call('/pending');
 if(p.id&&p.id!==current?.id){current=p;busy=false; result.textContent='';
  requestView.textContent=JSON.stringify(p.request,null,2);
  state.textContent=p.operation+' — bound user '+p.bound_owner;go.disabled=false;
  if(p.previous_document===documentId){state.textContent='Reopen the private display URL in a new document for this operation.';go.disabled=true;}}
 if(p.result&&busy){result.textContent=JSON.stringify(p.result,null,2);
  state.textContent='Observed backend response. Completion alone does not prove application.';
  await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
  if(document.visibilityState!=='visible')throw Error('Display is hidden; paint evidence unavailable');
  await call('/paint',{id:p.id,document_id:documentId,elapsed_ms:performance.now()-started});busy=false;}
 }catch(e){state.textContent=e.message;}setTimeout(tick,50);}tick();
</script>"""


class DisplayOperator:
    def __init__(self, manifest, output: Path):
        self.manifest, self.output = manifest, output
        self.token = secrets.token_urlsafe(32)
        self.pending = None
        self.artifacts = {}
        self.action = asyncio.Event()
        self.paint = asyncio.Event()
        self.action_at = None
        self.document_id = None
        self.document_history = {}
        self.rejected_actions = []
        self.server = self.server_task = None
        self.app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self.app.add_api_route('/', self.page, methods=['GET'])
        self.app.add_api_route('/pending', self.get_pending, methods=['GET'])
        self.app.add_api_route('/action', self.accept_action, methods=['POST'])
        self.app.add_api_route('/paint', self.accept_paint, methods=['POST'])

    @property
    def origin(self):
        return f'http://127.0.0.1:{self.manifest.display.port}'

    def authorized(self, request):
        return (request.headers.get('host') == self.origin.split('//')[1]
                and secrets.compare_digest(request.headers.get('x-operator-token', ''), self.token)
                and request.headers.get('origin', self.origin) == self.origin)

    async def page(self, request: Request):
        if request.headers.get('host') != self.origin.split('//')[1]:
            return JSONResponse({}, status_code=403)
        nonce = secrets.token_urlsafe(16)
        return HTMLResponse(PAGE.replace('<script>', f'<script nonce="{nonce}">'), headers={
            'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': f"default-src 'none'; script-src 'nonce-{nonce}'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})

    async def get_pending(self, request: Request):
        if not self.authorized(request):
            return JSONResponse({}, status_code=403)
        return JSONResponse(self.pending or {}, headers={'Cache-Control': 'no-store'})

    async def message(self, request, fields):
        if not self.authorized(request):
            raise ValueError('unauthorized display')
        raw = await request.body()
        if len(raw) > 2048:
            raise ValueError('oversized display message')
        body = json.loads(raw)
        if not isinstance(body, dict) or set(body) != fields:
            self.rejected_actions.append({'reason': 'unexpected_fields', 'recorded_at': datetime.now(UTC).isoformat()})
            raise ValueError('unexpected fields')
        if not self.pending or body['id'] != self.pending['id']:
            raise ValueError('stale operation')
        if not isinstance(body['document_id'], str) or not 1 <= len(body['document_id']) <= 80:
            raise ValueError('invalid document identity')
        return body

    async def accept_action(self, request: Request):
        try:
            body = await self.message(request, {'id', 'document_id'})
            if self.action.is_set():
                raise ValueError('duplicate action')
            if body['document_id'] == self.pending.get('previous_document'):
                raise ValueError('fresh document required')
            self.document_id = body['document_id']
            self.action_at = time.perf_counter()
            self.action.set()
            return JSONResponse({'accepted': True})
        except (ValueError, TypeError):
            return JSONResponse({}, status_code=409)

    async def accept_paint(self, request: Request):
        try:
            body = await self.message(request, {'id', 'document_id', 'elapsed_ms'})
            elapsed = body['elapsed_ms']
            if (not self.action.is_set() or self.paint.is_set() or 'result' not in self.pending
                    or body['document_id'] != self.document_id or isinstance(elapsed, bool)
                    or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0):
                raise ValueError('invalid paint acknowledgement')
            server_elapsed = (time.perf_counter() - self.action_at) * 1000
            # A browser claim cannot make the server-observed duration smaller.
            self.pending['paint'] = {'browser_elapsed_ms': elapsed, 'server_elapsed_ms': server_elapsed,
                'measured_elapsed_ms': max(elapsed, server_elapsed)}
            self.paint.set()
            return JSONResponse({'accepted': True})
        except (ValueError, TypeError):
            return JSONResponse({}, status_code=409)

    async def begin(self, case, step, resolved_payload, deadline):
        if step.principal_env not in self.manifest.display.owner_bindings:
            raise ValueError('missing server-side owner binding')
        bound = self.manifest.display.owner_bindings[step.principal_env]
        requested = (resolved_payload or {}).get('external_user_id')
        # Never rewrite an independently supplied negative request into a valid one.
        self.pending = {'id': secrets.token_hex(16), 'operation': step.operation, 'bound_owner': bound,
                        'request': resolved_payload}
        if step.fresh_client and step.principal_env in self.document_history:
            self.pending['previous_document'] = self.document_history[step.principal_env]
        self.action.clear(); self.paint.clear(); self.action_at = None
        self.rejected_actions = []
        await asyncio.wait_for(self.action.wait(), max(0, deadline-time.perf_counter()))
        return {'bound_owner': bound, 'requested_owner': requested, 'document_id': self.document_id,
                'fresh_client_requested': step.fresh_client}

    async def finish(self, case, step, observation, identity, deadline):
        self.pending['result'] = observation
        artifact = {'run_id': str(self.manifest.run_id), 'case_id': case.case_id, 'step': step.name,
            'source_head': self.manifest.source_head, 'identity': identity,
            'paint': None, 'response': observation,
            'rejected_actions': self.rejected_actions, 'owner_adjudicated': False}
        path = self.output / f'display-{case.case_id}-{step.name}.json'
        try:
            await asyncio.wait_for(self.paint.wait(), max(0, deadline-time.perf_counter()))
            artifact['paint'] = self.pending['paint']
        finally:
            raw = (json.dumps(artifact, ensure_ascii=False, indent=2)+'\n').encode()
            with path.open('xb') as handle:
                handle.write(raw)
            if artifact['paint'] is not None:
                self.artifacts[(case.case_id, step.name)] = (artifact, hashlib.sha256(raw).hexdigest())
                self.document_history[step.principal_env] = self.document_id
            self.pending = None

    def receipt(self, case, kind, source='inspect', receipt_name=None):
        artifact, sha = self.artifacts[(case.case_id, source)]
        record = {'run_id': str(self.manifest.run_id), 'case_id': case.case_id,
            'source_head': self.manifest.source_head, 'kind': kind, 'artifact_sha256': sha,
            'owner_adjudicated': False, 'recorded_at': datetime.now(UTC).isoformat()}
        if kind == 'customer_rendering':
            record['first_rendered_elapsed_ms'] = artifact['paint']['measured_elapsed_ms']
        elif kind == 'browser_identity':
            # Captured binding is not proof of adversarial substitution or reopening.
            record['browser_identity_checked'] = False
        else:
            raise ValueError('display does not collect faults or queue data')
        path = self.output / f'receipt-{case.case_id}-{receipt_name or kind}.json'
        with path.open('x', encoding='utf-8') as handle:
            json.dump(record, handle, indent=2)
        return record

    async def start(self):
        import uvicorn
        self.server = uvicorn.Server(uvicorn.Config(self.app, host='127.0.0.1',
            port=self.manifest.display.port, access_log=False, log_level='warning'))
        self.server_task = asyncio.create_task(self.server.serve())
        until = time.perf_counter()+3
        while not self.server.started:
            if self.server_task.done():
                self.server_task.result()
                raise RuntimeError('display failed to start')
            if time.perf_counter() >= until:
                raise TimeoutError('display startup')
            await asyncio.sleep(.01)
        print(f'Private local display: {self.origin}/#{self.token}', flush=True)

    async def close(self):
        self.pending = None
        if self.server:
            self.server.should_exit = True
        if self.server_task:
            await asyncio.wait_for(self.server_task, 3)


def review_receipt(receipt_path, artifact_path, output_path, confirmed):
    """Explicit human review, offline: new receipt only; originals remain unchanged."""
    from benchmarks.internal.explicit_operations_pilot import OperatorEvidence
    if not confirmed:
        raise ValueError('explicit human review confirmation required')
    record = OperatorEvidence.model_validate_json(Path(receipt_path).read_bytes())
    artifact_raw = Path(artifact_path).read_bytes()
    if hashlib.sha256(artifact_raw).hexdigest() != record.artifact_sha256:
        raise ValueError('artifact digest mismatch')
    artifact = json.loads(artifact_raw)
    if any(str(artifact.get(key)) != str(getattr(record, key)) for key in ('run_id', 'case_id', 'source_head')):
        raise ValueError('artifact binding mismatch')
    if record.kind == 'customer_rendering' and (not artifact.get('paint') or
            record.first_rendered_elapsed_ms != artifact['paint'].get('measured_elapsed_ms')):
        raise ValueError('render measurement differs from artifact')
    record.owner_adjudicated = True
    with Path(output_path).open('x', encoding='utf-8') as handle:
        handle.write(record.model_dump_json(indent=2)+'\n')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Offline human adjudication; no backend or evaluation calls.')
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--artifact', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--confirm-human-review', action='store_true')
    args = parser.parse_args()
    try:
        review_receipt(args.receipt, args.artifact, args.output, args.confirm_human_review)
    except Exception as exc:
        parser.exit(1, f'Review stopped: {type(exc).__name__}. Private details suppressed.\n')
