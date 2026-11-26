"""Measure logical file sizes without descending through filesystem links."""

import os
import shutil
import stat
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


def measure_directory(root: Path, groups: dict[str, Path]) -> dict:
    root = root.absolute()
    buckets = dict.fromkeys((*groups, "other"), 0)
    group_paths = {root / path: name for name, path in groups.items()}
    result = {"path": str(root), "exists": True, "complete": False, "logical_bytes": 0,
              "buckets": buckets, "skipped_links": 0, "read_errors": 0}
    for path in (*reversed(root.parents), root):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return {**result, "exists": False, "complete": True, "logical_bytes": None}
        except OSError:
            return {**result, "read_errors": 1}
        if is_link(metadata):
            return {**result, "skipped_links": 1}
        if not stat.S_ISDIR(metadata.st_mode):
            return {**result, "read_errors": 1}

    pending = [(root, group_paths.get(root, "other"))]
    while pending:
        directory, bucket = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except OSError:
                        result["read_errors"] += 1
                        continue
                    if is_link(metadata):
                        result["skipped_links"] += 1
                        continue
                    path = Path(entry.path)
                    entry_bucket = group_paths.get(path, bucket)
                    if stat.S_ISDIR(metadata.st_mode):
                        pending.append((path, entry_bucket))
                    elif stat.S_ISREG(metadata.st_mode):
                        buckets[entry_bucket] += metadata.st_size
        except OSError:
            result["read_errors"] += 1

    result["logical_bytes"] = sum(buckets.values())
    result["complete"] = not (result["skipped_links"] or result["read_errors"])
    return result
