"""Opt-in Unity 2022.3 acceptance using a newly generated disposable project."""

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from process_runner import ProcessRunner, StageError
from unity_executor import UnityExecutor, unity_product_version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("unity_exe", type=Path)
    parser.add_argument("--test-framework", default="1.1.33")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--compile-only", action="store_true")
    args = parser.parse_args()
    exe = str(args.unity_exe.resolve(strict=True))
    version = unity_product_version(exe)
    if not version:
        parser.error("Cannot read the Unity Editor version")
    root = Path.cwd().resolve() / f".tmp-test-unity-{uuid4().hex}"
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
    settings = SimpleNamespace(project=project, unity_exe=exe, unity_version=version, unity_timeout=args.timeout)
    runner = ProcessRunner()
    ctx = SimpleNamespace(artifact=lambda label, suffix, kind: logs / f"{label}-{uuid4().hex[:8]}{suffix}",
                          run=lambda argv, label, publish, **kw: runner.run(argv, project, logs / uuid4().hex, **kw))
    unity = UnityExecutor(settings)
    print(f"Disposable project and logs: {root}", flush=True)
    try:
        print("compile", unity.run(ctx, "compile"), flush=True)
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
