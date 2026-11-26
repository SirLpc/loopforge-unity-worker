# Worker Storage Maintenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Windows operators see workspace task and disk occupancy, release finished tasks, and reclaim space from one idle Unity cache per confirmed request.

**Architecture:** Keep paths and scheduling decisions in the Worker. Add an operator-only storage API and a PowerShell client that also calls the existing task release API. Reserve a slot under the scheduler guard and move the selected cache out of its original path before recursive deletion.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, Python standard library, Git for Windows, Windows PowerShell 5.1+, unittest.

**Spec:** [Worker 目录占用与手工释放设计](../specs/2026-11-26-worker-storage-maintenance-design.md)

## Global Constraints

- Support the existing single-project setup and named pools with the same endpoint shapes. No new dependency, config key, or SQLite schema change.
- New storage endpoints require the existing Operator Token. Existing routes and token rules retain their contracts.
- Only a configured slot and one confirmed project-direct cache are accepted for deletion. Valid values are `Library`, `Temp`, and `Obj`.
- Keep SQLite transactions and the Worker guard free of recursive scans, Git commands, and deletes. Do not execute maintenance against deployed checkouts in verification.
- Use the existing Windows sandbox approval flow for real Unity EditMode / PlayMode tests if needed. The maintenance paths can be tested with local fixtures without launching Unity.
- Set `$workerTestPython` to the existing `.venv\Scripts\python.exe` absolute path and run each command from the active worktree. Run local Git transport suites outside the sandbox when required by the repository.

## File Map

- `workspace_storage.py` provides non-following directory scans, filesystem capacity, cache preflight, and cache removal results.
- `git_workspace.py` verifies that the target tree is ignored and contains no tracked entries or Git submodule boundary.
- `development.py` reserves the slot and coordinates startup and restart. `pool_scheduler.py` excludes the reserved slot from admission and status.
- `worker.py` validates input, authenticates, and exposes storage responses.
- `scripts/workspace-maintenance.ps1` presents status and measurement, polls release, and previews and confirms clean.
- `tests/test_workspace_storage.py`, `tests/test_storage_api.py`, and `tests/test_workspace_maintenance_script.py` cover the filesystem, service, and Windows client, respectively. `README.md` documents the command and failure handling.

## Review Focus

- A project inside a checkout must account for its Unity caches while keeping similarly named checkout siblings in the other-files bucket. Task 1 asserts both paths are counted once.
- A Windows junction or inaccessible entry must never be counted as fully read. Tasks 1 and 2 exercise skipped sizes and unchanged external target data.
- An ignored cache may contain a file present only in HEAD after staged deletion, or lie inside a tracked gitlink. Task 2 protects both, including Windows case aliases.
- A failed or interrupted delete must leave the original cache path empty and make the leftover location visible after service restart. Task 2 asserts the normal and legacy restart paths.
- Canceling the prompt, stopping release polling, or receiving an ambiguous cleanup response must never trigger another mutation. Task 3 asserts the expected HTTP call sequence.

---

## Execution Setup

- [ ] Prepare the isolated worktree using `superpowers:using-git-worktrees`. Carry this plan and the approved design into that worktree, confirm the baseline and local test interpreter, then commit the two documents there as `docs: plan Worker storage maintenance`.

### Task 1 — Read-only storage inspection

**Files**

- Create `workspace_storage.py`, `tests/test_workspace_storage.py`, and `tests/test_storage_api.py`.
- Modify `development.py` and `worker.py`.

**Interfaces**

- `CacheName = Literal["Library", "Temp", "Obj"]` defines both the API enum and cache buckets.
- `volume_usage(path: Path) -> dict` returns `total_bytes` and `free_bytes`, with null values if capacity is unavailable. This function performs no recursive walk.
- `measure_directory(root: Path, groups: dict[str, Path]) -> dict` returns `path`, `exists`, `complete`, `logical_bytes`, `buckets`, `skipped_links`, and `read_errors`. Buckets map names to logical bytes and include `other`. A missing root has `exists=false` and `logical_bytes=null`. Partial totals and their counters stay visible.
- `DevelopmentWorker.storage_overview() -> dict` returns `pools` with the existing per-pool and per-slot occupancy fields plus configured `path` and `volume` for each slot, and `state` with `path` and `volume`. `DevelopmentWorker.storage_measure(slot_id: str | None = None, cache: CacheName | None = None) -> dict` returns the measurement plus `volume`. Omitted slot identifies the state directory. A selected cache measures only its own subtree.
- Routes are `GET /api/v1/storage`, `GET /api/v1/storage/state`, and `GET /api/v1/storage/slots/{slot_id}` with optional `?cache=Library`. An unknown slot returns 404 and an invalid cache name returns 422.

