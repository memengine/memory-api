"""Explicit user-action SDK transport, never hidden confirmation or polling."""
import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest

from tests.unit.test_sdk_preferences import invoke
from memoryos import Memory, AsyncMemory

REVIEW = {"id":str(uuid.uuid4()),"version":"a"*64,"property_key":"examples.language",
    "applicability":{"kind":"global"},"current_value":"C++","proposed_value":"Python",
    "question":"Confirm Python for the displayed scope?","actions":["confirm","keep_current","dismiss"],
    "expires_at":datetime.now(UTC).isoformat()}


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_reviews_are_separate_and_only_explicit_action_sends_confirmation(asynchronous):
    calls=[]
    def respond(request):
        body=json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.url.path.endswith("/answer"):
            assert body == {"external_user_id":"u","version":"a"*64,"action":"confirm"}
            data={"review_id":REVIEW["id"],"action":"confirm","replayed":False,"memory_id":str(uuid.uuid4())}
        elif request.method == "GET":
            data=[REVIEW]
        else:
            return httpx.Response(200,json={"data":[],"cached":False,"system_prompt_addition":"",
                "source_reviews":[],"preference_reviews":[REVIEW],"memory_visibility":{"state":"unavailable","scope":"affected_context"},
                "request_id":"r","timestamp":datetime.now(UTC).isoformat()})
        return httpx.Response(200,json={"data":data,"request_id":"r","timestamp":datetime.now(UTC).isoformat()})
    client=AsyncMemory("test") if asynchronous else Memory("test")
    await invoke(client,"close")
    cls=httpx.AsyncClient if asynchronous else httpx.Client
    client._client=cls(base_url=client.base_url,transport=httpx.MockTransport(respond))
    try:
        result=await invoke(client,"get","what is current?","u")
        assert result.source_reviews == [] and result.preference_reviews[0].proposed_value == "Python"
        assert len(calls) == 1 and result.memory_visibility.scope == "affected_context"
        listed=await invoke(client,"list_preference_reviews","u")
        assert listed[0].version == "a"*64 and len(calls) == 2
        answered=await invoke(client,"answer_preference_review",REVIEW["id"],external_user_id="u",version="a"*64,action="confirm")
        assert answered.action == "confirm" and len(calls) == 3
        assert calls[-1][1] == f"/v1/memories/preference-reviews/{REVIEW['id']}/answer"
        with pytest.raises(ValueError):
            await invoke(client,"answer_source_review",REVIEW["id"],external_user_id="u",version="a"*64,action="confirm")
        assert len(calls) == 3
    finally:
        await invoke(client,"close")
