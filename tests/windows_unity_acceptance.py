"""Opt-in Unity 2022.3 acceptance using a newly generated disposable project."""

import argparse
import hashlib
import json
import secrets
import shutil
import time
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from process_runner import ProcessRunner, StageError
from unity_executor import UnityExecutor, read_test_results, unity_product_version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("unity_exe", type=Path)
    parser.add_argument("--test-framework", default="1.1.33")
    parser.add_argument("--timeout", type=int, default=240)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--compile-only", action="store_true")
    mode.add_argument("--direct-merge", action="store_true")
    args = parser.parse_args()
    exe = str(args.unity_exe.resolve(strict=True))
    version = unity_product_version(exe)
    if not version:
        parser.error("Cannot read the Unity Editor version")
    root = Path.cwd().resolve() / f".tmp-test-unity-{uuid4().hex[:12]}"
    project = root / "project"
    logs = root / "logs"
    for directory in (project / "Assets", project / "ProjectSettings", project / "Packages", logs):
        directory.mkdir(parents=True)
    (project / "ProjectSettings/ProjectVersion.txt").write_text(f"m_EditorVersion: {version}\n", encoding="utf-8")
    dependencies = {} if args.compile_only else {
        "com.unity.test-framework": args.test_framework,
        "com.unity.modules.imgui": "1.0.0",
        "com.unity.modules.jsonserialize": "1.0.0",
        "com.unity.modules.physics": "1.0.0",
    }
    (project / "Packages/manifest.json").write_text(json.dumps({"dependencies": dependencies}), encoding="utf-8")
    source = project / "Assets/AcceptanceSample.cs"
    valid_source = "internal static class AcceptanceSample { internal static int Answer = 42; }\n"
    source.write_text(valid_source, encoding="utf-8")
    for mode in () if args.compile_only else ("EditMode", "PlayMode"):
        directory = project / "Assets" / mode
        directory.mkdir()
        assembly = {
            "name": f"Acceptance.{mode}",
            "optionalUnityReferences": ["TestAssemblies"], "includePlatforms": ["Editor"] if mode == "EditMode" else [],
            "autoReferenced": False,
        }
        (directory / f"{mode}.asmdef").write_text(json.dumps(assembly), encoding="utf-8")
        (directory / f"{mode}Test.cs").write_text(
            f"using NUnit.Framework;\npublic class {mode}Test {{ [Test] public void Runs() {{ Assert.AreEqual(4, 2 + 2); }} }}\n",
            encoding="utf-8",
        )
    if args.direct_merge:
        prepare_merge_sources(project)
    settings = SimpleNamespace(project=project, unity_exe=exe, unity_version=version, unity_timeout=args.timeout)
    runner = ProcessRunner()
    ctx = SimpleNamespace(artifact=lambda label, suffix, kind: logs / f"{label}-{uuid4().hex[:8]}{suffix}",
                          run=lambda argv, label, publish, **kw: runner.run(argv, project, logs / uuid4().hex, **kw))
    unity = UnityExecutor(settings)
    print(f"Disposable project and logs: {root}", flush=True)
    try:
        print("compile", unity.run(ctx, "compile"), flush=True)
        if args.direct_merge:
            # The first PlayMode run writes Editor settings. Track them in the fixture baseline.
            for mode in MODES:
                scope = {"assembly": f"Acceptance.{mode}", "fixtures": [f"{mode}Test"]}
                print("baseline", unity.run(ctx, mode.lower(), scope=scope), flush=True)
            direct_merge_acceptance(root, settings)
            return 0
        if not args.compile_only:
            for mode in ("editmode", "playmode"):
                print(mode, unity.run(ctx, mode), flush=True)
            test = project / "Assets/EditMode/EditModeTest.cs"
            test.write_text(test.read_text().replace("AreEqual(4", "AreEqual(5"), encoding="utf-8")
            expect_failure(unity, ctx, "editmode", "tests_failed")
        source.write_text("public class AcceptanceSample { this is intentionally invalid }\n", encoding="utf-8")
        expect_failure(unity, ctx, "compile", "compile_failed")
        source.write_text(valid_source, encoding="utf-8")
        print("compile after correction", unity.run(ctx, "compile"), flush=True)
    except StageError as error:
        print(f"{error.kind}: {error}. Evidence: {error.result}", file=sys.stderr, flush=True)
        return 1
    return 0


