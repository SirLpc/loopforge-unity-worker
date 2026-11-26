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


def service_client(base_url, secret):
    def call(path, payload=None):
        headers = {"Authorization": "Bearer " + secret, "Content-Type": "application/json"}
        data = None if payload is None else json.dumps(payload).encode()
        request = Request(base_url + path, data, headers)
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=6) as response:
            return json.loads(response.read(8 * 1024 * 1024 + 1))
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


def publish_workspace_contexts(workspace_root, context_secret, platform_call, project_id):
    workspaces = {}
    issued = time.time()
    for sandbox in list_sandboxes():
        try:
            run_id, workspace_owner = sandbox_identity(sandbox, workspace_root)
            run = platform_call("/api/runs/" + run_id)
            if (run.get("executionContext", {}).get("workspaceRunId") or run_id) != workspace_owner:
                continue
            if run.get("nodes", {}).get("worker-plan", {}).get("status") == "running":
                operation = "plan"
            else:
                operation = "approve"
            record = run_context(run, run_id, operation, project_id)
        except (ContextError, KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError):
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
    parser.add_argument("--runner-workspaces", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", args.project_id):
        parser.error("Provide the LoopForge project ID")
    platform_secret = args.platform_token_file.read_text(encoding="utf-8").strip()
    context_secret = args.context_token_file.read_text(encoding="utf-8").strip()
    if not platform_secret or len(context_secret) < 32 or hmac.compare_digest(context_secret, platform_secret):
        raise SystemExit("Configure independent protected platform and context credentials")
    platform_call = service_client(args.platform_url.rstrip("/"), platform_secret)
    workspace_root = args.runner_workspaces.resolve(strict=True)
    try:
        while True:
            try:
                publish_workspace_contexts(workspace_root, context_secret, platform_call, args.project_id)
            except (OSError, subprocess.SubprocessError):
                pass
            time.sleep(2)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
