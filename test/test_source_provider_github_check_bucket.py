"""Bucket classification for GitHub check rows in the PR source provider."""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.dashboard.source_providers.github import _github_check, _github_checks

_PAYLOAD_KEYS = {
    "name",
    "workflow",
    "status",
    "conclusion",
    "bucket",
    "url",
    "startedAt",
    "completedAt",
}


def _check_run(status: str, conclusion: Any) -> dict[str, Any]:
    return {
        "__typename": "CheckRun",
        "name": "CI",
        "workflowName": "ci",
        "status": status,
        "conclusion": conclusion,
    }


def _status_context(state: str) -> dict[str, Any]:
    return {"__typename": "StatusContext", "context": "ci/x", "state": state}


def test_completed_startup_failure_is_failed() -> None:
    assert _github_check(_check_run("COMPLETED", "STARTUP_FAILURE"))["bucket"] == "failed"


def test_completed_unknown_conclusion_is_failed() -> None:
    check = _github_check(_check_run("COMPLETED", "SOME_FUTURE_CONCLUSION"))
    assert check["bucket"] == "failed"


def test_rollup_projection_buckets_startup_failure_as_failed() -> None:
    checks = _github_checks([_check_run("COMPLETED", "STARTUP_FAILURE")])
    assert len(checks) == 1
    assert checks[0]["bucket"] == "failed"


@pytest.mark.parametrize("state", ["PENDING", "EXPECTED"])
def test_status_context_waiting_states_stay_pending(state: str) -> None:
    assert _github_check(_status_context(state))["bucket"] == "pending"


def test_statusless_row_with_unknown_state_stays_pending() -> None:
    assert _github_check(_status_context("SOMETHING_NEW"))["bucket"] == "pending"


@pytest.mark.parametrize("conclusion", [None, ""])
def test_completed_without_conclusion_stays_pending(conclusion: Any) -> None:
    assert _github_check(_check_run("COMPLETED", conclusion))["bucket"] == "pending"


@pytest.mark.parametrize("status", ["IN_PROGRESS", "QUEUED"])
def test_outstanding_check_run_stays_pending(status: str) -> None:
    assert _github_check(_check_run(status, None))["bucket"] == "pending"


@pytest.mark.parametrize(
    ("conclusion", "bucket"),
    [
        ("SUCCESS", "passed"),
        ("NEUTRAL", "passed"),
        ("SKIPPED", "skipped"),
        ("STALE", "skipped"),
        ("FAILURE", "failed"),
    ],
)
def test_known_conclusions_keep_their_bucket(conclusion: str, bucket: str) -> None:
    assert _github_check(_check_run("COMPLETED", conclusion))["bucket"] == bucket


def test_status_context_failure_is_failed() -> None:
    assert _github_check(_status_context("FAILURE"))["bucket"] == "failed"


def test_payload_shape_is_unchanged() -> None:
    check = _github_check(_check_run("COMPLETED", "STARTUP_FAILURE"))
    assert set(check) == _PAYLOAD_KEYS
