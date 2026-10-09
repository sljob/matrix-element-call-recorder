# Matrix Element Call + E2EE Recorder

[Русский README](README.ru.md)

Browser-based conference recording using Chromium, Puppeteer and FFmpeg. This release replaces encoded installer payloads with readable files downloaded from **sljob/matrix-element-call-recorder**. The recorder participates in the call as a Matrix client with E2EE keys; it does not break encryption.

## New installation

Use a clean Ubuntu server. This is not an upgrade or a language-migration script for an existing installation. Python 3, OpenSSL and iproute2 must be available for preflight. The installer installs Docker/Compose when absent, pulls its pinned service images and builds custom recorder/controller components. Internet access to GitHub, package repositories and image registries is required.

1. Select a full 40-character commit SHA containing this source release.
2. Download and inspect `install.sh` and `answers.example` from that commit.
3. Copy the template to `/root/answers.conf`, set `SOURCE_COMMIT` to that SHA, fill domain, the **new server's local IP**, and TLS file paths. Use `INSTALL_LANGUAGE=en` or `ru` (default: ru). The example IP/domain must be replaced.
4. Run as root:

```bash
chmod 700 ./install.sh
chmod 600 /root/answers.conf
set -o pipefail
bash ./install.sh --check /root/answers.conf 2>&1 | tee /root/install-check.log
# Only after the check succeeds:
bash ./install.sh --install /root/answers.conf 2>&1 | tee /root/install.log
```

`--check` downloads/verifies the source snapshot and runs the existing preflight. It can create the generated-credentials sidecar next to answers.conf; it is not strictly read-only. Retain this private sidecar for retries. Never publish answers.conf, generated credentials, private keys, database dumps or recorder profiles.

`SOURCE_COMMIT` cannot be `main`, a short SHA or a tag. All files are downloaded from the same immutable commit. The GitHub manifest `installer/sources.json` lists SHA256 hashes; all listed files are checked before executing `installer/install.py`. Missing files, failed HTTPS or hash mismatches stop installation. Hashes catch corruption/inconsistent files; they are not an independent signature from a trusted third party. Review the selected commit.

## What is installed

Synapse, PostgreSQL, Element Web/Call, LiveKit, JWT service, Traefik, recording controller, worker pool, protected recordings portal and publisher. The supplied generation logic and fixes are preserved. `RECORDING_WORKERS` accepts 2 or 4. Image pins remain in the installation engine; do not substitute old incompatible image tags.

The recording UI and gallery follow `INSTALL_LANGUAGE`. Element's own UI language remains a client setting. Employee search by display name is enabled; AD import and a complete employee roster are not included. Language and source commit are part of the installation fingerprint: changing answers and rerunning is not a supported migration.

Clients must reach the advertised media IP and ports (UDP 7882, TCP 7881; TURN UDP 3479/TLS TCP 5350 and configured relay UDP 30000–30020). Allow the required return traffic and verify routing. HTTPS success does not prove ICE/media connectivity. Trust your CA on client PCs, configure DNS and verify time synchronization separately. Private LAN IP preflight is retained; public NAT requires a separately reviewed configuration.

## Source layout

- `install.sh`: small download/verification launcher.
- `installer/install.py`: readable installation engine, configuration generation and existing patches.
- `src/`: final recorder, controller, pool and portal source overrides.
- `config/record-button.js`: recording button source.
- `locales/en.json`: English translations; Russian source strings are retained.
- `installer/templates/`: original source templates, publisher, package.json/lock and metadata snippet used by existing patch steps.
- `installer/compat/`: intermediate compatibility sources from the previous installer. Final `src/` overrides are applied afterwards.
- `installer/sources.json`: required file hashes.
- `tools/update-source-manifest.py`: updates hashes after reviewed source edits.

No application source is decoded from Base64. Remaining inline text in install.py consists of readable configuration/build templates, tests and patches. The templates/compat stages intentionally preserve the earlier installer's generation order; they are not downloaded binaries.

## Publishing this release (repository maintainer)

Copy **all contents** of the release ZIP into the repository root, preserving paths. Replace files listed as REPLACE and add NEW files in `GITHUB_PATHS.txt`. Keep existing LICENSE, contribution files and issue templates. No automatic GitHub commit is made by this package.

```bash
python3 tools/update-source-manifest.py
python3 tools/validate-sources.py
# Review your diff, then commit and push the complete release.
git rev-parse HEAD
```

Users paste the resulting SHA into their private answers.conf. Do not insert a commit's own SHA into the same commit: answers.example intentionally contains a placeholder. Publish sources and manifest together. Until this release is pushed, the current old repository commit will not work with the new launcher because required files are absent.

`config/index.html` from the old repository is not consumed by this installer: Element HTML is extracted from the pinned image and the recording button is injected during installation. Do not publish private production files as replacements.

## Verification limits

Bash/Python/JavaScript syntax, extraction equivalence and manifest consistency were checked. The launcher was tested against an in-memory GitHub response fixture for successful verification and corrupt-file rejection. No fresh Ubuntu/Docker deployment or real conference was performed in this environment. Validate a two-PC call, recording stop/finalization, MP4/MP3 playback, access restrictions and both languages before release to production.
