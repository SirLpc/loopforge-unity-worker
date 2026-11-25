"""Subprocess stand-in for CLI tests. Reads only fixture inputs."""

import json
import os
import sys
import time
from pathlib import Path


root = Path(sys.argv[1])
args = sys.argv[2:]


def argument(name):
    return args[args.index(name) + 1]


if "-batchmode" in args:
    assert "UNITY_WORKER_TOKEN" not in os.environ
    assert "UNITY_WORKER_APPROVAL_TOKEN" not in os.environ
    log = Path(argument("-logFile"))
    with log.open("w", encoding="utf-8") as file:
        file.write("Initialize engine version: 2022.3.62f3 (sandbox fixture)\n")
        file.write("Asset Pipeline Refresh\n")
        if (root / "license_fail").exists():
            file.write("No valid Unity Editor license found\n")
        if (root / "broken_compile").exists():
            file.write("Assets/MyClass.cs(3,1): error CS1002: ; expected\n")
        if "-quit" in args:
            file.write("Exiting batchmode successfully now!\n")
    if "-runTests" in args:
        assert "-quit" not in args
        failed = (root / "broken_tests").exists()
        outcome = "Failed" if failed else "Passed"
        case = f'<test-case result="{outcome}" />'
        if "-assemblyNames" in args:
            case = (f'<test-suite type="Assembly" name="{argument("-assemblyNames")}.dll">'
                    f'<test-case result="{outcome}" fullname="Game.UnitTests.Runs" classname="Game.UnitTests" /></test-suite>')
        Path(argument("-testResults")).write_text(
            f'<test-run result="{outcome}" total="1" passed="{int(not failed)}" failed="{int(failed)}">'
            f'{case}</test-run>', encoding="utf-8",
        )
elif args == ["--version"]:
    print("codex-cli 0.172.0 (fixture)")
elif args == ["exec", "--help"]:
    print("--strict-config --ignore-user-config --ignore-rules --output-last-message --output-schema --json --skip-git-repo-check")
elif args == ["sandbox", "--help"]:
    print("--include-managed-config --permission-profile")
elif "sandbox" in args:
    print("UNITY_SANDBOX_OK")
else:
    prompt = sys.stdin.read()
    assert "UNITY_WORKER_TOKEN" not in os.environ
    is_review = "--skip-git-repo-check" in args
    if is_review:
        assert Path.cwd() != root / "project"
        assert '":minimal"="read"' in " ".join(args)
        verdict = "pass"
        if (root / "review_once").exists():
            verdict = "request_changes"
            (root / "review_once").unlink()
        output = {"verdict": verdict, "summary": "Fixture review", "issues": []}
    else:
        assert Path.cwd() == root / "project"
        if (root / "slow_codex").exists():
            time.sleep(30)
        if (root / "codex_fail").exists():
            sys.exit(7)
        assert "greeting" in prompt
        (Path.cwd() / "hello.txt").write_text("updated\n")
        output = {"success": True, "summary": "Greeting changed", "changed_files": ["hello.txt"],
                  "implementation_notes": [], "known_issues": []}
    print(json.dumps({"type": "thread.started", "thread_id": "review-id" if is_review else "impl-id"}))
    Path(argument("--output-last-message")).write_text(json.dumps(output), encoding="utf-8")
