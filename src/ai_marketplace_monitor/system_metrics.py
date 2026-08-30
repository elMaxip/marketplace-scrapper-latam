"""What the machine running the scraper is spending, sampled in the background.

The question this answers is "is the box that does the scraping in trouble?",
and it is worth answering because the scraper's cost is not its own process:
each browser is a Chromium with a dozen processes behind it, and the difference
between two lanes and four is measured in gigabytes rather than in anything the
log would mention.

Three rules shape the whole module.

**Nothing is measured on a request.**  A sampler thread takes a reading every
:data:`INTERVAL` seconds and the API hands out the last one.  So a screen
polling every four seconds costs a dictionary copy, twenty screens cost twenty
dictionary copies, and nothing the scraping threads do ever waits on a metric.
It also makes the CPU figure *mean* something: ``cpu_percent`` reports the busy
fraction since it was last asked, so asking it on a fixed cadence gives "over
the last few seconds" and asking it per request would give "since whenever
somebody last opened this page".

**Nothing is invented.**  Every value is either a number that was read or the
string ``None`` with a reason beside it.  A GPU that cannot be queried reports
that it cannot be queried; it does not report zero, which is a number a user
would reasonably read as "idle".

**Nothing extra is opened.**  ``psutil`` reads the kernel's own counters, and
the one process this module does start -- ``nvidia-smi``, which is the only way
to ask an NVIDIA card anything without a driver binding -- runs on the sampler's
own slower cadence and is never started again once the machine has said it has
no such tool.

``psutil`` is a dependency and is nonetheless imported defensively: a container
built before it was added, or a platform it has no wheel for, should show the
metrics it can and say "no disponible" for the rest rather than take the status
screen down.

**In a container, "the machine" is whichever machine the kernel is.**  On Linux
that is the host and needs nothing: ``/proc/meminfo``, ``/proc/cpuinfo`` and
``/proc/stat`` are not namespaced, so a container already reads the host's real
memory and cores.  Two things are still wrong without help, and both are handled
here: the *disk* it measures is the container's own filesystem rather than the
volume the data lives on (``AIMM_HOST_ROOT``), and a **cgroup limit** is
invisible to ``/proc/meminfo`` -- a container capped at 4 GB on a 64 GB host
reports 64 GB and looks healthy right up to the moment the kernel kills it
(:func:`_cgroup_memory_limit`).

On Docker Desktop for Windows there is a second boundary and no way through it:
the kernel belongs to the WSL2 virtual machine, so every figure here describes
that VM and not Windows.  Mounting the host's ``/proc`` does not help, because
the host *is* the VM.  Saying so is the only honest option, and ``host.kind``
carries it.

The browser and tab counts are *not* here.  They are not a property of the
machine -- a Chrome the user has open is not the scraper's -- and the scraper
already knows exactly which browsers are its own, so they come from
:mod:`ai_marketplace_monitor.control`, where the threads that own those browsers
publish them.  :func:`snapshot` puts the two together.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

from . import control
from .utils import amm_home

logger = logging.getLogger(__name__)

#: Seconds between samples.
#:
#: Slower than the status screen polls (4-12s), and deliberately: the numbers
#: are a trend, not an instrument, and a five-second CPU average is a steadier
#: thing to look at than a one-second one.  It is also the window
#: ``cpu_percent`` reports over, so it is the cadence that gives the figure its
#: meaning.
INTERVAL = 5.0

#: Seconds between GPU samples.  Six times slower, because this one is the only
#: reading that costs a process.
GPU_INTERVAL = 30.0

#: How long ``nvidia-smi`` is given before it is treated as absent.
GPU_TIMEOUT = 4.0

#: Seconds between SMART reads, and how long ``smartctl`` is given.
#:
#: Far slower than everything else, and not only because it costs a process: a
#: wear figure moves over months.  Asking a drive for its SMART log also wakes a
#: spinning disk, so asking often would keep one awake for a number that has not
#: changed since the last time.
DISK_HEALTH_INTERVAL = 900.0
DISK_HEALTH_TIMEOUT = 10.0

#: Where the host's own filesystem is mounted, when the container was given it.
#:
#: Read from the environment rather than guessed, because guessing is how a
#: metric ends up describing something nobody asked about.  Empty (the default)
#: means "measure where the data actually lives", which is right outside a
#: container and honest inside one.
HOST_ROOT_ENV = "AIMM_HOST_ROOT"

#: Where a container's memory ceiling is written, newest layout first, with the
#: words each one uses for "no limit".
#:
#: A module constant rather than a literal inside the reader so a test can point
#: it at a file it wrote: the two cgroup layouts disagree about how to say
#: "uncapped", and that disagreement is exactly the part worth testing.
CGROUP_MEMORY_FILES = (
    ("/sys/fs/cgroup/memory.max", frozenset({"max"})),
    ("/sys/fs/cgroup/memory/memory.limit_in_bytes", frozenset()),
)

_lock = threading.Lock()
_sample: Dict[str, Any] = {}
_thread: Optional[threading.Thread] = None
_stop = threading.Event()

#: ``None`` before the first attempt, then True/False for good.  A machine with
#: no NVIDIA tooling is asked once in the life of the process.
_gpu_available: Optional[bool] = None
_gpu_sample: Dict[str, Any] = {}
_gpu_at: float = 0.0

#: The same arrangement for SMART: asked once, then remembered.
_smart_available: Optional[bool] = None
_disk_health_sample: Dict[str, Any] = {}
_disk_health_at: float = 0.0


def _psutil() -> Any:
    """``psutil`` if it is installed, else ``None``."""
    try:
        import psutil  # type: ignore

        return psutil
    except Exception:  # pragma: no cover - only on an install without it
        return None


def _unavailable(why: str) -> Dict[str, Any]:
    """The shape every metric falls back to.

    ``available`` rather than a bare ``None`` value, and a reason with it: an
    interface can then say *why* a number is missing, which is the difference
    between "this machine has no GPU" and "something is broken".
    """
    return {"available": False, "reason": why}


# --------------------------------------------------------------------------- #
# The individual readings
# --------------------------------------------------------------------------- #


def _cpu(psutil: Any) -> Dict[str, Any]:
    """System-wide CPU use since the previous sample.

    The whole machine, not this process: the browsers are separate processes
    and they are most of the cost, so a per-process figure would report a busy
    machine as idle.  ``process_percent`` is carried alongside for the one
    question the total cannot answer -- how much of it is us.
    """
    if psutil is None:
        return _unavailable("psutil no está instalado")
    try:
        percent = psutil.cpu_percent(interval=None)
        return {
            "available": True,
            "percent": round(float(percent), 1),
            "cores": psutil.cpu_count(logical=True),
            "physical_cores": psutil.cpu_count(logical=False),
            "load_average": _load_average(psutil),
        }
    except Exception as error:  # pragma: no cover - platform-specific
        return _unavailable(f"no se pudo leer la CPU: {error}")


def _load_average(psutil: Any) -> Optional[List[float]]:
    """1/5/15-minute load, where the platform has such a thing.

    Windows has no load average; psutil emulates one, and an emulated number
    presented as a measured one is exactly what this module is trying not to
    do, so it is only reported where it is real.
    """
    if platform.system() == "Windows":
        return None
    try:
        return [round(value, 2) for value in psutil.getloadavg()]
    except Exception:
        return None


def _memory(psutil: Any) -> Dict[str, Any]:
    """System RAM, and this process's resident share of it."""
    if psutil is None:
        return _unavailable("psutil no está instalado")
    try:
        memory = psutil.virtual_memory()
        total = int(memory.total)
        used = int(total - memory.available)
        limit = _cgroup_memory_limit()
        # A cgroup ceiling below what the kernel reports is the number that
        # actually decides whether this process lives, so it replaces the
        # total -- and is named, because "8 GB" meaning "the host has 64 and we
        # may use 8" is a different sentence to "the machine has 8".
        if limit is not None and limit < total:
            return {
                "available": True,
                "percent": round(used / limit * 100, 1) if limit else 0.0,
                "total": limit,
                "used": min(used, limit),
                "free": max(0, limit - used),
                "limited_by": "cgroup",
                "machine_total": total,
            }
        return {
            "available": True,
            "percent": round(float(memory.percent), 1),
            "total": total,
            "used": used,
            "free": int(memory.available),
            "limited_by": None,
            "machine_total": total,
        }
    except Exception as error:  # pragma: no cover - platform-specific
        return _unavailable(f"no se pudo leer la memoria: {error}")


