"""Task-local artifact names and byte-offset log reads."""

import codecs
import re

from process_runner import StageError


SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}")


def safe_child(directory, name):
    if not SAFE_NAME.fullmatch(name) or name.endswith("."):
        raise ValueError("Invalid artifact name")
    path = directory / name
    if path.is_symlink() or path.resolve() != path:
        raise ValueError("Artifact path escapes its task directory")
    return path


def task_directory(root, task_id):
    return safe_child(safe_child(root, "tasks"), task_id)


def redact_text(text, secrets):
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    return text


def log_page(path, offset, limit, secrets, *, live=False, tail=False, binary=False):
    needles = sorted((value.encode("utf-8") for value in secrets if value), key=len, reverse=True)
    overlap = max((len(value) - 1 for value in needles), default=0)
    try:
        with path.open("rb") as file:
            size = file.seek(0, 2)
            offset = max(0, size - limit) if tail else min(offset, size)
            end = min(size, offset + limit)
            start = max(0, offset - overlap)
            file.seek(start)
            data = file.read(min(size, end + overlap) - start)
    except FileNotFoundError:
        return {"offset": offset, "next_offset": offset, "eof": not live, "text": b"" if binary else ""}
    if live and start + len(data) == size:
        for needle in needles:
            for length in range(min(len(data), len(needle) - 1), 0, -1):
                if data.endswith(needle[:length]):
                    end = min(end, size - length)
                    break
    end = max(end, offset)
    for needle in needles:
        data = data.replace(needle, b"*" * len(needle))
    chunk = data[offset - start:end - start]
    if binary:
        content = chunk
    else:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        content = decoder.decode(chunk, final=not live and end == size)
        end -= len(decoder.getstate()[0])
    return {"offset": offset, "next_offset": end, "eof": not live and end == size, "text": content}


def artifact_bytes(path, secrets):
    offset = 0
    while True:
        page = log_page(path, offset, 65_536, secrets, binary=True)
        if page["text"]:
            yield page["text"]
        if page["eof"]:
            return
        offset = page["next_offset"]


class RunContext:
    def __init__(self, settings, runner, task, job, cancel, save):
        self.settings, self.runner, self.task, self.job = settings, runner, task, job
        self.cancel, self.save = cancel, save
        self.directory = task_directory(settings.logs, task["task_id"])
        self.directory.mkdir(parents=True, exist_ok=True)
        self.sequence = 0

    def check_cancelled(self):
        if self.cancel.is_set():
            raise StageError("cancelled", "Cancellation requested")

    def prefix(self, label):
        self.sequence += 1
        return safe_child(self.directory, f"{self.job['job_id']}-{self.sequence:03d}-{label}")

    def record(self, path, kind):
        safe_child(self.directory, path.name)
        self.save(artifacts=[*self.task["artifacts"], {"name": path.name, "kind": kind, "job_id": self.job["job_id"]}])

    def artifact(self, label, suffix, kind):
        prefix = self.prefix(label)
        path = safe_child(self.directory, prefix.name + suffix)
        self.record(path, kind)
        return path

    def run(self, argv, label, *, cwd=None, publish=False, **kwargs):
        self.check_cancelled()
        prefix = self.prefix(label)
        if publish:
            for suffix in (".stdout.log", ".stderr.log"):
                self.record(prefix.with_name(prefix.name + suffix), "log")
        return self.runner.run(argv, cwd or self.settings.project, prefix, cancel=self.cancel, **kwargs)
