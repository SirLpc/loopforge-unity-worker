"""Measure logical file sizes without descending through filesystem links."""

import os
import shutil
import stat
from collections import Counter
from pathlib import Path
from typing import Literal


CacheName = Literal["Library", "Temp", "Obj"]


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


def measure_directory(root: Path, groups: dict[str, Path]) -> dict:
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
        read_errors = skipped_links = 0
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except OSError:
                        read_errors += 1
                        continue
                    if is_link(metadata):
                        skipped_links += 1
                        continue
                    path = Path(entry.path)
                    entry_bucket = group_paths.get(path, bucket)
                    if stat.S_ISDIR(metadata.st_mode):
                        children.append((path, entry_bucket, identity))
                    elif stat.S_ISREG(metadata.st_mode):
                        sizes[entry_bucket] += metadata.st_size
        except OSError:
            read_errors += 1

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
