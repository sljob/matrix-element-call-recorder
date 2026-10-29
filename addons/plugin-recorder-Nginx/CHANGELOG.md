# Changelog

## 2.2.0 — pre-install conflict detection

Installing used to be optimistic: it verified that things existed, then built.
On a fresh snapshot that was not enough, because existence is not the same as
usability. The recorded failure was a bot account that existed with a different
password — every check was green, the install completed, and the first
recording died with `M_FORBIDDEN` inside the worker where nothing is logged.

This release moves every such question to before the build.

### Added

- **`installer/preflight.py`** — a standalone conflict-detection module with
  severity levels (`fatal`, `warn`, `info`, `ok`) and one printable report.
  Checks are plain functions over data, which makes each one testable against
  fixtures without Docker.
- **Real bot login verification.** `POST /_matrix/client/v3/login` is attempted
  for every bot, so an existing account with an unknown or mismatched password
  is now a blocker instead of a silent install.
- **Port conflicts.** Each port is tested for a listener, and the holder is
  identified: our own `rec-*` container (handled), a foreign container (blocker
  with its name), or a host process (blocker with name and pid from `ss -lntpH`).
- **Template smoke-render.** Compose and the nginx snippet are rendered into a
  scratch directory and parsed. Unrendered `{placeholders}`, invalid YAML,
  leftover traefik labels, unbalanced braces or missing block markers block the
  install — these are defects in the installer, and they must not reach disk.
- **Observer fix audit.** The core is checked for every fix that recording
  depends on (`REC_USER`, `REC_PASS`, `ROOM_ID`, `OUT_FILE`, `-vsync cfr`,
  `/tmp/ffprogress.log`, `MAP ` with a space, `virtual_speaker.monitor`). A
  stale `vendor/` copy is a blocker rather than a runtime failure.
- **nginx conflict detection.** `nginx -t` runs first, and every existing
  `*.conf` is scanned for another block that already serves `/recording/` with
  `proxy_pass`; two locations on one prefix means nginx takes the first one.
- **Leftover and image inventory.** Containers and images from a previous
  attempt are listed; stray `rec-*` containers that are not ours are a warning.
- **Answers file inspection.** Duplicate keys (the last silently wins), unknown
  keys (typos), `DOMAIN` with a scheme, `PLUGIN_DIR` pointing at a system
  directory, and `ADOPT_EXISTING` combined with loopback ports.
- **Resource check.** RAM against `2 GB × workers + 2 GB`, and free disk on `/`
  and `/var/lib/docker`.
- **`tools/resolve-bot-accounts.sh`** — inspect, reset the password through the
  Synapse admin API, or deactivate accounts so the installer recreates them.
  `--set-env` writes a working password back into the answers file. Reset is
  the default recommendation because it keeps the bot's device and E2EE session.
- **`docs/PREFLIGHT.md` / `.ru.md`** — every check, its severity, and the
  seventeen fixture scenarios.
- **`docs/INSTALL-SNAPSHOT.ru.md`** — end-to-end runbook for a clean snapshot,
  including how to get the files there (scp, git clone, archive).

### Changed

- `--check` is now a real gate: it exits `2` when blockers are present.
- `--install` refuses to proceed on blockers and exits `3`.
- Obstacles are overridden only by `FORCE_INSTALL="true"`, deliberately not by
  `--yes`: skipping a blocker has to be a decision, not a side effect of
  automation.
- Login probes treat `429` as a warning, never a blocker, so the check cannot
  manufacture the rate limit it is looking for.
- Passwords travel through `urllib`, not a quoted `curl` command line, so shell
  quoting can no longer turn a correct password into a false `M_FORBIDDEN`.

### Verified

- Seventeen preflight scenarios match their expected severity against a fake
  Synapse that reproduces `M_FORBIDDEN`, plus a live port holder and synthetic
  answers files.
- The composer/nignx render test caught a real defect during development: the
  smoke context was missing `ca_file`, and `render_compose()` dereferences it
  unconditionally. That is exactly the class of bug the check exists for.

## 2.1.0 — nginx edition

Forked from the Traefik variant (2.0.1). Routing is generated for nginx; the
Traefik label provider is no longer used anywhere.

### Routing
- **Removed** all `traefik.*` container labels from the compose templates.
- **Removed** Traefik discovery (`find_traefik`), the docker-provider probe and
  the Docker-SDK-too-old guard; they cannot fail a deployment any more.
- **Added** `write_nginx_snippet()`: generates `/recording/` and `/recordings/`
  locations (plus legacy `/files/`), writes them to
  `${PLUGIN_DIR}/nginx/recorder.conf`, and on a host nginx also to
  `/etc/nginx/conf.d/recorder-plugin.conf`.
- The generated block is delimited by `# >>> recorder-plugin` /
  `# <<< recorder-plugin` markers, so `--repair` replaces it instead of
  appending a duplicate.
- `nginx -t` runs before reload; on failure the previous file is restored (or
  the new one removed) and nginx is not reloaded.
- Upstream addressing adapts to the detected mode: `127.0.0.1:PORT` for host
  nginx, `rec-controller:8765` / `rec-portal:8899` for nginx in a container.
- `NGINX_CONTAINER` answers key replaces `TRAEFIK_CONTAINER`.
- When the plugin network differs from Element Web's, services join both
  networks so a containerised nginx can reach them by service name.

### Fixes carried into this fork
- **`SYNAPSE_URL` was never read from the environment.** `app.py` used it in
  `whoami()` and `room_power()` but never assigned it, so every
  `POST /api/record/start` raised `NameError` and Flask answered **500**.
  `/health` was unaffected, which made the controller look healthy while the
  button failed.
- **`RECORDER_URLS` pointed at loopback.** Workers run with
  `network_mode: host`, the controller on a bridge network, so
  `http://127.0.0.1:<port>` resolved to the controller itself and
  `worker_health()` always returned `None`. Now `http://host.docker.internal:<port>`
  using the `host-gateway` mapping that was already declared in the template.
- **`/api/record/status` ignored the button's parameter name.** The UI sends
  `roomId`, older clients `room_id`, the legacy controller `room`. All three
  are accepted now.
- **Worker/observer environment contract.** `server.js` now passes `REC_USER`,
  `REC_PASS`, `ROOM_ID` and `OUT_FILE`, which is what the observer reads; the
  previous names never matched, so the observer exited with `ROOM_ID required`.

### Verified
- `render_compose()` produces valid YAML in both single- and dual-network
  configurations (parsed with a YAML loader, not by eye).
- Controller endpoints exercised against a stub Synapse and a stub worker:
  `/health` 200, `/start` 202, duplicate `/start` 200 `already active`,
  `/stop` 202, `/status` transitions, `401` without a token, `400` without a
  room id, `503` with no idle worker. No route returns 500.
- Generated nginx block: balanced braces, every directive terminated, trailing
  slash present on both `proxy_pass` targets, `Authorization` untouched.
