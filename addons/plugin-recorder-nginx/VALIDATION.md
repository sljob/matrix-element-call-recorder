# RC3 validation

40 Python tests passed with root privileges, no skips; Node UI contract test passed.
Real Nginx process: HTTPS GET exact ui/index.js bytes and Element config, non-root worker file access.
Regression: umask 077 release permissions, comments/quotes/variables/nested Nginx blocks, inactive and duplicate vhosts, incorrect endpoints/mock health, deployment rollback.
Real FFmpeg fragmented MP4 publication and Node worker lifecycle tests passed. Matrix authorization uses test doubles; CLI preflight fixture uses explicit fake tools.
Docker Compose config schema validated; actual Docker containers and real Matrix E2EE call NOT executed.
This is a corrected release candidate, NOT production or real-camera certification. Read START-HERE.ru.md.
