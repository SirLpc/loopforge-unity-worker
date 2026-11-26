"""Write signed local Unity planning inputs into the dedicated Runner workspaces."""

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


PLANNING_PROFILES = ("feature", "ui", "issue-fix")
SANDBOX = re.compile(r"/agentflow-rt-run-(run-[0-9]+)-[a-f0-9]{12}\Z")


class ContextError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def sandbox_identity(hostname, workspace_root):
    if not re.fullmatch(r"[a-f0-9]{12}", hostname):
        raise ContextError("Invalid Runner container identity")
    info = subprocess.run(["docker", "inspect", "--type", "container", hostname], capture_output=True,
                          text=True, timeout=5, check=True)
    value, = json.loads(info.stdout)
    match = SANDBOX.fullmatch(value.get("Name", ""))
    if (not match or not value["Id"].startswith(hostname) or value["Config"]["Hostname"] != hostname
            or not value["State"]["Running"]):
        raise ContextError("Container is not an active LoopForge Runner sandbox")
    run_id = match[1]
    mounts = [Path(mount["Source"]) for mount in value["Mounts"]
              if mount.get("Type") == "bind" and mount.get("Destination") == "/workspace"]
    if len(mounts) != 1 or not mounts[0].is_relative_to(workspace_root):
        raise ContextError("Sandbox is outside the dedicated Runner workspace")
    parts = mounts[0].relative_to(workspace_root).parts
    if len(parts) != 3 or parts[0] != "runs" or parts[2] != "workspace" or not re.fullmatch(r"run-[0-9]+", parts[1]):
        raise ContextError("Sandbox is outside the dedicated Runner workspace")
    return run_id, parts[1]


def run_context(run, run_id, operation, project_id):
    workflow = run.get("workflowSnapshot", {})
    profile = next((profile for profile in PLANNING_PROFILES
                    if run.get("workflowId") == f"{project_id}-{profile}"), None)
    if (not profile or run.get("id") != run_id or workflow.get("projectId") != project_id
            or run.get("executionContext", {}).get("projectId") != project_id or run.get("cancelRequested")
            or run.get("pauseRequested") or run.get("status") != "running"):
        raise ContextError("Run is outside the active Unity planning workflows")
    nodes = run.get("nodes", {})
    definitions = {node["id"]: node["type"] for node in workflow.get("nodes", [])}
    expected = {"worker-plan": "script", "plan-approval": "approval", "handoff": "script"}
    if any(definitions.get(node) != kind for node, kind in expected.items()):
        raise ContextError("Run does not contain the Windows planning approval flow")
    node_id = "worker-plan" if operation == "plan" else "handoff"
    if nodes.get(node_id, {}).get("status") != "running":
        raise ContextError("The requested planning stage is not running")
    context = {"workflow_run_id": run_id, "planning_profile": profile}
    planning = nodes["worker-plan"]
    if operation == "plan":
        requirement = run.get("input", "").strip()
        feedback = planning.get("feedback", "").strip()
        if not 0 < len(requirement) <= 20_000 or len(feedback) > 4_000 or "\0" in requirement + feedback:
            raise ContextError("Unity planning input exceeds the supported limits")
        return {**context, "requirement": requirement, "feedback": feedback,
                "rework": planning["reworkCount"], "attempt": planning["attempt"]}
    approval = nodes.get("plan-approval", {})
    if planning.get("status") != "succeeded" or approval.get("status") != "succeeded":
        raise ContextError("The active plan has no human approval")
    # Check the decision tied to the current attempt and bind the exact uploaded JSON.
    artifacts = run.get("artifacts", [])
    if not any(artifact.get("nodeId") == "plan-approval" and artifact.get("attempt") == approval["attempt"]
               and artifact.get("name") == "decision" for artifact in artifacts):
        raise ContextError("The current plan approval has no decision artifact")
    hashes = {artifact.get("sha256", "") for artifact in artifacts
              if artifact.get("nodeId") == "worker-plan" and artifact.get("attempt") == planning["attempt"]
              and artifact.get("name") == "unity-planning"}
    if len(hashes) != 1 or not re.fullmatch(r"[a-f0-9]{64}", next(iter(hashes))):
        raise ContextError("The approved plan artifact could not be identified uniquely")
    return {**context, "plan_sha256": hashes.pop()}


