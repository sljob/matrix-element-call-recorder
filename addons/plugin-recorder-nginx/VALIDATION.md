# RC4 validation

47 Python tests passed with root privileges; Node UI test passed.
Seven new regressions cover nginx -T stdout/stderr separation, success notices, deprecation and duplicate diagnostics, and command failures.
Actual Nginx HTTPS module delivery, umask 077 permissions, rollback, FFmpeg publication and Node lifecycle remain tested.
Matrix authorization and positive CLI tests use explicit test doubles.
Actual Docker service deployment and real Matrix E2EE A/V capture were NOT performed.
This source release fixes reproduced installer defects; it does not supply the missing recorder Docker image.
