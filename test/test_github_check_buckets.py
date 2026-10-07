"""Bucketing of a single GitHub check row by ``_github_check``.

A completed check-run must never read as pending: STARTUP_FAILURE and any
other unrecognized conclusion on a COMPLETED row bucket as failed, while
status contexts and completed rows without a conclusion stay pending.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard.source_providers import github


def _bucket(item: dict) -> str:
    return github._github_check(item)["bucket"]


def test_startup_failure_is_failed() -> None:
    item = {"name": "ci", "status": "COMPLETED", "conclusion": "STARTUP_FAILURE"}
    assert _bucket(item) == "failed"


def test_unknown_completed_conclusion_is_failed() -> None:
    item = {"status": "COMPLETED", "conclusion": "SOME_FUTURE_CONCLUSION"}
    assert _bucket(item) == "failed"


@pytest.mark.parametrize(
    "item",
    [
        {"context": "ci/x", "state": "PENDING"},
        {"context": "ci/x", "state": "EXPECTED"},
        {"status": "COMPLETED", "conclusion": ""},
        {"status": "COMPLETED"},
        {"context": "c", "state": "WEIRD"},
        {"status": "IN_PROGRESS"},
        {"status": "QUEUED"},
        {"status": "IN_PROGRESS", "conclusion": "STARTUP_FAILURE"},
    ],
)
def test_pending_rows(item: dict) -> None:
    assert _bucket(item) == "pending"


@pytest.mark.parametrize("conclusion", ["SUCCESS", "NEUTRAL"])
def test_passed(conclusion: str) -> None:
    assert _bucket({"status": "COMPLETED", "conclusion": conclusion}) == "passed"


@pytest.mark.parametrize("conclusion", ["SKIPPED", "STALE"])
def test_skipped(conclusion: str) -> None:
    assert _bucket({"status": "COMPLETED", "conclusion": conclusion}) == "skipped"


@pytest.mark.parametrize(
    "item",
    [
        {"status": "COMPLETED", "conclusion": "FAILURE"},
        {"status": "COMPLETED", "conclusion": "CANCELLED"},
        {"status": "COMPLETED", "conclusion": "TIMED_OUT"},
        {"status": "COMPLETED", "conclusion": "ACTION_REQUIRED"},
        {"status": "COMPLETED", "conclusion": "ERROR"},
        {"context": "ci/x", "state": "FAILURE"},
        {"context": "ci/x", "state": "ERROR"},
    ],
)
def test_failed(item: dict) -> None:
    assert _bucket(item) == "failed"


def test_payload_shape_unchanged() -> None:
    out = github._github_check({"status": "COMPLETED", "conclusion": "STARTUP_FAILURE"})
    assert set(out) == {
        "name",
        "workflow",
        "status",
        "conclusion",
        "bucket",
        "url",
        "startedAt",
        "completedAt",
    }