FEATURE_NODES = {
    "clarify": "unity-scope", "design": "unity-planning", "implement": "unity-implementation",
    "review": "unity-review", "test": "unity-verification", "accept": "decision",
    "mr": "unity-delivery", "deliver": None,
}
ASSIGNMENT = ("task_id", "pool_id", "slot_id", "base_commit")
HUMAN_APPROVAL = b"# Approval\n\nApproved by a human gate.\n"
ARTIFACT_LIMIT = 8 * 1024 * 1024


def feature_artifact(run, node_id, reader, *, name=None):
    attempt = run["nodes"][node_id]["attempt"]
    matches = [a for a in run.get("artifacts", []) if a.get("nodeId") == node_id and a.get("attempt") == attempt
               and a.get("name") == (name or FEATURE_NODES[node_id])]
    if len(matches) != 1 or matches[0].get("runId") != run["id"]:
        raise ContextError("Missing or ambiguous current artifact from " + node_id)
    meta = matches[0]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}", meta["id"]):
        raise ContextError("Invalid platform artifact ID")
    raw = reader(meta["id"])
    if not isinstance(raw, bytes) or len(raw) > ARTIFACT_LIMIT or hashlib.sha256(raw).hexdigest() != meta["sha256"]:
        raise ContextError("Platform content differs from its recorded digest")
    return {key: meta[key] for key in ("id", "sha256", "name")}, raw


def public_feedback(value):
    encoded = value.encode()
    if len(encoded) <= 800:
        return value
    # Go slices at a byte offset, then encoding/json replaces each incomplete UTF-8 byte.
    clipped = encoded[:800].decode("utf-8", "surrogateescape")
    return re.sub(r"[\udc80-\udcff]", "\ufffd", clipped) + "…"


def full_rejection_feedback(run, feedback, reader):
    gate = run["nodes"]["accept"]
    source = {}
    retry = None
    for event in reversed(run.get("events", [])):
        if event.get("runId") != run["id"]:
            continue
        if event.get("nodeId") == "accept" and event.get("type") in {"gate.rejected", "node.started"}:
            source = event
            break
        if retry is None and event.get("nodeId") == "implement" and event.get("type") == "node.retry_requested":
            retry = event["message"]
    prefix = "打回到 implement："
    if (source.get("runId") != run["id"] or source.get("type") != "gate.rejected"
            or not source.get("message", "").startswith(prefix)):
        raise ContextError("Full rejection feedback is unavailable for this implementation round")
    reason = source["message"][len(prefix):].strip()
    notes = [a for a in run.get("artifacts", []) if a.get("nodeId") == "accept" and a.get("name") == "human-context"
             and a.get("attempt") == gate["attempt"]]
    if notes:
        _, raw = feature_artifact(run, "accept", reader, name="human-context")
        marker = "# 人工补充上下文\n\n"
        body = raw.decode("utf-8")
        if not body.startswith(marker) or not body.endswith("\n"):
            raise ContextError("Invalid rejection context artifact")
        context = body[len(marker):-1]
        reason = (reason + "\n\n" if reason else "") + "人工补充上下文：\n" + context
    displayed = reason
    if retry is not None:
        label, separator, note = retry.partition("：")
        if not separator or label not in {"从指定节点重跑", "从中断处继续"}:
            raise ContextError("Unknown implementation retry feedback")
        displayed = note
    if public_feedback(displayed) != feedback or len(reason) > 4000 or "\0" in reason:
        raise ContextError("Full rejection feedback differs from the current node or exceeds the Worker limit")
    return reason



