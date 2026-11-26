import logging
import threading
import time
from collections.abc import Callable
from typing import Protocol

log = logging.getLogger(__name__)

_SETTLING = {"STOPPING", "SUSPENDING"}
_UP = {"RUNNING", "PROVISIONING", "STAGING", "REPAIRING"}
_OPERATION_TIMEOUT_S = 300
_BOOT_GAP_S = 120


class Instance(Protocol):
    def status(self) -> str: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def suspend(self) -> None: ...
    def resume(self) -> None: ...


class GceInstance:
    def __init__(self, project: str, zone: str, name: str) -> None:
        from google.cloud import compute_v1

        self._client = compute_v1.InstancesClient()
        self._project, self._zone, self._name = project, zone, name

    def status(self) -> str:
        return str(self._client.get(project=self._project, zone=self._zone, instance=self._name).status)

    def start(self) -> None:
        self._client.start(project=self._project, zone=self._zone, instance=self._name).result(
            timeout=_OPERATION_TIMEOUT_S)

    def stop(self) -> None:
        self._client.stop(project=self._project, zone=self._zone, instance=self._name).result(
            timeout=_OPERATION_TIMEOUT_S)

    def suspend(self) -> None:
        self._client.suspend(project=self._project, zone=self._zone, instance=self._name).result(
            timeout=_OPERATION_TIMEOUT_S)

    def resume(self) -> None:
        self._client.resume(project=self._project, zone=self._zone, instance=self._name).result(
            timeout=_OPERATION_TIMEOUT_S)


class WorkerVm:
    def __init__(self, instance: Instance, idle_stop_s: int, idle_action: str, max_busy_s: int,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self.instance = instance
        self.idle_stop_s = idle_stop_s
        self.idle_action = idle_action
        self.max_busy_s = max_busy_s
        self.clock = clock
        self.sleep = sleep
        self.busy = 0
        self.last_active = clock()
        self.busy_since: float | None = None
        self.last_heartbeat: float | None = None
        self.seen_running = False
        self.hold_until: float | None = None
        self._lock = threading.Lock()

    def touch(self) -> None:
        self.last_active = self.clock()

    def heartbeat(self, busy: int) -> None:
        now = self.clock()
        just_booted = self.last_heartbeat is None or now - self.last_heartbeat > _BOOT_GAP_S
        self.last_heartbeat = now
        self.seen_running = True
        if busy or self.busy or just_booted:
            self.touch()
        if busy and self.busy_since is None:
            self.busy_since = self.clock()
        if not busy:
            self.busy_since = None
        self.busy = busy

    def wake(self) -> bool:
        self.touch()
        with self._lock:
            self.seen_running = True
            for _ in range(60):
                status = self.instance.status()
                if status in _UP:
                    return status != "RUNNING"
                if status == "SUSPENDED":
                    self.instance.resume()
                    return True
                if status in ("TERMINATED", "STOPPED"):
                    self.instance.start()
                    return True
                if status not in _SETTLING:
                    raise RuntimeError(f"worker is {status}")
                self.sleep(5)
            raise RuntimeError("worker did not settle within 5 minutes")

    def hold(self, seconds: int) -> None:
        self.hold_until = self.clock() + seconds
        self.touch()

    def release(self) -> None:
        self.hold_until = None
        self.touch()

    def held(self) -> bool:
        return self.hold_until is not None and self.clock() < self.hold_until

    def _should_sleep(self) -> bool:
        if self.held():
            return False
        now = self.clock()
        if self.busy:
            return self.busy_since is not None and now - self.busy_since > self.max_busy_s
        return now - self.last_active >= self.idle_stop_s

    def tick(self) -> None:
        if not self._should_sleep():
            return
        with self._lock:
            if not self._should_sleep():
                return
            running = self.instance.status() == "RUNNING"
            if not running or not self.seen_running:
                self.seen_running = running
                if running:
                    self.touch()
                return
            log.warning("putting the worker to sleep (busy=%s)", self.busy)
            if self.idle_action == "suspend":
                self.instance.suspend()
            else:
                self.instance.stop()
            self.busy = 0
            self.busy_since = None
            self.seen_running = False
            self.touch()
