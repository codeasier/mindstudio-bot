from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import yaml


class ConfigurationError(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ConfigurationError("YAML keys must be unique strings")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def load_yaml(text: str) -> dict:
    if len(text.encode()) > 1_048_576:
        raise ConfigurationError("YAML document exceeds 1 MiB")
    try:
        value = yaml.load(text, Loader=UniqueLoader)
    except (yaml.YAMLError, RecursionError) as exc:
        raise ConfigurationError("Invalid YAML document") from exc
    if not isinstance(value, dict):
        raise ConfigurationError("Expected a YAML mapping")
    return value


def mapping(value, allowed: set[str], required: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or set(value) - allowed or required - set(value):
        raise ConfigurationError("Unexpected or missing configuration fields")
    return value


def text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{name} must be a nonempty string")
    return value


def env_name(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ConfigurationError("Expected an environment variable name")
    return value


def secret(name: str) -> str:
    value = os.environ.get(env_name(name))
    if not value or not value.strip():
        raise ConfigurationError(f"Required environment variable is missing: {name}")
    return value


def repository_name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", value, flags=re.ASCII):
        raise ConfigurationError("Repository must be OWNER/REPO")
    if any(part in {".", ".."} for part in value.split("/")):
        raise ConfigurationError("Invalid repository name")
    return value


def template_path(value) -> str:
    value = text(value, "template")
    if (
        not value.startswith(".msbot/")
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or "\\" in value
        or any(ord(c) < 32 for c in value)
        or PurePosixPath(value).suffix not in {".yaml", ".yml"}
    ):
        raise ConfigurationError("Template must be a .msbot/ relative YAML path")
    return value


def gitcode_logins(value, *, name: str, limit: int) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= limit:
        raise ConfigurationError(f"{name} must list between 1 and {limit} GitCode logins")
    seen: list[str] = []
    folded: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", item):
            raise ConfigurationError(f"{name} must list GitCode logins")
        key = item.casefold()
        if key in folded:
            raise ConfigurationError(f"{name} contains a duplicate GitCode login")
        folded.add(key)
        seen.append(key)
    return seen


def audit_configuration(source: str) -> dict:
    root = mapping(load_yaml(source), {"version", "audits"}, {"version", "audits"})
    if type(root["version"]) is not int or root["version"] != 1:
        raise ConfigurationError("Expected version: 1")
    audits = mapping(root["audits"], {"bug_issue_close"}, {"bug_issue_close"})
    audit = mapping(
        audits["bug_issue_close"],
        {"enabled", "template", "notification", "authors"},
        {"enabled"},
    )
    if type(audit["enabled"]) is not bool:
        raise ConfigurationError("enabled must be a boolean")
    result = {"enabled": audit["enabled"]}
    if "template" in audit or audit["enabled"]:
        result["template"] = template_path(audit.get("template"))
    if "notification" in audit:
        result["notification"] = text(audit["notification"], "notification")
    if "authors" in audit:
        result["authors"] = gitcode_logins(audit["authors"], name="authors", limit=100)
    return result


@dataclass(frozen=True)
class FeishuConnection:
    url_env: str
    keywords: tuple[str, ...] = ()
    secret_env: str | None = None
    repositories: tuple[str, ...] = ()

    def url(self) -> str:
        value = secret(self.url_env)
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"open.feishu.cn", "open.larksuite.com", "open.larkoffice.com"}
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or not parsed.path.startswith("/open-apis/bot/v2/hook/")
            or not parsed.path.removeprefix("/open-apis/bot/v2/hook/")
            or parsed.query
            or parsed.fragment
        ):
            raise ConfigurationError("Expected an official HTTPS Feishu custom-bot webhook URL")
        return value


@dataclass(frozen=True)
class Settings:
    database: Path
    public_url: str
    admin_token_env: str = "MSBOT_ADMIN_TOKEN"
    feishu: dict[str, FeishuConnection] = field(default_factory=dict)
    tester_authors: tuple[str, ...] = ()

    @classmethod
    def load(cls, path: str) -> Settings:
        root = mapping(
            load_yaml(Path(path).read_text(encoding="utf-8")),
            {"database", "public_url", "admin_token_env", "feishu", "tester_authors"},
            {"database", "public_url", "tester_authors"},
        )
        public_url = text(root["public_url"], "public_url").rstrip("/")
        parsed = urlsplit(public_url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ConfigurationError("Invalid public_url")
        connections = {}
        entries = root.get("feishu", {})
        if not isinstance(entries, dict):
            raise ConfigurationError("feishu must be a mapping of connection names")
        for name, entry in entries.items():
            text(name, "connection name")
            entry = mapping(
                entry, {"url_env", "keywords", "secret_env", "repositories"}, {"url_env", "repositories"}
            )
            keywords = entry.get("keywords", [])
            if not isinstance(keywords, list) or len(keywords) > 10:
                raise ConfigurationError("keywords must contain at most ten strings")
            repositories = entry["repositories"]
            if not isinstance(repositories, list) or not repositories:
                raise ConfigurationError("Notification connections must list their allowed repositories")
            connection = FeishuConnection(
                env_name(entry["url_env"]),
                tuple(text(k, "keyword") for k in keywords),
                env_name(entry["secret_env"]) if entry.get("secret_env") is not None else None,
                tuple(repository_name(repo) for repo in repositories),
            )
            connection.url()
            if connection.secret_env:
                secret(connection.secret_env)
            connections[name] = connection
        admin_env = env_name(root.get("admin_token_env", "MSBOT_ADMIN_TOKEN"))
        secret(admin_env)
        testers = tuple(gitcode_logins(root["tester_authors"], name="tester_authors", limit=500))
        return cls(
            Path(text(root["database"], "database")).expanduser(),
            public_url,
            admin_env,
            connections,
            testers,
        )
