"""Tests for the browser launch options that keep a sign-in from being refused.

Playwright's Chromium announces itself as automated.  A site that reads that can
bounce an ordinary interactive login into an endless challenge loop -- the
CAPTCHA is answered correctly and the login page simply returns, because what
was rejected is the browser, not the answer.
"""

from __future__ import annotations

from ai_marketplace_monitor.monitor import MarketplaceMonitor


def test_chromium_drops_the_automation_flag() -> None:
    options = MarketplaceMonitor._launch_options("chromium")
    assert "--enable-automation" in options["ignore_default_args"]
    assert "--disable-blink-features=AutomationControlled" in options["args"]


def test_other_engines_get_no_chromium_flags() -> None:
    """Firefox and WebKit reject those arguments outright."""
    assert MarketplaceMonitor._launch_options("firefox") == {}
    assert MarketplaceMonitor._launch_options("webkit") == {}


class FakeConfig:
    def __init__(self, monitor: object) -> None:
        self.monitor = monitor


class FakeMonitorConfig:
    def __init__(self, servers: object) -> None:
        self.proxy_server = servers

    def get_proxy_options(self) -> dict | None:
        if not self.proxy_server:
            return None
        servers = self.proxy_server
        first = servers[0] if isinstance(servers, list) else servers
        return {"server": first}


class RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str, *args: object, **kwargs: object) -> None:
        self.warnings.append(message)

    def debug(self, *args: object, **kwargs: object) -> None:
        pass

    def info(self, *args: object, **kwargs: object) -> None:
        pass


def _monitor(config: object, logger: object) -> MarketplaceMonitor:
    monitor = MarketplaceMonitor.__new__(MarketplaceMonitor)
    monitor.config = config
    monitor.logger = logger
    return monitor


def test_no_proxy_configured() -> None:
    monitor = _monitor(FakeConfig(FakeMonitorConfig(None)), RecordingLogger())
    assert monitor._proxy_for_launch() is None


def test_single_proxy_is_used_without_complaint() -> None:
    logger = RecordingLogger()
    monitor = _monitor(FakeConfig(FakeMonitorConfig(["http://one:8080"])), logger)
    assert monitor._proxy_for_launch() == {"server": "http://one:8080"}
    assert logger.warnings == []


def test_rotating_proxies_warn_that_rotation_is_lost() -> None:
    """A persistent profile binds one proxy for its lifetime — say so."""
    logger = RecordingLogger()
    servers = ["http://one:8080", "http://two:8080"]
    monitor = _monitor(FakeConfig(FakeMonitorConfig(servers)), logger)
    assert monitor._proxy_for_launch() == {"server": "http://one:8080"}
    assert len(logger.warnings) == 1
    assert "rotation is not applied" in logger.warnings[0]


def test_missing_config_is_tolerated() -> None:
    assert _monitor(None, RecordingLogger())._proxy_for_launch() is None
