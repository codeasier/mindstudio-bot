from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from msbot.app import create_app
from msbot.config import Settings

ROOT = Path(__file__).resolve().parents[1]
MANAGED_REPO = ROOT / "examples/managed-repo"
TEMPLATE = (MANAGED_REPO / ".msbot/template/bug-close.yml").read_text(encoding="utf-8")
VALID_COMMENT = (ROOT / "examples/bug-close-comment.md").read_text(encoding="utf-8")
CONFIG = (MANAGED_REPO / ".msbot/config.yaml").read_text(encoding="utf-8")


class FakePlatform:
    def __init__(self):
        self.labels = ["bug", "resolved", "other-label"]
        self.comment_items = []
        self.config = CONFIG
        self.template = TEMPLATE
        self.removals = []
        self.hooks = []
        self.reads = []
        self.comment_error = None
        self.remove_error = None
        self.hook_error = None

    def repository(self):
        return {"id": 7, "default_branch": "main"}

    def branch(self, branch):
        assert branch == "main"
        return {"commit": {"id": "a" * 40}}

    def file_text(self, path, revision):
        self.reads.append((path, revision))
        if path == ".msbot/config.yaml":
            return self.config
        assert path == ".msbot/template/bug-close.yml"
        return self.template

    def issue(self, number):
        return {"number": number, "labels": [{"name": name} for name in self.labels]}

    def comments(self, _number):
        if self.comment_error:
            raise self.comment_error
        return deepcopy(self.comment_items)

    def remove_resolved(self, number):
        self.removals.append(number)
        self.labels.remove("resolved")
        if self.remove_error:
            raise self.remove_error
        return {}

    def create_webhook(self, url, password, mode):
        self.hooks.append((url, password, mode))
        if self.hook_error:
            raise self.hook_error
        return {"id": 21}

    def close(self):
        pass


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("MSBOT_ADMIN_TOKEN", "synthetic-admin")
    monkeypatch.setenv("MSBOT_GITCODE_TOKEN", "synthetic-token")
    monkeypatch.setenv("MSBOT_GITCODE_WEBHOOK_SECRET", "synthetic-webhook-password")
    platform = FakePlatform()
    settings = Settings(tmp_path / "audit.sqlite3", "https://msbot.example.test")
    app = create_app(settings, platform_factory=lambda *_args: platform, start_worker=False)
    with TestClient(app) as client:
        yield client, app.state.store, app.state.engine, platform


def event_payload():
    return {
        "object_kind": "issue",
        "event_type": "issue",
        "uuid": "event-id",
        "project": {"id": 7, "path_with_namespace": "owner/repo"},
        "object_attributes": {"iid": 42, "action": "update"},
        "changes": {
            "labels": {"previous": [{"title": "bug"}], "current": [{"title": "bug"}, {"title": "resolved"}]}
        },
    }


def registration(identity: str, **overrides):
    result = {
        "operation_id": identity,
        "repository": "owner/repo",
        "token_env": "MSBOT_GITCODE_TOKEN",
        "webhook_secret_env": "MSBOT_GITCODE_WEBHOOK_SECRET",
        "create_webhook": False,
    }
    return result | overrides


def headers(delivery="delivery-1"):
    return {
        "X-GitCode-Token": "synthetic-webhook-password",
        "X-GitCode-Event": "Issue Hook",
        "X-GitCode-Delivery": delivery,
    }


ADMIN = {"Authorization": "Bearer synthetic-admin"}
