"""Unity Worker SOP commands. Uses Python's standard library in the Linux sandbox."""

import argparse
import hashlib
import hmac
import json
import os
import re
import signal
import socket
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5


class ClientError(Exception):
    pass


class Interrupted(ClientError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, url, token):
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or not token):
            raise ClientError("Set a Worker HTTP(S) URL and token using the environment")
        self.url, self.token = url.rstrip("/"), token
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, path, payload=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(self.url + path, data=body, headers={
            "Authorization": "Bearer " + self.token, "Accept": "application/json", "Content-Type": "application/json",
        })
        for attempt in range(3):
            try:
                with self.opener.open(request, timeout=15) as response:
                    return json.load(response)
            except HTTPError as error:
                if error.code not in (502, 503, 504) or attempt == 2:
                    raise ClientError(f"Worker {path.split('?')[0]} returned HTTP {error.code}") from None
            except (URLError, OSError) as error:
                if attempt == 2:
                    raise ClientError("Worker connection failed. Check the saved task/job before retrying.") from error
            time.sleep(1 + attempt)


def operation_id(scope, action, attempt):
    return str(uuid5(NAMESPACE_URL, f"unity-worker:{scope}:{action}:{attempt}"))


def read_json(path):
    with path.open(encoding="utf-8-sig") as source:
        result = json.load(source)
    if not isinstance(result, dict):
        raise ClientError("Expected a JSON object")
    return result


def artifact_paths(name):
    path = Path(name)
    if not path.is_file() and not path.is_absolute() and ("*" in name or "?" in name):
        return [item for item in Path.cwd().glob(name) if item.is_file()]
    return [path]


def input_json(name, *, plan_request_id=None):
    files = artifact_paths(name)
    if plan_request_id is None:
        if len(files) != 1:
            raise ClientError(f"Expected one artifact matching {name}. Found {len(files)}.")
        return read_json(files[0])

    matches = [artifact for artifact in map(read_json, files) if artifact.get("plan_request_id") == plan_request_id]
    versions = {(artifact["task_id"], artifact["stage_result"]["revision"]) for artifact in matches}
    if len(versions) != 1:
        raise ClientError(f"Expected one Worker task and revision for the approved plan. Found {len(versions)}.")
    return matches[0]


def save_json(path, data):
    destination = Path(path)
    scratch = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        scratch.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        scratch.replace(destination)
    finally:
        scratch.unlink(missing_ok=True)


def handoff(task, job, plan_request_id=None):
    task_id = task["task_id"]
    summary = {key: task.get(key) for key in (
        "status", "current_stage", "revision", "retry_count", "failure_kind", "error_message", "delivery", "lease",
    )}
    if job:
        summary.update(job_status=job["status"], job_error=job.get("error_message"))
    result = {
        "task_id": task_id, "job_id": job.get("job_id") or task.get("job_id"),
        "pool_id": task.get("pool_id"), "slot_id": task.get("slot_id"), "base_commit": task.get("base_commit"),
        "stage_result": summary, "changed_files": task.get("changed_files", []),
        "review_result": task.get("review_result"),
        "artifact_reference": [f"/api/v1/tasks/{task_id}/artifacts/{entry['name']}"
                               for entry in task.get("artifacts", []) if entry["kind"] != "log"],
    }
    if plan_request_id:
        result["plan_request_id"] = plan_request_id
    return result


def check_assignment(task, previous):
    if any(previous.get(key) is not None and previous[key] != task.get(key) for key in ("pool_id", "slot_id")):
        raise ClientError("The upstream artifact references a different Worker pool or directory")


