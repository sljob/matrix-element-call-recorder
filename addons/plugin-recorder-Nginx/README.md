# E2EE Recorder — plugin for a running Element Call server

Adds browser-side recording of end-to-end encrypted Element Call conferences to
a server you already run. It does not modify your existing compose file, nginx
server blocks (unless you ask it to), homeserver.yaml or any upstream image.

This is the **nginx edition**. It generates the nginx locations itself and
requires no docker label provider.

The recorder joins the call as an ordinary Matrix client with its own E2EE keys
inside headless Chromium; Puppeteer and FFmpeg capture the result. It does not
break encryption and it is not a built-in Element Call feature.

## What it adds

* one button in Element Web, next to the call controls;
* <img width="511" height="61" alt="image" src="https://github.com/user-attachments/assets/42a34eed-74d7-4a84-898a-f6a8129878c2" />

* a recordings archive at /recordings/ with Matrix-based authorization;
* one controller, one portal, one publisher and N recorder workers, all in a
  separate compose project named recording-overlay;
* an nginx snippet (`/recording/` and `/recordings/`) written for you, applied
  to the host nginx after `nginx -t` passes, or left as a file when nginx runs
  in a container.

## Requirements

* a running Matrix stack with Element Web behind nginx (host binary or
  container);
* nginx with the `ngx_http_proxy_module` (present in every standard build);
* two free loopback ports (8765, 8899 by default) so nginx can reach the
  controller and the archive portal;
* one or more existing Matrix accounts for the recorders. The installer never
  creates accounts;
* ~2 GB RAM and ~2 vCPU per recorder worker.

## Install

1. Get the sources and pin the recorder core:

       bash tools/fetch-upstream.sh

   It writes vendor/SOURCE with the commit and the SHA256 of each file.
   Commit that file, not the JavaScript.

2. Copy the answers template and protect it:

       cp answers.example /root/answers-plugin.conf
       chmod 600 /root/answers-plugin.conf

3. Fill in DOMAIN, PLUGIN_DIR, RECORDER_ACCOUNTS and RECORDER_PASSWORDS.
   Set INSTALL_LANGUAGE explicitly; see below.

4. Dry run, then install:

       bash install.sh --check   /root/answers-plugin.conf
       bash install.sh --install /root/answers-plugin.conf

## Interface language

INSTALL_LANGUAGE accepts ru or en and applies to the recording button and the
recordings archive. It does not change Element's own interface language.

If the key is missing and the installer runs in a terminal, it asks which
language to use and writes the answer back into the answers file, so later
runs and --uninstall do not ask again. If there is no terminal to ask (CI,
piped output) the default is en with a warning. An unsupported value such as
de stops the installation before anything is built.

Earlier releases silently defaulted to ru in every component, which is why an
English operator could end up with Russian buttons.

## TLS

TLS terminates in nginx, exactly as it already does for Element Web. The plugin
adds no certificates and no TLS configuration; it only adds `proxy_pass`
locations to the vhost that already serves your domain.

If your domain uses a private CA, point `TLS_CA_FILE` at the CA certificate so
the controller and portal trust Synapse over HTTPS:

    TLS_CA_FILE="/root/certs/ca.crt"

The recorder worker receives the same CA and passes it to Chromium, so the
headless browser accepts your Element Call origin.

## Routing with nginx

The installer writes the snippet and, on a host nginx, applies it:

    # generated on every --install and --repair
    /opt/recorder-plugin/nginx/recorder.conf
    # applied to host nginx when it is detected
    /etc/nginx/conf.d/recorder-plugin.conf

The contract is two path prefixes and one static passthrough:

```
/recording/  -> rec-controller:8765   (strip /recording)
/recordings/ -> rec-portal:8899       (strip /recordings)
/files/      -> rec-portal:8899       (legacy root-absolute download links)
```

The trailing slash on `proxy_pass` is what strips the prefix; without it every
API call 404s. nginx matches the longest prefix, so `/recording/` never captures
`/recordings/` and no route priorities are needed. No raised
`proxy_read_timeout` is needed either: `/api/record/stop` answers `202`
immediately and finalization happens asynchronously in the worker and the
publisher while the UI polls `/api/record/status`.

Before reloading, the installer runs `nginx -t` and restores the previous file
if the test fails. If nginx is not installed on the host, the snippet is left in
the plugin directory for you to include:

    include /opt/recorder-plugin/nginx/recorder.conf;

When nginx runs in a container, the installer prints the file to copy into that
container's config directory. Set `NGINX_CONTAINER` in the answers file to
target it explicitly instead of relying on detection.

Upstream addressing follows the mode: `127.0.0.1:8765` / `127.0.0.1:8899` for a
host nginx, `rec-controller:8765` / `rec-portal:8899` for nginx in a container
on the same docker network.

### Ports

`PUBLISH_LOOPBACK_PORTS="true"` (the default) binds both services to
`127.0.0.1:8765` and `127.0.0.1:8899`, so nothing is reachable from outside.
Change `CONTROLLER_PORT` / `REC_HTTP_PORT` if those are taken — the generated
snippet follows automatically.

## Tests

    cd testbed
    docker compose -f compose.fake.yml up -d
    bash smoke.sh
    docker compose -f compose.fake.yml down

The smoke test proves detection, compose generation, button injection, prefix
stripping and token refusal. It cannot prove that Chromium joins a real E2EE
call; that needs Synapse and LiveKit. See docs/TESTING.md.

## Uninstall

    bash install.sh --uninstall /root/answers-plugin.conf

Containers and routers are removed, index.html is restored from the backup
taken before the first install. Recordings, profiles and backups stay in
PLUGIN_DIR. Images are kept on purpose: reinstalling should not rebuild the
1.5 GB Chromium base.

## Known limits

* An Element Web image update replaces index.html; rerun --reapply-ui.
* The recorder appears in the participant list as a normal user.
* PostgreSQL must use the C collation for Synapse; that is a server property,
  not something this plugin sets.
