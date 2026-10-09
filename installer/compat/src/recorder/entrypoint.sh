#!/bin/bash
set -euo pipefail
# RECORDER_STARTUP_FIX_V05
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 /tmp/pulse/native

mkdir -p /tmp/pulse /out /public /work/profile-spk
rm -f /out/*.active
export DISPLAY=:99
export PULSE_SERVER=unix:/tmp/pulse/native
Xvfb :99 -screen 0 1280x720x24 -nolisten tcp &
for i in $(seq 1 30); do
  xdpyinfo -display :99 >/dev/null 2>&1 && break
  sleep 1
done
xdpyinfo -display :99 >/dev/null
pulseaudio --exit-idle-time=-1 --disallow-exit -n   --load="module-native-protocol-unix socket=/tmp/pulse/native"   --load="module-null-sink sink_name=recsink"   --load=module-always-sink --daemonize=yes --log-target=stderr
pactl info >/dev/null
pactl list sinks short | grep -w recsink
exec node /app/server.js
