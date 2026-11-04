import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from gitcode_cli.errors import APIError

from msbot.config import FeishuConnection
from msbot.feishu import NotificationRejected, NotificationUnknown
from msbot.store import Store

from .conftest import ADMIN, VALID_COMMENT
from .test_audit import onboard, submit


def failed_job(rig):
    _, store, engine, platform = rig
    repo = onboard(rig)
    platform.comment_error = APIError("token=do-not-disclose", 503)
    job = submit(rig, repo)
    engine.run_once()
    assert store.job_get(job["id"])["status"] == "failed"
    return repo, store.job_get(job["id"])


def retry(rig, job, operation_id=None):
    return rig[0].post(
        f"/api/tasks/{job['id']}/retry",
        json={"operation_id": operation_id or str(uuid.uuid4())},
        headers=ADMIN,
    )


def test_failed_job_has_safe_diagnostics_log_and_admin_counts(rig, caplog):
    client, store, _, platform = rig
    _, job = failed_job(rig)
    assert job["result"]["error"]["status_code"] == 503
    assert job["result"]["error"]["retryable"]
    assert "do-not-disclose" not in str(job) + caplog.text
    assert job["id"] in caplog.text
    assert not platform.removals
    assert client.get("/api/tasks/stats").status_code == 401
    stats = client.get("/api/tasks/stats", headers=ADMIN).json()["data"]
    assert stats["failed"] == stats["counts"]["failed"] == 1
    assert stats["blocked"] == 0
    assert store.job_get(job["id"])["notification"] == "none"


def test_retry_retains_failure_and_webhook_dedup_and_reloads_state(rig):
    client, store, engine, platform = rig
    repo, job = failed_job(rig)
    operation_id = str(uuid.uuid4())
    result = retry(rig, job, operation_id)
    assert result.status_code == 200
    receipt = result.json()
    assert receipt["data"]["status"] == "succeeded"
    child_id = receipt["data"]["result"]["task_id"]
    assert receipt["data"]["result"]["retry_of"] == job["id"]
    assert store.job_get(child_id)["status"] == "queued"
    assert store.job_get(job["id"]) == job
    assert submit(rig, repo)["id"] == job["id"]
    assert retry(rig, job, operation_id).json() == receipt
    assert retry(rig, job).status_code == 409
    platform.comment_error = None
    platform.comment_items = [{"id": 1, "body": VALID_COMMENT}]
    platform.reads.clear()
    assert engine.run_once()
    assert platform.reads
    assert store.job_get(child_id)["status"] == "passed"
    assert not platform.removals
    assert retry(rig, job, operation_id).json() == receipt
    assert client.get(f"/api/operations/{operation_id}", headers=ADMIN).json()["data"] == receipt["data"]


def test_retry_reads_current_labels_and_disabled_configuration(rig):
    _, store, engine, platform = rig
    _, job = failed_job(rig)
    child = retry(rig, job).json()["data"]["result"]["task_id"]
    platform.config = platform.config.replace("enabled: true", "enabled: false")
    engine.run_once()
    assert store.job_get(child)["status"] == "ignored"
    assert not platform.removals


@pytest.mark.parametrize(
    ("status", "removal", "notification"),
    [
        ("queued", "none", "none"),
        ("running", "none", "none"),
        ("passed", "none", "none"),
        ("rejected", "confirmed", "none"),
        ("blocked", "unknown", "none"),
        ("failed", "calling", "none"),
        ("failed", "confirmed", "none"),
        ("failed", "none", "sending"),
        ("failed", "none", "unknown"),
    ],
)
def test_retry_refuses_unsafe_or_nonfailed_jobs(rig, status, removal, notification):
    _, store, _, _ = rig
    _, job = failed_job(rig)
    store.job_update(job["id"], status=status, removal=removal, notification=notification)
    assert retry(rig, job).status_code == 409
    assert len(store.job_list()) == 1


