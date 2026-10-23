# E2EE Recorder — plugin for a running Element Call server

Adds browser-side recording of end-to-end encrypted Element Call conferences to
a server you already run. It does not modify your compose file, Traefik static
configuration, homeserver.yaml or any upstream image.

The recorder joins the call as an ordinary Matrix client with its own E2EE keys
inside headless Chromium; Puppeteer and FFmpeg capture the result. It does not
break encryption and it is not a built-in Element Call feature.

## What it adds

* one button in Element Web, next to the call controls;
* a recordings archive at /recordings/ with Matrix-based authorization;
* one controller, one portal, one publisher and N recorder workers, all in a
  separate compose project named recording-overlay;
* two Traefik routers, published through container labels only.

## Requirements

* a running Matrix stack with Element Web behind Traefik;
* Traefik with its **docker provider enabled** and /var/run/docker.sock mounted.
  Labels are the only routing mechanism this plugin uses. If the provider is
  off, or its Docker SDK is older than the daemon minimum API, the installer
  now refuses to start instead of producing unroutable containers;
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

## TLS: why the browser shows TRAEFIK DEFAULT CERT

If your Traefik has no certificate for the domain and no ACME resolver, a
router with tls=true gets Traefik's built-in fallback certificate. Importing
that certificate into a browser does not help, because the request is answered
with the fallback before your trust store matters.

--check and --install probe port 443 with the domain as SNI and report which
certificate is actually served. When the answer is the fallback, the installer
prints the three possible fixes and continues; it cannot fix this from the
outside, because Traefik configuration belongs to the host.

The three options:

* A — ACME: add a resolver to your Traefik and set TRAEFIK_CERTRESOLVER in
  answers. Routers then use it directly.
* B — your own certificate: mount it into the Traefik container and declare it
  in the dynamic configuration:

      tls:
        stores:
          default:
            defaultCertificate:
              certFile: /certs/fullchain.pem
              keyFile: /certs/privkey.pem

* C — a lab with a self-signed CA: create the CA, sign a certificate for the
  domain, then apply B. The CA certificate also goes into TLS_CA_FILE so the
  plugin's own containers trust Synapse and Element Call.

## Routing

Two routers are created:

| Path | Backend | Priority |
|---|---|---|
| /recording and /recording/* | controller:8765 | 100 |
| /recordings and /recordings/* | portal:8899 | 90 |

The recording API rule is PathPrefix(/recording/) or Path(/recording), not
PathPrefix(/recording). The shorter form also matches /recordings, so the API
router with its higher priority would swallow the archive and return 404. This
was a real defect in 0.1.0 and is fixed here.

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
