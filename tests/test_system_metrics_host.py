"""Which machine the metrics describe, and the two readings a container gets wrong.

The bug that prompted this: a status screen on a 16 GB Windows laptop reported
7.7 GB, and the reasonable conclusion was "the metric is broken". It was not —
it was measuring the WSL2 virtual machine correctly, and saying nothing about
which machine that was.

Three things are pinned here.

**The reading is labelled.** ``machine.kind`` distinguishes a real host, a
container on Linux (whose ``/proc`` *is* the host's, because ``/proc/meminfo``
and ``/proc/stat`` are not namespaced) and the virtual machine Docker Desktop
runs on Windows (which no mount can see past).

**A cgroup ceiling wins over the kernel's total.** ``/proc/meminfo`` knows
nothing about cgroups, so a container capped at 4 GB on a 64 GB host reports
64 GB and looks healthy right up to the moment the kernel kills it.

**Nothing new is invented either.** Temperatures and SMART wear are absent on
most machines — no kernel modules, no device access — and both must say so with
a reason rather than report a zero.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

from ai_marketplace_monitor import control, system_metrics


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    control.reset_for_tests()
    system_metrics._smart_available = None
    system_metrics._disk_health_sample = {}
    system_metrics._disk_health_at = 0.0
    yield
    system_metrics.stop()
    system_metrics._smart_available = None
    system_metrics._disk_health_sample = {}
    system_metrics._disk_health_at = 0.0
    control.reset_for_tests()


# --------------------------------------------------------------------------- #
# Which machine
# --------------------------------------------------------------------------- #


def test_a_plain_machine_is_reported_as_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system_metrics, "_in_container", lambda: False)
    assert system_metrics._host()["kind"] == "host"


def test_a_linux_container_is_named_as_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Its numbers are the host's, but the reader deserves to know where from."""
    monkeypatch.setattr(system_metrics, "_in_container", lambda: True)
    monkeypatch.setattr(system_metrics, "_under_wsl", lambda: False)
    assert system_metrics._host()["kind"] == "container"


def test_docker_desktop_on_windows_is_named_a_vm(monkeypatch: pytest.MonkeyPatch) -> None:
    """The case that started this, and the one that cannot be fixed by mounting.

    The kernel belongs to the WSL2 virtual machine; Windows is on the other side
    of a hypervisor with no ``/proc`` to read. Saying so is the whole remedy.
    """
    monkeypatch.setattr(system_metrics, "_in_container", lambda: True)
    monkeypatch.setattr(system_metrics, "_under_wsl", lambda: True)
    assert system_metrics._host()["kind"] == "vm"


# --------------------------------------------------------------------------- #
# The cgroup ceiling
# --------------------------------------------------------------------------- #


def with_cgroup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contents: str, *, v1: bool = False
) -> None:
    """Point the reader at a cgroup file this test wrote."""
    path = tmp_path / ("memory.limit_in_bytes" if v1 else "memory.max")
    path.write_text(contents, encoding="utf-8")
    unlimited = frozenset() if v1 else frozenset({"max"})
    monkeypatch.setattr(system_metrics, "CGROUP_MEMORY_FILES", ((str(path), unlimited),))


def test_a_v2_cap_is_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with_cgroup(monkeypatch, tmp_path, "4294967296")
    assert system_metrics._cgroup_memory_limit() == 4294967296


def test_v2_says_max_for_no_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`max` must not become a number, and there is no number it could become."""
    with_cgroup(monkeypatch, tmp_path, "max")
    assert system_metrics._cgroup_memory_limit() is None


def test_the_v1_sentinel_is_not_a_machine_with_exabytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cgroup v1 has no word for "unlimited" and writes a number near 2**63.

    Taken literally that is a machine with eight exabytes of RAM, and the panel
    would cheerfully draw a bar at 0%.
    """
    with_cgroup(monkeypatch, tmp_path, "9223372036854771712", v1=True)
    assert system_metrics._cgroup_memory_limit() is None


