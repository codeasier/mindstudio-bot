import hashlib
import hmac
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Lock

import pytest
from fastapi.testclient import TestClient
from gitcode_cli.errors import APIError, NetworkError

from msbot.app import create_app
from msbot.config import ConfigurationError, FeishuConnection, GitCodeDefaults, Settings
from msbot.store import Store, separate_pending_candidates

from .conftest import ADMIN, CONFIG, VALID_COMMENT, FakePlatform, event_payload, headers
from .test_audit import onboard


@pytest.fixture
def unified_rig(tmp_path, monkeypatch):
    monkeypatch.setenv("MSBOT_ADMIN_TOKEN", "synthetic-admin")
    monkeypatch.setenv("MSBOT_GITCODE_TOKEN", "synthetic-token")
    monkeypatch.setenv("MSBOT_GITCODE_WEBHOOK_SECRET", "synthetic-webhook-password")
    platform = FakePlatform()
    calls = []

    def factory(*args):
        calls.append(args)
        return platform

    settings = Settings(
        tmp_path / "audit.sqlite3",
        "https://msbot.example.test",
        tester_authors=("alice",),
        gitcode=GitCodeDefaults(),
    )
    app = create_app(settings, platform_factory=factory, start_worker=False)
    with TestClient(app) as client:
        yield client, app.state.store, app.state.engine, platform, calls


def receive(rig, payload=None, delivery="delivery-1"):
    response = rig[0].post("/hooks/gitcode", json=payload or event_payload(), headers=headers(delivery))
    assert response.status_code == 202, response.text
    return response.json()


def project_delivery(project_id, delivery):
    return event_payload() | {
        "uuid": delivery,
        "project": {"id": project_id, "path_with_namespace": "owner/repo"},
    }


@pytest.mark.parametrize("valid_comment", [False, True])
def test_first_authenticated_event_runs_without_registration(unified_rig, valid_comment):
    client, store, engine, platform, calls = unified_rig
    if valid_comment:
        platform.comment_items = [{"id": 9, "body": VALID_COMMENT}]
    job = receive(unified_rig)["data"]
    assert job["status"] == "queued"
    assert len(store.repo_list()) == 1
    assert store.repo_list()[0]["status"] == "pending"
    assert store.operation_get(job["id"]) is None
    assert calls == []
    assert not platform.hooks
    assert engine.run_once()
    assert calls == [("owner/repo", "MSBOT_GITCODE_TOKEN"), ("owner/repo", "MSBOT_GITCODE_TOKEN")]
    assert store.repo_list()[0]["status"] == "active"
    assert store.job_get(job["id"])["status"] == ("passed" if valid_comment else "rejected")
    assert platform.removals == ([] if valid_comment else ["42"])
    assert platform.reads == [
        (".msbot/config.yaml", "a" * 40),
        (".msbot/template/bug-close.yml", "a" * 40),
    ]
    public = client.get("/api/repos", headers=ADMIN).json()["data"]
    assert public[0]["repository"] == "owner/repo"
    assert public[0]["webhook_id"] is None


def test_unified_endpoint_requires_instance_defaults(rig):
    client, store, _, _ = rig
    assert client.post("/hooks/gitcode", json=event_payload(), headers=headers()).status_code == 503
    assert not store.repo_list()
    assert not store.job_list()


def test_bad_password_is_rejected_before_parsing_or_discovery(unified_rig):
    client, store, _, _, calls = unified_rig
    response = client.post("/hooks/gitcode", content=b"not json", headers={"X-GitCode-Token": "wrong"})
    assert response.status_code == 401
    assert not calls
    assert not store.repo_list()
    assert not store.job_list()


