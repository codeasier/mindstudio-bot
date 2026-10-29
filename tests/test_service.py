import hashlib
import hmac
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from msbot.app import create_app
from msbot.config import Settings

from .conftest import ADMIN, FakePlatform, event_payload, headers, registration
from .test_audit import onboard, submit


def test_administrator_authentication(rig):
    client, _, _, _ = rig
    assert client.get("/api/repos").status_code == 401
    assert client.get("/api/tasks").status_code == 401
    assert client.post("/api/repos", json=registration(str(uuid.uuid4()))).status_code == 401


def test_webhook_password_required(rig):
    client, store, _, _ = rig
    repo = onboard(rig)
    response = client.post(
        f"/hooks/gitcode/{repo['id']}", json=event_payload(), headers={"X-GitCode-Token": "wrong"}
    )
    assert response.status_code == 401
    assert store.job_list() == []


def test_webhook_cannot_target_another_repository(rig):
    client, store, _, _ = rig
    repo = onboard(rig)
    payload = event_payload()
    payload["project"]["id"] = 8
    response = client.post(f"/hooks/gitcode/{repo['id']}", json=payload, headers=headers())
    assert response.status_code == 400
    assert store.job_list() == []


def test_signature_mode_verifies_exact_body(rig):
    client, store, _, _ = rig
    repo = onboard(rig, webhook_mode="signature")
    body = json.dumps(event_payload()).encode()
    signature = hmac.new(b"synthetic-webhook-password", body, hashlib.sha256).hexdigest()
    signed_headers = headers() | {"X-GitCode-Signature-256": "sha256=" + signature}
    assert (
        client.post(f"/hooks/gitcode/{repo['id']}", content=body, headers=signed_headers).status_code == 202
    )
    assert (
        client.post(f"/hooks/gitcode/{repo['id']}", content=body + b" ", headers=signed_headers).status_code
        == 401
    )
    assert len(store.job_list()) == 1


def test_unrecognized_label_difference_is_recorded_but_not_executed(rig):
    _, _, engine, platform = rig
    repo = onboard(rig)
    payload = event_payload()
    payload["changes"]["labels"] = {"unknown_shape": ["resolved"]}
    job = submit(rig, repo, payload)
    assert job["reason"] == "unrecognized_label_change"
    assert not engine.run_once()
    assert not platform.removals


def test_event_uuid_deduplicates_new_delivery_identifier(rig):
    _, store, _, _ = rig
    repo = onboard(rig)
    first = submit(rig, repo, delivery="request-1")
    second = submit(rig, repo, delivery="request-2")
    assert second["id"] == first["id"]
    assert len(store.job_list()) == 1


def test_registration_is_idempotent_and_does_not_repeat_hook_creation(rig):
    client, _, _, platform = rig
    identity = str(uuid.uuid4())
    body = registration(identity, create_webhook=True)
    first = client.post("/api/repos", json=body, headers=ADMIN)
    second = client.post("/api/repos", json=body, headers=ADMIN)
    assert first.json() == second.json()
    assert len(platform.hooks) == 1
    conflict = client.post("/api/repos", json=body | {"create_webhook": False}, headers=ADMIN)
    assert conflict.status_code == 409


def test_hook_timeout_is_unknown_and_never_blindly_resent(rig):
    client, _, _, platform = rig
    platform.hook_error = TimeoutError("synthetic")
    body = registration(str(uuid.uuid4()), create_webhook=True)
    first = client.post("/api/repos", json=body, headers=ADMIN)
    assert first.json()["data"]["status"] == "blocked"
    client.post("/api/repos", json=body, headers=ADMIN)
    assert len(platform.hooks) == 1


@pytest.mark.parametrize(
    "result",
    [
        {"id": 21, "result_code": 1},
        {"id": 0},
        {"id": 21, "url": "https://different.example.test"},
        {"id": 21, "issues_events": False},
    ],
)
def test_ambiguous_hook_response_does_not_activate_repository(rig, result):
    client, store, _, platform = rig
    platform.create_webhook = lambda *_args: result
    body = registration(str(uuid.uuid4()), create_webhook=True)
    response = client.post("/api/repos", json=body, headers=ADMIN)
    assert response.json()["data"]["status"] == "blocked"
    assert store.repo_list()[0]["status"] == "blocked"


def test_recovery_requeues_only_work_without_external_call(rig):
    _, store, engine, platform = rig
    repo = onboard(rig)
    first = submit(rig, repo)
    claimed = store.claim()
    assert claimed["id"] == first["id"]
    store.recover()
    assert store.job_get(first["id"])["status"] == "queued"
    assert engine.run_once()
    assert platform.removals == ["42"]


@pytest.mark.parametrize("stage", ["removal", "notification"])
def test_recovery_blocks_interrupted_external_calls(rig, stage):
    _, store, engine, _ = rig
    repo = onboard(rig)
    job = submit(rig, repo)
    store.claim()
    store.job_update(job["id"], **{stage: "calling" if stage == "removal" else "sending"})
    store.recover()
    result = store.job_get(job["id"])
    assert result["status"] == "blocked"
    assert result[stage] == "unknown"
    assert not engine.run_once()


def test_oversized_webhook_is_not_persisted(rig):
    client, store, _, _ = rig
    repo = onboard(rig)
    response = client.post(f"/hooks/gitcode/{repo['id']}", content=b" " * 1_048_577, headers=headers())
    assert response.status_code == 413
    assert store.job_list() == []


def test_lifespan_worker_processes_durable_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("MSBOT_ADMIN_TOKEN", "synthetic-admin")
    monkeypatch.setenv("MSBOT_GITCODE_WEBHOOK_SECRET", "synthetic-webhook-password")
    settings = Settings(tmp_path / "audit.sqlite3", "https://msbot.example.test")
    platform = FakePlatform()
    app = create_app(settings, platform_factory=lambda *_args: platform)
    with TestClient(app) as client:
        response = client.post("/api/repos", json=registration(str(uuid.uuid4())), headers=ADMIN)
        repo = response.json()["data"]["result"]
        response = client.post(f"/hooks/gitcode/{repo['id']}", json=event_payload(), headers=headers())
        identity = response.json()["data"]["id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if app.state.store.job_get(identity)["status"] == "rejected":
                break
            time.sleep(0.02)
        assert app.state.store.job_get(identity)["status"] == "rejected"
        assert client.get("/healthz").status_code == 200
    assert not app.state.worker.is_alive()


def test_two_workers_cannot_share_one_database(tmp_path):
    settings = Settings(tmp_path / "audit.sqlite3", "https://msbot.example.test")
    first = create_app(settings)
    second = create_app(settings)
    with TestClient(first), pytest.raises(RuntimeError, match="Another msbot worker"), TestClient(second):
        pass
