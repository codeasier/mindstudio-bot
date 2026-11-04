from __future__ import annotations

import logging
import os
import re
import uuid

from gitcode_cli.errors import APIError

from . import feishu
from .config import ConfigurationError, Settings, audit_configuration, secret, text
from .diagnostics import error_details
from .events import issue_author_login, label_names
from .forms import IssueForm
from .platform import Platform
from .store import Conflict, Store

log = logging.getLogger(__name__)


def task_matches_repository(job: dict, repo: dict) -> bool:
    """A task may run only against the repository row that accepted its own project identity."""
    payload = job.get("payload")
    project = payload.get("project") if isinstance(payload, dict) else None
    if not isinstance(project, dict) or project.get("path_with_namespace") != repo["name"]:
        return False
    return str(project.get("id")) == repo["project_id"]


class Engine:
    def __init__(self, settings: Settings, store: Store, platform_factory=Platform, notify=feishu.send):
        self.settings = settings
        self.store = store
        self.platform_factory = platform_factory
        self.notify = notify

    def author_allowlisted(self, audit: dict, login: str) -> bool:
        """Audit testers only. A repository list narrows that set by intersection."""
        folded = login.casefold()
        if folded not in self.settings.tester_authors:
            return False
        authors = audit.get("authors")
        return authors is None or folded in authors

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

    def confirm_repository(self, repo: dict) -> bool:
        """Verify a pending unified binding. Reads the GitCode project id and never writes."""
        if repo["status"] != "pending":
            return True
        platform = None
        info = None
        try:
            platform = self.platform_factory(repo["name"], repo["token_env"])
            info = platform.repository()
        except Exception:
            return False
        finally:
            if platform is not None:
                platform.close()
        if not isinstance(info, dict) or str(info.get("id")) != repo["project_id"]:
            return False
        if not self.store.activate_pending(repo["id"]):
            return False
        repo["status"] = "active"
        return True

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
        repo = None
        bundle = None
        try:
            repo = self.store.repo_get(job["repo_id"])
            if not task_matches_repository(job, repo):
                self.store.job_update(job["id"], status="ignored", reason="repository_unverified")
                return True
            if repo["status"] == "pending" and not self.confirm_repository(repo):
                self.store.job_update(job["id"], status="ignored", reason="repository_unverified")
                return True
            if job["reason"]:
                self.store.job_update(job["id"], status="ignored")
                return True
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
            login = issue_author_login(issue)
            if login is None:
                self.fail_audit(
                    job, repo, bundle, ConfigurationError("Author unavailable"), reason="author_unavailable"
                )
                return True
            if not self.author_allowlisted(bundle["audit"], login):
                self.store.job_update(job["id"], status="ignored", reason="author_not_allowlisted")
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
                self.store.job_update(job["id"], status="blocked", removal="unknown")
                self.fail_audit(job, repo, bundle, exc, reason=f"label_removal_unknown: {type(exc).__name__}")
                return True
            self.store.job_update(job["id"], removal="confirmed")
            self.finish_notification(job, repo, bundle, result)
        except Exception as exc:
            self.fail_audit(job, repo, bundle, exc)
        finally:
            if platform is not None:
                platform.close()
        return True

    def fail_audit(self, job: dict, repo: dict | None, bundle: dict | None, exc: Exception, *, reason=None):
        envs = {self.settings.admin_token_env}
        if repo:
            envs.update((repo["token_env"], repo["webhook_secret_env"]))
        for connection in self.settings.feishu.values():
            envs.add(connection.url_env)
            if connection.secret_env:
                envs.add(connection.secret_env)
        details = error_details(exc, secrets=tuple(os.environ.get(name, "") for name in envs))
        current = self.store.job_get(job["id"])
        # Unexpected exceptions after an external call must never become manually retryable.
        status = "failed" if current["removal"] == current["notification"] == "none" else "blocked"
        bundle = bundle or (repo.get("config") if repo else None)
        name = None
        if repo and bundle and current["failure_notification"] == "none":
            try:
                self.check_notification_binding(repo, bundle)
                name = bundle["audit"].get("notification")
            except ConfigurationError:
                pass
        # Persist the decision and sending marker atomically. A crash is then
        # visible as unknown on recovery, never silently none or auto-replayed.
        self.store.job_update(
            job["id"],
            status=status,
            reason=reason or f"audit_failed: {type(exc).__name__}",
            result=(current.get("result") or {}) | {"error": details},
            failure_notification="sending" if name else current["failure_notification"],
        )
        log.error("Audit %s %s: %s", job["id"], status, details)
        if name is None:
            return
        message = "\n".join(
            [
                "Bug Issue 关闭审计执行失败，需要人工检查。",
                f"仓库：{repo['name']}",
                f"Issue：https://gitcode.com/{repo['name']}/issues/{job['issue_number']}",
                f"任务：{job['id']}",
                f"错误：{details['message']}（HTTP {details.get('status_code')}）",
                f"标签操作状态：{current['removal']}。未确认审计通过，请检查任务后决定是否重新审计。",
            ]
        )
        try:
            self.notify(self.settings.feishu[name], message)
        except feishu.NotificationRejected:
            self.store.job_update(job["id"], failure_notification="failed")
        except Exception:
            self.store.job_update(job["id"], failure_notification="unknown")
        else:
            self.store.job_update(job["id"], failure_notification="sent")

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
