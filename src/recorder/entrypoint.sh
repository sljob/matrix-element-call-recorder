#!/bin/bash
set -e
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null
Xvfb :99 -screen 0 ${SCREEN_SIZE:-1280x720x24} -nolisten tcp &
for i in $(seq 1 50); do xdpyinfo -display :99 >/dev/null 2>&1 && break; sleep 0.2; done
mkdir -p /tmp/pulse
pulseaudio --exit-idle-time=-1 --disallow-exit -n \
  --load="module-native-protocol-unix socket=/tmp/pulse/native" \
  --load="module-null-sink sink_name=recsink sink_properties=device.description=recsink" \
  --load="module-always-sink" --daemonize=yes --log-target=stderr 2>/dev/null || true
sleep 1
pactl set-default-sink recsink 2>/dev/null || true
if [ -f /usr/local/share/ca-certificates/ca.crt ]; then
  mkdir -p /profile-spk/.pki/nssdb
  export HOME=/profile-spk
  [ ! -f /profile-spk/.pki/nssdb/cert9.db ] && certutil -d sql:/profile-spk/.pki/nssdb -N --empty-password 2>/dev/null || true
  certutil -d sql:/profile-spk/.pki/nssdb -A -t "C," -n CA -i /usr/local/share/ca-certificates/ca.crt 2>/dev/null || true
fi
if [ "${AUTOSTART_API:-1}" = "1" ]; then
  cd /app
  cp -f /work/*.js /app/ 2>/dev/null || true
  BIND="${BIND:-0.0.0.0}" PORT="${PORT:-8788}" node server.js >>/out/server.log 2>&1 &
fi
exec "$@"