def test_an_unreadable_cgroup_file_is_not_a_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Not every machine has cgroups; absence is "no cap", not an error."""
    monkeypatch.setattr(
        system_metrics, "CGROUP_MEMORY_FILES", ((str(tmp_path / "nope"), frozenset()),)
    )
    assert system_metrics._cgroup_memory_limit() is None


def test_nonsense_in_the_file_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with_cgroup(monkeypatch, tmp_path, "not a number")
    assert system_metrics._cgroup_memory_limit() is None


def test_a_cap_replaces_the_total(monkeypatch: pytest.MonkeyPatch) -> None:
    """The number that decides whether the process lives is the one shown.

    Without this the panel reports the host's 64 GB and looks fine until the
    kernel kills the container.
    """
    psutil = pytest.importorskip("psutil")
    monkeypatch.setattr(system_metrics, "_cgroup_memory_limit", lambda: 4 * 1024**3)
    reading = system_metrics._memory(psutil)
    assert reading["available"] is True
    assert reading["total"] == 4 * 1024**3
    assert reading["limited_by"] == "cgroup"
    # And the machine's real size is still carried, so the interface can say
    # "4 GB of the 16 this machine has" rather than implying the box is small.
    assert reading["machine_total"] > reading["total"]


def test_no_cap_leaves_the_reading_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    psutil = pytest.importorskip("psutil")
    monkeypatch.setattr(system_metrics, "_cgroup_memory_limit", lambda: None)
    reading = system_metrics._memory(psutil)
    assert reading["limited_by"] is None
    assert reading["total"] == reading["machine_total"]


def test_a_cap_larger_than_the_machine_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A limit above what exists is not a limit; showing it would inflate the box."""
    psutil = pytest.importorskip("psutil")
    monkeypatch.setattr(system_metrics, "_cgroup_memory_limit", lambda: 1 << 49)
    assert system_metrics._memory(psutil)["limited_by"] is None


# --------------------------------------------------------------------------- #
# The disk it measures
# --------------------------------------------------------------------------- #


def test_the_host_root_is_measured_when_it_was_mounted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With the host's filesystem mounted in, that is what gets measured.

    Inside a container the alternative is its own overlay, which on Docker
    Desktop is a sparse virtual disk advertising a terabyte it does not have —
    it reports free space right up until the real disk fills.
    """
    monkeypatch.setenv(system_metrics.HOST_ROOT_ENV, str(tmp_path))
    reading = system_metrics._disk()
    assert reading["available"] is True
    assert reading["measures"] == "host"
    assert reading["path"] == str(tmp_path)


def test_without_the_mount_it_measures_the_data_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(system_metrics.HOST_ROOT_ENV, raising=False)
    reading = system_metrics._disk()
    assert reading["measures"] == "data"


def test_an_empty_variable_is_not_a_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A compose file that sets the variable to nothing must not break the reading."""
    monkeypatch.setenv(system_metrics.HOST_ROOT_ENV, "   ")
    assert system_metrics._disk()["measures"] == "data"


# --------------------------------------------------------------------------- #
# Temperatures
# --------------------------------------------------------------------------- #


class Entry:
    def __init__(
        self, label: str, current: float, high: Any = None, critical: Any = None
    ) -> None:
        self.label = label
        self.current = current
        self.high = high
        self.critical = critical


def fake_psutil(readings: Dict[str, List[Entry]]) -> Any:
    class Fake:
        @staticmethod
        def sensors_temperatures() -> Dict[str, List[Entry]]:
            return readings

    return Fake()


def test_a_driver_name_becomes_a_component_name() -> None:
    """`k10temp` is every AMD CPU since Family 10h and tells a person nothing."""
    reading = system_metrics._temperatures(
        fake_psutil({"k10temp": [Entry("Tctl", 61.0, high=85.0)]})
    )
    assert reading["available"] is True
    assert reading["sensors"][0]["component"] == "CPU"
    assert reading["sensors"][0]["chip"] == "k10temp"
    assert reading["sensors"][0]["celsius"] == 61.0
    assert reading["sensors"][0]["high"] == 85.0


def test_an_unknown_chip_keeps_its_own_name() -> None:
    """Better the driver's name than a guess about what part it is."""
    reading = system_metrics._temperatures(fake_psutil({"weird_chip": [Entry("t", 40.0)]}))
    assert reading["sensors"][0]["component"] == "weird_chip"


def test_a_missing_threshold_stays_missing() -> None:
    """What counts as hot is a property of the part; inventing one is worse than none."""
    reading = system_metrics._temperatures(fake_psutil({"nvme": [Entry("Composite", 44.0)]}))
    assert reading["sensors"][0]["high"] is None
    assert reading["sensors"][0]["critical"] is None


def test_a_machine_with_no_sensors_says_why() -> None:
    """Most machines. Needs the kernel modules loaded, which is worth saying."""
    reading = system_metrics._temperatures(fake_psutil({}))
    assert reading["available"] is False
    assert "kernel" in reading["reason"]