def feature_context(run, run_id, project_id, artifact_reader, approval_key, now):
    from workflow_approval import sign_approval

    workflow_id = project_id + "-feature"
    wf = run.get("workflowSnapshot", {})
    kinds = [(n.get("id"), n.get("type")) for n in wf.get("nodes", [])]
    edges = [(e.get("source"), e.get("target"), e.get("when")) for e in wf.get("edges", [])]
    order = list(FEATURE_NODES)
    if (run.get("id") != run_id or run.get("workflowId") != workflow_id or wf.get("id") != workflow_id
            or wf.get("projectId") != project_id or run.get("executionContext", {}).get("projectId") != project_id
            or run.get("status") != "running" or run.get("pauseRequested") or run.get("cancelRequested")
            or sorted(kinds) != sorted((n, "approval" if n == "accept" else "script") for n in order)
            or sorted(edges) != sorted((a, b, "success") for a, b in zip(order, order[1:]))):
        raise ContextError("Run is outside the installed staged feature flow")
    nodes = run["nodes"]
    active = [n for n, state in nodes.items() if state.get("status") == "running"]
    if len(active) != 1 or active[0] not in FEATURE_NODES or active[0] == "accept":
        raise ContextError("No unique active feature script")
    node = active[0]
    state = nodes[node]
    attempt = state["attempt"]
    rework = nodes["implement"].get("reworkCount", 0)
    feedback = state.get("feedback", "")
    if (type(attempt) is not int or attempt < 1 or type(rework) is not int or rework < 0
            or not isinstance(feedback, str) or len(feedback) > 4000 or "\0" in feedback):
        raise ContextError("Invalid current feature execution")
    scope = {"workflow_project_id": project_id, "workflow_id": workflow_id, "workflow_run_id": run_id}
    context = {**scope, "node_id": node, "node_attempt": attempt, "rework": rework, "feedback": feedback,
               "operation": "feature-" + ("delivery" if node == "deliver" else node)}
    if node == "implement" and rework:
        try:
            context["feedback"] = full_rejection_feedback(run, feedback, artifact_reader)
        except (ContextError, UnicodeError) as error:
            context.update(feedback="", feedback_error=str(error))
    if node == "clarify":
        requirement = run.get("input", "").strip()
        if not 0 < len(requirement) <= 20000 or "\0" in requirement:
            raise ContextError("Unsupported feature requirement")
        context["requirement"] = requirement
        return context

    binding = dict(scope)
    previous = approval_meta = None
    for upstream in order[:order.index(node)]:
        prior = nodes[upstream]
        if prior.get("status") != "succeeded" or type(prior.get("attempt")) is not int or prior["attempt"] < 1:
            raise ContextError("Feature predecessor has no successful current attempt")
        meta, raw = feature_artifact(run, upstream, artifact_reader)
        if upstream == "accept":
            if raw != HUMAN_APPROVAL and not raw.startswith(HUMAN_APPROVAL + b"\n"):
                raise ContextError("Current decision is not an approval by a human gate")
            approval_meta = meta
            continue
        try:
            record = json.loads(raw)
        except (RecursionError, UnicodeError, ValueError) as error:
            raise ContextError("Invalid feature evidence JSON") from error
        expected = {**binding, "node_id": upstream, "node_attempt": prior["attempt"],
                    "parent_artifact_id": previous["id"] if previous else None}
        if not isinstance(record, dict) or any(k not in record or record[k] != v for k, v in expected.items()):
            raise ContextError("Feature evidence does not belong to the current execution chain")
        if upstream == "clarify":
            binding.update({k: record[k] for k in ASSIGNMENT})
        if upstream == "design":
            binding["plan_digest"] = record["plan_digest"]
        if upstream == "implement":
            if record.get("implementation_attempt") != rework:
                raise ContextError("Stale implementation round")
            binding.update(revision=record["revision"], implementation_attempt=rework)
        if upstream == "review":
            review = record.get("review_result") or {}
            if not isinstance(review, dict) or review.get("verdict") != "pass" or review.get("revision") != binding["revision"]:
                raise ContextError("Missing current passing review")
        if upstream == "test":
            checked = {"revision": binding["revision"], "test_job_id": record["job_id"], "implementation_attempt": rework}
            if record.get("verification") != checked:
                raise ContextError("Missing current successful test job")
            test_job_id = checked["test_job_id"]
        if upstream == "mr" and record.get("approval_artifact_id") != approval_meta["id"]:
            raise ContextError("Delivery receipt references a different approval")
        previous = meta
    context["upstream"] = previous
    if node == "mr":
        claims = {k: v for k, v in binding.items() if k != "workflow_project_id"}
        claims.update(project_id=project_id, accept_attempt=nodes["accept"]["attempt"], test_job_id=test_job_id,
                      approval_artifact_id=approval_meta["id"], approval_digest=approval_meta["sha256"],
                      test_artifact_id=previous["id"], test_digest=previous["sha256"])
        context["approval"] = sign_approval(claims, approval_key, now)
    return context


def service_client(base_url, secret):
    def call(path, payload=None):
        headers = {"Authorization": "Bearer " + secret, "Content-Type": "application/json"}
        data = None if payload is None else json.dumps(payload).encode()
        request = Request(base_url + path, data, headers)
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=6) as response:
            raw = response.read(ARTIFACT_LIMIT + 1)
            if len(raw) > ARTIFACT_LIMIT:
                raise ContextError("Platform response exceeds supported size")
            return raw if "/artifacts/" in path else json.loads(raw)
    return call


