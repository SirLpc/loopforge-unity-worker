"""Unity worker HTTP entry point. The MVP routes share the persistent lease."""

import json
import secrets
from contextlib import ExitStack, asynccontextmanager
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from artifacts import artifact_bytes, log_page, safe_child, task_directory
from development import DevelopmentWorker, RequestConflict, SlotNotFound, TaskNotFound
from process_runner import reserve_project
from pull_requests import HostedPullRequests
from worker_config import REMOTE_PATTERN, Settings, legacy_service, load_service_settings
from workspace_storage import CacheName


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000)]
Criterion = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)]
Action = Literal["prepare", "implement", "compile", "test", "review", "fix", "run", "cancel", "release", "pr"]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SmokeRequest(Body):
    job_id: UUID


class OperationRequest(Body):
    request_id: UUID


class StageRequest(OperationRequest):
    queue_wait_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)] | None = None


class TestScope(Body):
    assembly: str
    fixtures: list[str]


class TaskRequest(OperationRequest):
    pool_id: str | None = Field(default=None, pattern=rf"^{REMOTE_PATTERN}$")
    workflow_run_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)] | None = None
    requirement: Text
    plan: Text
    acceptance_criteria: list[Criterion] = Field(min_length=1, max_length=50)
    project_rules: str = Field(default="", max_length=20_000)
    test_selection: dict[str, list[TestScope]] | None = None


class PlanningTaskRequest(OperationRequest):
    pool_id: str | None = Field(default=None, pattern=rf"^{REMOTE_PATTERN}$")
    workflow_run_id: str = Field(pattern=r"^run-[A-Za-z0-9_-]{1,120}$")
    requirement: Text
    planning_profile: Literal["feature", "ui", "issue-fix"]


class PlanRequest(StageRequest):
    feedback: str = Field(default="", max_length=4000)


class AcceptPlanRequest(OperationRequest):
    slot_id: str = Field(pattern=rf"^{REMOTE_PATTERN}$")
    base_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class RevisionRequest(OperationRequest):
    revision: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class ApprovalRequest(RevisionRequest):
    approver: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    commit_message: str = Field(default="", max_length=2000)
    create_pr: bool = False


class RecoveryRequest(OperationRequest):
    confirmed_stopped: Literal[True]
    note: Criterion


