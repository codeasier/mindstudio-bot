from __future__ import annotations

import base64
import hashlib
import hmac
import time

import httpx

from .config import FeishuConnection, secret


class NotificationRejected(RuntimeError):
    pass


class NotificationUnknown(RuntimeError):
    pass


def payload_for(connection: FeishuConnection, message: str, timestamp: int | None = None) -> dict:
    prefix = " ".join(connection.keywords)
    result = {"msg_type": "text", "content": {"text": f"{prefix}\n{message}".strip()}}
    if connection.secret_env:
        timestamp = int(time.time()) if timestamp is None else timestamp
        signing_key = f"{timestamp}\n{secret(connection.secret_env)}".encode()
        result["timestamp"] = str(timestamp)
        result["sign"] = base64.b64encode(hmac.new(signing_key, b"", hashlib.sha256).digest()).decode()
    return result


def send(connection: FeishuConnection, message: str):
    try:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.post(connection.url(), json=payload_for(connection, message))
        if response.status_code != 200:
            raise NotificationUnknown("Feishu delivery outcome is unknown")
        data = response.json()
        if not isinstance(data, dict):
            raise NotificationUnknown("Feishu returned an invalid response")
        code = data.get("code", data.get("StatusCode"))
        if type(code) is not int:
            raise NotificationUnknown("Feishu response lacks an explicit result code")
        if code != 0:
            raise NotificationRejected(f"Feishu rejected notification (code={code})")
    except (httpx.HTTPError, ValueError) as exc:
        raise NotificationUnknown("Feishu delivery outcome is unknown") from exc
