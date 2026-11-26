import pytest

from threadwright.vm import WorkerVm


class FakeInstance:
    def __init__(self, *statuses: str) -> None:
        self.statuses = list(statuses)
        self.calls: list[str] = []

    def status(self) -> str:
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")

    def suspend(self) -> None:
        self.calls.append("suspend")

    def resume(self) -> None:
        self.calls.append("resume")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(instance: FakeInstance, action: str = "stop") -> tuple[WorkerVm, Clock]:
    clock = Clock()
    return WorkerVm(instance, 900, action, 7200, clock=clock, sleep=lambda _: None), clock


@pytest.mark.parametrize(
    ("statuses", "woke", "calls"),
    [
        (("RUNNING",), False, []),
        (("TERMINATED",), True, ["start"]),
        (("SUSPENDED",), True, ["resume"]),
        (("STAGING",), True, []),
        (("STOPPING", "STOPPING", "TERMINATED"), True, ["start"]),
    ],
)
def test_wake(statuses: tuple[str, ...], woke: bool, calls: list[str]) -> None:
    instance = FakeInstance(*statuses)
    vm, _ = make(instance)
    assert vm.wake() is woke
    assert instance.calls == calls


def test_wake_gives_up_when_the_worker_never_settles() -> None:
    vm, _ = make(FakeInstance("STOPPING"))
    with pytest.raises(RuntimeError, match="settle"):
        vm.wake()


def test_idle_worker_is_stopped_only_after_the_idle_window() -> None:
    instance = FakeInstance("RUNNING")
    vm, clock = make(instance)
    vm.heartbeat(0)
    clock.now += 899
    vm.tick()
    assert instance.calls == []
    clock.now += 2
    vm.tick()
    assert instance.calls == ["stop"]


def test_busy_worker_is_never_stopped() -> None:
    instance = FakeInstance("RUNNING")
    vm, clock = make(instance)
    vm.heartbeat(1)
    clock.now += 7000
    vm.tick()
    assert instance.calls == []


def test_heartbeat_keeps_long_tasks_alive_and_suspend_is_honoured() -> None:
    instance = FakeInstance("RUNNING")
    vm, clock = make(instance, "suspend")
    vm.heartbeat(1)
    clock.now += 600
    vm.heartbeat(0)
    clock.now += 600
    vm.tick()
    assert instance.calls == []
    clock.now += 301
    vm.tick()
    assert instance.calls == ["suspend"]


def test_a_worker_stuck_busy_is_stopped_after_the_cap() -> None:
    instance = FakeInstance("RUNNING")
    vm, clock = make(instance)
    vm.heartbeat(1)
    clock.now += 7000
    vm.heartbeat(1)
    vm.tick()
    assert instance.calls == []
    clock.now += 201
    vm.tick()
    assert instance.calls == ["stop"]


def test_a_task_arriving_during_the_idle_check_cancels_the_stop() -> None:
    instance = FakeInstance("RUNNING")
    vm, clock = make(instance)

    class TouchOnEnter:
        def __enter__(self) -> None:
            vm.touch()

        def __exit__(self, *_: object) -> None:
            return None

    vm._lock = TouchOnEnter()  # type: ignore[assignment]
    clock.now += 1000
    vm.tick()
    assert instance.calls == []


def test_a_manually_started_worker_gets_a_full_idle_window() -> None:
    instance = FakeInstance("RUNNING")
    vm, clock = make(instance)
    vm.heartbeat(0)
    clock.now += 5000
    vm.heartbeat(0)
    clock.now += 60
    vm.tick()
    assert instance.calls == []
    clock.now += 840
    vm.tick()
    assert instance.calls == ["stop"]


def test_regular_idle_heartbeats_do_not_keep_it_awake() -> None:
    instance = FakeInstance("RUNNING")
    vm, clock = make(instance)
    vm.heartbeat(0)
    for _ in range(16):
        clock.now += 60
        vm.heartbeat(0)
    vm.tick()
    assert instance.calls == ["stop"]


def test_a_start_seen_before_any_heartbeat_gets_a_full_window() -> None:
    instance = FakeInstance("TERMINATED")
    vm, clock = make(instance)
    clock.now += 5000
    vm.tick()
    instance.statuses = ["RUNNING"]
    clock.now += 30
    vm.tick()
    assert instance.calls == []
    clock.now += 899
    vm.tick()
    assert instance.calls == []
    clock.now += 2
    vm.tick()
    assert instance.calls == ["stop"]


def test_waking_does_not_double_the_idle_window() -> None:
    instance = FakeInstance("TERMINATED", "RUNNING")
    vm, clock = make(instance)
    vm.wake()
    clock.now += 901
    vm.tick()
    assert instance.calls == ["start", "stop"]


def test_hold_keeps_an_idle_worker_awake_until_it_expires() -> None:
    instance = FakeInstance("RUNNING")
    vm, clock = make(instance)
    vm.heartbeat(0)
    vm.hold(3600)
    clock.now += 3599
    vm.tick()
    assert instance.calls == []
    clock.now += 2
    vm.tick()
    assert instance.calls == ["stop"]


def test_release_gives_back_the_normal_idle_window() -> None:
    instance = FakeInstance("RUNNING")
    vm, clock = make(instance)
    vm.heartbeat(0)
    vm.hold(3600)
    clock.now += 600
    vm.release()
    clock.now += 899
    vm.tick()
    assert instance.calls == []
    clock.now += 2
    vm.tick()
    assert instance.calls == ["stop"]
