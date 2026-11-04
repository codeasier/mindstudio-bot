import base64
import json
from unittest.mock import Mock

import httpx
import pytest
from gitcode_cli.errors import APIError, NetworkError

from msbot.config import ConfigurationError
from msbot.diagnostics import error_details, is_transient_error
from msbot.platform import READ_RETRY_DELAYS, Platform

API = "https://api.gitcode.com/api/v5"


@pytest.fixture
def platform(monkeypatch):
    monkeypatch.setenv("MSBOT_GITCODE_TOKEN", "synthetic-read-token")
    waits = []
    monkeypatch.setattr("msbot.platform.sleep", waits.append)
    instance = Platform("owner/repo", "MSBOT_GITCODE_TOKEN")
    try:
        yield instance, waits
    finally:
        instance.close()


@pytest.mark.parametrize(
    "error", [APIError("limited", 429), APIError("unavailable", 503), NetworkError("lost")]
)
@pytest.mark.parametrize(
    ("method", "service", "upstream", "args", "response", "expected"),
    [
        ("repository", "repos", "get", (), {"id": 7}, {"id": 7}),
        (
            "branch",
            "repos",
            "get_branch",
            ("main",),
            {"commit": {"id": "a" * 40}},
            {"commit": {"id": "a" * 40}},
        ),
        (
            "file_text",
            "files",
            "get",
            (".msbot/config.yaml", "a" * 40),
            {"encoding": "base64", "content": base64.b64encode(b"version: 1").decode()},
            "version: 1",
        ),
        ("issue", "issues", "get", ("42",), {"labels": []}, {"labels": []}),
        ("comments", "issues", "list_all_comments", ("42",), [{"body": "complete"}], [{"body": "complete"}]),
    ],
)
def test_only_idempotent_service_reads_retry(
    platform, monkeypatch, error, method, service, upstream, args, response, expected
):
    instance, waits = platform
    read = Mock(side_effect=[error, response])
    monkeypatch.setattr(getattr(instance, service), upstream, read)
    assert getattr(instance, method)(*args) == expected
    assert read.call_count == 2
    assert waits == [READ_RETRY_DELAYS[0]]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, None, 600])
def test_permanent_api_errors_fail_without_retry(platform, monkeypatch, status):
    instance, waits = platform
    error = APIError("do not retry", status)
    read = Mock(side_effect=error)
    monkeypatch.setattr(instance.issues, "get", read)
    with pytest.raises(APIError) as caught:
        instance.issue("42")
    assert caught.value is error
    assert read.call_count == 1
    assert waits == []


@pytest.mark.parametrize(
    "error", [APIError("limited", 429), APIError("unavailable", 500), NetworkError("lost")]
)
def test_retry_exhaustion_preserves_original_exception_and_bound(platform, monkeypatch, error):
    instance, waits = platform
    read = Mock(side_effect=error)
    monkeypatch.setattr(instance.repos, "get", read)
    with pytest.raises(type(error)) as caught:
        instance.repository()
    assert caught.value is error
    assert read.call_count == 3
    assert waits == list(READ_RETRY_DELAYS)


@pytest.mark.parametrize(
    ("method", "service", "upstream", "args", "response"),
    [
        ("repository", "repos", "get", (), {}),
        ("issue", "issues", "get", ("42",), {"labels": None}),
        ("comments", "issues", "list_all_comments", ("42",), [{"id": 1}]),
        ("comments", "issues", "list_all_comments", ("42",), {"body": "incomplete"}),
        ("file_text", "files", "get", ("path", "a" * 40), {"encoding": "text", "content": "invalid"}),
    ],
)
def test_malformed_success_is_not_retried(platform, monkeypatch, method, service, upstream, args, response):
    instance, waits = platform
    read = Mock(return_value=response)
    monkeypatch.setattr(getattr(instance, service), upstream, read)
    with pytest.raises(ConfigurationError):
        getattr(instance, method)(*args)
    assert read.call_count == 1
    assert waits == []


@pytest.mark.parametrize(
    "error", [APIError("limited", 429), APIError("unavailable", 503), NetworkError("lost")]
)
@pytest.mark.parametrize(
    ("method", "service", "upstream", "args"),
    [
        ("remove_resolved", "issues", "remove_label", ("42",)),
        (
            "create_webhook",
            "hooks",
            "create",
            ("https://bot.example.test/hook", "synthetic-password", "password"),
        ),
    ],
)
def test_writes_are_never_retried(platform, monkeypatch, error, method, service, upstream, args):
    instance, waits = platform
    write = Mock(side_effect=error)
    monkeypatch.setattr(getattr(instance, service), upstream, write)
    with pytest.raises(type(error)):
        getattr(instance, method)(*args)
    assert write.call_count == 1
    assert waits == []


def test_upstream_http_attempts_do_not_multiply(platform, respx_mock):
    instance, waits = platform
    route = respx_mock.get(f"{API}/repos/owner/repo").mock(
        side_effect=[
            httpx.Response(503, json={"message": "unavailable"}),
            httpx.Response(429, json={"message": "limited"}),
            httpx.Response(200, json={"id": 7}),
        ]
    )
    assert instance.repository() == {"id": 7}
    assert route.call_count == 3
    assert waits == list(READ_RETRY_DELAYS)


def test_upstream_network_failure_is_bounded(platform, respx_mock):
    instance, waits = platform
    route = respx_mock.get(f"{API}/repos/owner/repo").mock(side_effect=httpx.ConnectError("unavailable"))
    with pytest.raises(NetworkError):
        instance.repository()
    assert route.call_count == 3
    assert waits == list(READ_RETRY_DELAYS)