def planning_artifact(task):
    result = task.get("planning") or {}
    assignment = {key: task.get(key) for key in ("task_id", "pool_id", "slot_id", "base_commit", "workflow_run_id")}
    digest = result.get("plan_digest", "")
    if (not all(isinstance(value, str) and value for value in assignment.values())
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", assignment["base_commit"])
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise ClientError("The Worker has no complete source-grounded plan for this assignment")
    return {
        **assignment, **{key: result[key] for key in ("plan_digest", "plan", "requirement_summary",
                                                      "acceptance_criteria", "test_selection", "source_evidence", "baseline")},
        "request_id": operation_id(assignment["task_id"], "plan-revision", digest),
        "artifact_reference": handoff(task, {})["artifact_reference"],
    }


def check_planning_assignment(task, plan):
    current = planning_artifact(task)
    if any(plan.get(key) != current[key] for key in current if key != "artifact_reference"):
        raise ClientError("The approved plan differs from this Worker's task, slot, Git baseline or plan revision")


def read_planning_context(path, operation, secret, sandbox, started):
    if path.is_symlink() or path.stat().st_size > 1_048_576:
        raise ClientError("Invalid local Unity planning context")
    envelope = read_json(path)
    payload = envelope["payload"]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    expected = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(envelope["signature"], expected):
        raise ClientError("Invalid local Unity planning context signature")
    context = payload["sandboxes"][sandbox]
    issued, expires = payload["issued_at"], payload["expires_at"]
    if not started <= issued <= time.time() <= expires <= issued + 10 or context["operation"] != operation:
        raise ClientError("Waiting for the current local Unity planning stage")
    return context


def planning_context(operation):
    secret = os.environ.get("UNITY_PLANNING_CONTEXT_TOKEN", "")
    sandbox = socket.gethostname()
    if len(secret) < 32 or not re.fullmatch(r"[a-f0-9]{12}", sandbox):
        raise ClientError("Set the local planning signing credential and run the script in its Runner container")
    path = Path(".unity-worker-context.json")
    started = time.time()
    until = time.monotonic() + 45
    while time.monotonic() < until:
        try:
            return read_planning_context(path, operation, secret, sandbox, started)
        except (OSError, KeyError, TypeError, ValueError, ClientError):
            time.sleep(1)
    raise ClientError("Timed out waiting for a signed local Unity planning context for this stage")


def run_planning(client, args):
    context = planning_context("plan")
    run_id = context["workflow_run_id"]
    if context["planning_profile"] != args.planning_profile:
        raise ClientError("The LoopForge run does not use the configured planning profile")
    created = client.request("/api/v1/planning-tasks", {
        "request_id": operation_id(run_id, "planning-task", 0), "pool_id": args.pool, "workflow_run_id": run_id,
        "requirement": context["requirement"], "planning_profile": args.planning_profile,
    })
    task_id = str(UUID(created["task_id"]))
    path = f"/api/v1/tasks/{task_id}"
    planning_id = operation_id(task_id, "plan", f"{context['rework']}:{context['attempt']}")
    payload = {"request_id": planning_id, "feedback": context["feedback"]}
    if args.queue_wait_seconds is not None:
        payload["queue_wait_seconds"] = args.queue_wait_seconds
    task = client.request(path)
    job = None
    if task.get("job_id") and task.get("planning_feedback") == payload["feedback"]:
        previous = client.request(f"/api/v1/jobs/{UUID(task['job_id'])}")
        if previous.get("action") == "plan" and previous.get("status") in {"queued", "running"}:
            job = previous
    print(f"Unity Worker 规划任务 {task_id}。", flush=True)
    try:
        if job is None:
            job = client.request(path + "/plan", payload)
        task, job = wait_job(client, path, job["job_id"], args.wait_seconds, args.follow,
                             queue_wait_seconds=args.queue_wait_seconds)
    except (Interrupted, KeyboardInterrupt):
        try:
            client.request(path + "/cancel", {"request_id": operation_id(task_id, "cancel", planning_id)})
        except ClientError as error:
            print(str(error), file=sys.stderr, flush=True)
        print(f"规划节点已停止，请核对任务 {task_id} 的 Windows 进程状态后释放。", file=sys.stderr, flush=True)
        raise
    if job["status"] != "succeeded":
        raise ClientError(f"Unity 规划任务 {task_id} 未完成 ({job.get('failure_kind') or job['status']})")
    result = planning_artifact(task)
    if (result["workflow_run_id"], result["pool_id"]) != (run_id, args.pool):
        raise ClientError("The planning result does not belong to the current run and pool")
    save_json(args.output, result)
    print(f"Unity 方案 {result['plan_digest']}，任务 {task_id}，等待审批。", flush=True)
    return 0


def accept_planning(client, args):
    decision = planning_context("approve")
    plan = None
    for path in artifact_paths(args.input):
        if path.is_symlink() or path.stat().st_size > 1_048_576:
            continue
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() == decision["plan_sha256"]:
            plan = json.loads(raw)
            break
    if not isinstance(plan, dict):
        raise ClientError("The mounted plan is not the artifact approved in LoopForge")
    if plan.get("workflow_run_id") != decision["workflow_run_id"]:
        raise ClientError("The approved artifact belongs to another LoopForge run")
    task_path = f"/api/v1/tasks/{UUID(plan['task_id'])}"
    check_planning_assignment(client.request(task_path), plan)
    result = client.request(task_path + "/accept-plan", {
        "request_id": operation_id(plan["request_id"], "accept-plan", 0),
        **{key: plan[key] for key in ("slot_id", "base_commit", "plan_digest")},
    })
    if result["status"] != "succeeded":
        raise ClientError("The Worker has not accepted the approved plan")
    save_json(args.output, plan)
    print(f"Worker 任务 {plan['task_id']} 已接收经审批的方案 {plan['plan_digest']}。", flush=True)
    return 0


def check_delivery(task, previous):
    check_assignment(task, previous)
    if not task["revision"] or task["revision"] != task["approved_revision"]:
        raise ClientError("The Worker has no approval for the current reviewed revision")
    if previous and previous.get("stage_result", {}).get("revision") != task["revision"]:
        raise ClientError("The upstream artifact references a different revision")


def wait_job(client, task_path, job_id, wait_seconds, follow, *, queue_wait_seconds=None):
    queue_deadline = time.monotonic() + (queue_wait_seconds or wait_seconds) + 3
    execution_deadline = None
    seen = {}
    previous_stage = None
    while True:
        job = client.request(f"/api/v1/jobs/{job_id}")
        task = client.request(task_path)
        status = job["status"]
        stage = "queued" if status == "queued" else task["current_stage"]
        if stage != previous_stage:
            pool = f" pool={task['pool_id']}" if task.get("pool_id") else ""
            slot = f" slot={task['slot_id']}" if task.get("slot_id") else ""
            print(f"Unity Worker {task['task_id']} {stage}{pool}{slot}", flush=True)
            previous_stage = stage
        if follow:
            for artifact in task["artifacts"]:
                if artifact["kind"] == "log" and artifact["job_id"] == job_id:
                    name = artifact["name"]
                    query = urlencode({"name": name, "offset": seen.get(name, 0)})
                    page = client.request(task_path + "/logs?" + query)
                    if page["text"]:
                        print(page["text"], end="", flush=True)
                    seen[name] = page["next_offset"]
        if status not in {"queued", "running"}:
            return task, job
        current = time.monotonic()
        if status == "running" and execution_deadline is None:
            execution_deadline = current + wait_seconds
        remaining = (execution_deadline if status == "running" else queue_deadline) - current
        if remaining <= 0:
            raise Interrupted(f"Polling timed out for Worker job {job_id}")
        time.sleep(min(3, remaining))


# Legacy CLI entry points.
def run_action(client, args):
    action = args.action
    if action == "health":
        print(json.dumps(client.request("/health")), flush=True)
        return 0
    if action == "plan":
        return run_planning(client, args)
    if action == "accept-planning":
        return accept_planning(client, args)
    if args.from_artifact and args.task_id:
        raise ClientError("Select either --from-artifact or --task-id")
    plan_request_id = None
    if args.approved_plan:
        plan_request_id = str(UUID(input_json(args.approved_plan)["request_id"]))
    previous = input_json(args.from_artifact, plan_request_id=plan_request_id) if args.from_artifact else {}
    task_id = args.task_id or previous.get("task_id")
    planned = None
    if action in ("create", "develop"):
        if task_id:
            raise ClientError("Task creation takes an approved plan, with no existing task ID")
        plan = input_json(args.input)
        run_id = args.run_id or plan.get("workflow_run_id") or os.environ.get("AGENTFLOW_RUN_ID")
        creation_id = plan.get("request_id")
        if not creation_id and not run_id:
            raise ClientError("Approved plan must supply request_id or workflow_run_id")
        plan_request_id = str(UUID(creation_id)) if creation_id else operation_id(run_id, "create", 0)
        if action == "develop" and "task_id" in plan:
            task_id, planned = plan["task_id"], plan
        else:
            payload = {key: plan[key] for key in ("requirement", "plan", "acceptance_criteria")}
            payload.update(workflow_run_id=run_id, project_rules=plan.get("project_rules", ""), request_id=plan_request_id)
            if args.pool:
                payload["pool_id"] = args.pool
            if "test_selection" in plan:
                payload["test_selection"] = plan["test_selection"]
            created = client.request("/api/v1/tasks", payload)
            task_id = created["task_id"]
        operation = None if action == "create" else "run"
    else:
        operation = "commit" if action == "deliver" else action
    if not task_id:
        raise ClientError("Supply --task-id or --from-artifact")
    task_path = f"/api/v1/tasks/{UUID(task_id)}"
    task = client.request(task_path)
    if planned is not None:
        check_planning_assignment(task, planned)
        if task.get("approved_plan_digest") != planned["plan_digest"]:
            raise ClientError("The Worker has not accepted the frozen LoopForge plan revision")
        if args.pool and planned["pool_id"] != args.pool:
            raise ClientError("The approved plan belongs to a different configured pool")
    if operation in (None, "status", "failure"):
        result = handoff(task, {}, plan_request_id)
        save_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    if operation == "commit":
        check_delivery(task, previous)
    elif operation == "release":
        check_assignment(task, previous)
    payload = {"request_id": operation_id(task_id, operation, args.attempt)}
    if operation in {"run", "prepare"} and args.queue_wait_seconds is not None:
        payload["queue_wait_seconds"] = args.queue_wait_seconds
    if operation == "commit":
        payload["revision"] = task["revision"]
    if operation == "approve":
        if not args.approver or not args.revision:
            raise ClientError("Approve requires --approver and the reviewed --revision")
        payload.update(approver=args.approver, revision=args.revision, create_pr=args.create_pr,
                       commit_message=args.commit_message or "")
    if operation == "recover":
        if not args.confirm_stopped or not args.note:
            raise ClientError("Recovery requires --confirm-stopped and an operator --note")
        payload.update(confirmed_stopped=True, note=args.note)
    job = None
    try:
        if action in ("develop", "run", "prepare", "deliver", "release") and args.attempt == 0 and task.get("job_id"):
            previous_job = client.request(f"/api/v1/jobs/{UUID(task['job_id'])}")
            if previous_job.get("action") == operation:
                if (previous_job["status"] == "failed" and previous_job.get("failure_kind") != "queue_timeout"
                        and action in {"develop", "deliver", "release"}):
                    payload["request_id"] = operation_id(task_id, operation, previous_job["job_id"])
                else:
                    job = previous_job
        if job is None:
            job = client.request(task_path + "/" + operation, payload)
        save_json(args.output, handoff(task, job, plan_request_id))
        print(f"Unity Worker task {task_id}, job {job['job_id']}", flush=True)
        task, job = wait_job(client, task_path, job["job_id"], args.wait_seconds, args.follow,
                             queue_wait_seconds=args.queue_wait_seconds)
    except (Interrupted, KeyboardInterrupt):
        print(f"Cancellation requested for Worker task {task_id}. Verify Windows process status.", file=sys.stderr)
        if operation not in ("cancel", "approve", "recover"):
            try:
                attempt_id = job["job_id"] if job else payload["request_id"]
                client.request(task_path + "/cancel", {"request_id": operation_id(task_id, "cancel", attempt_id)})
            except ClientError as error:
                print(str(error), file=sys.stderr)
        raise ClientError("Node stopped. Check the Worker task and job before releasing the lease.") from None
    result = handoff(task, job, plan_request_id)
    save_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if job["status"] == "succeeded" else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "render":
        if len(argv) < 2 or argv[1] == "render":
            raise SystemExit("Usage: worker_client.py render ACTION [arguments]")
        header = "SOP_ARGS = " + repr(argv[1:]) + "\n"
        source = Path(__file__).read_bytes()
        if argv[1].startswith("feature-"):
            common = source.split(b"# Legacy CLI entry points.", 1)[0]
            feature = Path(__file__).with_name("feature_client.py").read_bytes()
            source = common + feature + b"\nraise SystemExit(feature_main(SOP_ARGS))\n"
        sys.stdout.buffer.write(header.encode("utf-8") + source)
        return 0
    if argv and argv[0].startswith("feature-"):
        if __package__:
            from .feature_client import feature_main
        else:
            from feature_client import feature_main
        return feature_main(argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("health", "plan", "accept-planning", "create", "develop", "prepare", "implement", "compile", "test",
                                          "review", "fix", "run", "status", "failure", "approve", "deliver",
                                          "cancel", "release", "recover", "pr"))
    parser.add_argument("--input", default="unity-plan.json")
    parser.add_argument("--from-artifact")
    parser.add_argument("--approved-plan")
    parser.add_argument("--output", default="unity-task.json")
    parser.add_argument("--task-id")
    parser.add_argument("--run-id")
    parser.add_argument("--pool", help="Configured pool ID for plan, create or develop")
    parser.add_argument("--planning-profile", choices=("feature", "ui", "issue-fix"))
    parser.add_argument("--queue-wait-seconds", type=int, help="Time allowed to wait for a workspace")
    parser.add_argument("--attempt", type=int, default=0)
    parser.add_argument("--wait-seconds", type=int, default=int(os.environ.get("UNITY_WORKER_WAIT_SECONDS", "14400")))
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--approver")
    parser.add_argument("--revision")
    parser.add_argument("--create-pr", action="store_true")
    parser.add_argument("--commit-message")
    parser.add_argument("--confirm-stopped", action="store_true")
    parser.add_argument("--note")
    args = parser.parse_args(argv)
    if args.attempt < 0 or args.wait_seconds <= 0:
        parser.error("Select a non-negative attempt and positive wait time")
    if args.pool is not None and args.action not in {"plan", "create", "develop"}:
        parser.error("--pool is only available for plan, create or develop")
    if args.action == "plan" and (not args.pool or not args.planning_profile):
        parser.error("plan requires --pool and --planning-profile")
    if args.queue_wait_seconds is not None and (args.action not in {"plan", "develop", "run", "prepare"}
                                                or not 1 <= args.queue_wait_seconds <= 86400):
        parser.error("--queue-wait-seconds requires plan, develop, run or prepare and a value from 1 to 86400")
    if args.approved_plan and (args.action not in ("deliver", "release") or not args.from_artifact):
        parser.error("--approved-plan requires a delivery or release operation with --from-artifact")

    def interrupted(signum, frame):
        raise Interrupted("SOP node received a termination signal")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        variable = "UNITY_WORKER_APPROVAL_TOKEN" if args.action in ("approve", "recover") else "UNITY_WORKER_TOKEN"
        client = Client(os.environ.get("UNITY_WORKER_URL", ""), os.environ.get(variable, ""))
        return run_action(client, args)
    except (OSError, KeyError, ValueError, ClientError) as error:
        print("Unity Worker: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(globals().get("SOP_ARGS")))
