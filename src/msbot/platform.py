from __future__ import annotations

import base64

from gitcode_cli.client import GitCodeClient
from gitcode_cli.services.files import FileService
from gitcode_cli.services.issues import IssueService
from gitcode_cli.services.repos import RepoService

try:
    from gitcode_cli.services.webhooks import WebhookService
except ImportError:
    raise RuntimeError(
        "Install the gitcode-cli audit service changes described in docs/bug-close-audit.md"
    ) from None

from .config import ConfigurationError, repository_name, secret


class Platform:
    """Bind an explicit account and repository; all platform requests belong to upstream services."""

    def __init__(self, repository: str, token_env: str):
        if not hasattr(IssueService, "list_all_comments") or not hasattr(IssueService, "remove_label"):
            raise ConfigurationError("gitcode-cli is missing the required audit service methods")
        self.owner, self.name = repository_name(repository).split("/")
        self.client = GitCodeClient(secret(token_env))
        self.repos = RepoService(self.client)
        self.issues = IssueService(self.client)
        self.files = FileService(self.client)
        self.hooks = WebhookService(self.client)

    def close(self):
        self.client.close()

    def repository(self):
        result = self.repos.get(self.owner, self.name)
        if not isinstance(result, dict) or result.get("id") is None:
            raise ConfigurationError("Repository identity is unavailable")
        return result

    def branch(self, branch: str):
        return self.repos.get_branch(self.owner, self.name, branch)

    def file_text(self, path: str, revision: str) -> str:
        result = self.files.get(self.owner, self.name, path, ref=revision)
        if not isinstance(result, dict) or result.get("encoding") != "base64":
            raise ConfigurationError("Expected a Base64 file response")
        content = result.get("content")
        if not isinstance(content, str) or len(content) > 1_500_000:
            raise ConfigurationError("Invalid or oversized repository file")
        return base64.b64decode("".join(content.split()), validate=True).decode("utf-8")

    def issue(self, number: str):
        result = self.issues.get(self.owner, self.name, number)
        if not isinstance(result, dict) or not isinstance(result.get("labels"), list):
            raise ConfigurationError("Current Issue labels are unavailable")
        return result

    def comments(self, number: str) -> list[dict]:
        result = self.issues.list_all_comments(self.owner, self.name, number)
        if not isinstance(result, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("body"), str) for item in result
        ):
            raise ConfigurationError("Issue comments are incomplete or malformed")
        return result

    def remove_resolved(self, number: str):
        return self.issues.remove_label(self.owner, self.name, number, "resolved")

    def create_webhook(self, url: str, password: str, mode: str):
        return self.hooks.create(
            self.owner,
            self.name,
            url=url,
            password=password,
            encryption_type=0 if mode == "password" else 1,
            issues_events=True,
            push_events=False,
            tag_push_events=False,
            note_events=False,
            merge_requests_events=False,
        )
