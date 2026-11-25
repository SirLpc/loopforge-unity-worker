"""Assign resident Unity checkouts without holding locks around tool execution."""

import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

from process_runner import StageError
from task_store import now


class RequestConflict(Exception):
    pass


class PoolScheduler:
    def __init__(self, worker, unavailable):
        self.worker = worker
        self.store = worker.store
        self.config = worker.cfg
        self.unavailable = unavailable
        self.wake = threading.Event()
        self.editors = threading.Condition()
        self.thread = threading.Thread(target=self._serve, name="unity-workspace-scheduler", daemon=True)

    def start(self):
        self.thread.start()
        self.wake.set()

    def close(self):
        self.wake.set()
        if self.thread.ident is not None:
            self.thread.join()

    def _serve(self):
        while not self.worker.stopping:
            self.wake.wait(0.5)
            self.wake.clear()
            try:
                self.dispatch()
            except Exception:
                logging.getLogger(__name__).exception("Unity workspace dispatch failed")

    def slot_blocked(self, slot_id):
        return slot_id in self.unavailable or (
            slot_id == self.config.legacy_slot and self.worker.marker.exists())

    def _available(self, db, pool):
        return [slot.id for slot in pool.slots if slot.enabled and not self.slot_blocked(slot.id)
                and self.store.slot_lease(db, slot.id) is None]

    @staticmethod
    def _assign(task, job, slot_id):
        timestamp = now()
        job.update(status="running", started_at=timestamp)
        if task["status"] == "queued":
            task.update(status="pending", failure_kind=None, error_message=None)
        task.update(slot_id=slot_id, job_id=job["job_id"], lease=True, cancel_requested=False,
                    started_at=task["started_at"] or timestamp, finished_at=None)

    def admit(self, db, task, job, queue_wait_seconds):
        pool = self.config.pools.get(task["pool_id"])
        if pool is None:
            raise RequestConflict("The task's workspace pool is no longer configured")
        available = self._available(db, pool)
        if available and not self.store.queued_jobs(db, pool.id):
            self._assign(task, job, available[0])
            return True
        if queue_wait_seconds is None:
            raise RequestConflict("Worker busy or requires recovery")
        queued_at = now()
        deadline = datetime.fromisoformat(queued_at) + timedelta(seconds=queue_wait_seconds)
        job.update(status="queued", queued_at=queued_at, queue_deadline_at=deadline.isoformat(timespec="milliseconds"),
                   started_at=None)
        task.update(status="queued", job_id=job["job_id"], finished_at=None, cancel_requested=False,
                    failure_kind=None, error_message=None)
        return False

    def dispatch(self):
        with self.worker.guard:
            if self.worker.stopping:
                return
            launches = []
            with self.store.connect(write=True) as db:
                timestamp = now()
                for pool in self.config.pools.values():
                    queued = self.store.queued_jobs(db, pool.id)
                    if not queued:
                        continue
                    available = iter(self._available(db, pool))
                    for job in queued:
                        task = self.store.task(db, job["task_id"])
                        if job["queue_deadline_at"] <= timestamp:
                            message = "The job expired while waiting for a Unity workspace"
                            job.update(status="failed", finished_at=timestamp, failure_kind="queue_timeout",
                                       error_message=message)
                            task.update(status="failed", finished_at=timestamp, failure_kind="queue_timeout",
                                        error_message=message)
                        elif slot_id := next(available, None):
                            self._assign(task, job, slot_id)
                            launches.append(job)
                        else:
                            continue
                        self.store.save_task(db, task)
                        self.store.save_job(db, job)
            for job in launches:
                self.worker.spawn(job, {})

    @contextmanager
    def unity_permit(self, ctx):
        job_id = ctx.job["job_id"]
        with self.editors:
            while True:
                ctx.check_cancelled()
                with self.store.connect(write=True) as db:
                    if self.store.take_unity_permit(db, job_id, self.config.max_parallel_unity):
                        break
                self.editors.wait(0.25)
        unverified = False
        try:
            yield
        except StageError as error:
            unverified = error.kind == "recovery_required"
            raise
        except Exception as error:
            unverified = True
            raise StageError("recovery_required", "Could not verify Unity process exit") from error
        finally:
            if not unverified:
                try:
                    with self.store.connect(write=True) as db:
                        self.store.give_back_unity_permit(db, job_id)
                except Exception as error:
                    raise StageError("recovery_required", "Could not record release of the Unity permit") from error
                self.wake_editors()

    def wake_editors(self):
        with self.editors:
            self.editors.notify_all()

    def pool_status(self, db, pool):
        slots = []
        for slot in pool.slots:
            task = self.store.slot_lease(db, slot.id)
            if (task and task["status"] == "recovery_required") or (
                    slot.id == self.config.legacy_slot and self.worker.marker.exists()):
                status = "recovery_required"
            elif slot.id in self.unavailable:
                status = "unavailable"
            elif task:
                status = "leased"
            elif not slot.enabled:
                status = "draining"
            else:
                status = "idle"
            slots.append({"id": slot.id, "status": status,
                          "task_id": task["task_id"] if task else None,
                          "current_stage": task["current_stage"] if task else None})
        return {"id": pool.id, "queued_count": len(self.store.queued_jobs(db, pool.id)), "slots": slots}