def test_retry_requires_auth_valid_id_and_active_binding(rig):
    client, store, _, _ = rig
    repo, job = failed_job(rig)
    path = f"/api/tasks/{job['id']}/retry"
    assert client.post(path, json={"operation_id": str(uuid.uuid4())}).status_code == 401
    assert retry(rig, job, "not-a-uuid").status_code == 400
    assert retry(rig, {"id": "missing"}).status_code == 404
    store.repo_update(repo["id"], status="blocked")
    assert retry(rig, job).status_code == 409
    assert len(store.job_list()) == 1


def test_retry_operation_identity_cannot_target_another_job(rig):
    _, _, _, _ = rig
    _, job = failed_job(rig)
    identity = str(uuid.uuid4())
    assert retry(rig, job, identity).status_code == 200
    assert retry(rig, {"id": "different-task"}, identity).status_code == 409


def test_concurrent_retry_has_one_durable_receipt_and_child(rig):
    _, store, _, _ = rig
    _, job = failed_job(rig)
    identity = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.retry_job(job["id"], identity), range(4)))
    assert results.count(results[0]) == 4
    assert len(store.job_list()) == 2
    reloaded = Store(store.path)
    reloaded.recover()
    assert reloaded.retry_job(job["id"], identity) == results[0]
    assert len(reloaded.job_list()) == 2


@pytest.mark.parametrize("outcome", ["sent", "failed", "unknown"])
def test_failure_alert_preserves_audit_failure_and_never_auto_resends(rig, outcome):
    _, store, engine, platform = rig
    engine.settings.feishu["team"] = FeishuConnection("FEISHU_URL", repositories=("owner/repo",))
    platform.config += "    notification: team\n"
    messages = []

    def notify(_connection, message):
        messages.append(message)
        assert "已移除 resolved" not in message
        assert "resolved" in platform.labels
        if outcome == "failed":
            raise NotificationRejected("synthetic")
        if outcome == "unknown":
            raise NotificationUnknown("synthetic")

    engine.notify = notify
    repo, job = failed_job(rig)
    assert job["status"] == "failed"
    assert job["removal"] == job["notification"] == "none"
    assert job["failure_notification"] == outcome
    assert len(messages) == 1
    submit(rig, repo)
    store.recover()
    assert not engine.run_once()
    assert len(messages) == 1


def test_revoked_failure_alert_binding_is_not_used(rig):
    _, store, engine, platform = rig
    engine.settings.feishu["team"] = FeishuConnection("FEISHU_URL", repositories=("owner/repo",))
    platform.config += "    notification: team\n"
    repo = onboard(rig)
    engine.settings.feishu["team"] = FeishuConnection("FEISHU_URL", repositories=("other/repo",))
    messages = []
    engine.notify = lambda *args: messages.append(args)
    job = submit(rig, repo)
    engine.run_once()
    assert store.job_get(job["id"])["status"] == "failed"
    assert store.job_get(job["id"])["failure_notification"] == "none"
    assert not messages


def test_no_cached_configuration_preserves_underlying_api_error(rig):
    _, store, engine, platform = rig
    repo = onboard(rig)
    store.repo_update(repo["id"], config=None)
    platform.branch = lambda _: (_ for _ in ()).throw(APIError("secret body", 429))
    job = submit(rig, repo)
    engine.run_once()
    result = store.job_get(job["id"])
    assert result["status"] == "failed"
    assert result["result"]["error"]["status_code"] == 429
    assert not platform.removals


def test_unknown_failure_alert_is_recovered_without_replay(rig):
    _, store, engine, _ = rig
    _, job = failed_job(rig)
    store.job_update(job["id"], failure_notification="sending")
    store.recover()
    assert store.job_get(job["id"])["failure_notification"] == "unknown"
    assert store.job_get(job["id"])["status"] == "failed"
    assert not engine.run_once()