- [ ] Write failing filesystem and service tests using `support.test_directory`, `TestClient`, and normal auth headers. Assert `total = 3 + 5 + 7`, buckets `Library=5`, `.git=3`, `other=7` for a nested project with a similarly named root-level cache. Assert a missing path yields `logical_bytes is None` and a mocked unreadable entry yields `complete is False` with `read_errors == 1`. Cover a real Windows junction pointing to an external file, including the cache-root case, with target size excluded and `skipped_links == 1`. In API tests assert 401 without a token, 403 for the Worker Token, and 200 for Operator Token on all three reads. Patch the scan function to raise if called from overview. Assert the single-project slot ID `worker-01`, configured named pool slots, separate state total, and a targeted cache preview that succeeds even when an unrelated bucket cannot be read.
- [ ] Run `& $workerTestPython -m unittest discover -s tests -p 'test_*storage*.py' -v`. Expect failure for the missing scan module and route assertions.
- [ ] Implement `measure_directory` as one iterative walk. Test file metadata without following links, including Windows reparse attributes. Validate the configured root and its path components before descending. Use component-relative matching for buckets so nested paths appear exactly once. Share the entry classification with the cleanup step. For overview, reuse `self.pools()` and read disk capacity per configured path after obtaining the state snapshot.
- [ ] Add the GET handlers in `create_app` using `Depends(operator)` and the measurement methods. Use the existing error conventions and redact displayed strings with `clean_output`.
- [ ] Run the Task 1 command again. Expect all discovered tests to pass. Commit these files as `feat: inspect Worker workspace storage`.

### Task 2 — Guarded manual cache cleanup

**Files**

- Modify `workspace_storage.py`, `git_workspace.py`, `development.py`, `pool_scheduler.py`, `worker.py`, `tests/test_workspace_storage.py`, and `tests/test_storage_api.py`.

**Interfaces**

- `GitWorkspace.assert_disposable_cache(ctx, target: Path) -> None` raises `StageError` on unsafe Git state or an indeterminate result.
- `cleanup_remnants(project: Path) -> tuple[Path, ...]` reports direct children with the reserved `.unity-worker-cleanup-` prefix, without descending into any remnants.
- `clean_cache(workspace: GitWorkspace, ctx, cache: CacheName) -> dict` returns `status` (`cleaned` or `missing`), `cache`, `path`, `logical_bytes`, and updated `volume`. A post-move failure raises `StageError` with the remnant path in `result`.
- `DevelopmentWorker.clean_storage(slot_id: str, cache: CacheName) -> dict` reserves the slot and invokes `clean_cache`. `POST /api/v1/storage/slots/{slot_id}/clean` takes exactly `{"cache":"Library","confirmed":true}`. Return 200 for a successful or missing target, 404 for an unknown slot, 422 for invalid payload or confirmation, and 409 for a busy or unsafe slot. A file-operation failure returns a safe error with a remnant path if created.