def _process(psutil: Any, handle: Any) -> Dict[str, Any]:
    """The monitor's own process: how much of the machine is this program.

    Its own children are not counted, and that is on purpose: a browser is a
    Chromium tree of a dozen processes and walking it costs more than the
    figure is worth, while the totals above already include them.  What this
    answers is narrower and still useful -- whether the Python process itself
    is the thing growing.
    """
    if psutil is None or handle is None:
        return _unavailable("psutil no está instalado")
    try:
        with handle.oneshot():
            return {
                "available": True,
                "cpu_percent": round(float(handle.cpu_percent(interval=None)), 1),
                "memory": int(handle.memory_info().rss),
                "threads": handle.num_threads(),
                "pid": handle.pid,
            }
    except Exception as error:  # pragma: no cover - the process cannot see itself
        return _unavailable(f"no se pudo leer el proceso: {error}")


def _disk() -> Dict[str, Any]:
    """Space on the volume holding the monitor's data.

    That volume rather than every mount point: the cache of listings, the
    browser profiles and the config all live under ``~/.ai-marketplace-monitor``,
    and it is the one whose filling up stops the scraper.  ``shutil`` reads it,
    so this is the one metric that works with nothing installed at all.
    """
    # The host's own filesystem when the container was given it, and the data
    # directory otherwise.  Inside a container the second is the overlay -- on
    # Docker Desktop that is a sparse virtual disk advertising a terabyte it
    # does not have, which is the most misleading number this module can print:
    # it says there is room right up until the *host* fills up.
    target = os.environ.get(HOST_ROOT_ENV, "").strip() or str(amm_home)
    try:
        usage = shutil.disk_usage(target)
        percent = (usage.used / usage.total * 100) if usage.total else 0.0
        return {
            "available": True,
            "path": target,
            "measures": "host" if target != str(amm_home) else "data",
            "total": int(usage.total),
            "used": int(usage.used),
            "free": int(usage.free),
            "percent": round(percent, 1),
        }
    except Exception as error:  # pragma: no cover - an unreadable mount point
        return _unavailable(f"no se pudo leer el disco: {error}")