def test_no_psutil_is_reported_rather_than_raised() -> None:
    assert system_metrics._temperatures(None)["available"] is False


# --------------------------------------------------------------------------- #
# Storage life
# --------------------------------------------------------------------------- #


def test_smart_is_unavailable_without_the_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system_metrics.shutil, "which", lambda name: None)
    reading = system_metrics._read_disk_health()
    assert reading["available"] is False
    assert "smartctl" in reading["reason"]


def test_no_device_access_says_what_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The message names the compose change, because that is the whole remedy."""
    monkeypatch.setattr(system_metrics.shutil, "which", lambda name: "/usr/sbin/smartctl")
    monkeypatch.setattr(system_metrics, "_smart_devices", lambda binary: None)
    reading = system_metrics._read_disk_health()
    assert reading["available"] is False
    assert "SYS_RAWIO" in reading["reason"]


def test_it_stops_asking_once_it_cannot(monkeypatch: pytest.MonkeyPatch) -> None:
    """A drive that cannot be read will not become readable; asking wakes disks."""
    monkeypatch.setattr(system_metrics.shutil, "which", lambda name: None)
    system_metrics._read_disk_health()

    def explode(name: str) -> str:
        raise AssertionError("asked again after learning it cannot")

    monkeypatch.setattr(system_metrics.shutil, "which", explode)
    assert system_metrics._read_disk_health()["available"] is False


def test_an_nvme_wear_figure_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system_metrics.shutil, "which", lambda name: "/usr/sbin/smartctl")
    monkeypatch.setattr(system_metrics, "_smart_devices", lambda binary: ["/dev/nvme0"])
    monkeypatch.setattr(
        system_metrics,
        "_smart_drive",
        lambda binary, device: {
            "device": device,
            "model": "Samsung 980",
            "passed": True,
            "life_used": 7.0,
            "spare_available": 100.0,
            "power_on_hours": 9000,
            "celsius": 41.0,
        },
    )
    reading = system_metrics._read_disk_health()
    assert reading["available"] is True
    assert reading["drives"][0]["life_used"] == 7.0


def test_a_spinning_disk_has_no_wear_and_is_not_thereby_broken() -> None:
    """`life_used` of None sits beside a perfectly healthy drive."""
    payload: Dict[str, Any] = {
        "model_name": "WD Red",
        "smart_status": {"passed": True},
        "power_on_time": {"hours": 40000},
    }
    assert system_metrics._sata_life_used(payload) is None


def test_a_countdown_attribute_is_turned_into_wear_used() -> None:
    """Vendors publish life *remaining*; one number on screen must mean one thing."""
    payload = {
        "ata_smart_attributes": {
            "table": [{"name": "Wear_Leveling_Count", "value": 88}],
        }
    }
    assert system_metrics._sata_life_used(payload) == 12.0


def test_a_used_attribute_is_taken_as_it_is() -> None:
    payload = {
        "ata_smart_attributes": {"table": [{"name": "Percent_Lifetime_Used", "value": 12}]}
    }
    assert system_metrics._sata_life_used(payload) == 12.0


# --------------------------------------------------------------------------- #
# The whole sample
# --------------------------------------------------------------------------- #


def test_the_new_readings_are_on_the_snapshot() -> None:
    import time

    system_metrics.start()
    deadline = time.time() + 15
    snapshot: Dict[str, Any] = {}
    while time.time() < deadline:
        snapshot = system_metrics.snapshot()
        if not snapshot.get("pending"):
            break
        time.sleep(0.2)
    assert not snapshot.get("pending"), "the sampler produced nothing"
    for key in ("machine", "temperatures", "disk_health"):
        assert key in snapshot, key
    assert snapshot["machine"]["kind"] in ("host", "container", "vm")
    for key in ("temperatures", "disk_health"):
        assert "available" in snapshot[key]
        if not snapshot[key]["available"]:
            assert snapshot[key]["reason"]


def test_which_machine_is_answered_before_the_first_sample() -> None:
    """It needs no measurement, and the first seconds are when it is asked.

    Somebody looking at an empty panel is exactly the person wondering what it
    is about to describe.
    """
    system_metrics.stop()
    system_metrics._sample.clear()
    snapshot = system_metrics.snapshot()
    if snapshot.get("pending"):
        assert snapshot["machine"]["kind"] in ("host", "container", "vm")

