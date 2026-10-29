from __future__ import annotations

import re
import uuid

from gitcode_cli.errors import APIError

from . import feishu
from .config import ConfigurationError, Settings, audit_configuration, secret, text
from .events import label_names
from .forms import IssueForm
from .platform import Platform
from .store import Conflict, Store


class Engine:
    def __init__(self, settings: Settings, store: Store, platform_factory=Platform, notify=feishu.send):
        self.settings = settings
        self.store = store
        self.platform_factory = platform_factory
        self.notify = notify

    def check_notification_binding(self, repo: dict, bundle: dict):
        name = bundle["audit"].get("notification")
        if name:
            connection = self.settings.feishu.get(name)
            if connection is None or repo["name"] not in connection.repositories:
                raise ConfigurationError("Notification connection is not available to this repository")

    def load_bundle(self, repo: dict, platform) -> dict | None:
        try:
            info = platform.repository()
            default_branch = text(info.get("default_branch"), "default_branch")
            branch = platform.branch(default_branch)
            commit = branch.get("commit") if isinstance(branch, dict) else None
            if not isinstance(commit, dict):
                raise ConfigurationError("Default branch commit is unavailable")
            candidates = {str(commit[key]) for key in ("id", "sha") if commit.get(key) is not None}
            if len(candidates) != 1 or not re.fullmatch(r"[0-9a-fA-F]{40}", next(iter(candidates))):
                raise ConfigurationError("Default branch must resolve to one full commit")
            revision = next(iter(candidates))
            try:
                source = platform.file_text(".msbot/config.yaml", revision)
            except APIError as exc:
                if exc.status_code == 404:
                    self.store.repo_update(repo["id"], config=None, config_error="entry_missing")
                    return None
                raise
            audit = audit_configuration(source)
            bundle = {"revision": revision, "audit": audit, "template": ""}
            if audit["enabled"]:
                self.check_notification_binding(repo, bundle)
                bundle["template"] = platform.file_text(audit["template"], revision)
                IssueForm.parse(bundle["template"])
            self.store.repo_update(repo["id"], config=bundle, config_error=None)
            return bundle
        except Exception as exc:
            self.store.repo_update(repo["id"], config_error=f"config_refresh_failed: {type(exc).__name__}")
            if repo.get("config") is not None:
                previous = repo["config"]
                self.check_notification_binding(repo, previous)
                return previous
            raise ConfigurationError("No valid audit configuration is available") from exc

    def register(self, operation_id: str, request: dict) -> dict:
        operation, fresh = self.store.operation_begin(operation_id, request)
        if not fresh:
            return operation
        platform = None
        repo_id = None
        writing = False
        try:
            old = self.store.repo_get(request["repository"])
            if old is not None and old["status"] != "failed":
                same = all(
                    old[key] == request[key] for key in ("token_env", "webhook_secret_env", "webhook_mode")
                )
                if old["status"] == "active" and same:
                    return self.store.operation_finish(operation_id, "succeeded", self.public_repo(old))
                raise Conflict("Repository has an existing binding; inspect its original operation")
            secret(request["webhook_secret_env"])
            platform = self.platform_factory(request["repository"], request["token_env"])
            info = platform.repository()
            if info.get("id") is None:
                raise ConfigurationError("Missing repository identity")
            repo_id = old["id"] if old else str(uuid.uuid4())
            repo = {
                "id": repo_id,
                "name": request["repository"],
                "project_id": str(info["id"]),
                "token_env": request["token_env"],
                "webhook_secret_env": request["webhook_secret_env"],
                "webhook_mode": request["webhook_mode"],
                "config": old.get("config") if old else None,
            }
            if old is None:
                self.store.repo_insert(repo)
            else:
                if any(
                    old[key] != repo[key]
                    for key in ("project_id", "token_env", "webhook_secret_env", "webhook_mode")
                ):
                    raise Conflict(
                        "Resume a failed binding with the same identity and environment references"
                    )
                self.store.repo_update(repo_id, status="registering")
            self.load_bundle(repo, platform)
            if request["create_webhook"]:
                self.store.repo_update(repo_id, status="hook_calling")
                writing = True
                result = platform.create_webhook(
                    self.webhook_url(repo_id), secret(request["webhook_secret_env"]), request["webhook_mode"]
                )
                if not isinstance(result, dict) or type(result.get("id")) not in {int, str}:
                    raise ConfigurationError("Webhook creation lacks an explicit identifier")
                identity = result["id"]
                if (type(identity) is int and identity <= 0) or not str(identity).strip():
                    raise ConfigurationError("Webhook creation returned an invalid identifier")
                if "result_code" in result and (
                    type(result["result_code"]) is not int or result["result_code"] != 0
                ):
                    raise ConfigurationError("Webhook creation did not confirm success")
                if "url" in result and result["url"] != self.webhook_url(repo_id):
                    raise ConfigurationError("Webhook creation returned a different callback URL")
                if "issues_events" in result and result["issues_events"] not in (True, 1):
                    raise ConfigurationError("Webhook creation did not enable Issue events")
                self.store.repo_update(repo_id, webhook_id=str(result["id"]))
            self.store.repo_update(repo_id, status="active")
            return self.store.operation_finish(
                operation_id, "succeeded", self.public_repo(self.store.repo_get(repo_id))
            )
        except Exception as exc:
            status = "blocked" if writing else "failed"
            if repo_id:
                self.store.repo_update(repo_id, status=status)
            return self.store.operation_finish(
                operation_id, status, error=f"registration_{status}: {type(exc).__name__}"
            )
        finally:
            if platform is not None:
                platform.close()

    def webhook_url(self, repo_id: str) -> str:
        return f"{self.settings.public_url}/hooks/gitcode/{repo_id}"

    def public_repo(self, repo: dict) -> dict:
        return {
            "id": repo["id"],
            "repository": repo["name"],
            "status": repo["status"],
            "webhook_url": self.webhook_url(repo["id"]),
            "webhook_id": repo.get("webhook_id"),
            "webhook_mode": repo["webhook_mode"],
            "config_error": repo.get("config_error"),
            "config_revision": repo["config"]["revision"] if repo.get("config") else None,
            "audit_enabled": bool(repo.get("config") and repo["config"]["audit"]["enabled"]),
        }

    def run_once(self) -> bool:
        job = self.store.claim()
        if job is None:
            return False
        platform = None
        try:
            repo = self.store.repo_get(job["repo_id"])
            platform = self.platform_factory(repo["name"], repo["token_env"])
            bundle = job["snapshot"] or self.load_bundle(repo, platform)
            if not bundle or not bundle["audit"]["enabled"]:
                self.store.job_update(job["id"], status="ignored", reason="audit_disabled")
                return True
            self.store.job_update(job["id"], snapshot=bundle)
            self.check_notification_binding(repo, bundle)
            if job["removal"] == "confirmed":
                self.finish_notification(job, repo, bundle, job["result"])
                return True
            issue = platform.issue(job["issue_number"])
            if not {"bug", "resolved"} <= label_names(issue["labels"]):
                self.store.job_update(job["id"], status="ignored", reason="bug_or_resolved_absent")
                return True
            comments = platform.comments(job["issue_number"])
            form = IssueForm.parse(bundle["template"])
            best = [f"{item.label}：必填项未填写" for item in form.fields if item.required]
            for comment in comments:
                errors = form.validate(comment["body"])
                if not errors:
                    self.store.job_update(
                        job["id"],
                        status="passed",
                        reason="valid_close_registration",
                        result={"valid_comment_id": comment.get("id"), "comments_read": len(comments)},
                    )
                    return True
                if len(errors) < len(best):
                    best = errors
            result = {
                "errors": best,
                "comments_read": len(comments),
                "config_revision": bundle["revision"],
                "template": bundle["audit"]["template"],
                "issue_url": f"https://gitcode.com/{repo['name']}/issues/{job['issue_number']}",
            }
            # Recheck the target immediately before the write; never replace the whole label set.
            current = platform.issue(job["issue_number"])
            if not {"bug", "resolved"} <= label_names(current["labels"]):
                self.store.job_update(job["id"], status="ignored", reason="labels_changed_before_removal")
                return True
            self.store.job_update(job["id"], removal="calling", result=result)
            try:
                platform.remove_resolved(job["issue_number"])
                if "resolved" in label_names(platform.issue(job["issue_number"])["labels"]):
                    raise RuntimeError("Label removal could not be confirmed")
            except Exception as exc:
                self.store.job_update(
                    job["id"],
                    status="blocked",
                    removal="unknown",
                    reason=f"label_removal_unknown: {type(exc).__name__}",
                )
                return True
            self.store.job_update(job["id"], removal="confirmed")
            self.finish_notification(job, repo, bundle, result)
        except Exception as exc:
            self.store.job_update(job["id"], status="failed", reason=f"audit_failed: {type(exc).__name__}")
        finally:
            if platform is not None:
                platform.close()
        return True

    def finish_notification(self, job: dict, repo: dict, bundle: dict, result: dict):
        name = bundle["audit"].get("notification")
        if not name:
            self.store.job_update(job["id"], status="rejected", reason="invalid_close_registration")
            return
        message = "\n".join(
            [
                "Bug Issue 关闭审计未通过，已移除 resolved 标签。",
                f"仓库：{repo['name']}",
                f"Issue：{result['issue_url']}",
                f"模板：{result['template']}",
                "请在同一条评论中补齐关闭登记后重新添加 resolved。",
                "审计原因：",
                *[f"- {error}" for error in result["errors"]],
            ]
        )
        self.store.job_update(job["id"], notification="sending")
        try:
            self.notify(self.settings.feishu[name], message)
        except feishu.NotificationRejected:
            self.store.job_update(
                job["id"],
                status="rejected",
                notification="failed",
                reason="label_removed; notification_rejected",
            )
        except Exception:
            self.store.job_update(
                job["id"],
                status="blocked",
                notification="unknown",
                reason="label_removed; notification_unknown",
            )
        else:
            self.store.job_update(
                job["id"], status="rejected", notification="sent", reason="invalid_close_registration"
            )
