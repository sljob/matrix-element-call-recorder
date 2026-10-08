# Matrix Element Call Recorder — One‑shot Installer

Status: v0.1+ (updated 2026-10-08) • Ubuntu 22.04/24.04 • Docker/Compose

This repository ships a single installer that provisions a complete Matrix + Element stack with an end‑to‑end encrypted headless recorder and an ACL‑protected recordings portal.

What it installs
- Traefik reverse proxy (TLS offload; 80/443/8448; TCP 5350 for TURN‑TLS)
- Postgres + Synapse (homeserver)
- Element Web and Element Call (Jitsi disabled)
- LiveKit server (+ embedded TURN)
- Recording workers: Chromium + Puppeteer + FFmpeg (host network) — one active room per worker
- Recording controller (Flask) and publication pipeline (MP4/mp3/poster, index)
- Recordings portal (Flask+gunicorn) with Matrix ACL:
  - Server admins see all rooms
  - Room admins (PL ≥ 50) see only their rooms

Recording UX
- A small Record button is injected into Element Web (not Element Desktop/X). Clicking it calls the controller API with the user’s Matrix token.
- Recordings are published to /recordings and grouped by room. Room admins only see their own rooms; server admins see all.

Parallel recordings
- Each recorder worker can handle one room at a time.
- Use RECORDING_WORKERS to set the pool size (e.g. 4) for parallel recordings across different rooms.
- Tested pool sizes: 1–4. Ensure sufficient CPU/GPU headroom (≈1 vCPU per 720p@15fps job + 512–768 MB RAM).

Known limitations
- Element X on Android may render a “dark tile” during live TX; see Element Call known issue (Android hardware decoders). Recording from Element Web is supported and recommended.
- answers.conf is immutable after the first successful run (fingerprint file). Update by reinstall only.

Security & TLS
- Uses local certificates issued by a local CA. Chromium/NSS inside the recorder trusts your CA.
- DER/PEM mixed chains are handled correctly in this version.

Key improvements in this version
- Fixed handling of mixed DER/PEM certificates.
- Verifies SERVER_IP is actually bound on the host.
- Uses the declared IP for LiveKit `rtc.node_ip`.
- Protects against re‑running on top of an existing/prod installation.
- Minimal host dependencies bootstrap.
- Local Matrix user search by displayname enabled.
- LiveKit discovery check.
- Validates that media IPs match the server local address.
- Checks for all recorders, PulseAudio and Element Call readiness.
- Extra utility scripts:
  - Name search check script (displayname search diagnostic).
  - Read‑only full post‑install verification script.

Quick start
1) Copy `install.sh` and create `/root/answers.conf` from `answers.example`.
2) Place your local CA and leaf cert/key:
   - `TLS_CERT_FILE=/root/certs/fullchain.pem`
   - `TLS_KEY_FILE=/root/certs/privkey.pem`
   - `CA_CERT_FILE=/root/certs/ca.crt`
3) Dry‑run checks (no changes):
   ```bash
   bash install.sh --check /root/answers.conf
   ```
4) Install:
   ```bash
   bash install.sh --install /root/answers.conf
   systemctl enable --now element-stack.service
   ```

Health checks (localhost with your CA)
```bash
cd /opt/element-stack
CACERT=certs/ca.crt
for p in /_matrix/client/versions /.well-known/matrix/client /recording/health /recordings/; do
  curl --silent --fail --cacert "$CACERT" --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN$p" >/dev/null && echo "OK $p" || echo "FAIL $p"
done
```

Default routes behind Traefik
- https://DOMAIN/ → Element Web
- https://DOMAIN/_matrix, /_synapse → Synapse
- https://DOMAIN/call/ → Element Call
- https://DOMAIN/recording/* → Controller API
- https://DOMAIN/recordings/ → Recordings portal
- https://DOMAIN/.well-known/matrix/* → Well‑known

answers.conf (excerpt)
```ini
# Required
DOMAIN=example.org
HOSTNAME=matrix
SERVER_IP=192.0.2.10
SYNAPSE_SERVER_NAME=example.org

ADMIN_USER=admin
ADMIN_PASSWORD=changeme
RECORDER_USER=recorder
RECORDER_PASSWORD=changeme

POSTGRES_USER=synapse
POSTGRES_PASSWORD=changeme
POSTGRES_DB=synapse

LIVEKIT_KEY=lk_key
LIVEKIT_SECRET=lk_secret

TLS_CERT_FILE=/root/certs/fullchain.pem
TLS_KEY_FILE=/root/certs/privkey.pem
CA_CERT_FILE=/root/certs/ca.crt

# Images (pins)
ELEMENT_WEB_IMAGE=vectorim/element-web:latest
ELEMENT_CALL_IMAGE=ghcr.io/element-hq/element-call:latest
SYNAPSE_IMAGE=matrixdotorg/synapse:latest
POSTGRES_IMAGE=postgres:15
LIVEKIT_IMAGE=livekit/livekit-server:latest
TRAEFIK_IMAGE=traefik:2.11
JWT_IMAGE=ghcr.io/matrix-org/lk-jwt:latest
ADMIN_IMAGE=ghcr.io/etkecc/synapse-admin:latest
NGINX_IMAGE=nginx:alpine

# Optional
INSTALL_DIR=/opt/element-stack
RECORDER_API_PORT=8788
SOCAT_PORT=8090
RECORDINGS_WEB_PORT=8899
SYNAPSE_LOCAL_PORT=18008
LIVEKIT_LOCAL_PORT=17880
RECORDING_MIN_POWER_LEVEL=50

# NEW: parallel workers pool size
RECORDING_WORKERS=4
```

Operate
```bash
systemctl status element-stack.service
docker compose -f /opt/element-stack/compose.json ps
docker compose -f /opt/element-stack/compose.json logs --tail=200 recorder
ls -lah /opt/element-stack/public
```

Troubleshooting
- 403 on start: the user must have PL ≥ RECORDING_MIN_POWER_LEVEL in the room.
- 401 to recorder API: check RECORDER_API_TOKEN wiring between controller/pool and workers.
- “recorder is busy”: all workers are currently occupied; increase RECORDING_WORKERS or wait.
- Synapse won’t start: check data/synapse ownership (uid/gid 991), presence of signing.key.
- Element X/Android: known “dark tile”; use Element Web for recording.

License
- Apache‑2.0 (recommended).

