"""Measure logical file sizes without descending through filesystem links."""

import os
import shutil
import stat
from collections import Counter
from pathlib import Path
from tempfile import mkdtemp
from typing import Literal, get_args

from git_workspace import GitWorkspace
from process_runner import StageError


CacheName = Literal["Library", "Temp", "Obj"]
STAGING_PREFIX = ".unity-worker-cleanup-"


def is_link(metadata: os.stat_result) -> bool:
    return (stat.S_ISLNK(metadata.st_mode) or
            bool(getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT))


def volume_usage(path: Path) -> dict:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {"total_bytes": None, "free_bytes": None}
    return {"total_bytes": usage.total, "free_bytes": usage.free}


def _directory_identity(path: Path) -> tuple[tuple[int, int], ...] | None:
    identity = []
    for component in (*reversed(path.parents), path):
        metadata = component.lstat()
        if is_link(metadata):
            return None
        if not stat.S_ISDIR(metadata.st_mode):
            raise NotADirectoryError(component)
        identity.append((metadata.st_dev, metadata.st_ino))
    return tuple(identity)


def measure_directory(root: Path, groups: dict[str, Path], *, for_cleanup=False) -> dict:
    root = root.absolute()
    buckets = dict.fromkeys((*groups, "other"), 0)
    group_paths = {root / path: name for name, path in groups.items()}
    result = {"path": str(root), "exists": True, "complete": False, "logical_bytes": 0,
              "buckets": buckets, "skipped_links": 0, "read_errors": 0}
    pending = [(root, group_paths.get(root, "other"), None)]
    while pending:
        directory, bucket, parent_identity = pending.pop()
        try:
            identity = _directory_identity(directory)
        except FileNotFoundError:
            if parent_identity is None:
                return {**result, "exists": False, "complete": True, "logical_bytes": None}
            result["read_errors"] += 1
            continue
        except OSError:
            result["read_errors"] += 1
            continue
        if identity is None:
            result["skipped_links"] += 1
            continue
        if parent_identity is not None and parent_identity != identity[:-1]:
            result["read_errors"] += 1
            continue

        sizes = Counter()
        children = []
        git_markers = set()
        read_errors = skipped_links = 0
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except OSError:
                        read_errors += 1
                        continue
                    if for_cleanup:
                        name = entry.name.casefold()
                        if name == ".git":
                            raise StageError("unsafe_cache", "The cache contains an embedded Git repository")
                        if (name == "head" and stat.S_ISREG(metadata.st_mode)) or (
                                name in {"objects", "refs", "reftable"} and stat.S_ISDIR(metadata.st_mode)):
                            git_markers.add(name)
                    if is_link(metadata):
                        skipped_links += 1
                        continue
                    path = Path(entry.path)
                    entry_bucket = group_paths.get(path, bucket)
                    if stat.S_ISDIR(metadata.st_mode):
                        children.append((path, entry_bucket, identity))
                    elif stat.S_ISREG(metadata.st_mode):
                        sizes[entry_bucket] += metadata.st_size
                    elif for_cleanup:
                        raise StageError("unsafe_cache", "The cache contains a special filesystem entry")
        except OSError:
            read_errors += 1
        if {"head", "objects"} <= git_markers and git_markers & {"refs", "reftable"}:
            raise StageError("unsafe_cache", "The cache contains an embedded bare Git repository")

        # shortcut: path checks are best effort, use handle-relative traversal for hostile changes.
        try:
            observed = _directory_identity(directory)
        except OSError:
            result["read_errors"] += 1
            continue
        if observed is None:
            result["skipped_links"] += 1
        elif observed != identity:
            result["read_errors"] += 1
        else:
            result["read_errors"] += read_errors
            result["skipped_links"] += skipped_links
            for name, size in sizes.items():
                buckets[name] += size
            pending.extend(children)

    result["logical_bytes"] = sum(buckets.values())
    result["complete"] = not (result["skipped_links"] or result["read_errors"])
    return result


def cleanup_remnants(project: Path) -> tuple[Path, ...]:
    if _directory_identity(project) is None:
        raise StageError("unsafe_cache", "The project path contains a link or reparse point")
    with os.scandir(project) as entries:
        return tuple(project / entry.name for entry in entries if os.path.normcase(entry.name).startswith(STAGING_PREFIX))


def clean_cache(workspace: GitWorkspace, ctx, cache: CacheName) -> dict:
    project = workspace.project
    if cache not in get_args(CacheName) or not project.is_relative_to(workspace.root):
        raise StageError("unsafe_cache", "Invalid project or cache path")
    target = project / cache
    result = {"cache": cache, "path": str(target)}
    try:
        if cleanup_remnants(project):
            raise StageError("unsafe_cache", "A previous cleanup needs attention")
        if _directory_identity(workspace.root / ".git") is None:
            raise StageError("unsafe_cache", "The Git directory contains a link or reparse point")
        # A missing lock is safe. Inaccessible locks and dangling links are indeterminate.
        for directory in (project / "Temp", project / "Library"):
            try:
                if _directory_identity(directory) is None:
                    raise StageError("unsafe_cache", "Cannot verify Unity locks through a filesystem link")
                (directory / "UnityLockfile").lstat()
            except FileNotFoundError:
                continue
            raise StageError("workspace_in_use", "Unity project lock exists. Close the Editor or verify the stale lock.")
        try:
            identity = _directory_identity(target)
        except FileNotFoundError:
            workspace.validate_checkout_identity(ctx)
            return {**result, "status": "missing", "logical_bytes": 0, "volume": volume_usage(project)}
        if identity is None:
            raise StageError("unsafe_cache", "The cache path contains a link or reparse point")
        workspace.assert_disposable_cache(ctx, target)
        measured = measure_directory(target, {}, for_cleanup=True)
        if not measured["complete"] or not measured["exists"] or _directory_identity(target) != identity:
            raise StageError("unsafe_cache", "Could not verify every entry in the cache")
        staging = Path(mkdtemp(prefix=STAGING_PREFIX, dir=project))
        try:
            target.rename(staging / cache)
        except OSError:
            try:
                staging.rmdir()
            except OSError as error:
                raise StageError("cleanup_failed", "Inspect the remaining cleanup directory",
                                 result={"remnant": str(staging)}) from error
            raise
        try:
            shutil.rmtree(staging)
        except OSError as error:
            raise StageError("cleanup_failed", "Inspect the remaining cleanup directory",
                             result={"remnant": str(staging)}) from error
        return {**result, "status": "cleaned", "logical_bytes": measured["logical_bytes"],
                "volume": volume_usage(project)}
    except (OSError, ValueError) as error:
        raise StageError("unsafe_cache", "Could not safely access the configured checkout or cache") from error
