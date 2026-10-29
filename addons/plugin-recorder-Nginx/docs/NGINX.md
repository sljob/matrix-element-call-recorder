# Running the plugin behind nginx instead of nginx

nginx is only needed by the **installer**, to publish routes as container
labels. Serving traffic is a plain reverse-proxy job, and nginx does it fine.

## Contract

| Path | Backend | Prefix handling |
|---|---|---|
| `/recording` and `/recording/*` | `rec-controller:8765` | strip `/recording` |
| `/recordings` and `/recordings/*` | `rec-portal:8899` | strip `/recordings` |

Ready-to-paste config: [`nginx.example.conf`](../nginx.example.conf). Paste its
three `location` blocks into the `server { }` that already serves Element Web,
then:

    nginx -t && systemctl reload nginx

## Backend reachability

A host nginx reaches the backends only through published ports:

    PUBLISH_LOOPBACK_PORTS="true"     # default — binds 127.0.0.1:8765 and :8899

If nginx runs in a container on the same docker network, keep it `false` and
target the service names instead:

    proxy_pass http://rec-controller:8765/;
    proxy_pass http://rec-portal:8899/;

Without published ports a host nginx gets `connection refused` — that is the
first thing to check when `/recording/health` does not answer.

## Three things that are easy to get wrong

1. **`proxy_pass` must end with `/`.** That trailing slash is what strips the
   prefix. Without it the controller receives `/recording/api/record/start`
   and answers 404, while the route itself looks healthy.
2. **Never write `proxy_pass http://127.0.0;`.** nginx refuses to start with
   `host not found in upstream` and takes the whole vhost — and every site on
   it — down with it.
3. **No `proxy_read_timeout` bump is needed.** `/api/record/stop` answers `202`
   immediately; finalization runs asynchronously in the worker and the
   publisher, and the UI polls `/api/record/status`.

nginx needs no priorities here: longest-prefix matching already keeps
`location /recording/` from capturing `/recordings/`. That is exactly what the
nginx variant solves with priorities 100/90.

## Verification

On any machine that can reach the domain — no access to the server required:

    curl -s -o /dev/null -w '%{http_code}\n' https://<domain>/recording/health
    # 200 or 401 -> route, prefix strip and backend are all correct
    # 404        -> the /recording prefix was not stripped
    # 502        -> the backend is down or the port is not published

    curl -s -o /dev/null -w '%{http_code}\n' https://<domain>/recordings/
    # non-200 for an anonymous visitor, as expected

    curl -s https://<domain>/config.json | jq '.modules'
    # must list the recorder module, otherwise the button never loads

## Access model

| Action | Requirement |
|---|---|
| Start / stop a recording | room power level >= `RECORDING_MIN_POWER_LEVEL` (default 50) |
| Watch / download a room's recordings | room power level >= `RECORDINGS_MIN_POWER_LEVEL` (default 50) |
| See every recording | Synapse server admin, via `/_synapse/admin/v2/users` |

The Record button is rendered for every room member; the permission gate is on
the server. A power level computed in the browser is used only for a friendlier
error message and is never trusted: `POST /api/record/start` resolves the
caller through `/_matrix/client/v3/account/whoami`, reads `m.room.power_levels`
and answers `403` on its own.