def _in_container() -> bool:
    """Whether this process is inside a container.

    Two independent tells, because neither is guaranteed: the file Docker drops
    at the root, and the cgroup path a container gets.  Only used to *describe*
    the reading, never to change one.
    """
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", encoding="utf-8", errors="ignore") as handle:
            body = handle.read()
        return "docker" in body or "containerd" in body or "kubepods" in body
    except OSError:
        return False


def _under_wsl() -> bool:
    """Whether the kernel is the one Docker Desktop runs on Windows.

    This is the boundary that cannot be crossed: the numbers below describe the
    WSL2 virtual machine, and Windows is on the other side of a hypervisor with
    no ``/proc`` to read.  Detected from the kernel release, which says
    ``microsoft-standard-WSL2``.
    """
    return "microsoft" in platform.release().lower()


def _host() -> Dict[str, Any]:
    """Which machine the numbers below are actually about.

    The whole point of this entry.  "The machine running the scraper" is an
    unambiguous phrase right up to the moment there is a container involved, and
    then it has three possible answers -- the host, the container's slice of it,
    or a virtual machine standing between both and the real hardware.  Reporting
    a number without saying which one it came from is how somebody reads 7.7 GB
    on a 16 GB laptop and concludes the metric is broken.
    """
    container = _in_container()
    if not container:
        kind = "host"
    elif _under_wsl():
        kind = "vm"
    else:
        kind = "container"
    return {
        "kind": kind,
        "name": platform.node(),
        "platform": f"{platform.system()} {platform.release()}".strip(),
    }


