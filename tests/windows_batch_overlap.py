"""Run two isolated Unity BatchMode fixtures and verify process overlap and reports."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from process_runner import ProcessRunner, StageError
from unity_executor import UnityExecutor, unity_product_version


def live_editors():
    script = """
$processes = @(Get-CimInstance Win32_Process -Filter "Name = 'Unity.exe'" |
  Select-Object ProcessId, CommandLine, WorkingSetSize)
[pscustomobject]@{ editors = $processes;
  available_kib = (Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory } | ConvertTo-Json -Compress
"""
    raw = subprocess.check_output(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                  timeout=15)
    return json.loads(raw)


def verify_pair(exe, version, slots, directory, mode):
    gate = threading.Barrier(len(slots))
    observed = {}
    minimum_available = None
    combined_peak = 0

    def run(slot, logs):
        project = Path(slot["project"]).resolve(strict=True)
        logs.mkdir()
        settings = SimpleNamespace(project=project, unity_exe=exe, unity_version=version, unity_timeout=900,
                                   test_assemblies={mode: [slot[mode]["assembly"]]})
        runner = ProcessRunner()
        context = SimpleNamespace(artifact=lambda label, suffix, kind: logs / (label + suffix),
                                  run=lambda argv, label, publish, **kw: runner.run(
                                      argv, project, logs / "unity-process", **kw))
        gate.wait(timeout=10)
        return UnityExecutor(settings).run(context, mode, scope=slot[mode])

    with ThreadPoolExecutor(max_workers=len(slots)) as executor:
        jobs = [executor.submit(run, slot, directory / f"slot-{index + 1}") for index, slot in enumerate(slots)]
        while not all(job.done() for job in jobs):
            sample = live_editors()
            matched = {}
            for slot in slots:
                path = str(Path(slot["project"]).resolve())
                matches = [process for process in sample["editors"] if "-batchmode" in (process["CommandLine"] or "").lower()
                           and path.casefold() in (process["CommandLine"] or "").casefold()]
                if len(matches) == 1:
                    matched[path] = matches[0]
            if len(matched) == len(slots):
                observed = {path: process["ProcessId"] for path, process in matched.items()}
                free = int(sample["available_kib"])
                minimum_available = free if minimum_available is None else min(minimum_available, free)
                combined_peak = max(combined_peak, sum(int(item["WorkingSetSize"] or 0) for item in matched.values()))
            if not all(job.done() for job in jobs):
                time.sleep(0.4)
        reports = [job.result() for job in jobs]
    if not observed or len(set(observed.values())) != len(slots):
        raise RuntimeError(f"No actual process overlap observed for {mode}")
    remaining = {process["ProcessId"] for process in live_editors()["editors"]}
    if remaining.intersection(observed.values()):
        raise RuntimeError(f"Unity processes are still present after {mode}")
    return {"reports": reports, "overlapping_pids": observed, "minimum_available_kib": minimum_available,
            "combined_peak_bytes": combined_peak}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("report_dir", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
    slots = manifest["slots"]
    if len(slots) != 2 or len({Path(slot["project"]).resolve() for slot in slots}) != 2:
        parser.error("Provide exactly two independent acceptance projects")
    args.report_dir.mkdir()
    summary = {}
    for mode in ("editmode", "playmode"):
        directory = args.report_dir / mode
        directory.mkdir()
        try:
            summary[mode] = verify_pair(manifest["unity_exe"], unity_product_version(manifest["unity_exe"]),
                                        slots, directory, mode)
        except (RuntimeError, StageError) as error:
            summary[mode] = {"status": "failed", "error": str(error), "result": getattr(error, "result", None)}
            (args.report_dir / "results.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            print(json.dumps(summary[mode]), flush=True)
            return 1
        (args.report_dir / "results.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(mode, json.dumps(summary[mode]), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
