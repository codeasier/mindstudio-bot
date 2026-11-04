from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import quote, quote_plus

from gitcode_cli.errors import APIError, NetworkError

from .config import ConfigurationError


def _causes(exc: BaseException) -> list[BaseException]:
    chain = []
    seen = set()
    while isinstance(exc, BaseException) and id(exc) not in seen and len(chain) < 16:
        chain.append(exc)
        seen.add(id(exc))
        exc = exc.__cause__
    return chain


def _platform_error(chain: list[BaseException]) -> BaseException | None:
    return next((error for error in chain if isinstance(error, APIError | NetworkError)), None)


def _attribute(exc: BaseException, name: str) -> object:
    try:
        return getattr(exc, name, None)
    except Exception:
        # Diagnostics must not replace the original failure if an optional
        # field is a property that cannot be read.
        return None


def _status_code(exc: BaseException | None) -> int | None:
    value = _attribute(exc, "status_code") if isinstance(exc, APIError) else None
    return value if type(value) is int and 100 <= value <= 599 else None


def is_transient_error(exc: BaseException) -> bool:
    """Classify a failure, not permission to retry a write or notification."""
    source = _platform_error(_causes(exc))
    status = _status_code(source)
    return isinstance(source, NetworkError) or status == 429 or (status is not None and 500 <= status <= 599)


def _secret_values(secrets: Iterable[str]) -> set[str]:
    values = set()
    for value in secrets:
        if isinstance(value, str) and value:
            values.update((value, quote(value, safe=""), quote_plus(value, safe="")))
    return values


def _safe_identifier(value: object, secrets: set[str]) -> str | None:
    # Only explicitly exposed correlation IDs, never URLs, headers or bodies.
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        return None
    if any(secret in value for secret in secrets):
        return None
    return value


def error_details(exc: BaseException, *, secrets: Iterable[str] = ()) -> dict:
    """Return bounded, JSON-safe diagnostics without copying exception text.

    API errors can contain server bodies and network errors can contain URLs.
    Even a known upstream type is not authority to persist that text. Messages
    below are local classifications; absent status/correlation IDs stay null.
    """
    chain = _causes(exc)
    source = _platform_error(chain) or chain[-1]
    secret_values = _secret_values(secrets)
    status = _status_code(source)
    if isinstance(source, NetworkError):
        message = "GitCode network request failed"
    elif isinstance(source, APIError):
        message = "GitCode API request failed"
        if status is not None:
            message += f" (HTTP {status})"
    elif isinstance(exc, ConfigurationError):
        message = "Audit configuration is invalid or unavailable"
    else:
        message = "Audit operation failed"

    result = {
        "type": _safe_identifier(type(exc).__name__, secret_values) or "Exception",
        "cause_type": (
            _safe_identifier(type(source).__name__, secret_values) or "Exception"
            if source is not exc
            else None
        ),
        "message": message,
        "status_code": status,
        "trace_id": None,
        "request_id": None,
        "retryable": is_transient_error(exc),
    }
    for name in ("trace_id", "request_id"):
        for error in [source, *chain]:
            value = _safe_identifier(_attribute(error, name), secret_values)
            if value is not None:
                result[name] = value
                break
    return result
