"""Top-level package for ai-marketplace-monitor."""

import os
from importlib.metadata import PackageNotFoundError, version
from typing import NamedTuple

__author__ = """Bo Peng"""
__email__ = "ben.bob@gmail.com"

try:
    __version__ = version("ai-marketplace-monitor")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

#: Environment variable the container image is built with.  See the Dockerfile:
#: CI passes the git tag that triggered the build in as ``APP_VERSION`` and the
#: image freezes it here, which is the only place the running process can learn
#: it from -- there is no git checkout inside the image to ask.
VERSION_ENV = "AIMM_VERSION"


class AppVersion(NamedTuple):
    """What version is running, and how much that answer is worth.

    ``source`` exists because the two answers are not interchangeable and one of
    them is routinely wrong:

    ``tag``      the git tag the image was built and published from.  This is the
                 version in the sense anyone deploying means it -- what
                 ``ghcr.io/...:1.0.2`` contains.
    ``package``  ``__version__``, read from the installed package metadata, i.e.
                 whatever ``pyproject.toml`` says.  In this fork that number
                 still tracks upstream (0.10.x) while the releases published
                 from here are tagged v1.x, so it is *not* the deployed version
                 and must never be presented as one.
    """

    value: str
    source: str  # "tag" | "package"


def app_version() -> AppVersion:
    """The version of this running backend, preferring the image's git tag."""
    tag = os.environ.get(VERSION_ENV, "").strip()
    if tag:
        return AppVersion(tag, "tag")
    return AppVersion(__version__, "package")
