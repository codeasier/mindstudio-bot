import base64
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from msbot.app import create_app
from msbot.config import GitCodeDefaults, Settings

from .conftest import ADMIN, CONFIG, TEMPLATE, VALID_COMMENT, event_payload, headers, registration

API = "https://api.gitcode.com/api/v5"


@pytest.mark.parametrize("unified", [False, True])
def test_http_flow_uses_upstream_services_and_later_comment_page(tmp_path, monkeypatch, respx_mock, unified):
    monkeypatch.setenv("MSBOT_ADMIN_TOKEN", "synthetic-admin")
    monkeypatch.setenv("MSBOT_GITCODE_TOKEN", "synthetic-platform-token")
    monkeypatch.setenv("MSBOT_GITCODE_WEBHOOK_SECRET", "synthetic-webhook-password")
    revision = "b" * 40
    respx_mock.get(f"{API}/repos/owner/repo").respond(200, json={"id": 7, "default_branch": "main"})
    respx_mock.get(f"{API}/repos/owner/repo/branches/main").respond(200, json={"commit": {"id": revision}})

    def contents(request):
        assert request.url.params["ref"] == revision
        source = CONFIG if request.url.path.endswith("config.yaml") else TEMPLATE
        return httpx.Response(
            200, json={"encoding": "base64", "content": base64.b64encode(source.encode()).decode()}
        )

    respx_mock.get(f"{API}/repos/owner/repo/contents/.msbot/config.yaml").mock(side_effect=contents)
    respx_mock.get(f"{API}/repos/owner/repo/contents/.msbot/template/bug-close.yml").mock(
        side_effect=contents
    )
    labels = ["bug", "resolved", "other"]
    respx_mock.get(f"{API}/repos/owner/repo/issues/42").mock(
        side_effect=lambda _request: httpx.Response(
            200, json={"labels": [{"name": name} for name in labels], "user": {"login": "alice"}}
        )
    )

    def comments(request):
        assert request.url.params["per_page"] == "100"
        if request.url.params["page"] == "1":
            return httpx.Response(200, json=[{"id": i, "body": "not complete"} for i in range(100)])
        return httpx.Response(200, json=[{"id": 101, "body": VALID_COMMENT}])

    comment_route = respx_mock.get(f"{API}/repos/owner/repo/issues/42/comments").mock(side_effect=comments)
    removal_route = respx_mock.delete(f"{API}/repos/owner/repo/issues/42/labels/resolved").respond(
        200, json={}
    )
    settings = Settings(
        tmp_path / "audit.sqlite3",
        "https://msbot.example.test",
        tester_authors=("alice",),
        gitcode=GitCodeDefaults() if unified else None,
    )
    app = create_app(settings, start_worker=False)
    with TestClient(app) as client:
        if unified:
            webhook_url = "/hooks/gitcode"
        else:
            registered = client.post("/api/repos", json=registration(str(uuid.uuid4())), headers=ADMIN).json()
            webhook_url = f"/hooks/gitcode/{registered['data']['result']['id']}"
        response = client.post(webhook_url, json=event_payload(), headers=headers())
        assert response.status_code == 202
        identity = response.json()["data"]["id"]
        app.state.engine.run_once()
        result = app.state.store.job_get(identity)
        assert result["status"] == "passed"
        assert result["result"]["comments_read"] == 101
        assert comment_route.call_count == 2
        assert not removal_route.called

        # A new label-add event with no valid comment exercises the dedicated DELETE endpoint.
        comment_route.mock(return_value=httpx.Response(200, json=[]))

        def remove(_request):
            labels.remove("resolved")
            return httpx.Response(200, json={})

        removal_route.mock(side_effect=remove)
        payload = event_payload() | {"uuid": "new-label-add-event"}
        response = client.post(webhook_url, json=payload, headers=headers("delivery-2"))
        app.state.engine.run_once()
        assert app.state.store.job_get(response.json()["data"]["id"])["removal"] == "confirmed"
        assert labels == ["bug", "other"]
        assert removal_route.call_count == 1


def test_instance_configuration_requires_environment_references(tmp_path, monkeypatch):
    monkeypatch.setenv("MSBOT_ADMIN_TOKEN", "synthetic-admin")
    monkeypatch.setenv("MSBOT_GITCODE_TOKEN", "synthetic-token")
    monkeypatch.setenv("MSBOT_GITCODE_WEBHOOK_SECRET", "synthetic-webhook-password")
    monkeypatch.setenv("MSBOT_FEISHU_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/synthetic")
    monkeypatch.setenv("MSBOT_FEISHU_SECRET", "synthetic-signing-secret")
    source = (Path(__file__).resolve().parents[1] / "examples/instance.yaml").read_text()
    lines = []
    for line in source.splitlines():
        if line.startswith("# feishu:") or line.startswith("#   "):
            line = line[2:]
        if "可选。" in line:
            continue
        lines.append(line)
    source = "\n".join(lines) + "\n"
    path = tmp_path / "instance.yaml"
    path.write_text(source, encoding="utf-8")
    settings = Settings.load(str(path))
    assert settings.feishu["engineering"].keywords == ("Bug关闭审计",)
    assert settings.feishu["engineering"].secret_env == "MSBOT_FEISHU_SECRET"
    assert settings.tester_authors == ("replace-me",)
