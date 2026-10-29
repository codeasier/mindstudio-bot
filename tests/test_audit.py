import uuid

from gitcode_cli.errors import APIError, NetworkError

from msbot.config import FeishuConnection
from msbot.feishu import NotificationRejected, NotificationUnknown

from .conftest import ADMIN, VALID_COMMENT, event_payload, headers, registration


def onboard(rig, **overrides):
    client, _store, _engine, _platform = rig
    response = client.post("/api/repos", json=registration(str(uuid.uuid4()), **overrides), headers=ADMIN)
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "succeeded", response.json()
    return response.json()["data"]["result"]


def submit(rig, repo, payload=None, delivery="delivery-1"):
    response = rig[0].post(
        f"/hooks/gitcode/{repo['id']}", json=payload or event_payload(), headers=headers(delivery)
    )
    assert response.status_code == 202, response.text
    return response.json()["data"]


def test_valid_single_comment_preserves_labels(rig):
    client, store, engine, platform = rig
    repo = onboard(rig)
    platform.comment_items = [{"id": 1, "body": "ordinary reply"}, {"id": 2, "body": VALID_COMMENT}]
    job = submit(rig, repo)
    assert engine.run_once()
    result = store.job_get(job["id"])
    assert result["status"] == "passed"
    assert result["result"]["valid_comment_id"] == 2
    assert platform.removals == []
    assert "resolved" in platform.labels
    assert client.get(f"/api/tasks/{job['id']}", headers=ADMIN).status_code == 200


def test_invalid_comment_removes_only_resolved(rig):
    _, store, engine, platform = rig
    repo = onboard(rig)
    platform.comment_items = [{"id": 1, "body": "修好了"}]
    job = submit(rig, repo)
    assert engine.run_once()
    result = store.job_get(job["id"])
    assert result["status"] == "rejected"
    assert result["removal"] == "confirmed"
    assert result["notification"] == "none"
    assert result["result"]["errors"]
    assert platform.labels == ["bug", "other-label"]
    assert all(revision == "a" * 40 for _, revision in platform.reads)


def test_fields_are_not_combined_across_comments(rig):
    _, store, engine, platform = rig
    repo = onboard(rig)
    first, second = VALID_COMMENT.split("### 回归包类型")
    platform.comment_items = [{"id": 1, "body": first}, {"id": 2, "body": "### 回归包类型" + second}]
    job = submit(rig, repo)
    engine.run_once()
    assert store.job_get(job["id"])["status"] == "rejected"


def test_repeated_delivery_does_not_repeat_effects(rig):
    _, store, engine, platform = rig
    repo = onboard(rig)
    job = submit(rig, repo)
    engine.run_once()
    again = submit(rig, repo)
    assert again["id"] == job["id"]
    assert not engine.run_once()
    assert len(platform.removals) == 1
    assert len(store.job_list()) == 1


def test_delivery_identity_conflict_rejected(rig):
    client, _, _, _ = rig
    repo = onboard(rig)
    submit(rig, repo)
    changed = event_payload() | {"uuid": "different-event"}
    response = client.post(f"/hooks/gitcode/{repo['id']}", json=changed, headers=headers())
    assert response.status_code == 409


def test_unrelated_label_update_does_not_audit(rig):
    _, store, engine, platform = rig
    repo = onboard(rig)
    payload = event_payload()
    payload["changes"]["labels"]["previous"] = payload["changes"]["labels"]["current"]
    job = submit(rig, repo, payload)
    assert job["status"] == "ignored"
    assert not engine.run_once()
    assert platform.removals == []
    assert store.job_get(job["id"])["reason"] == "resolved_not_added"


def test_absent_bug_or_resolved_ignored(rig):
    _, store, engine, platform = rig
    repo = onboard(rig)
    platform.labels = ["resolved"]
    job = submit(rig, repo)
    engine.run_once()
    assert store.job_get(job["id"])["status"] == "ignored"
    assert not platform.removals


def test_read_failure_never_means_empty_comments(rig):
    _, store, engine, platform = rig
    repo = onboard(rig)
    platform.comment_error = NetworkError("token=DO_NOT_EXPOSE")
    job = submit(rig, repo)
    engine.run_once()
    result = store.job_get(job["id"])
    assert result["status"] == "failed"
    assert "DO_NOT_EXPOSE" not in str(result)
    assert not platform.removals