def test_unified_signature_authenticates_exact_body(unified_rig):
    _, store, engine, _, calls = unified_rig
    settings = replace(engine.settings, gitcode=GitCodeDefaults(webhook_mode="signature"))
    app = create_app(settings, platform_factory=engine.platform_factory, start_worker=False)
    body = json.dumps(event_payload(), ensure_ascii=False).encode()
    signature = "sha256=" + hmac.new(b"synthetic-webhook-password", body, hashlib.sha256).hexdigest()
    signed = headers() | {"X-GitCode-Signature-256": signature}
    with TestClient(app) as signed_client:
        assert signed_client.post("/hooks/gitcode", content=body, headers=headers()).status_code == 401
        assert signed_client.post("/hooks/gitcode", content=body + b" ", headers=signed).status_code == 401
        assert not calls
        response = signed_client.post("/hooks/gitcode", content=body, headers=signed)
        assert response.status_code == 202
    assert len(store.repo_list()) == len(store.job_list()) == 1


@pytest.mark.parametrize(
    "project",
    [
        None,
        {},
        {"id": 7, "path_with_namespace": "../repo"},
        {"id": True, "path_with_namespace": "owner/repo"},
        {"id": "0", "path_with_namespace": "owner/repo"},
        {"id": "0007", "path_with_namespace": "owner/repo"},
        {"id": "７", "path_with_namespace": "owner/repo"},
    ],
)
def test_malformed_project_never_triggers_lookup(unified_rig, project):
    client, store, _, _, calls = unified_rig
    payload = event_payload() | {"project": project}
    assert client.post("/hooks/gitcode", json=payload, headers=headers()).status_code == 400
    assert not calls
    assert not store.repo_list()
    assert not store.job_list()


@pytest.mark.parametrize("body", [b"not json", b"[]", b"\xff"])
def test_invalid_json_is_rejected_before_lookup(unified_rig, body):
    client, store, _, _, calls = unified_rig
    assert client.post("/hooks/gitcode", content=body, headers=headers()).status_code == 400
    assert not calls
    assert not store.repo_list()


def test_invalid_delivery_and_oversized_body_never_discover(unified_rig):
    client, store, _, _, calls = unified_rig
    payload = event_payload() | {"uuid": None}
    response = client.post(
        "/hooks/gitcode",
        json=payload,
        headers={"X-GitCode-Token": "synthetic-webhook-password", "X-GitCode-Event": "Issue Hook"},
    )
    assert response.status_code == 400
    assert client.post("/hooks/gitcode", content=b" " * 1_048_577, headers=headers()).status_code == 413
    assert not calls
    assert not store.repo_list()


def test_api_identity_mismatch_is_ignored_without_label_changes(unified_rig):
    client, store, engine, platform, calls = unified_rig
    payload = event_payload() | {"project": {"id": 8, "path_with_namespace": "owner/repo"}}
    response = client.post("/hooks/gitcode", json=payload, headers=headers())
    assert response.status_code == 202
    assert calls == []
    assert store.repo_list()[0]["status"] == "pending"
    assert len(store.job_list()) == 1
    assert engine.run_once()
    result = store.job_get(response.json()["data"]["id"])
    assert result["status"] == "ignored"
    assert result["reason"] == "repository_unverified"
    assert result["notification"] == "none"
    assert result["removal"] == "none"
    assert not platform.removals
    assert not platform.hooks
    assert store.repo_list()[0]["status"] == "pending"


def test_ignored_candidate_does_not_block_a_later_verified_identity(unified_rig):
    client, store, engine, platform, calls = unified_rig
    wrong = client.post("/hooks/gitcode", json=project_delivery(8, "wrong-event"), headers=headers("wrong"))
    assert wrong.status_code == 202, wrong.text
    wrong_job = wrong.json()["data"]["id"]
    wrong_repo = wrong.json()["data"]["repo_id"]
    assert engine.run_once()
    assert store.job_get(wrong_job)["status"] == "ignored"
    assert store.job_get(wrong_job)["reason"] == "repository_unverified"
    assert store.repo_get(wrong_repo)["status"] == "pending"
    assert store.repo_get(wrong_repo)["project_id"] == "8"
    assert not platform.removals
    assert not platform.hooks

    correct = client.post("/hooks/gitcode", json=project_delivery(7, "right-event"), headers=headers("right"))
    assert correct.status_code == 202, correct.text
    correct_job = correct.json()["data"]["id"]
    correct_repo = correct.json()["data"]["repo_id"]
    assert correct_repo != wrong_repo
    assert store.repo_get(wrong_repo)["project_id"] == "8"
    assert engine.run_once()
    assert store.job_get(correct_job)["status"] == "rejected"
    assert store.repo_get(correct_repo) == store.repo_get(correct_repo) | {
        "status": "active",
        "project_id": "7",
    }
    assert store.repo_get(wrong_repo)["status"] == "pending"
    assert store.repo_get(wrong_repo)["project_id"] == "8"
    assert store.job_get(wrong_job)["status"] == "ignored"
    assert platform.removals == ["42"]
    assert not platform.hooks

    seen = len(calls)
    conflict = client.post(
        "/hooks/gitcode", json=project_delivery(8, "wrong-again"), headers=headers("wrong-again")
    )
    assert conflict.status_code == 409
    assert calls[seen:] == []
    assert store.repo_get(correct_repo)["project_id"] == "7"
    assert store.repo_get(wrong_repo)["project_id"] == "8"
    assert platform.removals == ["42"]
    assert {job["id"] for job in store.job_list()} == {wrong_job, correct_job}