def test_legacy_jobs_additive_migration_preserves_records(rig):
    _, store, _, _ = rig
    _, job = failed_job(rig)
    with sqlite3.connect(store.path) as db:
        db.execute("DROP INDEX jobs_retry_parent")
        db.execute("ALTER TABLE jobs DROP COLUMN retry_of")
        db.execute("ALTER TABLE jobs DROP COLUMN failure_notification")
    migrated = Store(store.path)
    after = migrated.job_get(job["id"])
    assert after == job
    assert migrated.job_stats()["failed"] == 1
    with migrated.connect() as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert Store(store.path).job_get(job["id"]) == after


def test_write_outcome_unknown_has_diagnostics_alert_and_cannot_retry(rig):
    _, store, engine, platform = rig
    engine.settings.feishu["team"] = FeishuConnection("FEISHU_URL", repositories=("owner/repo",))
    platform.config += "    notification: team\n"
    messages = []
    engine.notify = lambda *args: messages.append(args)
    repo = onboard(rig)
    platform.remove_error = APIError("do-not-disclose", 503)
    job = submit(rig, repo)
    engine.run_once()
    result = store.job_get(job["id"])
    assert result["status"] == "blocked"
    assert result["removal"] == "unknown"
    assert result["result"]["error"]["status_code"] == 503
    assert result["failure_notification"] == "sent"
    assert len(messages) == 1
    assert retry(rig, job).status_code == 409
    store.recover()
    assert not engine.run_once()
    assert platform.removals == ["42"]


def test_failure_and_alert_intent_are_atomic_before_external_call(rig, monkeypatch):
    _, store, engine, platform = rig
    engine.settings.feishu["team"] = FeishuConnection("FEISHU_URL", repositories=("owner/repo",))
    platform.config += "    notification: team\n"
    messages = []
    engine.notify = lambda *args: messages.append(args)
    repo = onboard(rig)
    platform.comment_error = APIError("private", 503)
    job = submit(rig, repo)
    update = store.job_update

    class SimulatedCrash(BaseException):
        pass

    def crash_after_commit(identity, **values):
        update(identity, **values)
        if values.get("status") == "failed":
            assert store.job_get(identity)["failure_notification"] == "sending"
            raise SimulatedCrash

    monkeypatch.setattr(store, "job_update", crash_after_commit)
    with pytest.raises(SimulatedCrash):
        engine.run_once()
    assert not messages
    store.recover()
    assert store.job_get(job["id"])["status"] == "failed"
    assert store.job_get(job["id"])["failure_notification"] == "unknown"
    assert not engine.run_once()


@pytest.mark.parametrize("stage", ["removal", "notification"])
def test_running_unknown_external_write_never_requeues(rig, stage):
    _, store, engine, _ = rig
    repo = onboard(rig)
    job = submit(rig, repo)
    store.claim()
    store.job_update(job["id"], **{stage: "unknown"})
    store.recover()
    assert store.job_get(job["id"])["status"] == "blocked"
    assert not engine.run_once()


def test_different_operation_ids_concurrently_create_one_retry(rig):
    from msbot.store import Conflict

    _, store, _, _ = rig
    _, job = failed_job(rig)

    def attempt(_):
        try:
            return store.retry_job(job["id"], str(uuid.uuid4()))["status"]
        except Conflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(attempt, range(4)))
    assert outcomes.count("succeeded") == 1
    assert outcomes.count("conflict") == 3
    assert len(store.job_list()) == 2


def test_retry_rolls_back_child_when_receipt_write_fails(rig):
    _, store, _, _ = rig
    _, job = failed_job(rig)
    identity = str(uuid.uuid4())
    with store.connect() as db:
        db.execute(
            "CREATE TRIGGER fail_receipt BEFORE INSERT ON operations "
            "BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="simulated failure"):
        store.retry_job(job["id"], identity)
    assert len(store.job_list()) == 1
    assert store.operation_get(identity) is None
    with store.connect() as db:
        db.execute("DROP TRIGGER fail_receipt")
    assert store.retry_job(job["id"], identity)["status"] == "succeeded"
    assert len(store.job_list()) == 2