def signed_context(secret, sandboxes, issued):
    payload = {"issued_at": issued, "expires_at": issued + 10, "sandboxes": sandboxes}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
    return {"payload": payload, "signature": signature}


def write_context(workspace_root, workspace_owner, context):
    if not re.fullmatch(r"run-[0-9]+", workspace_owner):
        raise ContextError("Invalid workspace owner")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    target = ".unity-worker-context.json"
    temporary = target + "." + secrets.token_hex(12)
    with ExitStack() as directories:
        folder = os.open(workspace_root, flags)
        directories.callback(os.close, folder)
        for part in ("runs", workspace_owner, "workspace"):
            folder = os.open(part, flags, dir_fd=folder)
            directories.callback(os.close, folder)
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=folder)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(context, output, ensure_ascii=False, separators=(",", ":"))
                output.flush()
                os.fchmod(output.fileno(), 0o444)
            os.replace(temporary, target, src_dir_fd=folder, dst_dir_fd=folder)
        finally:
            try:
                os.unlink(temporary, dir_fd=folder)
            except FileNotFoundError:
                pass


def list_sandboxes():
    result = subprocess.run(
        ["docker", "ps", "--filter", "status=running", "--filter", "name=^/agentflow-rt-run-", "--format", "{{.ID}}"],
        capture_output=True, text=True, timeout=5, check=True,
    )
    return result.stdout.split()


def publish_workspace_contexts(workspace_root, context_secret, platform_call, project_id, approval_key=None):
    workspaces = {}
    issued = time.time()
    for sandbox in list_sandboxes():
        try:
            run_id, workspace_owner = sandbox_identity(sandbox, workspace_root)
            run = platform_call("/api/runs/" + run_id)
            if (run.get("executionContext", {}).get("workspaceRunId") or run_id) != workspace_owner:
                continue
            if "clarify" in run.get("nodes", {}) and "mr" in run["nodes"]:
                if not approval_key:
                    continue
                def artifact_reader(identifier):
                    return platform_call("/api/runs/" + run_id + "/artifacts/" + identifier)
                record = feature_context(run, run_id, project_id, artifact_reader, approval_key, issued)
                operation = record["operation"]
            else:
                operation = "plan" if run.get("nodes", {}).get("worker-plan", {}).get("status") == "running" else "approve"
                record = run_context(run, run_id, operation, project_id)
        except (ContextError, AttributeError, KeyError, OSError, RecursionError, TypeError, ValueError, subprocess.SubprocessError):
            continue
        workspaces.setdefault(workspace_owner, {})[sandbox] = {"operation": operation, **record}
    for owner, records in workspaces.items():
        try:
            write_context(workspace_root, owner, signed_context(context_secret, records, issued))
        except (ContextError, OSError):
            # A workspace can vanish during normal Runner cleanup.
            continue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True, help="LoopForge project ID used by install_sops")
    parser.add_argument("--platform-url", default="http://127.0.0.1:8090")
    parser.add_argument("--platform-token-file", type=Path, required=True)
    parser.add_argument("--context-token-file", type=Path, required=True)
    parser.add_argument("--approval-key-file", type=Path, help="Protected staged delivery signing key")
    parser.add_argument("--runner-workspaces", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", args.project_id):
        parser.error("Provide the LoopForge project ID")
    platform_secret = args.platform_token_file.read_text(encoding="utf-8").strip()
    context_secret = args.context_token_file.read_text(encoding="utf-8").strip()
    if not platform_secret or len(context_secret) < 32 or hmac.compare_digest(context_secret, platform_secret):
        raise SystemExit("Configure independent protected platform and context credentials")
    approval_key = None
    if args.approval_key_file:
        approval_key = args.approval_key_file.read_text(encoding="ascii").rstrip("\r\n")
        if (len(approval_key) < 32 or not approval_key.isprintable() or " " in approval_key
                or approval_key in {context_secret, platform_secret}):
            raise SystemExit("Configure an independent protected workflow approval key")
    platform_call = service_client(args.platform_url.rstrip("/"), platform_secret)
    workspace_root = args.runner_workspaces.resolve(strict=True)
    try:
        while True:
            try:
                publish_workspace_contexts(workspace_root, context_secret, platform_call, args.project_id, approval_key)
            except (OSError, subprocess.SubprocessError):
                pass
            time.sleep(2)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