- [ ] Add failing tests for protected cache trees, unsafe requests, concurrent admission, and recovery. Assert a confirmed request removes only the chosen ignored cache, preserves another cache and Git/state sentinels, and shares the service's existing auth boundary. Assert false, absent, or non-boolean confirmation and caller-supplied paths leave the cache unchanged. Use local Git fixtures to protect a normal tracked file including a Windows case alias, an entry present exclusively in HEAD following `git rm --cached`, a non-ignored local file, a gitlink covering the target, and an ignored nested repository. Also protect Unity lockfiles, junction roots and descendants, uncertain scans, invalid roots, leases, startup-smoke markers, and stopping state.
- [ ] In `test_storage_api.py`, add an event-controlled cleanup test asserting a second maintenance request and smoke for the same slot get 409, a sibling remains assignable, and a queued job waits until its eligible slot is available. Assert `maintenance` in new status, `unavailable` in existing pool status, and `shutdown_when_idle() is False` while maintenance runs. Simulate recursive deletion raising after the move. Assert the original cache path is absent, the reported remnant is within the project, the slot is unavailable on the running Worker and on a recreated Worker, and handling the remnant followed by restart makes the slot available. Cover both single-project and named-pool startup. Release test events in `finally` blocks.
- [ ] Run `& $workerTestPython -m unittest discover -s tests -p 'test_*storage*.py' -v`. Expect the new cleanup tests to fail until the endpoint, cleanup functions and scheduler checks are in place.
- [ ] Implement `assert_disposable_cache` with the existing scoped Git runner and read-only validation. Check exact path and descendants using Git ignore and untracked queries. Inspect NUL-delimited index and HEAD entries, comparing path components case-insensitively on Windows. Reject tracked entries at or below the target and tracked paths above it, which can represent a gitlink even when the parent path is absent from a target-only query. Do not mutate the index or contact a remote. Use a short-lived Git context under Worker state.
- [ ] Implement `clean_cache`. Confirm configured checkout/project/target identity and direct ancestry with non-following metadata, verify Unity is closed and Git allows disposal, and scan the cache for unreadable entries, reparse points and embedded `.git` names. After all checks, create an exclusive staging directory in the same project, move the target into it, and delete only that staging directory. Keep a failed remnant discoverable, including the case where a failed move cannot clean up the empty staging directory. Missing targets are harmless. Never traverse outside the validated staging directory.
- [ ] Implement reservation and restart handling. While holding `DevelopmentWorker.guard`, resolve the configured slot, reject stopping, blocked or leased slots, and add the ID to the active maintenance set. Do the filesystem work outside the guard. Under the guard in `finally`, block any slot with a remaining staging directory, remove active maintenance, and set `scheduler.wake`. Check for remnants at startup before a slot can be admitted, including in the implicit legacy pool. Make `PoolScheduler.slot_blocked` account for maintenance and make `shutdown_when_idle` wait for it. Encode the new label in storage overview only. Keep existing pool clients on known values and log the maintenance attempt and result with no credentials.
- [ ] Add the POST handler using the existing Operator dependency and strict boolean validation. Report a partial failure with the operator-visible remnant and apply existing output redaction. Keep request data out of filesystem paths.
- [ ] Run the storage suite again and run `& $workerTestPython -m unittest discover -s tests -p 'test_pools.py' -v` and `& $workerTestPython -m unittest discover -s tests -p 'test_restart.py' -v`. Expect all three to pass. Commit the changes as `feat: safely clean idle Worker caches`.

### Task 3 — Operator PowerShell command

**Files**

- Create `scripts/workspace-maintenance.ps1` and `tests/test_workspace_maintenance_script.py`.
- Modify `README.md`.

**Interfaces**

- Positional operation is `Status` by default, or `Measure`, `Release`, `Clean`. Inputs are `-WorkerUrl` (default `UNITY_WORKER_URL`), `-CredentialDirectory`, `-SlotId`, `-State`, `-TaskId`, and `-Cache`.
- Use `UNITY_WORKER_APPROVAL_TOKEN` or `UNITY_WORKER_APPROVAL_TOKEN.dpapi` as in the existing operator script. Use the three read routes and clean route above. Release posts a UUID `request_id` to `/api/v1/tasks/{task_id}/release` and polls `/api/v1/jobs/{job_id}`.

- [ ] Write failing Windows PowerShell tests with a local mock API or PowerShell command mocks, using a non-credential fixture token. Assert Status makes one overview GET and prints a slot and its free capacity. Assert Measure selects either the state or configured slot route and shows bucket bytes and partial warnings. Assert Release sends one POST, prints its Job ID, and polls to success or reports the failure detail with a nonzero exit. Assert Clean uses the targeted preview first, warns for `Library`, and sends exactly one POST after confirmation with canonical cache casing. Assert no POST for declined/incomplete preview, no cancel request for interrupted release polling, and no automatic POST retry after a lost clean response. Verify DPAPI or environment credential resolution and that prompt and error output omit the token.
- [ ] Run `& $workerTestPython -m unittest discover -s tests -p 'test_workspace_maintenance_script.py' -v`. Expect missing-script failures.
- [ ] Implement the script with operation-specific argument validation, escaped slot IDs in URLs, authenticated HTTP requests, bounded release polling and short actionable status output. Use the targeted Measure response before asking for explicit interactive confirmation. For ambiguous clean responses, tell the operator to query status and preview again. Return the correct exit status for validation, API, and operation errors. Leave the task running independently of interrupted client polling.
- [ ] Document usage for all four operations, Operator Token and DPAPI setup, the quick overview versus on-demand scans, cache limits and the Library reimport cost, release and clean races, and how to inspect a remnant and restart. Add the four operator API routes to the API table. Document test-only invocation against disposable checkouts.
- [ ] Run the script tests and then `& $workerTestPython -m unittest discover -s tests -v`. Expect all tests to pass. Review `git diff --check`, including committed changes against the base, and complete the Superpowers code-review step. Commit as `feat: add PowerShell Worker maintenance commands`.

## Final Verification

- [ ] Check representative Status, Measure, successful Release and blocked Release, confirmed and declined Clean, and partial cleanup recovery against temporary fixtures. Record the results and any real-Unity verification left untouched.
- [ ] Run a final diff check and validate the clean worktree status. Use `superpowers:finishing-a-development-branch` for integration after the final review.
