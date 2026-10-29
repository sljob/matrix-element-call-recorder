# Changelog

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
  button failed. Declared next to the other environment reads, `.rstrip("/")`
  included.
- **`RECORDER_URLS` pointed at loopback.** Workers run with
  `network_mode: host`, the controller on a bridge network, so
  `http://127.0.0.1:<port>` resolved to the controller itself and
  `worker_health()` always returned `None`. Now `http://host.docker.internal:<port>`
  using the `host-gateway` mapping that was already declared in the template.
- **`/api/record/status` ignored the button's parameter name.** The UI sends
  `roomId`, older clients `room_id`, the legacy controller `room`. All three
  are accepted now.

### Verified before release
- `render_compose()` produces valid YAML in both single- and dual-network
  configurations (parsed with a YAML loader, not by eye).
- Controller endpoints exercised against a stub Synapse and a stub worker:
  `/health` 200, `/start` 202, duplicate `/start` 200 `already active`,
  `/stop` 202, `/status` transitions, `401` without a token, `400` without a
  room id, `503` with no idle worker. No route returns 500.
- Generated nginx block: balanced braces, every directive terminated, trailing
  slash present on both `proxy_pass` targets, `Authorization` untouched.
