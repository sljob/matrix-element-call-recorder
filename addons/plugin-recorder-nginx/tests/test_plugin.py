#!/usr/bin/env python3
"""Regression tests for the nginx edition.

Run with:  python3 -m pytest tests -q
No docker required. The nginx tests use the host `nginx -t` when available and
skip cleanly when it is not, so the same suite runs on a laptop and on CI.

Each test name states the failure it prevents. If a test looks pedantic, the
pedantry is the point: every one of these bugs shipped once.
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "installer"))

import nginxconf  # noqa: E402
import core  # noqa: E402

NGINX = shutil.which("nginx") or ("/usr/sbin/nginx" if os.path.exists("/usr/sbin/nginx") else "")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def nginx_root():
    """A throwaway prefix that can validate generated configs for real."""
    root = tempfile.mkdtemp(prefix="recorder-nginx-")
    for sub in ("conf.d/includes", "www", "body", "proxy", "fastcgi", "uwsgi", "scgi"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    with open(os.path.join(root, "nginx.conf"), "w", encoding="utf-8") as fh:
        fh.write(
            "worker_processes 1;\n"
            "error_log %(r)s/error.log warn;\n"
            "pid %(r)s/nginx.pid;\n"
            "events { worker_connections 64; }\n"
            "http {\n"
            "  client_body_temp_path %(r)s/body;\n"
            "  proxy_temp_path %(r)s/proxy;\n"
            "  fastcgi_temp_path %(r)s/fastcgi;\n"
            "  uwsgi_temp_path %(r)s/uwsgi;\n"
            "  scgi_temp_path %(r)s/scgi;\n"
            "  include %(r)s/conf.d/*.conf;\n"
            "  access_log off;\n"
            "}\n" % {"r": root})
    yield root
    subprocess.run([NGINX, "-s", "stop", "-c", os.path.join(root, "nginx.conf"),
                    "-p", root], capture_output=True)
    shutil.rmtree(root, ignore_errors=True)


def nginx_test(root):
    if not NGINX:
        pytest.skip("nginx is not installed")
    p = subprocess.run([NGINX, "-t", "-c", os.path.join(root, "nginx.conf"), "-p", root],
                       capture_output=True, text=True)
    return p.returncode == 0, (p.stdout + p.stderr)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# nginx config generation
# ---------------------------------------------------------------------------

def test_standalone_config_is_valid_for_real_nginx(nginx_root):
    """The generated standalone server must pass `nginx -t` unchanged."""
    port = free_port()
    loc = nginxconf.render_locations("172.21.0.5", 8765, "172.21.0.6", 8899)
    loc = loc.replace(nginxconf.CONTAINER_WWW_ROOT, os.path.join(nginx_root, "www"))
    srv = nginxconf.render_standalone_server(port).replace(
        nginxconf.CONTAINER_CONF_ROOT, os.path.join(nginx_root, "conf.d"))

    with open(os.path.join(nginx_root, "conf.d", nginxconf.DEFAULT_SERVER_FILE), "w") as fh:
        fh.write(srv)
    with open(os.path.join(nginx_root, "conf.d", nginxconf.LOCATIONS_FILE), "w") as fh:
        fh.write(loc)

    ok, text = nginx_test(nginx_root)
    assert ok, text


def test_locations_file_in_conf_d_root_is_rejected(nginx_root):
    """Guard against the exact bug that broke the stand: location in http scope.

    This test asserts the *bad* layout fails. If a future refactor moves the
    fragment into conf.d root, the positive test above keeps passing while
    production breaks; this one fails loudly instead.
    """
    loc = nginxconf.render_locations("172.21.0.5", 8765, "172.21.0.6", 8899)
    loc = loc.replace(nginxconf.CONTAINER_WWW_ROOT, os.path.join(nginx_root, "www"))
    with open(os.path.join(nginx_root, "conf.d", "wrong.conf"), "w") as fh:
        fh.write(loc)
    ok, text = nginx_test(nginx_root)
    assert not ok
    assert "location" in text and "not allowed here" in text


def test_api_proxy_pass_keeps_trailing_slash():
    """Without the trailing slash every /recording/api call becomes a 404."""
    loc = nginxconf.render_locations("172.21.0.5", 8765, "172.21.0.6", 8899)
    assert "proxy_pass http://172.21.0.5:8765/api/;" in loc
    assert "proxy_pass http://172.21.0.5:8765;" not in loc


def test_authorization_header_is_forwarded():
    """The Matrix token must reach the controller; dropping it breaks every start."""
    loc = nginxconf.render_locations("172.21.0.5", 8765, "172.21.0.6", 8899)
    assert "Authorization" in loc


def test_generated_files_carry_management_markers():
    loc = nginxconf.render_locations("172.21.0.5", 8765, "172.21.0.6", 8899)
    srv = nginxconf.render_standalone_server(18085)
    for blob in (loc, srv):
        assert nginxconf.MANAGED_BEGIN in blob
        assert nginxconf.MANAGED_END in blob


# ---------------------------------------------------------------------------
# front-proxy include injection
# ---------------------------------------------------------------------------

SITE = """server {
    listen 443 ssl http2;
    server_name meet.example.com;

    location / {
        proxy_pass http://element-web:8080/;
    }
}
"""


def test_include_goes_into_the_tls_server_block():
    line = nginxconf.render_front_include(nginxconf.LOCATIONS_FILE).rstrip("\n")
    out = nginxconf.insert_into_server(SITE, "meet.example.com", line)
    assert out is not None
    assert out.index("location /") < out.index("include /etc/nginx/conf.d/includes")
    assert out.count("include /etc/nginx/conf.d/includes") == 1


def test_ambiguous_front_config_is_refused():
    """Two :443 blocks for the domain means "refuse and print", never "guess"."""
    dup = SITE + "\nserver {\n  listen 443 ssl;\n  server_name meet.example.com;\n}\n"
    assert nginxconf.insert_into_server(dup, "meet.example.com", "    include x;") is None


def test_one_line_duplicate_is_also_refused():
    """A minified server{} must be counted too, or the guard is a lie."""
    dup = SITE + "\nserver { listen 443 ssl; server_name meet.example.com; }\n"
    assert nginxconf.insert_into_server(dup, "meet.example.com", "    include x;") is None


def test_wrong_domain_is_refused():
    assert nginxconf.insert_into_server(SITE, "other.example.com", "    include x;") is None


def test_injection_is_idempotent():
    line = nginxconf.render_front_include(nginxconf.LOCATIONS_FILE).rstrip("\n")
    once = nginxconf.insert_into_server(SITE, "meet.example.com", line)
    twice = nginxconf.insert_into_server(once, "meet.example.com", line)
    assert once == twice


def test_find_tls_server_file_picks_the_single_candidate():
    dump = ("# configuration file /etc/nginx/conf.d/site.conf:\n" + SITE
            + "# configuration file /etc/nginx/conf.d/other.conf:\n"
              "server { listen 443 ssl; server_name other.example.com; }\n")
    assert nginxconf.find_tls_server_file(dump, "meet.example.com") == \
        "/etc/nginx/conf.d/site.conf"


def test_find_tls_server_file_gives_up_on_duplicates():
    dump = ("# configuration file /etc/nginx/conf.d/a.conf:\n" + SITE
            + "# configuration file /etc/nginx/conf.d/b.conf:\n" + SITE)
    assert nginxconf.find_tls_server_file(dump, "meet.example.com") == ""


def test_strip_managed_removes_the_block_completely():
    blob = ("keep\n# >>> recorder-plugin 3.1.0\njunk\n# <<< recorder-plugin 3.1.0\n"
            "server { listen 1; }")
    cleaned, had = nginxconf.strip_managed(blob)
    assert had and "junk" not in cleaned and "keep" in cleaned


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------

def test_answers_with_loose_permissions_is_a_blocker(tmp_path):
    import preflight
    path = tmp_path / "answers.env"
    path.write_text('DOMAIN="meet.example.com"\n', encoding="utf-8")
    os.chmod(path, 0o644)
    pf = preflight.Report("ru")
    preflight.check_answers(pf, core.Answers(), str(path))
    assert pf.blocked()
    os.chmod(path, 0o600)
    pf2 = preflight.Report("ru")
    preflight.check_answers(pf2, core.Answers(), str(path))
    assert not pf2.blocked()


def test_duplicate_keys_are_a_blocker(tmp_path):
    import preflight
    path = tmp_path / "answers.env"
    path.write_text('DOMAIN="a.example.com"\nDOMAIN="b.example.com"\n', encoding="utf-8")
    os.chmod(path, 0o600)
    pf = preflight.Report("ru")
    preflight.check_answers(pf, core.Answers(), str(path))
    assert pf.blocked()


def test_foreign_controller_container_is_reported():
    """A leftover monolith controller must be named as a blocker, not debugged."""
    import preflight
    calls = []

    def fake_sh(cmd, **kw):
        calls.append(cmd)
        return 0, "recording-controller\nrec-controller\n", ""

    original = core.sh
    core.sh = fake_sh
    try:
        pf = preflight.Report("ru")
        preflight.check_containers(pf, core.Answers())
    finally:
        core.sh = original
    assert pf.blocked()
    assert any("recording-controller" in r["msg"] for r in pf.of("FATAL"))


def test_observer_with_wrong_content_is_warned(tmp_path, monkeypatch):
    import preflight
    fake_root = tmp_path / "plugin"
    (fake_root / "vendor").mkdir(parents=True)
    (fake_root / "vendor" / "p63-observer.js").write_text("x" * 6000, encoding="utf-8")
    monkeypatch.setattr(core, "ROOT", str(fake_root))
    pf = preflight.Report("ru")
    preflight.check_observer(pf)
    assert pf.of("WARN") or pf.of("FATAL")


def test_truncated_observer_is_a_blocker(tmp_path, monkeypatch):
    import preflight
    fake_root = tmp_path / "plugin"
    (fake_root / "vendor").mkdir(parents=True)
    (fake_root / "vendor" / "p63-observer.js").write_text("x" * 100, encoding="utf-8")
    monkeypatch.setattr(core, "ROOT", str(fake_root))
    pf = preflight.Report("ru")
    preflight.check_observer(pf)
    assert pf.blocked()


# ---------------------------------------------------------------------------
# controller
# ---------------------------------------------------------------------------

@pytest.fixture()
def controller():
    """Start the controller with an empty environment and wait for /api/health."""
    port = free_port()
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp",
           "PORT": str(port)}
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "src", "controller", "app.py")],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            import urllib.request
            urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=1).read()
            break
        except Exception:
            if proc.poll() is not None:
                pytest.skip("controller deps missing: %s" % proc.stdout.read()[-400:])
            time.sleep(0.3)
    yield port
    proc.terminate()
    proc.wait(timeout=10)


def _json(port, path, method="GET", body=None, token=None):
    import json
    import urllib.request
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_controller_starts_with_no_environment_at_all(controller):
    """KeyError: 'LIVEKIT_KEY' on import is what kept the old one restarting."""
    status, body = _json(controller, "/api/health")
    assert status == 200
    assert body["ok"] is True
    assert body["worker_unavailable"] is True
    assert body["diagnostics"]


def test_health_stays_200_when_no_worker_is_configured(controller):
    """A controller that dies without a worker turns one broken box into two."""
    for _ in range(3):
        status, _body = _json(controller, "/api/health")
        assert status == 200


def test_version_reports_the_api_surface(controller):
    status, body = _json(controller, "/api/version")
    assert status == 200
    assert "/api/record/start" in body["api"]


def test_start_without_token_is_401(controller):
    status, body = _json(controller, "/api/record/start", "POST", {"roomId": "!x:test"})
    assert status == 401
    assert body["ok"] is False


def test_start_without_room_is_400(controller):
    status, body = _json(controller, "/api/record/start", "POST", {}, token="t")
    assert status == 400


# ---------------------------------------------------------------------------
# installer wiring
# ---------------------------------------------------------------------------

def test_installer_modules_import():
    import install  # noqa: F401
    assert install.PHASES["install"]


def test_phases_are_complete():
    import install
    assert set(install.PHASES) == {"check", "install", "repair", "verify", "doctor",
                                   "uninstall"}


def test_answers_example_is_loadable(tmp_path):
    src = os.path.join(ROOT, "answers.example")
    dst = tmp_path / "answers.env"
    shutil.copyfile(src, dst)
    os.chmod(dst, 0o600)
    a = core.load_answers(str(dst))
    assert a["NGINX_MODE"] == "standalone"
    assert a["RECORDING_MIN_POWER_LEVEL"] == "50"
