"""Query-plan regression, not a production latency or throughput benchmark.

Only connection-local temporary tables are populated. No customer rows change.
"""
import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from api.db.database import get_sync_database_url
from api.db.models import PendingExtractionCandidate
from api.schemas.preferences import PreferenceReadContext
from api.services.preference_claim_service import preference_fingerprint
from api.services.source_review_service import source_review_query


@pytest.mark.parametrize("row_count", [1000, 10000, 100000])
def test_scoped_and_legacy_review_plans(row_count):
    engine = create_engine(get_sync_database_url())
    tenant, proxy = uuid.uuid4(), uuid.uuid4()
    context = PreferenceReadContext(applicability={"kind": "context", "namespace": "project", "key": "A"}, property_keys=["examples.language"])
    fingerprint = preference_fingerprint("examples.language", context.applicability)
    with engine.connect() as connection, connection.begin():
        connection.execute(text("CREATE TEMP TABLE review_plan (LIKE pending_extraction_candidates INCLUDING DEFAULTS INCLUDING CONSTRAINTS) ON COMMIT DROP"))
        connection.execute(text("CREATE INDEX review_plan_baseline ON review_plan (proxy_user_id,status)"))
        connection.execute(text("""
            INSERT INTO review_plan (id,tenant_id,proxy_user_id,category,content,importance_score,
            confidence_score,candidate_reason,candidate_fingerprint,status,metadata,created_at,updated_at,last_seen_at)
            SELECT gen_random_uuid(), :tenant, :proxy, 'preference', 'synthetic',5,0.5,
            'source_decision_pending',md5(i::text), 'pending',
            jsonb_build_object('extraction_evidence', CASE WHEN i % 997 = 0 THEN
              jsonb_build_object('source_review',jsonb_build_object('kind','restate_source'))
            ELSE jsonb_build_object('source_review',jsonb_build_object('kind','restate_source'),
              'explicit_preference_v1',jsonb_build_object('kind','synthetic'),
              'preference_fingerprint', CASE WHEN i % 1000 = 0 THEN :fingerprint ELSE md5(i::text) END) END),
            now() - i * interval '1 second',now(),now()
            FROM generate_series(1,:row_count) i
        """), {"tenant": tenant, "proxy": proxy, "fingerprint": fingerprint, "row_count": row_count})
        connection.execute(text("ANALYZE review_plan"))

        def explain(ctx):
            sql = str(source_review_query(tenant_id=str(tenant), proxy_user_id=str(proxy), preference_context=ctx, now=datetime.now(UTC)).compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
            )).replace("pending_extraction_candidates", "review_plan")
            result = connection.execute(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql)).scalar_one()[0]
            return result

        multi = PreferenceReadContext(applicability=context.applicability, property_keys=["examples.language"] + [f"missing.{i}" for i in range(15)])
        missing = PreferenceReadContext(applicability=context.applicability, property_keys=["missing.property"])
        contexts = {"scoped": context, "legacy": None, "sixteen_properties": multi, "no_match": missing}
        before = {name: explain(ctx) for name, ctx in contexts.items()}
        owner_index = next(index for index in PendingExtractionCandidate.__table__.indexes if index.name == "ix_source_review_owner")
        owner_ddl = str(CreateIndex(owner_index).compile(dialect=postgresql.dialect())).replace(
            "pending_extraction_candidates", "review_plan",
        ).replace("ix_source_review_owner", "review_plan_owner")
        connection.execute(text(owner_ddl))
        connection.execute(text("ANALYZE review_plan"))
        after = {name: explain(ctx) for name, ctx in contexts.items()}
        compiled = source_review_query(tenant_id=str(tenant), proxy_user_id=str(proxy), preference_context=context).compile(
            dialect=postgresql.dialect(), compile_kwargs={"render_postcompile": True},
        )
        prepared_sql = str(compiled)
        arguments = {}
        for number, (key, value) in enumerate(compiled.params.items(), 1):
            prepared_sql = prepared_sql.replace(f"%({key})s", f"${number}")
            arguments[f"p{number}"] = value
        prepared_sql = prepared_sql.replace("pending_extraction_candidates", "review_plan")
        connection.execute(text("PREPARE review_plan_generic AS " + prepared_sql))
        connection.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
        execution = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) EXECUTE review_plan_generic(" + ",".join(f":{key}" for key in arguments) + ")"
        generic = connection.execute(text(execution), arguments).scalar_one()[0]
        if row_count == 100000:
            assert "review_plan_owner" in json.dumps(generic["Plan"])
        assert generic["Plan"]["Actual Rows"] == min(3, row_count // 1000)
        print(json.dumps({"path": "generic_prepared", "rows": row_count, "after_ms": generic["Execution Time"]}))
        for label in before:
            plan = json.dumps(after[label]["Plan"])
            expected = 0 if label == "no_match" else min(3, row_count // (997 if label == "legacy" else 1000))
            assert after[label]["Plan"]["Actual Rows"] == expected
            if row_count == 100000:
                assert "review_plan_owner" in plan
                assert "Seq Scan" not in plan
            # Buffer-work regression is meaningful; elapsed time is informational.
            baseline_blocks = before[label]["Plan"].get("Local Hit Blocks", 0) + before[label]["Plan"].get("Local Read Blocks", 0)
            indexed_blocks = after[label]["Plan"].get("Local Hit Blocks", 0) + after[label]["Plan"].get("Local Read Blocks", 0)
            if row_count == 100000:
                assert indexed_blocks < baseline_blocks / 10
            print(json.dumps({"path": label, "rows": row_count, "before_ms": before[label]["Execution Time"],
                "after_ms": after[label]["Execution Time"], "before_blocks": baseline_blocks, "after_blocks": indexed_blocks}))
    engine.dispose()