def create_app(settings=None, *, runner=None, agent=None, pr_adapter=None):
    cfg = legacy_service(settings) if isinstance(settings, Settings) else settings or load_service_settings()
    pr_adapter = pr_adapter or HostedPullRequests.from_env()

    def authorize(authorization: str = Header(default="")):
        header = authorization.encode()
        if secrets.compare_digest(header, ("Bearer " + cfg.legacy.token).encode()):
            return "worker"
        if cfg.legacy.approval_token and secrets.compare_digest(header, ("Bearer " + cfg.legacy.approval_token).encode()):
            return "operator"
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})

    def operator(role: str = Depends(authorize)):
        if role != "operator":
            raise HTTPException(403, "This operation requires the separate operator credential")

    @asynccontextmanager
    async def lifespan(app):
        with ExitStack() as locks:
            locks.enter_context(reserve_project(cfg.logs))
            unavailable = {}
            for slot in cfg.slots.values():
                try:
                    checkout = slot.settings.checkout
                    if not checkout.is_dir() or checkout.resolve(strict=True) != checkout:
                        unavailable[slot.id] = "workspace_missing"
                        continue
                    locks.enter_context(reserve_project(checkout))
                except (OSError, RuntimeError):
                    if cfg.implicit:
                        raise
                    unavailable[slot.id] = "workspace_locked"
            worker = DevelopmentWorker(cfg, runner, agent, pr_adapter, unavailable=unavailable)
            app.state.worker = worker
            worker.start()
            try:
                yield
            finally:
                worker.close()

    app = FastAPI(title="Unity Worker", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan, dependencies=[Depends(authorize)])

    def worker():
        return app.state.worker

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc):
        return JSONResponse(status_code=422, content={"detail": "Invalid request fields or route parameters"})

    @app.exception_handler(RequestConflict)
    async def conflict(request: Request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(TaskNotFound)
    async def not_found(request: Request, exc):
        return JSONResponse(status_code=404, content={"detail": "Unknown task or job"})

    @app.exception_handler(SlotNotFound)
    async def slot_not_found(request: Request, exc):
        return JSONResponse(status_code=404, content={"detail": "Unknown slot"})

    def payload_for(body):
        payload = body.model_dump(mode="json", exclude={"request_id"})
        serialized = json.dumps(payload, ensure_ascii=False)
        if any(value in serialized for value in cfg.secrets):
            raise HTTPException(422, "Remove credentials from the request")
        return payload

    def artifact_path(task, name, kind=None):
        if not any(entry["name"] == name and (kind is None or entry["kind"] == kind)
                   for entry in task["artifacts"]):
            raise HTTPException(404, "Unknown artifact")
        try:
            return safe_child(task_directory(cfg.logs, task["task_id"]), name)
        except ValueError as error:
            raise HTTPException(404, "Unknown artifact") from error

    @app.get("/health")
    def health():
        return {"ok": True, "busy_or_blocked": worker().worker(legacy=True)["status"] != "idle",
                "library_exists": (cfg.legacy.project / "Library").is_dir(), "operation": "unity_startup_import_smoke"}

    def smoke_result(job):
        if job["action"] != "smoke":
            raise TaskNotFound
        status = {"recovery_required": "blocked", "cancelled": "failed"}.get(job["status"], job["status"])
        result = {"job_id": job["job_id"], "status": status}
        if status != "running":
            result.update(exit_code=job["exit_code"], duration_seconds=job.get("duration_seconds"))
            if job["error_message"]:
                result["error"] = job["error_message"]
        return result

    @app.post("/jobs", status_code=202)
    def submit_smoke(request: SmokeRequest):
        return smoke_result(worker().smoke(str(request.job_id)))

    @app.get("/jobs/{job_id}")
    def smoke_status(job_id: UUID):
        result = smoke_result(worker().job(str(job_id)))
        try:
            path = safe_child(cfg.logs, f"{job_id}.log")
        except ValueError as error:
            raise HTTPException(404, "Unknown log") from error
        result["log_tail"] = log_page(path, 0, 8192, cfg.secrets, live=result["status"] == "running", tail=True)["text"]
        return result

    @app.get("/api/v1/workers")
    def list_workers():
        return {"workers": [worker().worker()]}

    @app.get("/api/v1/pools")
    def list_pools():
        return {"pools": worker().pools()}

    @app.get("/api/v1/storage", dependencies=[Depends(operator)])
    def storage_overview():
        return worker().storage_overview()

    @app.get("/api/v1/storage/state", dependencies=[Depends(operator)])
    def storage_state():
        return worker().storage_measure()

    @app.get("/api/v1/storage/slots/{slot_id}", dependencies=[Depends(operator)])
    def storage_slot(slot_id: str, cache: CacheName | None = None):
        return worker().storage_measure(slot_id, cache)

    @app.post("/api/v1/tasks", status_code=202)
    def create_task(request: TaskRequest):
        payload = payload_for(request)
        if payload.get("pool_id") in (None, cfg.default_pool):
            payload.pop("pool_id", None)
        if payload.get("test_selection") is None:
            payload.pop("test_selection", None)
        try:
            return worker().create(str(request.request_id), payload)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/v1/planning-tasks", status_code=202)
    def create_planning_task(request: PlanningTaskRequest):
        payload = payload_for(request)
        if payload["pool_id"] in (None, cfg.default_pool):
            del payload["pool_id"]
        try:
            return worker().create(str(request.request_id), payload)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/v1/tasks/{task_id}")
    def get_task(task_id: UUID):
        task = worker().task(str(task_id))
        return {key: value for key, value in task.items() if key != "remote_identity"}

    @app.get("/api/v1/jobs/{job_id}")
    def get_job(job_id: UUID):
        return worker().job(str(job_id))

    @app.get("/api/v1/tasks/{task_id}/artifacts")
    def list_artifacts(task_id: UUID):
        task = worker().task(str(task_id))
        return {"artifacts": [{**entry, "path": f"/api/v1/tasks/{task_id}/artifacts/{entry['name']}"}
                              for entry in task["artifacts"]]}

    @app.get("/api/v1/tasks/{task_id}/artifacts/{name}")
    def get_artifact(task_id: UUID, name: str):
        task = worker().task(str(task_id))
        path = artifact_path(task, name)
        entry = next(entry for entry in task["artifacts"] if entry["name"] == name)
        if worker().job(entry["job_id"])["status"] == "running":
            raise HTTPException(409, "Use the log cursor while the originating job is running")
        if not path.is_file():
            raise HTTPException(404, "The job did not create this artifact")
        return StreamingResponse(artifact_bytes(path, cfg.secrets), media_type="application/octet-stream",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})

    @app.get("/api/v1/tasks/{task_id}/logs")
    def get_logs(task_id: UUID, name: str | None = None, offset: int = Query(0, ge=0),
                 limit: int = Query(8192, ge=4, le=65_536)):
        task = worker().task(str(task_id))
        if not name:
            names = [entry["name"] for entry in task["artifacts"] if entry["kind"] == "log"]
            name = next((candidate for candidate in reversed(names)
                         if artifact_path(task, candidate, "log").is_file()
                         and artifact_path(task, candidate, "log").stat().st_size), None)
            if not name:
                return {"name": None, "offset": offset, "next_offset": offset, "text": "", "eof": not task["lease"]}
        path = artifact_path(task, name, "log")
        entry = next(entry for entry in task["artifacts"] if entry["name"] == name)
        live = worker().job(entry["job_id"])["status"] == "running"
        return {"name": name, **log_page(path, offset, limit, cfg.secrets, live=live)}

    @app.post("/api/v1/tasks/{task_id}/approve", status_code=202, dependencies=[Depends(operator)])
    def approve(task_id: UUID, request: ApprovalRequest):
        return worker().action(str(task_id), "approve", str(request.request_id), payload_for(request))

    @app.post("/api/v1/tasks/{task_id}/recover", status_code=202, dependencies=[Depends(operator)])
    def recover(task_id: UUID, request: RecoveryRequest):
        return worker().action(str(task_id), "recover", str(request.request_id), payload_for(request))

    @app.post("/api/v1/tasks/{task_id}/commit", status_code=202)
    def commit(task_id: UUID, request: RevisionRequest):
        return worker().action(str(task_id), "commit", str(request.request_id), payload_for(request))

    @app.post("/api/v1/tasks/{task_id}/plan", status_code=202)
    def plan(task_id: UUID, request: PlanRequest):
        return worker().action(str(task_id), "plan", str(request.request_id), payload_for(request))

    @app.post("/api/v1/tasks/{task_id}/accept-plan", status_code=202)
    def accept_plan(task_id: UUID, request: AcceptPlanRequest):
        return worker().action(str(task_id), "accept-plan", str(request.request_id), payload_for(request))

    @app.post("/api/v1/tasks/{task_id}/{action}", status_code=202)
    def operation(task_id: UUID, action: Action, request: StageRequest):
        payload = {}
        if request.queue_wait_seconds is not None:
            if action not in {"run", "prepare"}:
                raise HTTPException(422, "Queue wait is only allowed for run or prepare")
            payload["queue_wait_seconds"] = request.queue_wait_seconds
        return worker().action(str(task_id), action, str(request.request_id), payload)

    return app