def _cgroup_memory_limit() -> Optional[int]:
    """The container's own memory ceiling in bytes, when one was set.

    ``/proc/meminfo`` knows nothing about cgroups, so a container capped at 4 GB
    on a 64 GB host reports 64 GB and looks perfectly healthy until the kernel
    kills it.  This is the number that decides that, and it is worth more than
    the host's total whenever it exists.

    Both cgroup versions, because a machine may be on either.  ``max`` (v2) and
    the enormous sentinel (v1) both mean "no limit", which is reported as
    ``None`` rather than as a number nobody set.
    """
    for path, unlimited in CGROUP_MEMORY_FILES:
        try:
            with open(path, encoding="utf-8") as handle:
                raw = handle.read().strip()
        except OSError:
            continue
        if raw in unlimited:
            return None
        try:
            value = int(raw)
        except ValueError:
            continue
        # cgroup v1 writes a number near 2**63 to mean "no limit".  Anything
        # past a petabyte is that sentinel rather than a machine.
        if value <= 0 or value > 1 << 50:
            return None
        return value
    return None


_GPU_QUERY = "name,utilization.gpu,memory.used,memory.total,temperature.gpu"


def _read_gpu() -> Dict[str, Any]:
    """Ask ``nvidia-smi``, once, and read what it says.

    The only GPU this can honestly report on.  An AMD or Intel card, or an
    NVIDIA one without its tools installed, comes back as unavailable rather
    than as zeros: there is no portable way to ask, and a made-up 0% would read
    as "the GPU is idle" instead of "nobody asked it".
    """
    global _gpu_available
    if _gpu_available is False:
        return _unavailable("no se detectó una GPU con métricas disponibles")

    binary = shutil.which("nvidia-smi")
    if binary is None:
        _gpu_available = False
        return _unavailable("no se detectó una GPU con métricas disponibles")

    try:
        result = subprocess.run(
            [binary, f"--query-gpu={_GPU_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=GPU_TIMEOUT,
            check=False,
        )
    except Exception as error:
        _gpu_available = False
        return _unavailable(f"no se pudo consultar la GPU: {error}")

    if result.returncode != 0 or not result.stdout.strip():
        # A driver that is installed but not answering.  Not marked permanently
        # unavailable: the tool is there, so this may be a card that is asleep
        # rather than a machine that has none.
        return _unavailable("la GPU no respondió a la consulta")

    cards: List[Dict[str, Any]] = []
    for line in result.stdout.strip().splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 4:
            continue

        def number(text: str) -> Optional[float]:
            try:
                return float(text)
            except ValueError:
                # "[N/A]", which is what nvidia-smi prints for a figure a
                # particular card does not expose.  Absent, not zero.
                return None

        cards.append(
            {
                "name": parts[0],
                "percent": number(parts[1]),
                "memory_used": (
                    int(number(parts[2]) * 1024 * 1024) if number(parts[2]) is not None else None
                ),
                "memory_total": (
                    int(number(parts[3]) * 1024 * 1024) if number(parts[3]) is not None else None
                ),
                "temperature": number(parts[4]) if len(parts) > 4 else None,
            }
        )

    if not cards:
        return _unavailable("no se pudo interpretar la respuesta de la GPU")
    _gpu_available = True
    return {"available": True, "cards": cards}


#: What a kernel driver calls a sensor, and what a person calls it.
#:
#: The chip names are the driver's, not the hardware's: ``k10temp`` is every AMD
#: CPU since Family 10h and ``coretemp`` is every Intel one.  A row reading
#: "k10temp - Tctl 61 C" is accurate and tells somebody nothing.
_SENSOR_LABELS = {
    "coretemp": "CPU",
    "k10temp": "CPU",
    "zenpower": "CPU",
    "cpu_thermal": "CPU",
    "soc_thermal": "SoC",
    "acpitz": "Placa",
    "nvme": "SSD NVMe",
    "drivetemp": "Disco",
    "amdgpu": "GPU",
    "nouveau": "GPU",
    "iwlwifi": "WiFi",
    "pch_skylake": "Chipset",
    "pch_cannonlake": "Chipset",
}


def _celsius(value: Any) -> Optional[float]:
    """A temperature as a number, or None when the driver did not give one."""
    try:
        return round(float(value), 1) if value else None
    except (TypeError, ValueError):
        return None


def _percent(value: Any) -> Optional[float]:
    """A percentage as a number, or None. Never a zero standing in for unknown."""
    try:
        return round(float(value), 1) if value is not None else None
    except (TypeError, ValueError):
        return None


def _temperatures(psutil: Any) -> Dict[str, Any]:
    """Every temperature the machine is willing to report.

    ``psutil.sensors_temperatures`` reads ``/sys/class/hwmon``, which Docker
    already mounts from the host on Linux -- so this needs no extra privilege
    and no extra mount, and it reports the *host's* hardware even from inside a
    container.  What it does need is the kernel modules: a machine with no
    ``coretemp`` / ``k10temp`` / ``nvme`` loaded has an empty ``hwmon`` and
    there is nothing to read, which is a fact about that machine rather than a
    fault in this code.

    Absent everywhere else.  Windows exposes nothing psutil can reach without a
    vendor driver, and WSL2 has no hardware at all -- both come back
    unavailable, which is the truthful answer and the reason this is not simply
    assumed to work.
    """
    if psutil is None:
        return _unavailable("psutil no está instalado")
    if not hasattr(psutil, "sensors_temperatures"):
        return _unavailable("esta plataforma no expone sensores de temperatura")
    try:
        readings = psutil.sensors_temperatures()
    except Exception as error:  # pragma: no cover - platform-specific
        return _unavailable(f"no se pudieron leer los sensores: {error}")

    sensors: List[Dict[str, Any]] = []
    for chip, entries in sorted(readings.items()):
        for entry in entries:
            current = getattr(entry, "current", None)
            if current is None:
                continue
            sensors.append(
                {
                    "component": _SENSOR_LABELS.get(chip, chip),
                    "chip": chip,
                    "label": getattr(entry, "label", "") or chip,
                    "celsius": round(float(current), 1),
                    # The thresholds the *driver* reports, not ones invented
                    # here: what counts as hot is a property of the part, and a
                    # number this module made up would be worse than none.
                    "high": _celsius(getattr(entry, "high", None)),
                    "critical": _celsius(getattr(entry, "critical", None)),
                }
            )

    if not sensors:
        return _unavailable(
            "el equipo no expone sensores de temperatura (en Linux hacen falta "
            "los módulos del kernel, por ejemplo coretemp, k10temp o nvme)"
        )
    return {"available": True, "sensors": sensors}


def _read_disk_health() -> Dict[str, Any]:
    """How much life the drives report having left, via ``smartctl``.

    Wear is not in ``/sys``.  It lives in the drive's own SMART log, read with
    an admin command over the device node, so this needs two things a container
    does not have by default: ``smartmontools`` installed (it is, in the image)
    and access to the disks (``cap_add: SYS_RAWIO`` plus the devices, which the
    compose file ships commented out).  Without them this says so and stops
    asking, rather than failing quietly every fifteen minutes.

    ``--json`` because the text output is a different shape per drive family,
    and parsing it is how a wear figure ends up off by a vendor's idea of a
    percentage.

    The number that matters differs by kind of drive, and each is reported only
    when the drive gives it: NVMe exposes ``percentage_used`` (0 is new, 100 is
    the rated endurance spent) and ``available_spare``; SATA SSDs publish the
    same idea under a name of their vendor's choosing; a spinning disk has no
    wear figure at all and only its verdict and its hours.  That is why
    ``life_used`` is allowed to be ``None`` beside a drive that is perfectly
    healthy.
    """
    global _smart_available
    if _smart_available is False:
        return _unavailable("no hay acceso SMART a los discos")

    binary = shutil.which("smartctl")
    if binary is None:
        _smart_available = False
        return _unavailable("smartctl no está instalado")

    devices = _smart_devices(binary)
    if devices is None:
        _smart_available = False
        return _unavailable(
            "el contenedor no tiene acceso a los discos (hacen falta cap_add "
            "SYS_RAWIO y los devices en el compose)"
        )
    if not devices:
        _smart_available = False
        return _unavailable("no se encontró ningún disco con SMART")

    drives = []
    for name in devices:
        drive = _smart_drive(binary, name)
        if drive is not None:
            drives.append(drive)
    if not drives:
        return _unavailable("los discos no respondieron a la consulta SMART")
    _smart_available = True
    return {"available": True, "drives": drives}


def _smart_devices(binary: str) -> Optional[List[str]]:
    """The drives smartctl can see, or ``None`` when it may not look."""
    try:
        result = subprocess.run(
            [binary, "--scan", "--json=c"],
            capture_output=True,
            text=True,
            timeout=DISK_HEALTH_TIMEOUT,
            check=False,
        )
        payload = json.loads(result.stdout or "{}")
    except KeyboardInterrupt:
        raise
    except Exception:
        return None
    return [str(e["name"]) for e in payload.get("devices", []) if e.get("name")]


def _smart_drive(binary: str, device: str) -> Optional[Dict[str, Any]]:
    """One drive's health, or ``None`` when it could not be read."""
    try:
        result = subprocess.run(
            [binary, "--info", "--health", "--attributes", "--json=c", device],
            capture_output=True,
            text=True,
            timeout=DISK_HEALTH_TIMEOUT,
            check=False,
        )
        payload = json.loads(result.stdout or "{}")
    except KeyboardInterrupt:
        raise
    except Exception:
        return None
    if not payload:
        return None

    health = payload.get("smart_status") or {}
    nvme = payload.get("nvme_smart_health_information_log") or {}
    life_used = nvme.get("percentage_used")
    if life_used is None:
        life_used = _sata_life_used(payload)

    return {
        "device": device,
        "model": payload.get("model_name") or device,
        # None rather than True when the drive said nothing: "passed" is a
        # claim, and this module does not make claims the hardware did not.
        "passed": health.get("passed") if "passed" in health else None,
        "life_used": _percent(life_used),
        "spare_available": _percent(nvme.get("available_spare")),
        "power_on_hours": (payload.get("power_on_time") or {}).get("hours"),
        "celsius": _celsius((payload.get("temperature") or {}).get("current")),
    }


def _sata_life_used(payload: Dict[str, Any]) -> Optional[float]:
    """A SATA SSD's wear, from whichever attribute its vendor chose to use.

    There is no standard one, which is the whole difficulty: the same idea is
    published as "percentage used", as a countdown of life *remaining*, and as a
    wear-levelling count that starts at 100 and falls.  The countdown forms are
    turned into "used" here so that one number on the screen means one thing.
    """
    used_names = {"percent_lifetime_used", "ssd_life_used"}
    remaining_names = {
        "percent_lifetime_remain",
        "ssd_life_left",
        "wear_leveling_count",
        "media_wearout_indicator",
        "percent_life_remaining",
    }
    table = (payload.get("ata_smart_attributes") or {}).get("table") or []
    for attribute in table:
        name = str(attribute.get("name", "")).lower()
        value = attribute.get("value")
        if value is None:
            continue
        if name in used_names:
            return float(value)
        if name in remaining_names:
            return 100.0 - float(value)
    return None


def _disk_health() -> Dict[str, Any]:
    """The last SMART reading, taken again only when it is stale."""
    global _disk_health_sample, _disk_health_at
    now = time.monotonic()
    if _disk_health_sample and now - _disk_health_at < DISK_HEALTH_INTERVAL:
        return _disk_health_sample
    _disk_health_sample = _read_disk_health()
    _disk_health_at = now
    return _disk_health_sample


def _gpu() -> Dict[str, Any]:
    """The last GPU reading, taken again only when it is stale.

    The one reading that costs a process, so it is on a cadence of its own --
    see :data:`GPU_INTERVAL`.  A GPU's utilisation moves slowly enough that this
    loses nothing.
    """
    global _gpu_sample, _gpu_at
    now = time.monotonic()
    if _gpu_sample and now - _gpu_at < GPU_INTERVAL:
        return _gpu_sample
    _gpu_sample = _read_gpu()
    _gpu_at = now
    return _gpu_sample


# --------------------------------------------------------------------------- #
# The sampler
# --------------------------------------------------------------------------- #


def _take_sample(psutil: Any, handle: Any) -> Dict[str, Any]:
    return {
        "at": time.time(),
        "interval": INTERVAL,
        # Kept flat as well as inside `host`, so an older interface that reads
        # these two keys goes on working.
        "host": platform.node(),
        "platform": f"{platform.system()} {platform.release()}".strip(),
        "machine": _host(),
        "cpu": _cpu(psutil),
        "memory": _memory(psutil),
        "disk": _disk(),
        "gpu": _gpu(),
        "temperatures": _temperatures(psutil),
        "disk_health": _disk_health(),
        "process": _process(psutil, handle),
    }


def _loop() -> None:
    """Take a reading every :data:`INTERVAL` seconds until told to stop."""
    psutil = _psutil()
    handle = None
    if psutil is not None:
        try:
            handle = psutil.Process(os.getpid())
            # The first call to either of these returns 0.0 by definition --
            # they report over the span since the previous call and there has
            # not been one.  Primed here so the first sample the interface sees
            # is a measurement rather than a placeholder zero.
            psutil.cpu_percent(interval=None)
            handle.cpu_percent(interval=None)
        except Exception:  # pragma: no cover - a process that cannot see itself
            handle = None

    while not _stop.is_set():
        try:
            sample = _take_sample(psutil, handle)
        except Exception as error:  # pragma: no cover - must never kill the thread
            logger.debug("Could not sample system metrics: %s", error)
            sample = {"at": time.time(), "error": str(error)}
        with _lock:
            global _sample
            _sample = sample
        _stop.wait(INTERVAL)


def start() -> None:
    """Start sampling, if it is not already running.

    Idempotent, and called from :func:`snapshot`: nothing samples until
    somebody looks at the numbers, and a monitor whose status screen is never
    opened pays nothing at all for this module.
    """
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(
            target=_loop, name="amm-system-metrics", daemon=True
        )
        _thread.start()


def stop() -> None:
    """Stop sampling.  For tests and for a clean shutdown."""
    global _thread
    _stop.set()
    thread = _thread
    if thread is not None:
        thread.join(timeout=INTERVAL + 1)
    with _lock:
        _thread = None


def snapshot() -> Dict[str, Any]:
    """The machine and the scraper's browsers, as the last sample saw them.

    Never blocks on a measurement.  Before the first sample lands -- roughly the
    first moment somebody opens the status screen -- ``pending`` is true and
    every metric is missing, which is a state worth naming: "starting" is a
    different thing to show than "unavailable".
    """
    start()
    with _lock:
        sample = dict(_sample)
    if not sample:
        return {
            "pending": True,
            "interval": INTERVAL,
            # Which machine this is does not need a sample -- it is known the
            # moment the process starts -- so it is answered even here. The
            # first few seconds are exactly when somebody is looking at an
            # empty panel wondering what it is about to describe.
            "machine": _host(),
            "browsers": control.browsers(),
        }
    return {"pending": False, **sample, "browsers": control.browsers()}
