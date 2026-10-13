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

## Optional staff directory — 0.1.6

Server-only addon: no endpoint agents. Creates one non-admin bot and one private, intentionally unencrypted Staff (Сотрудники) room. Only joined room members are listed; the admin invites employees. The bot does not join other chats or calls. Only bot PL100 can post messages/reactions. Room admin PL99 can invite employees, but cannot post or change state requiring PL100. Root/Synapse server admin powers remain unaffected.

### New server installation
```ini
STAFF_DIRECTORY_ENABLED=false
```
In answers before first installation: false/omitted skips the addon; true installs after the main stack succeeds. The bootstrap validates selection before the main engine; --check creates no accounts/rooms. DOMAIN/INSTALL_DIR come from answers. Interactive domain confirmation and hidden administrative access token input are required. Never put tokens in answers. Requires Python>=3.10, systemd>=250 with systemd-creds/systemd-run/systemd-analyze. No automatic OS upgrades.

### Existing server without the addon

Run `bash addons/staff-directory/install.sh` from a complete local checkout, or the self-contained standalone.sh in that directory. Never rerun the main installer on production or edit immutable answers.conf. Adds only three addon directories (/opt, /etc, /var/lib/matrix-staff-directory) and three systemd services, without restarting existing containers or changing compose, ports or recorder profile. Requires existing compose.json, CA and synapse/jwt/livekit services. New Matrix bot/room creation is not transactional: after failure new entities may remain; encrypted progress.cred records their IDs for recovery. Configured addons are not overwritten. Multiple independent bot/room instances on one server are not supported.

### Status and device origin

Busy: recognized LiveKit ACTIVE. Online/away: Matrix presence. Otherwise recent local device timestamp within600s gives recently online; expired activity with offline presence gives offline according to Matrix; errors show unknown. Each line includes last request origin: mobile/PC. Select maximum last_seen_ts, excluding dehydrated devices. User-Agent classification is a heuristic, not a device-name guess. Unknown clients are labelled unknown; equal timestamps with distinct recognized types are labelled multiple device types. Background sync counts: this is not call-device detection, attention, screen-lock detection or proof of remote working.

Intervals: publisher/presence5s, LiveKit adapter5s, device collector15s. Recent window10min. Server timestamp updates, API time and client delivery add latency: no real-time guarantee. For N members, up to12N presence+4N device requests per minute plus LiveKit. A publication changes when the result changes or on the15min heartbeat, not every5s. A timestamp older than20min is stale. API failures/large membership increase cycle time; monitor load. Services run as root and Docker adapter access is root-equivalent.

### Credentials and disabling

Bot/admin tokens and LiveKit secrets use encrypted systemd host-key credentials (not TPM). Collector gets admin; publisher gets bot/LiveKit only. This is a full Synapse admin token, not a read-only scope; use an independently revocable session. Encryption at rest does not protect against root/process compromise. The host key is separate at /var/lib/systemd/credential.secret; do not transplant .cred files between servers. config.json contains no tokens.

Disable only addon: `bash /opt/matrix-staff-directory/disable-addon.sh`. Bot/room/history remain. Not a transactional Matrix rollback. Do not remove state directories or rerun the main stack installer after addon failure.

### Validation status

Operator confirmed Cloud INSTALL OK and patch26 on13 October2026: ROOM OK, fresh collector, VERIFY OK readback and three active services. Desktop/mobile classification worked. Earlier Meet status logic passed4/4. Cloud four-stage acceptance has not been supplied; no fresh VM deployment with STAFF_DIRECTORY_ENABLED=true was performed. New0.1.6 first installation creates bot-only permissions directly and is tested programmatically, not claimed fully end-to-end-tested. Optional acceptance: `bash addons/staff-directory/acceptance.sh` as root with interactive terminal (message/call/leave/disconnect all account devices10–15min). Do not reinstall or reapply patch26 on current Cloud.

patches/26_cloud_acl_origin_fast.sh preserves the successfully applied patch for ORIGINAL unmodified Cloud0.1.5 only. It uses admin self-demotion before bot ACL changes because equal-power users cannot demote each other. Not a standalone installer or a repeatable migration; new0.1.6 provisioning does not need this step.

Publish bootstrap, addon and sources.json in one commit. SOURCE_COMMIT must identify the complete source commit. Publication is not proof of fresh-VM validation. Addon status text is currently Russian, independently of INSTALL_LANGUAGE. Desktop publication guide: addons/staff-directory/GITHUB-DESKTOP.ru.md.
