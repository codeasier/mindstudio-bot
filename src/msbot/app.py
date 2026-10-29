from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool, field_validator

from .config import ConfigurationError, Settings, env_name, repository_name, secret
from .engine import Engine
from .events import EventError, classify
from .platform import Platform
from .store import Conflict, Store, encoded

log = logging.getLogger(__name__)


class Registration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str
    repository: str
    token_env: str
    webhook_secret_env: str
    webhook_mode: Literal["password", "signature"] = "password"
    create_webhook: StrictBool = True

    @field_validator("repository")
    @classmethod
    def valid_repository(cls, value):
        return repository_name(value)

    @field_validator("token_env", "webhook_secret_env")
    @classmethod
    def valid_env(cls, value):
        return env_name(value)


def create_app(settings: Settings, *, platform_factory=Platform, notify=None, start_worker=True) -> FastAPI:
    store = Store(settings.database)
    options = {"notify": notify} if notify is not None else {}
    engine = Engine(settings, store, platform_factory, **options)
    stop = threading.Event()

    def consume():
        while not stop.is_set():
            try:
                if not engine.run_once():
                    stop.wait(0.5)
            except Exception as exc:
                # Never log upstream exception text: transport URLs may contain credentials.
                log.error("Audit worker encountered %s", type(exc).__name__)
                stop.wait(1)

    @asynccontextmanager
    async def lifespan(app):
        lock = None
        worker = None
        try:
            if start_worker:
                lock = os.open(f"{settings.database}.worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise RuntimeError("Another msbot worker is already using this database") from None
            store.recover()
            if start_worker:
                worker = threading.Thread(target=consume, name="msbot-audit-worker", daemon=True)
                worker.start()
            app.state.worker = worker
            yield
        finally:
            stop.set()
            if worker is not None:
                worker.join()
            if lock is not None:
                os.close(lock)

    app = FastAPI(title="MindStudio Bug Issue Close Audit", lifespan=lifespan)
    app.state.store = store
    app.state.engine = engine

    async def administrator(request: Request):
        expected = f"Bearer {secret(settings.admin_token_env)}".encode()
        actual = request.headers.get("authorization", "").encode()
        if not hmac.compare_digest(actual, expected):
            raise HTTPException(401, "Invalid administrator credentials")

    @app.exception_handler(ConfigurationError)
    async def configuration_error(_request, _exception):
        return JSONResponse(status_code=503, content={"ok": False, "error": "configuration_unavailable"})

    @app.exception_handler(Conflict)
    async def conflict_error(_request, exception):
        return JSONResponse(status_code=409, content={"ok": False, "error": str(exception)})

    @app.get("/healthz")
    def health():
        worker = getattr(app.state, "worker", None)
        healthy = not start_worker or (worker is not None and worker.is_alive())
        return JSONResponse(status_code=200 if healthy else 503, content={"ok": healthy})

    @app.post("/hooks/gitcode/{repo_id}", status_code=202)
    async def webhook(repo_id: str, request: Request):
        repo = store.repo_get(repo_id)
        if repo is None:
            raise HTTPException(404, "Unknown repository binding")
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 1_048_576:
                raise HTTPException(413, "Webhook body exceeds 1 MiB")
        raw = bytes(content)
        password = secret(repo["webhook_secret_env"]).encode()
        if repo["webhook_mode"] == "signature":
            expected = "sha256=" + hmac.new(password, raw, hashlib.sha256).hexdigest()
            actual = request.headers.get("x-gitcode-signature-256", "")
        else:
            expected = password.decode()
            actual = request.headers.get("x-gitcode-token", "")
        if not hmac.compare_digest(actual.encode(), expected.encode()):
            raise HTTPException(401, "Invalid Webhook credentials")
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError
            number, reason = classify(payload, request.headers.get("x-gitcode-event", ""), repo)
        except (ValueError, EventError):
            raise HTTPException(400, "Invalid or mismatched GitCode event") from None
        delivery = request.headers.get("x-gitcode-delivery") or payload.get("uuid")
        if (
            not isinstance(delivery, str)
            or not delivery
            or len(delivery) > 200
            or any(ord(c) < 32 for c in delivery)
        ):
            raise HTTPException(400, "Missing or invalid delivery identity")
        digest = hashlib.sha256(encoded(payload).encode()).hexdigest()
        job, duplicate = store.accept(repo_id, delivery, digest, payload, number, reason)
        return {"ok": True, "duplicate": duplicate, "data": job}

    @app.post("/api/repos", dependencies=[Depends(administrator)])
    def register(body: Registration):
        request = body.model_dump(exclude={"operation_id"})
        try:
            operation = engine.register(body.operation_id, request)
        except ValueError as exc:
            if isinstance(exc, Conflict):
                raise
            raise HTTPException(400, "Invalid operation identifier") from None
        return {"ok": True, "operation_id": body.operation_id, "data": operation}

    @app.get("/api/repos", dependencies=[Depends(administrator)])
    def repositories():
        return {"ok": True, "data": [engine.public_repo(repo) for repo in store.repo_list()]}

    @app.get("/api/tasks", dependencies=[Depends(administrator)])
    def tasks():
        return {"ok": True, "data": store.job_list()}

    @app.get("/api/tasks/{identity}", dependencies=[Depends(administrator)])
    def task(identity: str):
        result = store.job_get(identity)
        if result is None:
            raise HTTPException(404, "Unknown audit")
        return {"ok": True, "data": result}

    @app.get("/api/operations/{identity}", dependencies=[Depends(administrator)])
    def operation(identity: str):
        result = store.operation_get(identity)
        if result is None:
            raise HTTPException(404, "Unknown operation")
        result.pop("request_hash", None)
        return {"ok": True, "data": result}

    return app
