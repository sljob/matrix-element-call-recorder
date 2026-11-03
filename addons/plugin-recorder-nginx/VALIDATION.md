# RC2 validation

Input: user-supplied RC1 archive; fixes implemented in this release tree, not on the user server.

Run `sudo env PYTHONDONTWRITEBYTECODE=1 bash tools/verify.sh`. 30 Python tests and Node UI test passed in this environment. Real FFmpeg media fixtures, real nginx -t, Compose schema, HTTP Range, worker lifecycle, installer file transactions and provenance checks run.

New regression coverage: missing inspect regression through check(), single dependency probe, /app ESM workdir and shell args, missing vhost report, POOL_WORKERS bot conflicts, full MXID comparisons, own repair recognition and PID ownership, safe command diagnostics, direct preflight CLI plus shell/installer entrypoints from another directory. Positive CLI tests use fake external Docker/Nginx executables and a local test Matrix login server; they are NOT live Docker tests. Tests that require root must not be silently reported as passed when skipped.

Live Docker image execution, user-server install/repair and real E2EE camera/audio acceptance are NOT verified here. RC2 fixes demonstrated installer failures; it is not a production certification.
