# Changelog

All notable changes to the E2EE Recorder plugin. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
semantic versioning.

## [2.0.1] — Permission split, nginx support

### Added

- **`RECORDINGS_MIN_POWER_LEVEL`** (default `50`). A separate gate for reading
  the archive, independent from `RECORDING_MIN_POWER_LEVEL` which gates
  recording. Being allowed to record and being allowed to read the result back
  are different rights, and the reference monolithic portal already behaved
  this way; the overlay portal did not.
- **`RECORDING_MIN_POWER_LEVEL`** is now documented in `answers.example`. It was
  read by the installer all along (`a.i("RECORDING_MIN_POWER_LEVEL", 50)`) but
  never listed, so it could not be changed without reading the source.
- **`nginx.example.conf`** — three locations (API, archive, legacy `/files/`)
  with the prefix-strip and streaming bits that actually matter.
- **`PUBLISH_LOOPBACK_PORTS`** (default `true`). Publishes `rec-controller` and
  `rec-portal` on `127.0.0.1:8765` / `127.0.0.1:8899` so an external reverse
  proxy can reach them. The installer refuses to start if either port is taken.
- `minPowerLevel` is written into Element Web's module config, so the button's
  local pre-check matches what the controller enforces.

### Fixed

- **Archive authorization was membership-only.** `allowed_entries()` read the
  power level and discarded it (`member, _pl = ...`), and `/files/<rel>` did the
  same, so any room member could list and download that room's recordings. Both
  now require `power >= RECORDINGS_MIN_POWER_LEVEL`. A copied link is no longer
  a capability.
- **Download links were root-absolute** (`href='/files/...'`), which broke
  behind any prefix strip — both the Traefik router and the nginx example. They
  are now relative to the archive page.
- The `answers.example` header told users to run `./install.sh --answers file`
  while the CLI takes the file positionally; both forms are now documented.

## [2.0.0] — Official Element Web Module API (ESM)

### Changed

- **Native React integration via Element Web Module API.** Replaced the legacy
  DOM `MutationObserver` + `index.html` `<script>` injection with an official
  ESM module (`index.js`, `manifest.json`, `package.json`) registered at
  `/modules/recorder/index.js` using `api.extras.addRoomHeaderButtonCallback`.
  The record/stop controls are now rendered by Element Web's own React 19 tree
  inside `RoomHeader` and never disappear on re-render.
- **Zero domain hardcoding.** All API and archive requests are resolved
  dynamically against `window.location.origin` (`/recording/api/record/...` and
  `/recordings/`).
- **Read-only bind-mount & rootless nginx support.** `install.py` inspects
  Element Web container bind-mounts (`:ro`), updates both the host-mounted
  `config.json` and `/tmp/element-web-config/config.json`, cleans legacy
  `record-button.js` script tags, and stages `/modules/recorder` using UID 0.
- **Extended modes & answer file (`answers.example`).** Added `--check`,
  `--install`, `--repair` (`--reapply-ui`), and `--uninstall` modes, host
  prerequisite verification with interactive `apt-get` install prompt, and
  multi-bot configuration (`RECORDER_BOT_COUNT`, `RECORDER_BOT_USERS`,
  `RECORDER_BOT_PREFIX`, `RECORDER_BOT_PASSWORDS`).

## [0.1.1] — fixes found by a real deployment

0.1.0 was tested against a synthetic stack and passed. Installing it on a real
Synapse + Element + Traefik host exposed four defects. All four are fixed here.

### Fixed

- **`/recording` captured `/recordings`.** The API router used
  `PathPrefix(/recording)`, which also matches the archive path. With priority
  100 against the portal's 90, every request to `/recordings/` was sent to the
  controller with the prefix stripped, and Flask answered 404. The rule is now
  `PathPrefix(/recording/) || Path(/recording)`, so `/recordings` no longer
  belongs to the API router.

- **No docker-provider preflight.** Traefik 3.2 presents Docker API 1.24 to a
  daemon that requires 1.40 and newer. Every container label was silently
  ignored, no router was created, yet `--install` printed "overlay stack
  started". `check_routing_prereqs()` now inspects the Traefik command line and
  its log, and refuses to install with a description of both fixes.

- **TLS was assumed, never checked.** With no certresolver and no default
  certificate, a router with `tls=true` receives Traefik's built-in fallback
  certificate. Importing that certificate into a browser cannot help, because
  the fallback is served before the trust store is consulted. `--check` and
  `--install` now probe port 443 with the domain as SNI and report which
  certificate is actually presented, then print the three possible fixes.

- **`INSTALL_LANGUAGE` defaulted to `ru` in silence.** Every component read the
  variable and fell back to Russian, so an English operator got Russian
  buttons. The installer now asks when the key is missing and a terminal is
  available, writes the answer back into the answers file, and refuses
  unsupported values such as `de` before building anything.

### Changed

- `answers.example` sets `INSTALL_LANGUAGE=en`, documents that an empty value
  means "ask, or `en` without a terminal", and explains the TLS situation
  instead of assuming the host has a working certificate.
- The install plan prints the resolved language and the TLS mode.
- Version bumped to 0.1.1.

### Added

- `locales/ru.json`, so both interface languages are shipped as data rather
  than hard-coded in the button script.
- This changelog.

## [0.1.0] — initial release

- Recorder joins an E2EE Element Call room as a regular Matrix client inside
  headless Chromium, with Puppeteer and FFmpeg capturing the result.
- One controller, one portal, one publisher and N workers in the separate
  `recording-overlay` compose project.
- Button injected into Element Web's `index.html`, with a backup taken first.
- Two Traefik routers published through container labels only.
- `--check`, `--install`, `--reapply-ui`, `--uninstall`.
- Archive at `/recordings/` with Matrix-based authorization: a copied link is
  not a capability.
- Fake-stack smoke test covering detection, compose rendering, injection,
  prefix stripping and token refusal.

### Known limitation at 0.1.0

- The archive was unreachable because of the route defect fixed in 0.1.1.
