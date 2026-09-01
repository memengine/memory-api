from benchmarks.internal.governance_integrity_cases import DATASETS
from benchmarks.internal.governance_integrity_cases import load_governance_integrity_cases


def test_governance_integrity_pack_is_development_only_and_unique() -> None:
    cases = load_governance_integrity_cases()
    assert len(cases) == 39
    assert len({case.id for case in cases}) == len(cases)
    assert all("development" in path.parts for path in DATASETS)
    assert all("holdout" not in str(path).lower() for path in DATASETS)


def test_governance_integrity_extension_covers_complete_lifecycle_boundaries() -> None:
    cases = load_governance_integrity_cases()
    areas = {case.area for case in cases}
    metrics = {metric for case in cases for metric in case.validates}
    assert {
        "conflict_provenance", "retrieval_provenance", "deletion_governance",
        "vector_provenance", "agent_provenance", "writer_provenance",
        "temporal_provenance", "api_export_readback", "governance_observability",
        "full_path_governance",
    }.issubset(areas)
    assert {
        "evidence_preservation", "claim_revision_alignment",
        "version_chain_provenance", "qdrant_metadata_consistency",
        "retrieval_provenance_correctness", "source_agent_preservation",
        "privacy_deletion_distinction", "governed_access_revocation",
        "writer_attribution_correctness", "temporal_chain_provenance",
        "governance_observability",
    }.issubset(metrics)
