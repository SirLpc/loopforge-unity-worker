import shutil
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


@contextmanager
def test_directory():
    workspace = Path.cwd().resolve()
    directory = workspace / f".tmp-test-{uuid4().hex}"
    directory.mkdir()
    try:
        yield directory
    finally:
        if directory.resolve().parent == workspace:
            def remove_read_only(operation, path, _):
                Path(path).chmod(stat.S_IWRITE)
                operation(path)

            shutil.rmtree(directory, onerror=remove_read_only)
