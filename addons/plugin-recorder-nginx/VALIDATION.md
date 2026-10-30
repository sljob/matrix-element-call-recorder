# Validation — 3.0.0-rc1

Date: 2026-10-30

## Passed in the development sandbox

- Python AST compilation for installer, backend, publisher and tools.
- Node syntax checks for worker, UI module and pinned observer.
- 16 Python tests passed.
- Element Module API registration and status transition test passed with mocked host/fetch.
- Real FFmpeg fragmented MP4 fixture: MP4 validation, audio MP3 extraction, poster extraction, atomic metadata commit and idempotent republish.
- Real Node worker lifecycle: authenticated health, single-slot start, duplicate start rejection, room ownership on stop, finalization marker, publish, restart state recovery.
- API authorization tests: Matrix membership denial, power-level denial, cross-origin POST denial, private file access, HTTP Range 206 and path traversal denial.
- Nginx block insertion idempotence, conflicting location rejection and real `nginx -t` syntax test.
- Docker Compose schema validation with `docker compose config`.
- Installer transaction tests: identity/profile preservation across repair and rollback of Element/Nginx files after simulated reload failure.
- Public tree scan contains no test domain, test password, CA, SPKI hash, token, profile, recording or server log.

## Not claimed as passed

- A fresh installation on an actual clean Ubuntu server.
- A real E2EE MatrixRTC call with the pinned recorder image.
- Camera/video and microphone/audio acceptance through current Element Call, Synapse, LiveKit and Nginx.
- A production install with a private CA and the user's actual SPKI.
- Docker daemon execution in this sandbox.

Therefore this tree is a **release candidate**, not a production-certified release. The acceptance procedure is in `docs/ACCEPTANCE.ru.md`. Do not publish it as “tested production” until the real-server acceptance is completed.