def test_queued_mismatch_cannot_execute_after_another_candidate_is_verified(unified_rig):
    client, store, engine, platform, _calls = unified_rig
    wrong = client.post("/hooks/gitcode", json=project_delivery(8, "wrong-event"), headers=headers("wrong"))
    correct = client.post("/hooks/gitcode", json=project_delivery(7, "right-event"), headers=headers("right"))
    assert wrong.status_code == correct.status_code == 202
    wrong_job = wrong.json()["data"]["id"]
    wrong_repo = wrong.json()["data"]["repo_id"]
    correct_repo = correct.json()["data"]["repo_id"]
    with store.connect() as db:
        db.execute("UPDATE jobs SET status='blocked' WHERE id=?", (wrong_job,))
    assert engine.run_once()
    assert store.repo_get(correct_repo)["status"] == "active"
    assert store.repo_get(correct_repo)["project_id"] == "7"
    assert store.repo_get(wrong_repo)["project_id"] == "8"
    with store.connect() as db:
        db.execute(
            "UPDATE jobs SET status='queued', reason=NULL WHERE id=?",
            (wrong_job,),
        )
    assert engine.run_once()
    assert store.job_get(wrong_job)["status"] == "ignored"
    assert store.job_get(wrong_job)["reason"] == "repository_unverified"
    assert store.job_get(wrong_job)["removal"] == "none"
    assert store.repo_get(wrong_repo)["status"] == "pending"
    assert store.repo_get(wrong_repo)["project_id"] == "8"
    assert store.repo_get(correct_repo)["status"] == "active"
    assert platform.removals == ["42"]
    assert not platform.hooks


def test_candidate_identities_survive_restart(unified_rig):
    client, store, engine, platform, _calls = unified_rig
    wrong = client.post("/hooks/gitcode", json=project_delivery(8, "wrong-event"), headers=headers("wrong"))
    correct = client.post("/hooks/gitcode", json=project_delivery(7, "right-event"), headers=headers("right"))
    assert wrong.status_code == correct.status_code == 202
    wrong_job = wrong.json()["data"]["id"]
    correct_job = correct.json()["data"]["id"]
    wrong_repo = wrong.json()["data"]["repo_id"]
    correct_repo = correct.json()["data"]["repo_id"]
    with store.connect() as db:
        db.execute("UPDATE jobs SET status='running'")
    store.recover()
    assert {job["status"] for job in store.job_list()} == {"queued"}
    assert store.repo_get(wrong_repo)["project_id"] == "8"
    assert store.repo_get(correct_repo)["project_id"] == "7"
    assert engine.run_once()
    assert engine.run_once()
    assert store.job_get(wrong_job)["reason"] == "repository_unverified"
    assert store.job_get(correct_job)["status"] == "rejected"
    assert store.repo_get(wrong_repo)["status"] == "pending"
    assert store.repo_get(wrong_repo)["project_id"] == "8"
    assert store.repo_get(correct_repo)["status"] == "active"
    assert store.repo_get(correct_repo)["project_id"] == "7"
    assert platform.removals == ["42"]
    assert not engine.run_once()


