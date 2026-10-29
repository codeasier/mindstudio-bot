import base64
import hashlib
import hmac

import httpx
import pytest

from msbot.config import ConfigurationError, FeishuConnection
from msbot.feishu import NotificationRejected, NotificationUnknown, payload_for, send

URL = "https://open.feishu.cn/open-apis/bot/v2/hook/synthetic-hook"


def test_official_signing_algorithm_and_security_keywords(monkeypatch):
    monkeypatch.setenv("SIGNING_SECRET", "synthetic-secret")
    connection = FeishuConnection("URL_ENV", ("Bug关闭审计", "engineering"), "SIGNING_SECRET")
    payload = payload_for(connection, "审计未通过", timestamp=1599360473)
    expected = base64.b64encode(
        hmac.new(b"1599360473\nsynthetic-secret", b"", hashlib.sha256).digest()
    ).decode()
    assert payload["sign"] == expected
    assert payload["timestamp"] == "1599360473"
    assert "Bug关闭审计" in payload["content"]["text"]
    assert "engineering" in payload["content"]["text"]
    assert "synthetic-secret" not in str(payload)


@pytest.mark.parametrize("data", [{"code": 0}, {"StatusCode": 0}])
def test_explicit_success_code(monkeypatch, respx_mock, data):
    monkeypatch.setenv("URL_ENV", URL)
    route = respx_mock.post(URL).mock(return_value=httpx.Response(200, json=data))
    send(FeishuConnection("URL_ENV"), "synthetic notification")
    assert route.call_count == 1


def test_nonzero_bot_result_is_not_http_success(monkeypatch, respx_mock):
    monkeypatch.setenv("URL_ENV", URL)
    respx_mock.post(URL).mock(
        return_value=httpx.Response(200, json={"code": 19024, "msg": "keyword mismatch"})
    )
    with pytest.raises(NotificationRejected):
        send(FeishuConnection("URL_ENV"), "synthetic notification")


@pytest.mark.parametrize("data", [{}, {"code": False}, {"code": "0"}, []])
def test_ambiguous_success_is_unknown(monkeypatch, respx_mock, data):
    monkeypatch.setenv("URL_ENV", URL)
    respx_mock.post(URL).mock(return_value=httpx.Response(200, json=data))
    with pytest.raises(NotificationUnknown):
        send(FeishuConnection("URL_ENV"), "synthetic notification")


def test_timeout_not_automatically_retried(monkeypatch, respx_mock):
    monkeypatch.setenv("URL_ENV", URL)
    route = respx_mock.post(URL).mock(side_effect=httpx.ReadTimeout("synthetic timeout"))
    with pytest.raises(NotificationUnknown):
        send(FeishuConnection("URL_ENV"), "synthetic notification")
    assert route.call_count == 1


def test_custom_bot_url_is_instance_side_and_validated(monkeypatch):
    monkeypatch.setenv("URL_ENV", "https://example.invalid/secret")
    with pytest.raises(ConfigurationError):
        FeishuConnection("URL_ENV").url()
