# The monitor, as a container.
#
# What is in here and why it is not one stage:
#
#   builder   installs the package and its dependencies into a virtualenv, and
#             is thrown away.  Nothing it needs to do that -- pip's cache, the
#             source tree, build backends -- reaches the image that ships.
#   runtime   the virtualenv, Chromium and the handful of programs a browser
#             needs to run without a screen.
#
# Chromium is deliberately *not* slimmed.  `playwright install --with-deps`
# pulls in a long list of X and font libraries and every one of them is load
# bearing: without them Chromium starts and then dies on the first page, which
# looks like a scraping bug rather than a missing package.  The size to attack
# is elsewhere.
#
# Running headed rather than headless is also deliberate.  Marketplaces
# challenge headless browsers far more readily, so the container brings a
# virtual screen (Xvfb) and lets you look at it (x11vnc + noVNC) for the
# moments a person has to solve a CAPTCHA or finish a login by hand.
#
# Build and run:
#   docker build -t aimm .
#   docker run --rm -it -p 8467:8467 -v aimm-data:/home/aimm/.ai-marketplace-monitor aimm
#
# Or, with the web UI beside it, `docker compose up` from the UI repository.

# --------------------------------------------------------------------------- #
# Stage 1: build the virtualenv
# --------------------------------------------------------------------------- #
FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# A virtualenv rather than the system site-packages, because a directory is the
# one thing that can be copied to the next stage as a unit.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# Dependencies first, on their own layer, from the file that declares them.
# The package itself is installed again below; what this buys is that editing
# the source does not re-download two hundred megabytes of wheels.  The stub
# package exists only so the build backend has something to build.
#
# `[stealth]` is patchright, and it is in the image now where the comment in
# pyproject.toml still says the container does not need it.  It did not, while
# the container was only ever asked for Facebook and Mercado Libre.  Lider is
# behind PerimeterX, and the tell that survives every launch flag is the
# driver: Playwright drives Chromium over CDP and enabling the `Runtime` domain
# to evaluate scripts leaves traces a page can read.  patchright runs them in
# isolated contexts instead.  `browser_engine.py` picks it up by import, so
# nothing else in the image changes -- and uninstalling it still falls back.
COPY pyproject.toml README.md ./
RUN mkdir -p src/ai_marketplace_monitor \
    && printf '__version__ = "0.0.0"\n' > src/ai_marketplace_monitor/__init__.py \
    && pip install --no-cache-dir ".[stealth]" \
    && pip uninstall -y ai-marketplace-monitor

# Now the real thing.  `--no-deps` because the layer above already resolved
# them, and re-resolving would undo the caching it exists for.
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

# --------------------------------------------------------------------------- #
# Stage 2: what actually ships
# --------------------------------------------------------------------------- #
FROM python:3.12-slim-bookworm AS runtime

# The git tag CI built this image from (see .github/workflows/docker.yml).
# An image has no git checkout to ask, so the tag has to be frozen in at build
# time or the running process cannot know it -- and the number in pyproject.toml
# is not it: this fork still carries upstream's 0.10.x there while its own
# releases are tagged v1.x, which is exactly why a freshly pulled image kept
# reporting "0.10.2".  Empty by default, so a local `docker build` falls back to
# the package version and says so rather than inventing a tag.
ARG APP_VERSION=""

ENV DEBIAN_FRONTEND=noninteractive \
    AIMM_VERSION=$APP_VERSION \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    # Outside any one user's home, so the browsers are installed once as root
    # and read by the unprivileged user the monitor actually runs as.
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DISPLAY=:99 \
    SCREEN_GEOMETRY=1280x800x24 \
    VNC_PORT=5900 \
    AIMM_WEBUI_HOST=0.0.0.0 \
    AIMM_WEBUI_PORT=8467 \
    AIMM_ENABLE_VNC=1 \
    AIMM_NOVNC_DIR=/usr/share/novnc \
    AIMM_VNC_HOST=127.0.0.1 \
    AIMM_VNC_PORT=5900 \
    HOME=/home/aimm

#   xvfb, x11vnc, xauth   a screen for a browser that must not look headless
#   websockify, novnc     that screen, in a browser tab, for CAPTCHAs
#   supervisor            three long-running programs, one container
#   tini                  PID 1 that reaps children and forwards signals
# Chromium's own libraries are not listed: `playwright install --with-deps`
# below knows which ones it needs far better than this file does.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        x11vnc \
        xauth \
        supervisor \
        websockify \
        novnc \
        ca-certificates \
        tini \
        smartmontools \
    && rm -rf /var/lib/apt/lists/*

# `smartmontools` is for the storage-life reading on the status screen, and
# installing it grants nothing on its own: reading a drive's SMART log needs the
# device nodes and CAP_SYS_RAWIO, which the compose file leaves commented out
# for the operator to turn on deliberately. Without them the monitor reports the
# reading as unavailable and stops asking.

# noVNC's page is vnc.html on some versions and vnc_lite.html on others.
RUN if [ ! -e /usr/share/novnc/vnc.html ] && [ -e /usr/share/novnc/vnc_lite.html ]; then \
        ln -s /usr/share/novnc/vnc_lite.html /usr/share/novnc/vnc.html; \
    fi

COPY --from=builder /opt/venv /opt/venv

# After the virtualenv, because this is Playwright's own installer running.
# `--with-deps` is the apt half and has to be root; the browsers land in
# PLAYWRIGHT_BROWSERS_PATH and are made readable by everyone, because the
# process that opens them is not root.
RUN playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/*

# patchright's own Chromium, into the same PLAYWRIGHT_BROWSERS_PATH.  It is a
# fork and shares the registry layout, so a matching revision costs nothing and
# a differing one is the second copy that makes patchright work at all -- which
# is why this is a separate layer with its own `chmod`: the apt half above is
# already done and must not be repeated.
RUN patchright install chromium \
    && chmod -R a+rX /ms-playwright

# An unprivileged user, and the reason is Chromium rather than principle: a
# browser running as root is a browser one sandbox escape away from the host,
# and Chromium itself refuses to enable its own sandbox as uid 0.
#
# Note for anyone upgrading: the data directory moved with the user.  It was
# /root/.ai-marketplace-monitor and is now /home/aimm/.ai-marketplace-monitor,
# which is where the config, the cache and the browser profiles live.
RUN useradd --create-home --home-dir /home/aimm --shell /bin/bash --uid 10001 aimm \
    && mkdir -p /home/aimm/.ai-marketplace-monitor /var/log/supervisor /var/run/supervisor \
    && chown -R aimm:aimm /home/aimm /var/log/supervisor /var/run/supervisor

COPY docker/supervisord.conf /etc/supervisor/aimm.conf

USER aimm
WORKDIR /home/aimm

EXPOSE 8467

VOLUME ["/home/aimm/.ai-marketplace-monitor"]

# Unauthenticated on purpose: it is the one endpoint that answers before a
# login, which is exactly what a health check needs.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('AIMM_WEBUI_PORT','8467')+'/api/auth/info', timeout=4).status==200 else 1)"

# tini as PID 1: supervisord forwards a signal to its children, but only
# something that reaps them keeps a container from filling with zombie Chromium
# processes.  It is also what turns `docker stop` into the SIGTERM the monitor
# now shuts down cleanly on.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/aimm.conf", "-n"]