@pytest.mark.parametrize("method", ["remove_resolved", "create_webhook"])
@pytest.mark.parametrize("status", [429, 503])
def test_upstream_http_writes_make_only_one_attempt(platform, respx_mock, method, status):
    instance, waits = platform
    if method == "remove_resolved":
        route = respx_mock.delete(f"{API}/repos/owner/repo/issues/42/labels/resolved")
        args = ("42",)
    else:
        route = respx_mock.post(f"{API}/repos/owner/repo/hooks")
        args = ("https://bot.example.test/hook", "synthetic-password", "password")
    route.respond(status, json={"message": "transient but unsafe to retry a write"})
    with pytest.raises(APIError):
        getattr(instance, method)(*args)
    assert route.call_count == 1
    assert waits == []


@pytest.mark.parametrize("recovers", [True, False])
def test_later_comment_page_failure_restarts_whole_read(platform, respx_mock, recovers):
    instance, waits = platform
    pages = []
    attempts = 0

    def comments(request):
        nonlocal attempts
        assert request.url.params["per_page"] == "100"
        page = int(request.url.params["page"])
        pages.append(page)
        if page == 1:
            attempts += 1
            return httpx.Response(
                200, json=[{"id": index, "body": f"attempt {attempts}"} for index in range(100)]
            )
        if attempts == 1 or not recovers:
            return httpx.Response(503, json={"message": "later page unavailable"})
        return httpx.Response(200, json=[{"id": 101, "body": "complete later page"}])

    route = respx_mock.get(f"{API}/repos/owner/repo/issues/42/comments").mock(side_effect=comments)
    if recovers:
        result = instance.comments("42")
        assert len(result) == 101
        assert result[0]["body"] == "attempt 2"
        assert result[-1]["id"] == 101
        assert pages == [1, 2, 1, 2]
        assert waits == [READ_RETRY_DELAYS[0]]
    else:
        with pytest.raises(APIError):
            instance.comments("42")
        assert pages == [1, 2, 1, 2, 1, 2]
        assert waits == list(READ_RETRY_DELAYS)
    assert route.call_count == len(pages)


def test_error_details_unwraps_cause_without_retaining_server_body_or_urls():
    upstream = APIError("access_token=UNLISTED_SECRET https://example.test/private BODY", 503)
    upstream.trace_id = "trace-123"
    upstream.request_id = "req-456"
    upstream.headers = {"Authorization": "Bearer HEADER_SECRET"}
    outer = ConfigurationError("No valid audit configuration is available")
    outer.__cause__ = upstream
    assert error_details(outer) == {
        "type": "ConfigurationError",
        "cause_type": "APIError",
        "message": "GitCode API request failed (HTTP 503)",
        "status_code": 503,
        "trace_id": "trace-123",
        "request_id": "req-456",
        "retryable": True,
    }


def test_missing_status_and_correlation_ids_are_explicitly_null():
    details = error_details(NetworkError("token=DO_NOT_EXPOSE"))
    assert details["message"] == "GitCode network request failed"
    assert details["status_code"] is None
    assert details["trace_id"] is None
    assert details["request_id"] is None
    assert details["retryable"] is True
    assert "DO_NOT_EXPOSE" not in json.dumps(details)


def test_error_details_never_stringifies_unknown_exceptions():
    class UnexpectedFailure(Exception):
        def __str__(self):
            raise AssertionError("Unknown exception text must never be read")

    details = error_details(UnexpectedFailure("secret-token"))
    assert details["type"] == "UnexpectedFailure"
    assert details["message"] == "Audit operation failed"
    assert details["retryable"] is False
    assert "secret-token" not in json.dumps(details)


def test_unreadable_optional_correlation_id_does_not_break_error_reporting():
    class UnexpectedFailure(Exception):
        @property
        def trace_id(self):
            raise ValueError("Unusable optional field")

    details = error_details(UnexpectedFailure())
    assert details["type"] == "UnexpectedFailure"
    assert details["trace_id"] is None


@pytest.mark.parametrize("identifier", ["https://example.test/?token=hidden", "bad\nheader", "x" * 129, 123])
def test_unsafe_correlation_ids_are_omitted(identifier):
    error = APIError("unavailable", 503)
    error.trace_id = identifier
    error.request_id = identifier
    assert error_details(error)["trace_id"] is None
    assert error_details(error)["request_id"] is None


def test_known_credentials_are_excluded_from_correlation_ids():
    error = APIError("do not preserve raw response", 503)
    error.trace_id = "trace-synthetic-secret"
    error.request_id = "req-normal"
    details = error_details(error, secrets=("synthetic-secret",))
    assert details["trace_id"] is None
    assert details["request_id"] == "req-normal"
    assert "synthetic-secret" not in json.dumps(details)


def test_exception_cause_cycles_do_not_break_diagnostics():
    first = ConfigurationError("wrapper")
    second = APIError("limited", 429)
    first.__cause__ = second
    second.__cause__ = first
    assert is_transient_error(first)
    assert error_details(first)["status_code"] == 429


@pytest.mark.parametrize("status", [500, 502, 503, 504, 599, 429])
def test_transient_api_statuses(status):
    assert is_transient_error(APIError("transient", status))


@pytest.mark.parametrize("status", [True, "503", None, 600])
def test_invalid_status_codes_are_not_exposed_or_retried(status):
    error = APIError("unknown status", status)
    assert error_details(error)["status_code"] is None
    assert not is_transient_error(error)
