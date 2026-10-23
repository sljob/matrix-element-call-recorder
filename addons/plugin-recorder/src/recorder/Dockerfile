# ==========================================================================
#  recorder worker image
#
#  Mount discipline (this is where a careless copy of the reference project
#  breaks):
#    * application code lives in /app and NOTHING is ever mounted over it --
#      an empty bind mount would hide node_modules and the observer;
#    * the persistent Matrix profile (E2EE keys, storageState.json) is /profile;
#    * recordings go to /output.
#
#  Host networking: the worker must reach Element Call on 127.0.0.1, and
#  Chromium needs a large /dev/shm, which the compose file sets to 2 GB.
# ==========================================================================
FROM mcr.microsoft.com/playwright:v1.49.1-jammy

ENV DEBIAN_FRONTEND=noninteractive \
    NODE_ENV=production \
    DISPLAY=:99

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        pulseaudio \
        pulseaudio-utils \
        xvfb \
        x11-utils \
        curl \
        ca-certificates \
        fonts-dejavu-core \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Chromium comes from the playwright base image; the path is pinned so the
# observer never has to guess which build it is driving.
ENV CHROME_PATH=/ms-playwright/chromium-1148/chrome-linux/chrome \
    PUPPETEER_SKIP_DOWNLOAD=1 \
    PUPPETEER_EXECUTABLE_PATH=/ms-playwright/chromium-1148/chrome-linux/chrome

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund && npm cache clean --force

# worker shim (this repository, reviewed here)
COPY server.js ./

# pinned observer core, fetched by tools/fetch-upstream.sh into vendor/
# if vendor/ is empty the build fails on purpose -- see README "Pinned core"
COPY p63-observer.js /app/p63-observer.js

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

RUN mkdir -p /profile /output && chmod 700 /profile

# Host networking makes EXPOSE decorative, but it documents the contract.
EXPOSE 8788

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8788}/health" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
