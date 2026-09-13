from __future__ import annotations

import asyncio
from pathlib import Path

from test.backend.akasha_retrieval_eval import run_recall_benchmark


def test_akasha_retrieval_benchmark_recovers_expected_evidence(tmp_path: Path) -> None:
    """Guard engine behaviour, independently of an external embedding model."""

    result = asyncio.run(run_recall_benchmark(tmp_path, cutoffs=(1, 3, 5)))

    assert result["benchmark"] == "akasha-retrieval-v1"
    overall = result["overall"]
    assert isinstance(overall, dict)
    # The benchmark intentionally keeps a knowledge-update and a temporal
    # completion case.  The latter cannot be recovered by lexical overlap or
    # dense similarity alone; it must arrive through the completion lane.
    assert overall["recall_at"] == {"1": 0.8333, "3": 1.0, "5": 1.0}
    assert overall["hit_at"] == {"1": 0.8333, "3": 1.0, "5": 1.0}
    # The temporal-completion target is intentionally ranked third: the
    # release seed is the strongest direct match and the successor arrives
    # through the completion lane.  Keep this as a regression baseline rather
    # than hiding the ranking trade-off behind an aggregate recall score.
    assert overall["mrr"] == 0.8889

    by_id = {case["case_id"]: case for case in result["cases"]}
    assert by_id["knowledge-update"]["retrieved_turn_ids"][0] == "region-shanghai"
    assert by_id["temporal-completion"]["retrieved_turn_ids"][2] == "aurora-docs"
    assert "completion" in by_id["temporal-completion"]["lanes"]
    assert by_id["lexical-fallback"]["retrieved_turn_ids"][0] == "lexical-canary"
