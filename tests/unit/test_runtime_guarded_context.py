"""Partial status must neither erase safe context nor exceed its prompt budget."""
import json
from xml.etree import ElementTree

import pytest

from api.schemas.responses import MemoryVisibility
from api.services.context_builder import ContextBuilder
from tests.unit.test_context_builder import make_memory_result


@pytest.mark.parametrize("format", ["bullets", "json", "xml"])
@pytest.mark.parametrize("budget", [0, 1, 64, 100, 500])
def test_guarded_notice_and_safe_memories_share_one_strict_budget(format, budget):
    builder = ContextBuilder()
    result = builder.build_guarded([make_memory_result(content="Exam is October 18")],
        visibility=MemoryVisibility(state="processing", scope="affected_context"), format=format, max_tokens=budget)
    assert result.token_count == builder._count_tokens(result.system_prompt_addition) <= budget
    if result.system_prompt_addition and format == "json":
        assert json.loads(result.system_prompt_addition)["memory_visibility"]["scope"] == "affected_context"
    if result.system_prompt_addition and format == "xml":
        ElementTree.fromstring(result.system_prompt_addition)


@pytest.mark.parametrize("format", ["bullets", "json", "xml"])
def test_user_wide_status_cannot_render_even_supplied_stale_candidates(format):
    result = ContextBuilder().build_guarded([make_memory_result(content="Old exam is October 10")],
        visibility=MemoryVisibility(state="unavailable"), format=format)
    assert "October 10" not in result.system_prompt_addition and result.memory_count == 0