SCRIPT = "Assets/Runtime/AcceptanceSample.cs"
MODES = ("EditMode", "PlayMode")


def sample(feature, target):
    return f"public static class AcceptanceSample {{ public const int Feature = {feature}, Target = {target}; }}\n"


def prepare_merge_sources(project):
    (project / "Assets/AcceptanceSample.cs").unlink()
    runtime = project / "Assets/Runtime"
    runtime.mkdir()
    (runtime / "Acceptance.Runtime.asmdef").write_text('{"name":"Acceptance.Runtime"}', encoding="utf-8")
    (project / SCRIPT).write_text(sample(0, 0), encoding="utf-8")
    for mode in MODES:
        assembly = project / f"Assets/{mode}/{mode}.asmdef"
        data = json.loads(assembly.read_text())
        data["references"] = ["Acceptance.Runtime"]
        assembly.write_text(json.dumps(data), encoding="utf-8")
        (project / f"Assets/{mode}/{mode}Test.cs").write_text(
            f"using NUnit.Framework;\npublic class {mode}Test {{ [Test] public void Runs() "
            "{ Assert.AreEqual(0, AcceptanceSample.Feature); } }\n", encoding="utf-8",
        )


class MergeFixtureAgent:
    def clarify(self, ctx, briefing):
        return {"goal": briefing["requirement"], "audience": ["Fixture maintainers"], "scope": [SCRIPT],
                "acceptance_criteria": ["Both test modes check the feature value"], "open_questions": []}

    def plan(self, ctx, briefing):
        return {"requirement_summary": briefing["requirement"], "plan": "Increment Feature and update its tests.",
                "acceptance_criteria": ["Both test modes check the feature value"],
                "source_evidence": [{"path": SCRIPT, "line": 1, "excerpt": (ctx.settings.project / SCRIPT).read_text().strip()}],
                "baseline_compile": False, "baseline_tests": {"editmode": [], "playmode": []},
                "test_selection": {mode.lower(): [{"assembly": f"Acceptance.{mode}", "fixtures": [f"{mode}Test"]}]
                                   for mode in MODES}}

    def execute(self, ctx, prompt, *, review=False):
        project = ctx.settings.project
        value = int(ctx.task["requirement"].split()[-1])
        script = project / SCRIPT
        if review:
            candidate = '"merge_sha"' in prompt
            expected = sample(value, value if candidate else value - 1)
            check(script.read_text() == expected, "Review saw an unexpected fixture resolution")
            return {"verdict": "pass", "summary": "Checked both constant values", "issues": [],
                    "session_id": "candidate-review" if candidate else "implementation-review"}
        changed = [SCRIPT]
        if prompt.startswith("Resolve the listed C# content conflicts"):
            check("<<<<<<<" in script.read_text(), "Expected Git conflict markers")
            # The second case deliberately drops the feature so its real EditMode assertion fails.
            script.write_text(sample(value if value == 1 else value - 1, value), encoding="utf-8")
            session = "solver"
        else:
            check(script.read_text() == sample(value - 1, value - 1), "Unexpected starting feature or target")
            script.write_text(sample(value, value - 1), encoding="utf-8")
            for mode in MODES:
                path = f"Assets/{mode}/{mode}Test.cs"
                test = project / path
                test.write_text(test.read_text().replace(f"AreEqual({value - 1},", f"AreEqual({value},"), encoding="utf-8")
                changed.append(path)
            session = "implementation"
        return {"success": True, "summary": "Applied fixture edit", "changed_files": changed,
                "implementation_notes": [], "known_issues": [], "session_id": session}


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def direct_merge_acceptance(root, unity):
    from fastapi.testclient import TestClient
    from test_workspace import git
    from worker import create_app
    from worker_config import PoolSettings, ServiceSettings, Settings, SlotSettings

    project, remote, author = unity.project, root / "origin.git", root / "target-author"
    (project / ".gitignore").write_text("/Library/\n/Logs/\n/Temp/\n/Obj/\n/UserSettings/\n", encoding="utf-8")
    git(root, "init", "--bare", "--initial-branch=main", str(remote))
    git(project, "init", "--initial-branch=main")
    git(project, "config", "user.name", "Worker Acceptance")
    git(project, "config", "user.email", "fixture@example.invalid")
    git(project, "add", "--all")
    git(project, "commit", "-m", "Track the imported disposable Unity project")
    git(project, "remote", "add", "origin", remote.as_posix())
    git(project, "push", "-u", "origin", "main")
    git(root, "clone", str(remote), str(author))
    urls = [git(project, "remote", "get-url", *opts, "origin") for opts in ([], ["--push"])]
    identity = hashlib.sha256("\0".join(urls).encode()).hexdigest()
    settings = Settings(**vars(unity), logs=root / "logs", token=secrets.token_hex(32), git_exe=shutil.which("git"),
                        workflow_approval_key=secrets.token_hex(32), expected_remote_identity=identity, max_fixes=0,
                        test_assemblies={mode.lower(): (f"Acceptance.{mode}",) for mode in MODES})
    slot = SlotSettings("slot", "acceptance", True, settings)
    pool = PoolSettings("acceptance", identity, (slot,), direct_merge=True)
    app = create_app(ServiceSettings(pool.id, slot.id, {pool.id: pool}), agent=MergeFixtureAgent())
    with TestClient(app, headers={"Authorization": f"Bearer {settings.token}"}) as client:
        for value in (1, 2):
            merge_case(client, settings, remote, author, value)


