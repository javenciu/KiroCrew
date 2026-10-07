"""parse_gh_stderr: a later ``HTTP 429`` wins over an earlier non-throttle status."""

from __future__ import annotations

import pytest

from kiro_crew.taskq.adapters import github as gh
from kiro_crew.taskq.dependency import KIND_AUTH_FAILED, KIND_RATE_LIMITED

NOW = 1000.0


@pytest.mark.parametrize(
    "raw",
    [
        "HTTP 403: Forbidden\nHTTP 429",
        "HTTP 401: Unauthorized\nHTTP 429",
        "HTTP 404: Not Found\nHTTP 429",
        "HTTP 502: Bad Gateway (retrying) HTTP 429",
    ],
)
def test_a_later_429_wins_over_an_earlier_status(raw):
    failure = gh.parse_gh_stderr(raw, now=NOW)
    assert failure.category == gh.CATEGORY_RATE_LIMITED
    assert failure.status == 429


def test_retry_after_survives_behind_an_earlier_403():
    failure = gh.parse_gh_stderr("HTTP 403: Forbidden\nHTTP 429\nRetry-After: 30", now=NOW)
    assert failure.category == gh.CATEGORY_RATE_LIMITED
    assert failure.retry_at == 1030.0


def test_classify_stderr_reports_a_rate_limit_not_an_auth_failure():
    signal = gh.classify_stderr("HTTP 403: Forbidden\nHTTP 429", now=NOW)
    assert signal is not None
    assert signal.kind == KIND_RATE_LIMITED
    assert signal.dependency_scope == gh.SCOPE_API


@pytest.mark.parametrize(
    ("raw", "category", "status"),
    [
        ("HTTP 403: Forbidden", gh.CATEGORY_AUTHORIZATION, 403),
        ("HTTP 401: Bad credentials", gh.CATEGORY_AUTHENTICATION, 401),
        ("HTTP 404: Not Found", gh.CATEGORY_NOT_FOUND, 404),
        ("HTTP 502", gh.CATEGORY_TRANSIENT, 502),
        ("HTTP 422: Unprocessable\nHTTP 429", gh.CATEGORY_RATE_LIMITED, 429),
        ("HTTP 403: API rate limit exceeded", gh.CATEGORY_RATE_LIMITED, 403),
    ],
)
def test_single_status_classification_is_unchanged(raw, category, status):
    failure = gh.parse_gh_stderr(raw, now=NOW)
    assert failure.category == category
    assert failure.status == status


def test_a_lone_403_still_maps_to_auth_failed():
    signal = gh.classify_stderr("HTTP 403: Forbidden", now=NOW)
    assert signal is not None
    assert signal.kind == KIND_AUTH_FAILED


def test_abuse_detection_is_a_secondary_limit():
    failure = gh.parse_gh_stderr("HTTP 403: You have triggered an abuse detection mechanism")
    assert failure.category == gh.CATEGORY_RATE_LIMITED
    assert failure.secondary is True
