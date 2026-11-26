"""Run one Worker process and report when the launcher should replace it."""

import _winapi
import sys
from ctypes import wintypes

import uvicorn

from process_runner import checked, win_function
from worker import create_app


open_event = win_function("OpenEventW", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)


class WorkerServer(uvicorn.Server):
    def __init__(self, app, host, port, restart_event):
        super().__init__(uvicorn.Config(app, host=host, port=port, workers=1, proxy_headers=False))
        self.app = app
        self.restart_event = restart_event
        self.restart_pending = False
        self.restarting = False

    async def on_tick(self, counter):
        if await super().on_tick(counter):
            return True
        if not self.restart_pending:
            self.restart_pending = (
                _winapi.WaitForSingleObject(self.restart_event, 0) == _winapi.WAIT_OBJECT_0
            )
        if self.restart_pending:
            self.restarting = self.app.state.worker.shutdown_when_idle()
        return self.restarting


def main():
    host, port, event_name = sys.argv[1:]
    restart_event = checked(open_event(_winapi.SYNCHRONIZE, False, event_name))
    try:
        server = WorkerServer(create_app(), host, int(port), restart_event)
        server.run()
    finally:
        _winapi.CloseHandle(restart_event)
    if server.restarting:
        return 75
    return 0 if server.started else 3


if __name__ == "__main__":
    sys.exit(main())
