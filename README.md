# Matrix Element Call Recorder (Installer)

A single-command installer that deploys a complete Matrix + Element Call recording stack with per-room ACL and a recording button injected into Element Web.

Important: this project disables the built‑in Jitsi module and uses Element Call only. The recording button works in Element Web; participants may join from Element Desktop or Element X (Android/iOS). Known issue on Element X Android: sometimes a “dark tile” is recorded instead of video (tracked at https://github.com/element-hq/element-call/issues/3937).

## What it installs (v0.1)

- Traefik reverse proxy (80/443/8448, TCP 5350 for TURN‑TLS)
- PostgreSQL (Synapse database)
- Synapse homeserver
- Element Web (+ injected `record-button.js`)
- Element Call
- LiveKit server with built‑in TURN
- JWT service for LiveKit
- Recorder (Chromium + Puppeteer + FFmpeg; host network)
- Recording controller (Flask API)
- Recordings portal (Flask + gunicorn) with Matrix‑based ACL
- Well‑known static for Matrix discovery
- systemd unit `element-stack.service`

## Access control

- Only room admins (power level ≥ 50, configurable via `RECORDING_MIN_POWER_LEVEL`) can start/stop recordings via the Element Web button.
- Room admins see only their rooms’ recordings in the portal.
- Server admins see all recordings.

## Requirements and constraints

- OS: Ubuntu (ID=ubuntu), root
- Clean Docker: no existing containers at install time
- Free TCP ports: 80, 443, 8448, 5350 (and local ports described below)
- Certificates are local files signed by your local CA; the CA is imported into the recorder’s Chromium NSS store.
- Parallel recordings are NOT supported in v0.1 (single active job).

## Default local ports

- Recorder API: 127.0.0.1:8788
- Element Call (loopback): 127.0.0.1:8090
- Recordings web: 127.0.0.1:8899
- Synapse local: 127.0.0.1:18008
- LiveKit local: 127.0.0.1:17880
- LiveKit UDP: 7882, 3479, 30000–30020

## Directory layout (INSTALL_DIR=/opt/element-stack)

- certs/ {fullchain.pem, privkey.pem, ca.crt}
- config/ {element.json, call.json, traefik.json, dynamic.yml, element-index.html, record-button.js}
- data/{postgres,synapse,controller}/
- data/profile/ — persistent Matrix crypto profile (do not delete)
- out/ — draft recordings
- public/ — published recordings and index
- src/{recorder,controller,recordings-auth}/
- well-known/.well-known/matrix/{client,server}
- .installer-config-sha256 — answers.conf fingerprint

## Health checks (via traefik using local CA)

- curl --cacert certs/ca.crt --resolve DOMAIN:443:127.0.0.1 https://DOMAIN/_matrix/client/versions
- curl --cacert certs/ca.crt --resolve DOMAIN:443:127.0.0.1 https://DOMAIN/recording/health
- curl --cacert certs/ca.crt --resolve DOMAIN:443:127.0.0.1 https://DOMAIN/recordings/

## Install

1) Place files in /root:
- `install.sh`
- `answers.conf` (or start from `answers.example` and rename to `answers.conf`)
- Local CA and TLS:
  - TLS_CERT_FILE=/root/certs/fullchain.pem
  - TLS_KEY_FILE=/root/certs/privkey.pem
  - CA_CERT_FILE=/root/certs/ca.crt

2) Run:
```bash
chmod +x install.sh
sudo -E bash ./install.sh --install /root/answers.conf
```

3) Start/stop/logs:
```bash
systemctl enable --now element-stack.service
systemctl status element-stack.service
systemctl stop element-stack.service
docker compose -f /opt/element-stack/compose.json ps
docker compose -f /opt/element-stack/compose.json logs --tail=100
```

## answers.conf (required keys)

- DOMAIN, HOSTNAME, SERVER_IP, SYNAPSE_SERVER_NAME
- ADMIN_USER, ADMIN_PASSWORD
- RECORDER_USER, RECORDER_PASSWORD
- POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB
- LIVEKIT_KEY, LIVEKIT_SECRET
- ELEMENT_WEB_IMAGE, ELEMENT_CALL_IMAGE, SYNAPSE_IMAGE
- POSTGRES_IMAGE, LIVEKIT_IMAGE, TRAEFIK_IMAGE
- JWT_IMAGE, ADMIN_IMAGE, NGINX_IMAGE
- TLS_CERT_FILE, TLS_KEY_FILE, CA_CERT_FILE

Optional (defaults in parentheses):
- INSTALL_DIR (/opt/element-stack)
- RECORDER_API_PORT (8788), SOCAT_PORT (8090), RECORDINGS_WEB_PORT (8899)
- SYNAPSE_LOCAL_PORT (18008), LIVEKIT_LOCAL_PORT (17880)
- RECORDING_MIN_POWER_LEVEL (50)

## Known limitations

- Only one concurrent recording (v0.1).
- Element X Android “dark tile” issue: https://github.com/element-hq/element-call/issues/3937

## Security notes

- Do not commit real `answers.conf` or private keys to Git.
- `data/profile/` contains E2EE keys for recorder: never delete once in production.
- The installer enforces `SYNAPSE_SERVER_NAME == DOMAIN` and uses the local CA.