def merge_case(client, settings, remote, author, value):
    from artifacts import task_directory
    from test_workflow_approval import claims_for
    from test_workspace import git
    from workflow_approval import sign_approval

    def post(path, **body):
        response = client.post(path, json={"request_id": str(uuid4()), **body})
        check(response.status_code == 202, f"{path}: {response.status_code} {response.text}")
        return response.json()

    task_id = post("/api/v1/planning-tasks", workflow_run_id="run-" + uuid4().hex, workflow_mode="staged-feature",
                   workflow_project_id="Acceptance", planning_profile="feature", pool_id="acceptance",
                   requirement=f"Set the feature value to {value}", publish_checkpoints=True,
                   delivery_mode="direct-merge")["task_id"]
    path = f"/api/v1/tasks/{task_id}"
    directory = task_directory(settings.logs, task_id)

    def task():
        response = client.get(path)
        response.raise_for_status()
        return response.json()

    def stage(action, *, expected="succeeded", **body):
        job_id = post(f"{path}/{action}", **body)["job_id"]
        print(f"{task_id} {action} {job_id}", flush=True)
        # run and workflow-deliver each perform at most three Editor invocations in this fixture.
        deadline = time.monotonic() + settings.unity_timeout * 3 + 600
        while time.monotonic() < deadline:
            response = client.get(f"/api/v1/jobs/{job_id}")
            response.raise_for_status()
            job = response.json()
            if job["status"] != "running":
                check(job["status"] == expected, f"{action}: {job}")
                return job
            time.sleep(1)
        raise TimeoutError(f"{action} job {job_id} exceeded its acceptance deadline")

    stage("clarify")
    stage("plan")
    planned = task()
    stage("accept-plan", slot_id=planned["slot_id"], base_commit=planned["base_commit"],
          plan_digest=planned["planning"]["plan_digest"])
    stage("run")
    approved = task()
    claims = claims_for(approved)
    source = claims["commit_sha"]
    git(author, "pull", "--ff-only", "origin", "main")
    (author / SCRIPT).write_text(sample(value - 1, value), encoding="utf-8")
    git(author, "add", "--", SCRIPT)
    git(author, "commit", "-m", "Independently advance the target on the same C# line")
    git(author, "push", "origin", "main")
    base = git(remote, "rev-parse", "main")
    for label, contents in (("test", approved["verification"]), ("approval", {"accept": True, "commit_sha": source})):
        artifact = directory / f"fixture-{label}.json"
        artifact.write_bytes(json.dumps(contents, sort_keys=True).encode("utf-8"))
        claims[f"{label}_artifact_id"] = artifact.name
        claims[f"{label}_digest"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    proof = sign_approval({**claims, "delivery_mode": "direct-merge"}, settings.workflow_approval_key, time.time())
    job = stage("workflow-deliver", approval=proof, expected="succeeded" if value == 1 else "failed")
    current = task()
    journal = current["merge_journal"]
    candidate = journal["merge_sha"]
    target = git(remote, "rev-parse", "main")
    check(journal["conflict_paths"] == [SCRIPT] and journal["base_sha"] == base, "Wrong conflict or target baseline")
    check(git(remote, "rev-parse", approved["task_branch"]) == source == journal["source_sha"], "Source SHA changed")
    check(git(settings.project, "rev-list", "--parents", "-1", candidate).split() == [candidate, base, source],
          "Candidate does not have the expected parents")
    evidence = {"task": task_id, "job": job["job_id"], "source": source, "base": base, "candidate": candidate,
                "target": target, "conflicts": journal["conflict_paths"]}
    if value == 1:
        receipt = current["delivery"]
        checks = receipt["verification"]
        check(receipt["status"] == "merged" and receipt["target_sha"] == candidate == target == checks["merge_sha"],
              "Confirmed target differs from tested candidate")
        check(checks["compile"]["exit_code"] == 0 and checks["review"]["verdict"] == "pass", "Candidate checks failed")
        check([run["mode"] for run in checks["runs"]] == [m.lower() for m in MODES], "Missing test mode")
        for mode, run in zip(MODES, checks["runs"]):
            xml = directory / run["test_results"]
            counts = read_test_results(xml, {"assembly": f"Acceptance.{mode}", "fixtures": [f"{mode}Test"]})
            check(counts["passed"] == counts["total"] == 1, f"Unexpected scoped results in {xml}")
            evidence[mode] = {"xml": str(xml), **counts}
        evidence["compile"] = str(directory / checks["compile"]["log"])
        stage("release")
        check(task()["status"] == "completed" and not task()["lease"], "Slot was not released")
        evidence["released"] = True
    else:
        failure = journal["verification_failure"]
        check(job["failure_kind"] == failure["kind"] == "tests_failed" and target == base, "Failure did not block push")
        result = failure["result"]
        check(result["step"] == "editmode" and result["failed"] == 1, "Wrong failing stage or test count")
        xml = directory / result["test_results"]
        try:
            read_test_results(xml, {"assembly": "Acceptance.EditMode", "fixtures": ["EditModeTest"]})
        except StageError as error:
            check(error.kind == "tests_failed" and error.result["failed_tests"][0]["name"] == "EditModeTest.Runs",
                  "Expected the fixture assertion to fail")
        else:
            raise AssertionError(f"Expected a failed test in {xml}")
        check(bool(current["lease"]), "Failed task lost its lease")
        evidence.update(failure=failure, xml=str(xml), released=False)
    print(json.dumps(evidence, ensure_ascii=False), flush=True)


def expect_failure(unity, ctx, mode, kind):
    try:
        unity.run(ctx, mode)
    except StageError as error:
        if error.kind == kind:
            print(f"{mode}: correctly blocked {kind}", flush=True)
            return
        raise
    raise StageError("unexpected_success", f"Expected {mode} to fail with {kind}")


if __name__ == "__main__":
    raise SystemExit(main())
