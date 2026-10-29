from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path


class Conflict(ValueError):
    pass


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decoded(row):
    if row is None:
        return None
    result = dict(row)
    for key in {"config", "snapshot", "result", "payload"} & result.keys():
        if result[key] is not None:
            result[key] = json.loads(result[key])
    return result


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS repos (
                    id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, project_id TEXT NOT NULL,
                    token_env TEXT NOT NULL, webhook_secret_env TEXT NOT NULL,
                    webhook_mode TEXT NOT NULL, status TEXT NOT NULL, webhook_id TEXT,
                    config TEXT, config_error TEXT
                );
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, status TEXT NOT NULL,
                    result TEXT, error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, repo_id TEXT NOT NULL REFERENCES repos(id),
                    delivery TEXT NOT NULL, event_key TEXT, request_hash TEXT NOT NULL, payload TEXT NOT NULL,
                    issue_number TEXT, status TEXT NOT NULL, reason TEXT,
                    snapshot TEXT, result TEXT, removal TEXT NOT NULL DEFAULT 'none',
                    notification TEXT NOT NULL DEFAULT 'none',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(repo_id, delivery), UNIQUE(repo_id, event_key)
                );
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created_at);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def operation_begin(self, identity: str, request: dict) -> tuple[dict, bool]:
        try:
            if uuid.UUID(identity).version != 4:
                raise ValueError
        except (ValueError, AttributeError) as exc:
            raise ValueError("operation_id must be a UUIDv4") from exc
        digest = hashlib.sha256(encoded(request).encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM operations WHERE id=?", (identity,)).fetchone()
            if row:
                if row["request_hash"] != digest:
                    raise Conflict("operation_id was already used with different parameters")
                return decoded(row), False
            db.execute(
                "INSERT INTO operations(id, request_hash, status) VALUES(?, ?, 'running')", (identity, digest)
            )
        return self.operation_get(identity), True

    def operation_get(self, identity: str):
        with self.connect() as db:
            return decoded(db.execute("SELECT * FROM operations WHERE id=?", (identity,)).fetchone())

    def operation_finish(self, identity: str, status: str, result=None, error=None):
        with self.connect() as db:
            db.execute(
                "UPDATE operations SET status=?, result=?, error=? WHERE id=?",
                (status, encoded(result) if result is not None else None, error, identity),
            )
        return self.operation_get(identity)

    def repo_get(self, identity: str):
        with self.connect() as db:
            return decoded(
                db.execute("SELECT * FROM repos WHERE id=? OR name=?", (identity, identity)).fetchone()
            )

    def repo_list(self):
        with self.connect() as db:
            return [decoded(row) for row in db.execute("SELECT * FROM repos ORDER BY name")]

    def repo_insert(self, entry: dict):
        with self.connect() as db:
            try:
                db.execute(
                    "INSERT INTO repos(id, name, project_id, token_env, webhook_secret_env, "
                    "webhook_mode, status) VALUES(:id, :name, :project_id, :token_env, "
                    ":webhook_secret_env, :webhook_mode, 'registering')",
                    entry,
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("Repository is already registered") from exc

    def repo_update(self, identity: str, **values):
        allowed = {"status", "webhook_id", "config", "config_error"}
        if not values or set(values) - allowed:
            raise ValueError("Invalid repository update")
        if "config" in values and values["config"] is not None:
            values["config"] = encoded(values["config"])
        with self.connect() as db:
            db.execute(
                f"UPDATE repos SET {', '.join(f'{key}=?' for key in values)} WHERE id=?",
                (*values.values(), identity),
            )

    def accept(self, repo_id: str, delivery: str, digest: str, payload: dict, number, reason: str | None):
        event_key = payload.get("uuid")
        if not isinstance(event_key, str) or not event_key or len(event_key) > 200:
            event_key = None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE repo_id=? AND (delivery=? OR event_key=?)",
                (repo_id, delivery, event_key),
            ).fetchone()
            if row:
                if row["request_hash"] != digest:
                    raise Conflict("Delivery identifier was already used with a different body")
                return self.public_job(decoded(row)), True
            identity = str(uuid.uuid4())
            db.execute(
                "INSERT INTO jobs(id, repo_id, delivery, event_key, request_hash, payload, "
                "issue_number, status, reason) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    identity,
                    repo_id,
                    delivery,
                    event_key,
                    digest,
                    encoded(payload),
                    number,
                    "ignored" if reason else "queued",
                    reason,
                ),
            )
        return self.job_get(identity), False

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT jobs.* FROM jobs JOIN repos ON jobs.repo_id=repos.id "
                "WHERE jobs.status='queued' AND repos.status='active' ORDER BY jobs.rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE jobs SET status='running', updated_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],)
            )
            return decoded(row)

    def job_update(self, identity: str, **values):
        allowed = {"status", "reason", "snapshot", "result", "removal", "notification"}
        if not values or set(values) - allowed:
            raise ValueError("Invalid audit update")
        for key in {"snapshot", "result"} & values.keys():
            values[key] = encoded(values[key])
        with self.connect() as db:
            db.execute(
                f"UPDATE jobs SET {', '.join(f'{key}=?' for key in values)}, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (*values.values(), identity),
            )

    @staticmethod
    def public_job(job):
        if job is None:
            return None
        return {
            key: value for key, value in job.items() if key not in {"payload", "snapshot", "request_hash"}
        }

    def job_get(self, identity: str):
        with self.connect() as db:
            return self.public_job(
                decoded(db.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone())
            )

    def job_list(self, limit=50):
        with self.connect() as db:
            return [
                self.public_job(decoded(row))
                for row in db.execute("SELECT * FROM jobs ORDER BY rowid DESC LIMIT ?", (limit,))
            ]

    def recover(self):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status='blocked', reason='interrupted_external_write', "
                "removal=CASE WHEN removal='calling' THEN 'unknown' ELSE removal END, "
                "notification=CASE WHEN notification='sending' THEN 'unknown' ELSE notification END "
                "WHERE status='running' AND (removal='calling' OR notification='sending')"
            )
            db.execute("UPDATE jobs SET status='queued' WHERE status='running'")
            db.execute(
                "UPDATE operations SET status='blocked', "
                "error='interrupted_registration; inspect original operation' "
                "WHERE status='running'"
            )
            db.execute("UPDATE repos SET status='blocked' WHERE status IN ('registering', 'hook_calling')")
