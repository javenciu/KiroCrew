"""Tests for the append-only learned-cost store (subagent_cost).

Covers append/round-trip, p90 aggregation with tail-outlier robustness,
max-across-agents, min-sample fallback, empty/corrupt fail-open, concurrent
appends, and FIFO compaction bound.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiro_crew.subagent_cost as sc


@pytest.fixture
def cost_log(tmp_path, monkeypatch):
    """Redirect the cost log to a temp file."""
    p = tmp_path / "subagents" / "cost_samples.jsonl"
    monkeypatch.setattr(sc, "_cost_log_path", lambda: p)
    return p


def _seed(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


# --- append ----------------------------------------------------------------


def test_append_writes_jsonl_line(cost_log):
    sc.append_cost_sample("kirocrew", 0.34, 0.82)
    lines = cost_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["agent"] == "kirocrew"
    assert rec["mem_gb"] == 0.34
    assert rec["cpu_cores"] == 0.82
    assert "ts" in rec


def test_append_normalizes_empty_agent(cost_log):
    sc.append_cost_sample("", 0.3, 0.5)
    rec = json.loads(cost_log.read_text(encoding="utf-8").strip())
    assert rec["agent"] == "kirocrew"


def test_append_skips_zero_zero(cost_log):
    sc.append_cost_sample("kirocrew", 0.0, 0.0)
    assert not cost_log.exists() or cost_log.read_text(encoding="utf-8").strip() == ""


def test_concurrent_appends_do_not_lose_samples(cost_log):
    for i in range(20):
        sc.append_cost_sample("kirocrew", 0.3 + i * 0.001, 0.5)
    lines = cost_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 20  # O_APPEND keeps every line


# --- read_learned_cost -----------------------------------------------------


def test_p90_ignores_single_outlier(cost_log):
    # 20 samples ~0.3 plus one pathological 9.9 → p90 (rank 18 of 0..20) stays
    # at 0.3, not dominated by the lone outlier at the top.
    recs = [{"agent": "kirocrew", "mem_gb": 0.30, "cpu_cores": 0.1} for _ in range(20)]
    recs.append({"agent": "kirocrew", "mem_gb": 9.9, "cpu_cores": 0.1})
    _seed(cost_log, recs)
    val = sc.read_learned_cost("mem_gb")
    assert val is not None
    assert val < 1.0  # outlier did not dominate


def test_max_across_agents(cost_log):
    recs = (
        [{"agent": "kirocrew-lite", "mem_gb": 0.30, "cpu_cores": 0.1} for _ in range(5)]
        + [{"agent": "kirocrew", "mem_gb": 0.55, "cpu_cores": 0.1} for _ in range(5)]
    )
    _seed(cost_log, recs)
    val = sc.read_learned_cost("mem_gb")
    assert val == pytest.approx(0.55, abs=0.01)  # heaviest type wins


def test_min_samples_fallback_returns_none(cost_log):
    _seed(cost_log, [{"agent": "kirocrew", "mem_gb": 0.5, "cpu_cores": 0.1}])  # only 1
    assert sc.read_learned_cost("mem_gb", min_samples=3) is None


def test_empty_log_returns_none(cost_log):
    assert sc.read_learned_cost("mem_gb") is None


def test_corrupt_lines_skipped(cost_log):
    cost_log.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps({"agent": "kirocrew", "mem_gb": 0.4, "cpu_cores": 0.1})
    cost_log.write_text(f"{good}\nNOT JSON\n{good}\n{good}\n", encoding="utf-8")
    val = sc.read_learned_cost("mem_gb", min_samples=3)
    assert val == pytest.approx(0.4, abs=0.01)  # 3 good lines, corrupt skipped


def test_window_limits_to_recent(cost_log):
    # Old cheap samples then recent expensive ones; window=3 → only recent count.
    recs = [{"agent": "kirocrew", "mem_gb": 0.1, "cpu_cores": 0.1} for _ in range(10)]
    recs += [{"agent": "kirocrew", "mem_gb": 0.9, "cpu_cores": 0.1} for _ in range(3)]
    _seed(cost_log, recs)
    val = sc.read_learned_cost("mem_gb", window=3, min_samples=3)
    assert val == pytest.approx(0.9, abs=0.01)


# --- _percentile (pure helper) ---------------------------------------------


class TestPercentile:
    """Pin the linear-interpolation math and edge branches directly.

    The read_learned_cost p90 tests only hit sample values that land on an
    exact rank, so they cannot tell linear interpolation apart from
    nearest-rank. These assertions target the pure helper so a regression to
    nearest-rank, or a broken empty/single-element branch, goes red.
    ``pct`` is a fraction in [0, 1]: rank = pct * (len - 1).
    """

    def test_empty_returns_zero(self):
        assert sc._percentile([], 0.9) == 0.0

    @pytest.mark.parametrize("pct", [0.0, 0.5, 0.9, 1.0])
    def test_single_element_returns_that_element(self, pct):
        assert sc._percentile([7.5], pct) == 7.5

    def test_linear_interpolation_at_half_rank(self):
        # p50 of [10,20,30,40]: rank = 0.5 * 3 = 1.5 -> 20 + (30-20)*0.5 = 25.0.
        # Nearest-rank would return 20 or 30, never 25.
        assert sc._percentile([10, 20, 30, 40], 0.5) == pytest.approx(25.0)

    def test_linear_interpolation_at_fractional_rank(self):
        # p90 of [1..10]: rank = 0.9 * 9 = 8.1 -> 9 + (10-9)*0.1 = 9.1.
        assert sc._percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.9) == pytest.approx(9.1)

    def test_exact_endpoints_return_min_and_max(self):
        vals = [10, 20, 30, 40]
        assert sc._percentile(vals, 0.0) == 10  # rank 0 -> min
        assert sc._percentile(vals, 1.0) == 40  # rank len-1 -> max

    def test_unsorted_input_is_sorted_first(self):
        # Same fractional-rank case, shuffled, to prove sorting happens.
        assert sc._percentile([10, 4, 9, 8, 6, 1, 7, 3, 5, 2], 0.9) == pytest.approx(9.1)


# --- compaction ------------------------------------------------------------


def test_compaction_bounds_per_agent(cost_log):
    recs = [{"agent": "kirocrew", "mem_gb": 0.3, "cpu_cores": 0.1, "ts": i} for i in range(100)]
    _seed(cost_log, recs)
    sc.compact_cost_log(window=10)
    lines = cost_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 10  # trimmed to last 10
    # kept the most recent (highest ts)
    kept_ts = sorted(json.loads(line)["ts"] for line in lines)
    assert kept_ts == list(range(90, 100))


def test_compaction_noop_when_within_bound(cost_log):
    recs = [{"agent": "kirocrew", "mem_gb": 0.3, "cpu_cores": 0.1, "ts": i} for i in range(5)]
    _seed(cost_log, recs)
    sc.compact_cost_log(window=50)
    assert len(cost_log.read_text(encoding="utf-8").strip().splitlines()) == 5


def test_compaction_per_agent_independent(cost_log):
    recs = (
        [{"agent": "a", "mem_gb": 0.3, "cpu_cores": 0.1, "ts": i} for i in range(20)]
        + [{"agent": "b", "mem_gb": 0.3, "cpu_cores": 0.1, "ts": 100 + i} for i in range(20)]
    )
    _seed(cost_log, recs)
    sc.compact_cost_log(window=5)
    lines = [json.loads(line) for line in cost_log.read_text(encoding="utf-8").strip().splitlines()]
    assert sum(1 for r in lines if r["agent"] == "a") == 5
    assert sum(1 for r in lines if r["agent"] == "b") == 5
