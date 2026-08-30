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

The browser and tab counts are *not* here.  They are not a property of the
machine -- a Chrome the user has open is not the scraper's -- and the scraper
already knows exactly which browsers are its own, so they come from
:mod:`ai_marketplace_monitor.control`, where the threads that own those browsers
publish them.  :func:`snapshot` puts the two together.
"""

from __future__ import annotations

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

_lock = threading.Lock()
_sample: Dict[str, Any] = {}
_thread: Optional[threading.Thread] = None
_stop = threading.Event()

#: ``None`` before the first attempt, then True/False for good.  A machine with
#: no NVIDIA tooling is asked once in the life of the process.
_gpu_available: Optional[bool] = None
_gpu_sample: Dict[str, Any] = {}
_gpu_at: float = 0.0


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
        return {
            "available": True,
            "percent": round(float(memory.percent), 1),
            "total": int(memory.total),
            "used": int(memory.total - memory.available),
            "free": int(memory.available),
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
    try:
        usage = shutil.disk_usage(str(amm_home))
        percent = (usage.used / usage.total * 100) if usage.total else 0.0
        return {
            "available": True,
            "path": str(amm_home),
            "total": int(usage.total),
            "used": int(usage.used),
            "free": int(usage.free),
            "percent": round(percent, 1),
        }
    except Exception as error:  # pragma: no cover - an unreadable mount point
        return _unavailable(f"no se pudo leer el disco: {error}")


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
        "host": platform.node(),
        "platform": f"{platform.system()} {platform.release()}".strip(),
        "cpu": _cpu(psutil),
        "memory": _memory(psutil),
        "disk": _disk(),
        "gpu": _gpu(),
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
            "browsers": control.browsers(),
        }
    return {"pending": False, **sample, "browsers": control.browsers()}