def test_concurrent_distinct_candidates_do_not_overwrite_each_other(unified_rig):
    client, store, _engine, _platform, calls = unified_rig
    barrier = Barrier(8)

    def post(index):
        barrier.wait(timeout=5)
        project_id = 7 if index % 2 == 0 else 8
        delivery = f"delivery-{index}"
        return client.post(
            "/hooks/gitcode",
            json=project_delivery(project_id, delivery),
            headers=headers(delivery),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(post, range(8)))
    assert {response.status_code for response in responses} == {202}
    repos = store.repo_list()
    assert {repo["project_id"] for repo in repos} == {"7", "8"}
    assert {repo["status"] for repo in repos} == {"pending"}
    assert len(repos) == 2
    assert len(store.job_list()) == 8
    assert calls == []


def test_legacy_unique_name_keeps_pending_project_id(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.executescript("""
        CREATE TABLE repos (
            id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, project_id TEXT NOT NULL,
            token_env TEXT NOT NULL, webhook_secret_env TEXT NOT NULL,
            webhook_mode TEXT NOT NULL, status TEXT NOT NULL, webhook_id TEXT,
            config TEXT, config_error TEXT
        );
    """)
    legacy.execute(
        "INSERT INTO repos(id, name, project_id, token_env, webhook_secret_env, webhook_mode, status) "
        "VALUES('pending-8', 'owner/repo', '8', 'MSBOT_GITCODE_TOKEN', "
        "'MSBOT_GITCODE_WEBHOOK_SECRET', 'password', 'pending')"
    )
    legacy.commit()
    legacy.close()
    store = Store(path)
    payload = project_delivery(7, "right-event")
    _job, duplicate = store.accept_unified(
        "owner/repo",
        "7",
        {
            "token_env": "MSBOT_GITCODE_TOKEN",
            "webhook_secret_env": "MSBOT_GITCODE_WEBHOOK_SECRET",
            "webhook_mode": "password",
        },
        "right",
        "digest",
        payload,
        "42",
        None,
    )
    assert not duplicate
    assert store.repo_get("pending-8")["project_id"] == "8"
    assert store.repo_get("pending-8")["status"] == "pending"
    assert {repo["project_id"] for repo in store.repo_list()} == {"7", "8"}


def _legacy_queue(path):
    """Old UNIQUE(name) database with two bindings and one already-accepted queued job."""
    legacy = sqlite3.connect(path)
    legacy.executescript("""
        CREATE TABLE repos (
            id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, project_id TEXT NOT NULL,
            token_env TEXT NOT NULL, webhook_secret_env TEXT NOT NULL,
            webhook_mode TEXT NOT NULL, status TEXT NOT NULL, webhook_id TEXT,
            config TEXT, config_error TEXT
        );
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY, repo_id TEXT NOT NULL REFERENCES repos(id),
            delivery TEXT NOT NULL, event_key TEXT, request_hash TEXT NOT NULL, payload TEXT NOT NULL,
            issue_number TEXT, status TEXT NOT NULL, reason TEXT, snapshot TEXT, result TEXT,
            removal TEXT NOT NULL DEFAULT 'none', notification TEXT NOT NULL DEFAULT 'none',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(repo_id, delivery), UNIQUE(repo_id, event_key)
        );
    """)
    legacy.executemany(
        "INSERT INTO repos(id, name, project_id, token_env, webhook_secret_env, webhook_mode, status) "
        "VALUES(?, ?, ?, 'MSBOT_GITCODE_TOKEN', 'MSBOT_GITCODE_WEBHOOK_SECRET', 'password', ?)",
        [
            ("active-1", "owner/other", "1", "active"),
            ("pending-8", "owner/repo", "8", "pending"),
        ],
    )
    legacy.execute(
        "INSERT INTO jobs(id, repo_id, delivery, event_key, request_hash, payload, issue_number, status) "
        "VALUES('job-1', 'pending-8', 'delivery-old', 'event-old', 'digest', '{}', '42', 'queued')"
    )
    legacy.commit()
    legacy.close()


def _legacy_snapshot(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    repos = [tuple(row) for row in db.execute("SELECT id, name, project_id, status FROM repos ORDER BY id")]
    jobs = [tuple(row) for row in db.execute("SELECT id, repo_id, status FROM jobs ORDER BY id")]
    names = {row["name"] for row in db.execute("SELECT name FROM sqlite_master")}
    schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='repos'").fetchone()[0]
    db.close()
    return repos, jobs, names, schema


def test_legacy_migration_keeps_queued_job(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    _legacy_queue(path)
    store = Store(path)
    claimed = store.claim()
    assert claimed["id"] == "job-1"
    assert claimed["repo_id"] == "pending-8"
    assert claimed["status"] == "queued"
    assert store.repo_get("pending-8")["project_id"] == "8"
    assert store.repo_get("active-1")["status"] == "active"
    with store.connect() as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT name FROM sqlite_master WHERE name='repos_next'").fetchone() is None
    Store(path)
    assert len(store.repo_list()) == 2
    assert store.job_get("job-1")["status"] == "running"


@pytest.mark.parametrize("stage", ["copy", "drop", "rename"])
def test_legacy_migration_interruption_rolls_back_and_retries(tmp_path, monkeypatch, stage):
    path = tmp_path / "legacy.sqlite3"
    _legacy_queue(path)
    before = _legacy_snapshot(path)

    def interrupt(stage_name):
        if stage_name == stage:
            raise RuntimeError("migration interrupted")

    monkeypatch.setattr("msbot.store._migration_checkpoint", interrupt)
    with pytest.raises(RuntimeError, match="migration interrupted"):
        separate_pending_candidates(path)
    monkeypatch.undo()

    assert _legacy_snapshot(path) == before
    _repos, _jobs, names, schema = before
    assert "repos_next" not in names
    assert "UNIQUE" in schema

    store = Store(path)
    claimed = store.claim()
    assert claimed["id"] == "job-1"
    assert claimed["repo_id"] == "pending-8"
    assert store.repo_get("pending-8")["project_id"] == "8"
    assert store.repo_get("active-1")["project_id"] == "1"
    with store.connect() as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    again = Store(path)
    assert {repo["id"] for repo in again.repo_list()} == {"active-1", "pending-8"}
    assert again.job_get("job-1")["repo_id"] == "pending-8"


def test_legacy_migration_retries_over_a_leftover_scratch_table(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    _legacy_queue(path)
    leftover = sqlite3.connect(path)
    leftover.execute("CREATE TABLE repos_next (id TEXT PRIMARY KEY)")
    leftover.commit()
    leftover.close()
    store = Store(path)
    assert store.claim()["id"] == "job-1"
    assert store.repo_get("pending-8")["project_id"] == "8"
    with store.connect() as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT name FROM sqlite_master WHERE name='repos_next'").fetchone() is None


def test_legacy_migration_fk_violation_keeps_the_old_database(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    _legacy_queue(path)
    broken = sqlite3.connect(path)
    broken.execute("UPDATE jobs SET repo_id='missing'")
    broken.commit()
    broken.close()
    before = _legacy_snapshot(path)
    with pytest.raises(sqlite3.IntegrityError, match="dangling references"):
        separate_pending_candidates(path)
    assert _legacy_snapshot(path) == before
    with pytest.raises(sqlite3.IntegrityError, match="dangling references"):
        Store(path)


@pytest.mark.parametrize("error", [APIError("sensitive-token", 403), NetworkError("sensitive-token")])
def test_upstream_lookup_failure_is_sanitized_and_closes_client(unified_rig, error):
    client, store, engine, platform, _ = unified_rig
    closed = []

    def failed():
        raise error

    platform.repository = failed
    platform.close = lambda: closed.append(True)
    response = client.post("/hooks/gitcode", json=event_payload(), headers=headers())
    assert response.status_code == 202
    assert "sensitive-token" not in response.text
    assert closed == []
    assert engine.run_once()
    assert closed == [True]
    result = store.job_get(response.json()["data"]["id"])
    assert result["status"] == "ignored"
    assert result["reason"] == "repository_unverified"
    assert result["notification"] == "none"
    assert not platform.removals
    assert not platform.hooks
    assert store.repo_list()[0]["status"] == "pending"


@pytest.mark.parametrize("problem", ["missing", "disabled", "invalid", "invalid_template", "read_failure"])
def test_configuration_gates_actions_on_first_delivery(unified_rig, problem):
    _, store, engine, platform, _ = unified_rig
    original = platform.file_text

    def source(path, revision):
        if path == ".msbot/config.yaml" and problem in {"missing", "read_failure"}:
            raise APIError("synthetic", 404 if problem == "missing" else 503)
        return original(path, revision)

    platform.file_text = source
    if problem == "disabled":
        platform.config = CONFIG.replace("enabled: true", "enabled: false")
    elif problem == "invalid":
        platform.config = "invalid"
    elif problem == "invalid_template":
        platform.template = "body: []"
    job = receive(unified_rig)["data"]
    assert engine.run_once()
    result = store.job_get(job["id"])
    assert result["status"] == ("ignored" if problem in {"missing", "disabled"} else "failed")
    assert not platform.removals
    assert result["notification"] == "none"
    # A later valid config works without another registration step.
    platform.file_text = original
    platform.config, platform.template = CONFIG, FakePlatform().template
    next_job = receive(unified_rig, event_payload() | {"uuid": "later-event"}, "later-delivery")["data"]
    assert engine.run_once()
    assert store.job_get(next_job["id"])["status"] == "rejected"
    assert len(store.repo_list()) == 1


def test_unified_route_preserves_bundle_fallback_and_entry_deletion(unified_rig):
    _, store, engine, platform, _ = unified_rig
    platform.comment_items = [{"id": 9, "body": VALID_COMMENT}]
    receive(unified_rig)
    engine.run_once()
    platform.template = "body: []"
    job = receive(unified_rig, event_payload() | {"uuid": "invalid-update"}, "delivery-2")["data"]
    engine.run_once()
    assert store.job_get(job["id"])["status"] == "passed"
    assert store.repo_list()[0]["config_error"].startswith("config_refresh_failed")

    def missing(*_args):
        raise APIError("missing", 404)

    platform.file_text = missing
    job = receive(unified_rig, event_payload() | {"uuid": "missing-entry"}, "delivery-3")["data"]
    engine.run_once()
    assert store.job_get(job["id"])["status"] == "ignored"
    assert store.repo_list()[0]["config"] is None
    assert not platform.removals


def test_notification_connection_does_not_block_unified_audit(unified_rig):
    _, store, engine, platform, _ = unified_rig
    engine.settings.feishu["team"] = FeishuConnection("FEISHU_URL")
    platform.config += "    notification: team\n"
    messages = []
    engine.notify = lambda connection, message: messages.append(connection)
    job = receive(unified_rig)["data"]
    engine.run_once()
    result = store.job_get(job["id"])
    assert result["status"] == "rejected"
    assert result["removal"] == "confirmed"
    assert len(messages) == 1
    assert platform.removals == ["42"]


def test_delivery_is_deduplicated_across_routes_and_restart(unified_rig):
    client, store, engine, platform, _ = unified_rig
    job = receive(unified_rig)["data"]
    repo = store.repo_list()[0]
    response = client.post(f"/hooks/gitcode/{repo['id']}", json=event_payload(), headers=headers())
    assert response.json()["duplicate"]
    assert response.json()["data"]["id"] == job["id"]
    engine.run_once()
    store.recover()
    # Event uuid also deduplicates a new delivery ID.
    duplicate = receive(unified_rig, delivery="redelivery")
    assert duplicate["duplicate"]
    assert duplicate["data"]["id"] == job["id"]
    assert not engine.run_once()
    assert platform.removals == ["42"]
    assert len(store.job_list()) == 1


def test_conflicting_delivery_does_not_overwrite_original_task(unified_rig):
    client, store, _, _, _ = unified_rig
    job = receive(unified_rig)["data"]
    response = client.post("/hooks/gitcode", json=event_payload() | {"uuid": "changed"}, headers=headers())
    assert response.status_code == 409
    assert store.job_get(job["id"]) == job
    assert len(store.job_list()) == 1


def test_unrelated_event_is_recorded_without_running_audit(unified_rig):
    _, store, engine, platform, calls = unified_rig
    payload = event_payload()
    payload["object_attributes"]["action"] = "open"
    job = receive(unified_rig, payload)["data"]
    assert job["status"] == "queued"
    assert job["reason"] == "not_issue_update"
    assert calls == []
    assert engine.run_once()
    result = store.job_get(job["id"])
    assert result["status"] == "ignored"
    assert result["reason"] == "not_issue_update"
    assert not platform.reads
    assert not platform.removals
    assert len(store.repo_list()) == 1
    assert store.repo_list()[0]["status"] == "active"


def test_discovery_reuses_legacy_binding_without_overwriting_it(unified_rig, monkeypatch):
    _, store, _, _, _ = unified_rig
    monkeypatch.setenv("LEGACY_SECRET", "another-password")
    repo = onboard(unified_rig[:4], webhook_secret_env="LEGACY_SECRET", webhook_mode="signature")
    previous = store.repo_get(repo["id"])
    job = receive(unified_rig)["data"]
    assert job["repo_id"] == repo["id"]
    assert store.repo_get(repo["id"]) == previous
    assert len(store.repo_list()) == 1


@pytest.mark.parametrize("conflict", ["identity", "status"])
def test_existing_binding_conflicts_are_not_overwritten(unified_rig, conflict, monkeypatch):
    client, store, _, platform, calls = unified_rig
    monkeypatch.setenv("OTHER_TOKEN", "synthetic-other-token")
    repo = onboard(unified_rig[:4])
    payload = event_payload()
    if conflict == "identity":
        platform.repository = lambda: {"id": 8, "default_branch": "main"}
        payload["project"]["id"] = 8
    else:
        store.repo_update(repo["id"], status="blocked")
    previous = store.repo_get(repo["id"])
    seen = len(calls)
    assert client.post("/hooks/gitcode", json=payload, headers=headers()).status_code == 409
    assert calls[seen:] == []
    assert store.repo_get(repo["id"]) == previous
    assert not store.job_list()


def test_active_binding_is_accepted_without_gitcode_or_credential_changes(unified_rig, monkeypatch):
    client, store, _, platform, calls = unified_rig
    monkeypatch.setenv("OTHER_TOKEN", "synthetic-other-token")
    repo = onboard(unified_rig[:4], token_env="OTHER_TOKEN")
    previous = store.repo_get(repo["id"])
    seen = len(calls)

    def forbid_gitcode():
        raise AssertionError("GitCode contacted before 202")

    platform.repository = forbid_gitcode
    response = client.post("/hooks/gitcode", json=event_payload(), headers=headers())
    assert response.status_code == 202
    assert calls[seen:] == []
    assert store.repo_get(repo["id"]) == previous
    assert response.json()["data"]["repo_id"] == repo["id"]
    assert len(store.job_list()) == 1


def test_concurrent_first_deliveries_create_one_repository_and_task(unified_rig):
    client, store, _, platform, calls = unified_rig
    barrier = Barrier(4)

    def first_delivery(_index):
        barrier.wait(timeout=5)
        return client.post("/hooks/gitcode", json=event_payload(), headers=headers())

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(first_delivery, range(4)))
    assert {response.status_code for response in responses} == {202}
    bodies = [response.json() for response in responses]
    assert len({body["data"]["id"] for body in bodies}) == 1
    assert sum(not body["duplicate"] for body in bodies) == 1
    assert len(store.repo_list()) == len(store.job_list()) == 1
    assert store.repo_list()[0]["status"] == "pending"
    assert calls == []
    assert not platform.hooks


def test_registered_repository_accepts_fifty_concurrent_hooks_locally(unified_rig):
    client, store, engine, platform, calls = unified_rig
    repo = onboard(unified_rig[:4])
    calls.clear()

    def forbid_gitcode():
        raise AssertionError("GitCode contacted before 202")

    platform.repository = forbid_gitcode
    barrier = Barrier(50)

    def post(_index):
        barrier.wait(timeout=10)
        return client.post("/hooks/gitcode", json=event_payload(), headers=headers("burst-delivery"))

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=50) as pool:
        responses = list(pool.map(post, range(50)))
    elapsed = time.perf_counter() - started
    assert elapsed < 5
    assert {response.status_code for response in responses} == {202}
    bodies = [response.json() for response in responses]
    assert {body["data"]["id"] for body in bodies} == {bodies[0]["data"]["id"]}
    assert sum(body["duplicate"] for body in bodies) == 49
    assert bodies[0]["data"]["repo_id"] == repo["id"]
    assert calls == []
    assert not platform.hooks
    assert len(store.job_list()) == 1

    platform.repository = lambda: {"id": 7, "default_branch": "main"}
    inflight = {"current": 0, "max": 0}
    guard = Lock()
    original = platform.issue

    def issue(number):
        with guard:
            inflight["current"] += 1
            inflight["max"] = max(inflight["max"], inflight["current"])
        try:
            return original(number)
        finally:
            with guard:
                inflight["current"] -= 1

    platform.issue = issue
    assert engine.run_once()
    assert not engine.run_once()
    assert inflight["max"] == 1
    assert platform.removals == ["42"]
    assert not platform.hooks


def test_repository_scoped_ids_and_configurations_remain_independent(unified_rig):
    _, store, engine, first, calls = unified_rig
    second = FakePlatform()
    second.repository = lambda: {"id": 8, "default_branch": "main"}
    second.config = CONFIG.replace("enabled: true", "enabled: false")

    def factory(name, token_env):
        calls.append((name, token_env))
        return first if name == "owner/repo" else second

    engine.platform_factory = factory
    job_one = receive(unified_rig)["data"]
    payload = event_payload() | {"project": {"id": "8", "path_with_namespace": "owner/another"}}
    job_two = receive(unified_rig, payload)["data"]
    assert job_one["id"] != job_two["id"]
    assert engine.run_once()
    assert engine.run_once()
    assert store.job_get(job_one["id"])["status"] == "rejected"
    assert store.job_get(job_two["id"])["status"] == "ignored"
    assert not second.removals
    assert len(store.repo_list()) == len(store.job_list()) == 2


@pytest.mark.parametrize("mode", ["password", "signature"])
def test_instance_defaults_load_and_old_files_remain_valid(tmp_path, monkeypatch, mode):
    for name in ("MSBOT_ADMIN_TOKEN", "ACCOUNT_TOKEN", "HOOK_SECRET"):
        monkeypatch.setenv(name, "synthetic-value")
    path = tmp_path / "instance.yaml"
    source = (
        f"database: {tmp_path}/state.db\npublic_url: https://msbot.example.test\ntester_authors:\n  - alice\n"
    )
    path.write_text(source, encoding="utf-8")
    assert Settings.load(str(path)).gitcode is None
    path.write_text(
        source + f"gitcode:\n  token_env: ACCOUNT_TOKEN\n  webhook_secret_env: HOOK_SECRET\n"
        f"  webhook_mode: {mode}\n",
        encoding="utf-8",
    )
    assert Settings.load(str(path)).gitcode == GitCodeDefaults("ACCOUNT_TOKEN", "HOOK_SECRET", mode)


@pytest.mark.parametrize(
    "entry", ["null", "{webhook_mode: other}", "{token_env: 'literal-token!'}", "{unknown: true}", "{}"]
)
def test_invalid_defaults_and_missing_secrets_fail_at_startup(tmp_path, monkeypatch, entry):
    monkeypatch.setenv("MSBOT_ADMIN_TOKEN", "synthetic-admin")
    monkeypatch.delenv("MSBOT_GITCODE_TOKEN", raising=False)
    monkeypatch.delenv("MSBOT_GITCODE_WEBHOOK_SECRET", raising=False)
    path = tmp_path / "instance.yaml"
    path.write_text(
        f"database: {tmp_path}/state.db\npublic_url: https://msbot.example.test\n"
        f"tester_authors:\n  - alice\ngitcode: {entry}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        Settings.load(str(path))
