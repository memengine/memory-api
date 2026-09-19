import pytest

from benchmarks.internal.phase3a_holdout_release import claim_holdout_once


def test_phase3a_holdout_claim_is_single_use(tmp_path) -> None:
    dataset = tmp_path / "holdout.json"
    dataset.write_text("{}", encoding="utf-8")

    marker = claim_holdout_once(dataset)

    assert marker.exists()
    with pytest.raises(RuntimeError, match="already been consumed"):
        claim_holdout_once(dataset)