def test_unknown_removal_is_not_retried(rig):
    _, store, engine, platform = rig
    repo = onboard(rig)
    platform.remove_error = TimeoutError("may have completed")
    job = submit(rig, repo)
    engine.run_once()
    assert store.job_get(job["id"])["status"] == "blocked"
    store.recover()
    assert not engine.run_once()
    assert platform.removals == ["42"]


def test_disabled_configuration_prevents_removal(rig):
    _, store, engine, platform = rig
    repo = onboard(rig)
    platform.config = platform.config.replace("enabled: true", "enabled: false")
    job = submit(rig, repo)
    engine.run_once()
    assert store.job_get(job["id"])["reason"] == "audit_disabled"
    assert not platform.removals


def test_invalid_update_retains_last_valid_bundle(rig):
    _, store, engine, platform = rig
    repo = onboard(rig)
    platform.template = "body: []"
    job = submit(rig, repo)
    engine.run_once()
    assert store.job_get(job["id"])["status"] == "rejected"
    assert store.repo_get(repo["id"])["config_error"].startswith("config_refresh_failed")


def test_missing_entry_disables_old_bundle(rig):
    _, store, engine, platform = rig
    repo = onboard(rig)
    original = platform.file_text

    def missing(path, ref):
        if path == ".msbot/config.yaml":
            raise APIError("not found", 404)
        return original(path, ref)

    platform.file_text = missing
    job = submit(rig, repo)
    engine.run_once()
    assert store.job_get(job["id"])["status"] == "ignored"
    assert store.repo_get(repo["id"])["config"] is None
    assert not platform.removals


def test_feishu_sent_only_after_confirmed_removal(rig):
    _, store, engine, platform = rig
    engine.settings.feishu["team"] = FeishuConnection(
        "FEISHU_URL", ("安全关键词",), repositories=("owner/repo",)
    )
    platform.config += "    notification: team\n"
    messages = []

    def notify(connection, message):
        assert "resolved" not in platform.labels
        assert connection.keywords == ("安全关键词",)
        messages.append(message)

    engine.notify = notify
    repo = onboard(rig)
    job = submit(rig, repo)
    engine.run_once()
    assert store.job_get(job["id"])["notification"] == "sent"
    assert len(messages) == 1
    assert ".msbot/template/bug-close.yml" in messages[0]


def test_notification_failure_does_not_undo_removal(rig):
    _, store, engine, platform = rig
    engine.settings.feishu["team"] = FeishuConnection("FEISHU_URL", repositories=("owner/repo",))
    platform.config += "    notification: team\n"

    def reject(*_args):
        raise NotificationRejected("synthetic rejection")

    engine.notify = reject
    repo = onboard(rig)
    job = submit(rig, repo)
    engine.run_once()
    result = store.job_get(job["id"])
    assert result["status"] == "rejected"
    assert result["notification"] == "failed"
    assert result["removal"] == "confirmed"
    assert "resolved" not in platform.labels


def test_unknown_notification_blocks_without_resend(rig):
    _, store, engine, platform = rig
    engine.settings.feishu["team"] = FeishuConnection("FEISHU_URL", repositories=("owner/repo",))
    platform.config += "    notification: team\n"

    def unknown(*_args):
        raise NotificationUnknown("timeout")

    engine.notify = unknown
    repo = onboard(rig)
    job = submit(rig, repo)
    engine.run_once()
    assert store.job_get(job["id"])["status"] == "blocked"
    assert store.job_get(job["id"])["notification"] == "unknown"
    assert not engine.run_once()


def test_notification_connection_cannot_be_selected_by_another_repository(rig):
    client, _, engine, platform = rig
    engine.settings.feishu["team"] = FeishuConnection("FEISHU_URL", repositories=("other/repo",))
    platform.config += "    notification: team\n"
    response = client.post("/api/repos", json=registration(str(uuid.uuid4())), headers=ADMIN)
    assert response.json()["data"]["status"] == "failed"
    assert not platform.removals


def test_revoked_notification_binding_does_not_reuse_old_authorization(rig):
    _, store, engine, platform = rig
    engine.settings.feishu["team"] = FeishuConnection("FEISHU_URL", repositories=("owner/repo",))
    platform.config += "    notification: team\n"
    repo = onboard(rig)
    job = submit(rig, repo)
    engine.settings.feishu["team"] = FeishuConnection("FEISHU_URL", repositories=("other/repo",))
    engine.run_once()
    assert store.job_get(job["id"])["status"] == "failed"
    assert not platform.removals
