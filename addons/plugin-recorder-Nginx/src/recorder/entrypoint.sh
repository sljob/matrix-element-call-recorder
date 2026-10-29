#!/bin/bash
# ==========================================================================
#  Worker entrypoint: virtual display, audio sink, then the HTTP shim.
#
#  Order matters. FFmpeg grabs :99 with x11grab and the "virtual_speaker"
#  pulse sink; both must exist before the first /start or the first frame
#  and the audio track come out empty.
# ==========================================================================
set -Eeuo pipefail

DISPLAY_NUM="${DISPLAY:-:99}"
SCREEN="${SCREEN_GEOMETRY:-1920x1080x24}"

log() { printf '%s [entrypoint] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

# --- virtual X ------------------------------------------------------------
if ! xdpyinfo -display "$DISPLAY_NUM" >/dev/null 2>&1; then
    log "starting Xvfb on $DISPLAY_NUM at $SCREEN"
    rm -f "/tmp/.X${DISPLAY_NUM#:}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM#:}" 2>/dev/null || true
    Xvfb "$DISPLAY_NUM" -screen 0 "$SCREEN" -ac +extension RANDR +render -noreset &
    for _ in $(seq 1 30); do
        xdpyinfo -display "$DISPLAY_NUM" >/dev/null 2>&1 && break
        sleep 0.5
    done
    xdpyinfo -display "$DISPLAY_NUM" >/dev/null 2>&1 || {
        log "Xvfb did not come up"; exit 1;
    }
fi
log "display $DISPLAY_NUM ready"

# --- pulseaudio with a null sink -----------------------------------------
# Headless container has no sound card. Element Call picks "virtual_speaker"
# as output; pulseaudio records from its monitor, which is what FFmpeg reads.
mkdir -p /root/.config/pulse
if ! pulseaudio --check 2>/dev/null; then
    log "starting pulseaudio"
    pulseaudio --start --exit-idle-time=-1 --disallow-exit \
        --load="module-null-sink sink_name=virtual_speaker sink_properties=device.description=Virtual_Speaker" \
        --load="module-native-protocol-unix" >/dev/null 2>&1 || true
fi
for _ in $(seq 1 20); do
    pactl info >/dev/null 2>&1 && break
    sleep 0.5
done

if pactl list short sinks 2>/dev/null | grep -q virtual_speaker; then
    pactl set-default-sink virtual_speaker 2>/dev/null || true
    log "pulse sink virtual_speaker active"
else
    log "WARNING: virtual_speaker not present — audio track will be silent"
fi

# --- chromium path --------------------------------------------------------
# vendor/p63-observer.js launches CHROME_PATH (playwright image layout). Older
# builds hardcoded /usr/bin/chromium, and the upstream copy still does when the
# env var is absent, so provide the symlink as a fallback (#12a).
if [ -n "${CHROME_PATH:-}" ] && [ -x "${CHROME_PATH}" ]; then
    if [ ! -e /usr/bin/chromium ]; then
        ln -s "$CHROME_PATH" /usr/bin/chromium 2>/dev/null && log "chromium symlink -> $CHROME_PATH" || true
    fi
fi

# --- audio preflight ------------------------------------------------------
# The single most expensive class of failure was finding out only after the
# recording finished that the pulse source did not exist (#20/#21). Check it
# here, once, and say so loudly instead of producing a silent file later.
if pactl list short sources 2>/dev/null | grep -q "^.*${PULSE_SRC:-virtual_speaker.monitor}"; then
    log "audio source ${PULSE_SRC:-virtual_speaker.monitor} present"
else
    log "WARNING: audio source ${PULSE_SRC:-virtual_speaker.monitor} MISSING - recordings will be video-only"
fi

# --- profile state --------------------------------------------------------
mkdir -p "$PROFILE_DIR" "$OUTPUT_DIR"
if [ -f "$PROFILE_DIR/storageState.json" ]; then
    log "profile present ($(stat -c%s "$PROFILE_DIR/storageState.json") bytes) — skipping login"
else
    log "no profile yet — first recording will perform Matrix login as $RECORDER_USER"
fi

# --- keep the entrypoint small: exec the shim ----------------------------
log "starting worker on port ${PORT:-8788}"
cd /app
exec node server.js
