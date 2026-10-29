#!/usr/bin/env python3
"""
Matrix Element Call Recorder Plugin v2.0 — Installer Engine

Modes:
  check      Read-only discovery, component verification & installation plan.
  install    Build/start backend services, generate the nginx server block, register
             the ESM module in Element Web via the official Module API.
  repair     Re-apply Element Web ESM module (/modules/recorder), config.json
             module entry & nginx server block (safe after container updates).
  uninstall  Remove plugin containers, nginx snippet & Element Web module
             registration while preserving recordings and bot E2EE profile-spk.

Architecture (v2.0):
  - UI integration uses the official Element Web Module API
    (`api.extras.addRoomHeaderButtonCallback`) shipped as an ESM bundle at
    `/modules/recorder/index.js` + `/modules/recorder/package.json`
    and registered in Element Web's `config.json` ("modules" array and
    "io.github.matrix-element-call-recorder" settings object).
  - Never uses DOM MutationObserver overlay or index.html <script> injection
    (legacy <script src="/record-button.js.patch.js"> markers are cleaned up
    automatically during install/repair/uninstall).
  - Never hardcodes any domain: the browser module resolves every request
    relative to `window.location.origin`.
"""

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse

PLUGIN_VERSION = "2.1.0"
PROJECT = "recording-overlay"
NGINX_PREFIX = "/recording"
NGINX_PORTAL_PREFIX = "/recordings"
MODULE_CONFIG_KEY = "io.github.matrix-element-call-recorder"
MODULE_REL_ENTRYPOINT = "/modules/recorder/index.js"
LEGACY_MARKER_BEGIN = "<!-- recorder-plugin:begin -->"
LEGACY_MARKER_END = "<!-- recorder-plugin:end -->"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

R, B, C, G, Y, E = "\033[0m", "\033[1m", "\033[36m", "\033[32m", "\033[33m", "\033[31m"

MESSAGES = {
    "ru": {
        "missing_pkg_title": "Обнаружены отсутствующие системные компоненты:",
        "missing_cnt_title": "Не найдены обязательные контейнеры Matrix/Element:",
        "offer_install": "Установить недостающие пакеты сейчас через apt-get? [y/N]: ",
        "installing_pkgs": "Установка недостающих пакетов: %s",
        "abort_missing": "Установка прервана: установите недостающие компоненты и повторите запуск.",
        "check_ok": "Проверка завершена (режим read-only — изменения на сервере не вносились).",
        "install_ok": "Плагин кнопки записи v2.0 успешно установлен.",
        "repair_ok": "Восстановление (repair) модуля UI и маршрутов успешно выполнено.",
        "uninstall_ok": "Плагин удалён. Записи и E2EE-профиль бота (profile-spk) сохранены.",
    },
    "en": {
        "missing_pkg_title": "Missing host packages/binaries detected:",
        "missing_cnt_title": "Required Matrix/Element containers not found:",
        "offer_install": "Install missing host packages now via apt-get? [y/N]: ",
        "installing_pkgs": "Installing missing packages: %s",
        "abort_missing": "Aborted: install the missing components and re-run.",
        "check_ok": "Check completed (read-only mode — nothing was modified on the host).",
        "install_ok": "Call Recorder Plugin v2.0 installed successfully.",
        "repair_ok": "UI module and routing repair completed successfully.",
        "uninstall_ok": "Plugin removed. Recordings and bot E2EE profile (profile-spk) were preserved.",
    },
}


def say(kind, msg):
    icon = {"ok": G + "OK " + R, "warn": Y + "!! " + R,
            "err": E + "XX " + R, "info": C + "-- " + R}[kind]
    print(icon + msg)
    sys.stdout.flush()


def die(msg):
    say("err", msg)
    sys.exit(1)


def tr(a, key):
    lang = a.get("INSTALLER_LANG", "ru")
    if lang not in ("ru", "en"):
        lang = "ru"
    return MESSAGES[lang].get(key, MESSAGES["en"].get(key, key))


def sh(cmd, check=False, timeout=300):
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"
    except FileNotFoundError as exc:
        return 127, "", "%s: %s" % (cmd[0] if isinstance(cmd, list) else cmd, exc.strerror)
    out, err = p.stdout.strip(), p.stderr.strip()
    if check and p.returncode != 0:
        die("command failed: %s\n%s" % (cmd, err[-1500:]))
    return p.returncode, out, err


def docker_json(args):
    rc, out, _ = sh(["docker"] + args)
    if rc != 0 or not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------
# answers parsing & v1/v2 key normalization
# --------------------------------------------------------------------------

class Answers(dict):
    def __missing__(self, key):
        return ""

    def b(self, key, default=False):
        v = str(self.get(key, "")).strip().lower()
        return default if not v else v in ("1", "true", "yes", "on")

    def i(self, key, default=0):
        try:
            return int(str(self.get(key, "")).strip())
        except (TypeError, ValueError):
            return default


def load_answers(path, assume_yes=False):
    if not os.path.isfile(path):
        die("answers file not found: %s" % path)
    a = Answers()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            a[k.strip()] = v.strip().strip('"').strip("'")
    a["__path__"] = path
    if assume_yes:
        a["__yes__"] = "true"
    normalize_answers(a)
    return a


def normalize_answers(a):
    if not a.get("INSTALLER_LANG"):
        a["INSTALLER_LANG"] = a.get("INSTALL_LANGUAGE") or "ru"
    if a["INSTALLER_LANG"] not in ("ru", "en"):
        a["INSTALLER_LANG"] = "ru"

    if not a.get("BUTTON_LANG"):
        a["BUTTON_LANG"] = a.get("INSTALL_LANGUAGE") or a["INSTALLER_LANG"] or "auto"
    if a["BUTTON_LANG"] not in ("ru", "en", "auto"):
        die("unsupported BUTTON_LANG=%r (expected: ru | en | auto)" % a["BUTTON_LANG"])

    a["INSTALL_LANGUAGE"] = "en" if a["BUTTON_LANG"] == "en" else "ru"

    base_dir = a.get("BASE_DIR", "").strip()
    if not base_dir or base_dir == "auto":
        base_dir = "/matrix" if os.path.isdir("/matrix") else "/opt/recorder-plugin"
    a["BASE_DIR"] = base_dir

    if not a.get("PLUGIN_DIR"):
        a["PLUGIN_DIR"] = (os.path.join(base_dir, "recorder-plugin")
                           if base_dir == "/matrix" else base_dir)
    if not a.get("RECORDINGS_DIR"):
        a["RECORDINGS_DIR"] = os.path.join(a["PLUGIN_DIR"], "public")
    if not a.get("RECORDER_BOT_PROFILE_DIR"):
        a["RECORDER_BOT_PROFILE_DIR"] = os.path.join(a["PLUGIN_DIR"], "profile-spk")

    for k, legacy in (("ELEMENT_WEB_CONTAINER", "ELEMENT_CONTAINER"),
                      ("NGINX_CONTAINER", ""),
                      ("SYNAPSE_CONTAINER", ""),
                      ("ELEMENT_CALL_CONTAINER", "")):
        val = a.get(k, "").strip()
        if val == "auto":
            val = ""
        if not val and legacy and a.get(legacy, "").strip() not in ("", "auto"):
            val = a.get(legacy, "").strip()
        a[k] = val
    a["ELEMENT_CONTAINER"] = a["ELEMENT_WEB_CONTAINER"]

    net = a.get("DOCKER_ADDONS_NETWORK", "").strip()
    if net == "auto":
        net = ""

    if not a.get("ELEMENT_CALL_URL") and a.get("ELEMENT_CALL_INTERNAL_URL"):
        a["ELEMENT_CALL_URL"] = a["ELEMENT_CALL_INTERNAL_URL"]
    if not a.get("SYNAPSE_URL") and a.get("SYNAPSE_INTERNAL_URL", "") not in ("", "auto"):
        a["SYNAPSE_URL"] = a["SYNAPSE_INTERNAL_URL"]

    if not a.get("RECORDER_BASE_PORT"):
        a["RECORDER_BASE_PORT"] = a.get("RECORDER_HOST_PORT") or "8788"
    if not a.get("CONTROLLER_PORT"):
        a["CONTROLLER_PORT"] = "8765"
    if not a.get("REC_HTTP_PORT"):
        a["REC_HTTP_PORT"] = "8899"

    ca = a.get("CUSTOM_CA_CERT", "").strip() or a.get("TLS_CA_FILE", "").strip()
    if ca == "auto":
        ca = ""
        for cand in ("/matrix/ssl/ca.crt", "/opt/element-stack/certs/ca.crt"):
            if os.path.isfile(cand):
                ca = cand
                break
        if not ca and os.path.isdir("/matrix/ssl"):
            for fn in sorted(os.listdir("/matrix/ssl")):
                if fn.endswith(".crt"):
                    ca = os.path.join("/matrix/ssl", fn)
                    break
    a["TLS_CA_FILE"] = ca

    spki = a.get("CHROMIUM_SPKI_HASH", "").strip() or a.get("SPKI_HASH", "").strip()
    a["SPKI_HASH"] = "" if spki == "auto" else spki

    if not a.get("RECORDING_API_PREFIX"):
        a["RECORDING_API_PREFIX"] = "/recording"
    if not a.get("RECORDINGS_PUBLIC_PATH"):
        a["RECORDINGS_PUBLIC_PATH"] = "/recordings/"


def resolve_bot_accounts(a, domain):
    """Build (users_mxid_list, passwords_list, worker_count) from v2 or v1 keys."""
    raw_users = []
    if a.get("RECORDER_ACCOUNTS", "").strip():
        raw_users = [u.strip() for u in a["RECORDER_ACCOUNTS"].split(",") if u.strip()]
    elif a.get("RECORDER_BOT_USERS", "").strip():
        raw_users = [u.strip() for u in a["RECORDER_BOT_USERS"].split(",") if u.strip()]

    bot_count = (a.i("RECORDER_BOT_COUNT", 0)
                 or a.i("RECORDING_WORKERS", 0)
                 or max(len(raw_users), 1))
    prefix = a.get("RECORDER_BOT_PREFIX", "recorder").strip() or "recorder"

    if not raw_users:
        raw_users = [prefix] if bot_count == 1 else [
            "%s-%d" % (prefix, i + 1) for i in range(bot_count)
        ]
    elif len(raw_users) == 1 and bot_count > 1:
        base = raw_users[0].lstrip("@").split(":", 1)[0]
        raw_users = ["%s-%d" % (base, i + 1) for i in range(bot_count)]

    users = []
    for u in raw_users:
        if u.startswith("@") and ":" in u:
            users.append(u)
        else:
            local = u.lstrip("@").split(":", 1)[0]
            users.append("@%s:%s" % (local, domain))

    raw_pw = (a.get("RECORDER_BOT_PASSWORDS", "").strip()
              or a.get("RECORDER_PASSWORDS", "").strip()
              or a.get("RECORDER_BOT_PASSWORD", "").strip())
    pw_list = [p.strip() for p in raw_pw.split(",") if p.strip()]
    if len(pw_list) == 1 and len(users) > 1:
        pw_list = pw_list * len(users)

    workers = a.i("RECORDING_WORKERS", 0) or a.i("RECORDER_BOT_COUNT", 0) or len(users)
    workers = max(1, min(workers, len(users)))
    return users, pw_list, workers


# --------------------------------------------------------------------------
# host component verification & optional auto-install
# --------------------------------------------------------------------------

HOST_REQUIREMENTS = [
    ("docker",  "docker.io",               "Docker Engine"),
    ("curl",    "curl",                    "HTTP client (curl)"),
    ("jq",      "jq",                      "JSON processor (jq)"),
    ("openssl", "openssl",                 "OpenSSL CLI (TLS & SPKI checks)"),
]


def verify_host_components(a, allow_install=False):
    """Check required host binaries; report missing and offer apt-get install."""
    missing = []
    for binary, pkg, label in HOST_REQUIREMENTS:
        if shutil.which(binary):
            say("ok", "host component present: %s (%s)" % (label, binary))
        else:
            say("warn", "host component MISSING: %s (binary '%s', apt package '%s')"
                % (label, binary, pkg))
            missing.append((binary, pkg, label))

    rc, out, _ = sh(["docker", "compose", "version"])
    if rc == 0:
        lines = [l for l in out.splitlines() if l.strip()]
        say("ok", "docker compose v2: " + (lines[0] if lines else "available"))
    else:
        say("warn", "docker compose v2 plugin MISSING (apt package: docker-compose-v2)")
        missing.append(("docker-compose-plugin", "docker-compose-v2", "Docker Compose v2"))

    if not missing:
        return True

    pkgs = [pkg for _, pkg, _ in missing]
    print()
    print(B + tr(a, "missing_pkg_title") + R)
    for binary, pkg, label in missing:
        print("  - %-28s -> apt-get install -y %s" % (label, pkg))
    print()

    if not allow_install:
        return False

    policy = a.get("INSTALL_MISSING_COMPONENTS", "prompt").strip().lower()
    if a.b("__yes__") and policy == "prompt":
        policy = "yes"

    do_install = False
    if policy in ("yes", "true", "1", "auto"):
        do_install = True
    elif policy == "prompt" and sys.stdin.isatty():
        try:
            ans = input(tr(a, "offer_install")).strip().lower()
        except EOFError:
            ans = "n"
        do_install = ans in ("y", "yes", "д", "да")

    if not do_install:
        die(tr(a, "abort_missing"))

    say("info", tr(a, "installing_pkgs") % " ".join(pkgs))
    sh(["apt-get", "update", "-qq"], check=True, timeout=300)
    sh(["apt-get", "install", "-y"] + pkgs, check=True, timeout=600)
    return True


# --------------------------------------------------------------------------
# container & domain discovery (read-only)
# --------------------------------------------------------------------------

def docker_ready():
    rc, _, err = sh(["docker", "info"])
    if rc != 0:
        die("docker daemon is not reachable: " + err[-300:])


def running_containers():
    rc, out, _ = sh(["docker", "ps", "--format", "{{.Names}}"])
    return [n for n in out.splitlines() if n.strip()]


def inspect(name):
    d = docker_json(["inspect", name])
    return d[0] if d else None


def container_labels(name):
    d = inspect(name)
    return ((d or {}).get("Config", {}).get("Labels") or {})


def container_networks(name):
    d = inspect(name)
    return sorted(((d or {}).get("NetworkSettings", {}).get("Networks") or {}).keys())


def container_env(name):
    d = inspect(name)
    env = {}
    for kv in ((d or {}).get("Config", {}).get("Env") or []):
        if "=" in kv:
            k, v = kv.split("=", 1)
            env[k] = v
    return env


def find_web_proxy(a):
    """Locate whatever terminates TLS for DOMAIN: nginx container or host nginx.

    Returns (kind, name) where kind is "container" or "host" or "".
    """
    explicit = a.get("NGINX_CONTAINER", "").strip()
    if explicit:
        return "container", explicit

    for n in running_containers():
        if n.startswith("rec-") or PROJECT in n:
            continue
        d = inspect(n) or {}
        ports = (d.get("NetworkSettings", {}).get("Ports") or {})
        if not any(p in ports and ports[p] for p in ("80/tcp", "443/tcp")):
            continue
        img = ((d.get("Config") or {}).get("Image") or "").lower()
        if "nginx" in n.lower() or "nginx" in img:
            return "container", n

    # Host nginx is the common case for an existing Element Web install.
    rc, out, _ = sh("command -v nginx >/dev/null 2>&1 && nginx -v 2>&1", timeout=15)
    if rc == 0 and out.strip():
        return "host", ""
    return "", ""


def nginx_container_root(name):
    """Return (root, confdir) inside an nginx container, best effort."""
    d = inspect(name) or {}
    mounts = (d.get("Mounts") or [])
    for m in mounts:
        dest = (m.get("Destination") or "")
        src = (m.get("Source") or "")
        if dest in ("/etc/nginx", "/etc/nginx/conf.d") and src:
            return ("/etc/nginx", src)
    return ("/etc/nginx", "")


def host_nginx_confdir():
    for cand in ("/etc/nginx/sites-enabled", "/etc/nginx/conf.d"):
        if os.path.isdir(cand):
            return cand
    return "/etc/nginx/conf.d"


def find_element(a):
    explicit = a.get("ELEMENT_CONTAINER", "").strip()
    if explicit:
        return explicit
    names = running_containers()
    for n in names:
        if n.startswith("rec-"):
            continue
        img = ((inspect(n) or {}).get("Config", {}).get("Image") or "").lower()
        if "element-web" in img or "vectorim/element-web" in img:
            return n
    for n in names:
        low = n.lower()
        if "element" in low and "call" not in low and not n.startswith("rec-"):
            return n
    return ""


def find_element_call(a):
    explicit = a.get("ELEMENT_CALL_CONTAINER", "").strip()
    if explicit:
        return explicit
    for n in running_containers():
        img = ((inspect(n) or {}).get("Config", {}).get("Image") or "").lower()
        if "element-call" in img or "element-call" in n.lower():
            return n
    return ""


def find_synapse(a):
    explicit_url = a.get("SYNAPSE_URL", "").strip()
    explicit_cnt = a.get("SYNAPSE_CONTAINER", "").strip()
    if explicit_cnt:
        return explicit_url or ("http://%s:8008" % explicit_cnt), explicit_cnt
    for n in running_containers():
        img = ((inspect(n) or {}).get("Config", {}).get("Image") or "").lower()
        if "matrixdotorg/synapse" in img or n.endswith("synapse") or n == "synapse":
            return explicit_url or ("http://%s:8008" % n), n
    return explicit_url, ""


def detect_element_root(element):
    for cand in ("/app", "/usr/share/nginx/html", "/srv/element-web"):
        rc, out, _ = sh(["docker", "exec", element, "sh", "-c",
                         "test -f %s/config.json -o -f %s/index.html && echo yes"
                         % (cand, cand)])
        if out.strip() == "yes":
            return cand
    return ""


def detect_module_loader_script(element):
    """Check if the stock Element Web image has /docker-entrypoint.d/18-load-element-modules.sh."""
    rc, out, _ = sh(["docker", "exec", element, "sh", "-c",
                     "test -f /docker-entrypoint.d/18-load-element-modules.sh && echo yes"])
    return out.strip() == "yes"


def autodetect_domain(a, element):
    dom = a.get("DOMAIN", "").strip()
    if dom and dom != "auto":
        return dom
    if element:
        for p in ("/tmp/element-web-config/config.json", "/app/config.json",
                  "/usr/share/nginx/html/config.json"):
            rc, out, _ = sh(["docker", "exec", element, "sh", "-c",
                             "cat %s 2>/dev/null" % p])
            if rc == 0 and out:
                try:
                    cfg = json.loads(out)
                    sn = (cfg.get("default_server_config", {})
                             .get("m.homeserver", {})
                             .get("server_name", ""))
                    if sn and "." in sn:
                        return sn.strip()
                except ValueError:
                    pass
    for n in running_containers():
        for k, v in container_labels(n).items():
            if ".rule" in k and "Host(" in v:
                m = re.search(r"Host\(`([^`]+)`\)", v)
                if m and "." in m.group(1):
                    return m.group(1).strip()
    return ""


def nginx_routes_present(a, ctx):
    """True when the nginx server block already proxies /recording to us.

    Reads the host nginx config tree, or the container's mounted config dir.
    """
    needles = (NGINX_PREFIX, "%d" % a.i("CONTROLLER_PORT", 8765))
    roots = []
    if ctx.get("proxy_kind") == "host":
        roots.append(host_nginx_confdir())
    elif ctx.get("proxy_kind") == "container" and ctx.get("proxy_name"):
        _, src = nginx_container_root(ctx["proxy_name"])
        if src:
            roots.append(src)
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for base, _dirs, files in os.walk(root):
            for fn in files:
                if not fn.endswith(".conf"):
                    continue
                try:
                    with open(os.path.join(base, fn), encoding="utf-8",
                              errors="replace") as fh:
                        blob = fh.read()
                except OSError:
                    continue
                if needles[0] in blob and needles[1] in blob:
                    return True
    return False


def port_busy(port):
    for family, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET, "0.0.0.0")):
        s = socket.socket(family, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((addr, port))
        except OSError:
            return True
        finally:
            s.close()
    return False


def pick_ports(a, count):
    base = a.i("RECORDER_BASE_PORT", 8788)
    ports, p = [], base
    while len(ports) < count:
        if p > 65000:
            die("no free ports found above %d" % base)
        if not port_busy(p):
            ports.append(p)
        p += 1
    return ports


def discover(a):
    ctx = {}
    docker_ready()

    element = find_element(a)
    if not element:
        die("Element Web container not found. Set ELEMENT_WEB_CONTAINER in answers.")
    ctx["element"] = element
    ctx["element_root"] = detect_element_root(element)
    ctx["has_module_loader"] = detect_module_loader_script(element)
    say("ok", "Element Web: %s (root=%s, 18-load-element-modules.sh=%s)"
        % (element, ctx["element_root"] or "?",
           "yes" if ctx["has_module_loader"] else "no"))

    domain = autodetect_domain(a, element)
    if not domain or "." not in domain:
        die("DOMAIN could not be autodetected; set DOMAIN in answers.")
    a["DOMAIN"] = domain
    say("ok", "Domain: %s" % domain)

    proxy_kind, proxy_name = find_web_proxy(a)
    ctx["proxy_kind"] = proxy_kind
    ctx["proxy_name"] = proxy_name
    if proxy_kind == "container":
        say("ok", "nginx (container): %s" % proxy_name)
    elif proxy_kind == "host":
        say("ok", "nginx (host binary)")
    else:
        say("warn", "nginx not found yet; the server block will still be generated. "
                    "Set NGINX_CONTAINER in answers if it runs in a container.")

    net = a.get("DOCKER_ADDONS_NETWORK", "").strip()
    if proxy_kind == "container" and proxy_name:
        nets = container_networks(proxy_name)
        if not net and nets:
            net = nets[0]
    if not net:
        el_nets = container_networks(element)
        net = el_nets[0] if el_nets else "bridge"
    ctx["network"] = net
    ctx["element_network"] = (container_networks(element) or [net])[0]
    say("ok", "docker network: %s (Element Web: %s)"
        % (net, ctx["element_network"]))

    ec = find_element_call(a)
    ctx["element_call"] = ec
    if ec:
        say("ok", "Element Call container: %s" % ec)
    else:
        say("warn", "Element Call container not seen by name (using ELEMENT_CALL_INTERNAL_URL)")

    syn_url, syn_cnt = find_synapse(a)
    ctx["synapse_url"] = syn_url
    ctx["synapse_container"] = syn_cnt
    if syn_url:
        say("ok", "Synapse: %s (%s)" % (syn_url, syn_cnt or "external"))
    else:
        say("warn", "SYNAPSE_URL not found; set SYNAPSE_INTERNAL_URL in answers")

    users, pwds, workers = resolve_bot_accounts(a, domain)
    ctx["users"] = users
    ctx["passwords"] = pwds
    ctx["workers"] = workers
    ctx["ports"] = [] if a.b("ADOPT_EXISTING") else pick_ports(a, workers)

    # Ports that only exist when an external reverse proxy is used. Refuse
    # early instead of letting `docker compose up` fail on a bind conflict.
    if not a.b("ADOPT_EXISTING") and a.b("PUBLISH_LOOPBACK_PORTS", True):
        for key, default, label in (("CONTROLLER_PORT", 8765, "rec-controller"),
                                    ("REC_HTTP_PORT", 8899, "rec-portal")):
            pnum = a.i(key, default)
            if port_busy(pnum):
                die("%s: port %d is already in use on 127.0.0.1. "
                    "Set %s to a free port, or PUBLISH_LOOPBACK_PORTS=\"false\" "
                    "if nginx reaches the plugin another way." % (label, pnum, key))
    ctx["call_url"] = (a.get("ELEMENT_CALL_URL", "").strip()
                       or ("https://%s" % domain))
    ctx["token"] = a.get("RECORDER_TOKEN", "").strip() or secrets.token_hex(24)

    ca = a.get("TLS_CA_FILE", "").strip()
    ctx["ca_file"] = os.path.join(a["PLUGIN_DIR"], "ca", "ca.crt") if (ca and os.path.isfile(ca)) else ""
    ctx["tls_mode"] = detect_tls(a, ctx)
    return ctx


def print_plan(a, ctx):
    print()
    print(B + "Installation Plan (v%s — Module API)" % PLUGIN_VERSION + R)
    print("  domain              : %s" % a["DOMAIN"])
    print("  plugin dir          : %s" % a["PLUGIN_DIR"])
    print("  recordings dir      : %s" % a["RECORDINGS_DIR"])
    print("  bot E2EE profile    : %s" % a["RECORDER_BOT_PROFILE_DIR"])
    print("  installer / UI lang : %s / %s" % (a["INSTALLER_LANG"], a["BUTTON_LANG"]))
    if ctx["proxy_kind"] == "container":
        proxy_line = "nginx container %s" % ctx["proxy_name"]
    elif ctx["proxy_kind"] == "host":
        proxy_line = "nginx (host binary)"
    else:
        proxy_line = "not detected (server block generated anyway)"
    print("  Reverse proxy       : %s" % proxy_line)
    print("  docker network      : %s" % ctx["network"])
    print("  published ports     : 127.0.0.1:%s (api), 127.0.0.1:%s (portal)"
          % (a.i("CONTROLLER_PORT", 8765), a.i("REC_HTTP_PORT", 8899)))
    print("  Element Web         : %s (root: %s, loader: %s)"
          % (ctx["element"], ctx["element_root"] or "?",
             "18-load-element-modules.sh" if ctx["has_module_loader"] else "direct config.json"))
    print("  Synapse             : %s" % (ctx["synapse_url"] or "not detected"))
    print("  Element Call URL    : %s" % ctx["call_url"])
    print("  recorder bots (%d)   : %s" % (len(ctx["users"]), ", ".join(ctx["users"])))
    if not a.b("ADOPT_EXISTING"):
        print("  workers / ports     : %d -> %s"
              % (ctx["workers"], ", ".join(str(p) for p in ctx["ports"])))
    print("  UI integration      : ESM Module API (%s)" % MODULE_REL_ENTRYPOINT)
    print()


# --------------------------------------------------------------------------
# Element Web ESM Module API registration (v2.0 — replaces index.html patch)
# --------------------------------------------------------------------------

def backup_element(a, ctx, tag):
    dest = os.path.join(a["PLUGIN_DIR"], "backups",
                        "%s-%s" % (tag, time.strftime("%Y%m%d-%H%M%S")))
    os.makedirs(dest, exist_ok=True)
    el, root = ctx["element"], ctx["element_root"] or "/app"
    for name in ("config.json", "index.html"):
        sh(["docker", "cp", "%s:%s/%s" % (el, root, name),
            os.path.join(dest, name)])
    say("ok", "Element Web backup saved: " + dest)
    return dest


def element_host_mounts(el):
    """Return {destination_in_container: source_on_host} for bind mounts."""
    d = inspect(el) or {}
    res = {}
    for m in (d.get("Mounts") or []):
        if m.get("Destination") and m.get("Source"):
            res[m["Destination"]] = m["Source"]
    return res


def clean_legacy_index_injection(el, root, tmp):
    """Remove legacy <script src="/record-button..."> from index.html (both host bind-mount & container)."""
    mounts = element_host_mounts(el)
    seen = set()
    for dest_p in ("%s/index.html" % root, "/usr/share/nginx/html/index.html", "/app/index.html"):
        host_src = mounts.get(dest_p)
        if host_src and host_src not in seen and os.path.isfile(host_src):
            seen.add(host_src)
            with open(host_src, encoding="utf-8", errors="replace") as fh:
                html = fh.read()
            cleaned = re.sub(
                r"[ \t]*\n?" + re.escape(LEGACY_MARKER_BEGIN) + r".*?"
                + re.escape(LEGACY_MARKER_END) + r"[ \t]*\n?",
                "", html, flags=re.S,
            )
            cleaned = re.sub(r'<script[^>]+record-button[^>]*></script>\s*', "", cleaned)
            if cleaned != html:
                with open(host_src, "w", encoding="utf-8") as fh:
                    fh.write(cleaned)
                say("ok", "removed legacy <script record-button> from host mount %s" % host_src)

    local_idx = os.path.join(tmp, "index.html")
    rc, _, _ = sh(["docker", "cp", "%s:%s/index.html" % (el, root), local_idx])
    if rc == 0 and os.path.isfile(local_idx):
        with open(local_idx, encoding="utf-8", errors="replace") as fh:
            html = fh.read()
        cleaned = re.sub(
            r"[ \t]*\n?" + re.escape(LEGACY_MARKER_BEGIN) + r".*?"
            + re.escape(LEGACY_MARKER_END) + r"[ \t]*\n?",
            "", html, flags=re.S,
        )
        cleaned = re.sub(r'<script[^>]+record-button[^>]*></script>\s*', "", cleaned)
        if cleaned != html:
            with open(local_idx, "w", encoding="utf-8") as fh:
                fh.write(cleaned)
            sh(["docker", "cp", local_idx, "%s:%s/index.html" % (el, root)])
            say("ok", "removed legacy DOM script injection from %s:%s/index.html" % (el, root))
    sh(["docker", "exec", "-u", "0", el, "sh", "-c",
        "rm -f %s/record-button.js.patch.js %s/record-button.js 2>/dev/null || true"
        % (root, root)])


def build_module_config_dict(a):
    return {
        "apiBase": a.get("RECORDING_API_PREFIX", "/recording") or "/recording",
        "recordingsPath": a.get("RECORDINGS_PUBLIC_PATH", "/recordings/") or "/recordings/",
        "lang": a.get("BUTTON_LANG", "auto") or "auto",
        # Kept in sync with RECORDING_MIN_POWER_LEVEL, which the controller
        # enforces. Without this the button locally assumed 50 and told a PL40
        # user "insufficient power" for a deployment that allows PL40.
        "minPowerLevel": a.i("RECORDING_MIN_POWER_LEVEL", 50),
        "showRecordingsLink": a.b("BUTTON_SHOW_RECORDINGS_LINK",
                                  a.b("SHOW_RECORDINGS_LINK", True)),
        "alwaysVisible": a.b("BUTTON_ALWAYS_VISIBLE", True),
        "pollIntervalMs": a.i("BUTTON_POLL_INTERVAL_MS", 5000) or 5000,
    }


def patch_config_json_obj(cfg, a, add_module=True):
    """Add or remove the ESM module entry and MODULE_CONFIG_KEY in a config.json dict."""
    mods = list(cfg.get("modules") or [])
    mods = [m for m in mods if "recorder" not in str(m)]
    if add_module:
        mods.append(MODULE_REL_ENTRYPOINT)
        cfg["modules"] = mods
        cfg[MODULE_CONFIG_KEY] = build_module_config_dict(a)
    else:
        if mods:
            cfg["modules"] = mods
        else:
            cfg.pop("modules", None)
        cfg.pop(MODULE_CONFIG_KEY, None)
    return cfg


def update_element_config_files(el, root, tmp, a, add_module=True):
    """Update host bind-mounted config.json (if any) AND /tmp/element-web-config/config.json."""
    mounts = element_host_mounts(el)
    seen = set()
    for dest_p in ("%s/config.json" % root, "/app/config.json", "/usr/share/nginx/html/config.json"):
        host_src = mounts.get(dest_p)
        if host_src and host_src not in seen and os.path.isfile(host_src):
            seen.add(host_src)
            try:
                with open(host_src, encoding="utf-8") as fh:
                    cfg = json.load(fh)
                patch_config_json_obj(cfg, a, add_module=add_module)
                with open(host_src, "w", encoding="utf-8") as fh:
                    json.dump(cfg, fh, indent=2, ensure_ascii=False)
                    fh.write("\n")
                say("ok", "updated host-mounted Element config: %s" % host_src)
            except Exception as exc:
                say("warn", "could not update host config %s: %s" % (host_src, exc))

    local_cfg = os.path.join(tmp, "runtime-config.json")
    rc, _, _ = sh(["docker", "cp", "%s:/tmp/element-web-config/config.json" % el, local_cfg])
    if rc == 0 and os.path.isfile(local_cfg):
        try:
            with open(local_cfg, encoding="utf-8") as fh:
                cfg = json.load(fh)
            patch_config_json_obj(cfg, a, add_module=add_module)
            with open(local_cfg, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            sh(["docker", "cp", local_cfg, "%s:/tmp/element-web-config/config.json" % el])
            sh(["docker", "exec", "-u", "0", el, "chown", "nginx:nginx",
                "/tmp/element-web-config/config.json"])
            say("ok", "updated runtime Element config: %s:/tmp/element-web-config/config.json" % el)
        except Exception as exc:
            say("warn", "could not update runtime config.json: %s" % exc)


def install_element_module(a, ctx):
    """Install /modules/recorder (ESM Module API) into Element Web container."""
    el = ctx["element"]
    root = ctx["element_root"] or "/app"
    if not ctx["element_root"]:
        say("warn", "Element Web root not detected; defaulting to /app")

    if a.b("BACKUP_ELEMENT_WEB", True):
        bdir = os.path.join(a["PLUGIN_DIR"], "backups")
        if not (os.path.isdir(bdir) and any(d.startswith("preinstall-") for d in os.listdir(bdir))):
            backup_element(a, ctx, "preinstall")

    tmp = os.path.join(a["PLUGIN_DIR"], "state", "module-stage")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(os.path.join(tmp, "recorder", "locales"), exist_ok=True)

    for rel in ("index.js", "package.json", "manifest.json",
                "locales/ru.json", "locales/en.json"):
        src_path = os.path.join(ROOT, *rel.split("/"))
        if rel == "index.js" and not os.path.isfile(src_path):
            src_path = os.path.join(ROOT, "src", "index.js")
        if not os.path.isfile(src_path):
            die("missing plugin module file: %s" % src_path)
        shutil.copyfile(src_path, os.path.join(tmp, "recorder", *rel.split("/")))

    clean_legacy_index_injection(el, root, tmp)

    mod_src = os.path.join(tmp, "recorder")
    sh(["docker", "exec", "-u", "0", el, "sh", "-c",
        "mkdir -p /modules && chmod 755 /modules"])
    rc, _, err = sh(["docker", "cp", mod_src, "%s:/modules/" % el])
    if rc != 0:
        die("failed to copy /modules/recorder into %s: %s" % (el, err))
    sh(["docker", "exec", "-u", "0", el, "chmod", "-R", "a+rX", "/modules/recorder"])
    say("ok", "ESM module staged into %s:/modules/recorder" % el)

    update_element_config_files(el, root, tmp, a, add_module=True)
    say("ok", "Element Web Module API registered (%s)" % MODULE_REL_ENTRYPOINT)


def remove_element_module(a, el):
    """Remove /modules/recorder and clean config.json during --uninstall."""
    if not el:
        return
    root = detect_element_root(el) or "/app"
    tmp = os.path.join(a["PLUGIN_DIR"], "state", "uninstall-tmp")
    os.makedirs(tmp, exist_ok=True)
    clean_legacy_index_injection(el, root, tmp)
    update_element_config_files(el, root, tmp, a, add_module=False)
    sh(["docker", "exec", "-u", "0", el, "sh", "-c",
        "rm -rf /modules/recorder 2>/dev/null || true"])
    shutil.rmtree(tmp, ignore_errors=True)
    say("ok", "removed ESM module from Element Web (%s)" % el)


# --------------------------------------------------------------------------
# compose rendering & nginx preflight
# --------------------------------------------------------------------------

COMPOSE_HEAD = """# Generated by recorder-plugin {v} — do not edit by hand.
# Regenerate: bash install.sh --install <answers.env>
services:
"""

COMPOSE_TAIL = """
networks:
  default:
    name: {network}
    external: true
{element_net_line}"""

CONTROLLER = """
  rec-controller:
    build:
      context: {state}/build/controller
      args: {{PLUGIN_VERSION: "{v}"}}
    image: recorder-plugin/controller:{v}
    container_name: rec-controller
    restart: unless-stopped
    networks: [default{element_net_list}]
    extra_hosts:
      - "host.docker.internal:host-gateway"
{ports_api}    environment:
      RECORDER_URLS: "{urls}"
      RECORDER_TOKEN: "{token}"
      RECORDER_ACCOUNTS: "{accounts}"
      RECORDING_MIN_POWER_LEVEL: "{minpl}"
      INSTALL_LANGUAGE: "{lang}"
      DOMAIN: "{domain}"
      SYNAPSE_URL: "{synapse}"
      OUTPUT_DIR: "/output"
      PUBLIC_DIR: "/public"
      TLS_CA_FILE: "{ca_inside}"
      PLUGIN_VERSION: "{v}"
    volumes:
      - {plugin}/output:/output
      - {recordings_dir}:/public
{ca_volume}"""

PORTAL = """
  rec-portal:
    build:
      context: {state}/build/portal
      args: {{PLUGIN_VERSION: "{v}"}}
    image: recorder-plugin/portal:{v}
    container_name: rec-portal
    restart: unless-stopped
    networks: [default{element_net_list}]
{ports_portal}    environment:
      PUBLIC_DIR: "/public"
      SYNAPSE_URL: "{synapse}"
      DOMAIN: "{domain}"
      INSTALL_LANGUAGE: "{lang}"
      RECORDINGS_MIN_POWER_LEVEL: "{viewpl}"
      TLS_CA_FILE: "{ca_inside}"
      PLUGIN_VERSION: "{v}"
    volumes:
      - {recordings_dir}:/public:ro
{ca_volume}"""

PUBLISHER = """
  rec-publisher:
    build:
      context: {state}/build/publisher
      args: {{PLUGIN_VERSION: "{v}"}}
    image: recorder-plugin/publisher:{v}
    container_name: rec-publisher
    restart: unless-stopped
    networks: [default]
    environment:
      OUTPUT_DIR: "/output"
      PUBLIC_DIR: "/public"
      DOMAIN: "{domain}"
      INSTALL_LANGUAGE: "{lang}"
    volumes:
      - {plugin}/output:/output:ro
      - {recordings_dir}:/public
"""

WORKER = """
  rec-worker-{idx}:
    build:
      context: {state}/build/recorder
      args: {{PLUGIN_VERSION: "{v}"}}
    image: recorder-plugin/recorder:{v}
    container_name: rec-worker-{idx}
    restart: unless-stopped
    network_mode: host
    shm_size: "2gb"
    environment:
      PORT: "{port}"
      RECORDER_TOKEN: "{token}"
      RECORDER_USER: "{user}"
      RECORDER_PASSWORD: "{password}"
      PROFILE_DIR: "/profile"
      OUTPUT_DIR: "/output"
      ELEMENT_CALL_URL: "{call_url}"
      DOMAIN: "{domain}"
      # Chromium MAP rule; server.js rewrites "host:ip" to "host ip" (#18).
      HOST_RESOLVER: "{domain}:127.0.0.1"
      SPKI_HASH: "{spki}"
      # The worker is on the host network and cannot use docker DNS (#07),
      # so it must reach Synapse through the published loopback port (#09).
      SYNAPSE_HOST_URL: "{synapse_host}"
      LK_INTERNAL: "{lk_internal}"
      # Must match the null sink entrypoint.sh creates (#20).
      PULSE_SRC: "{pulse_src}"
      TLS_CA_FILE: "{ca_inside}"
      CHROME_PATH: "{chrome_path}"
      # Capture parameters; the observer falls back to its own defaults when
      # these are empty. Resolution must match the Xvfb geometry in entrypoint.
      RECORD_RESOLUTION: "{resolution}"
      SCREEN_GEOMETRY: "{resolution}x24"
      RECORD_FRAMERATE: "{framerate}"
      FPS: "{framerate}"
      CRF: "{crf}"
      RECORD_SECS: "{record_secs}"
      INSTALL_LANGUAGE: "{lang}"
      FFMPEG_DISPLAY: ":99"
    volumes:
      - {profile_host}:/profile
      - {plugin}/output:/output
      - {plugin}/ca:/ca:ro
"""

ADOPT = """
  rec-adopt-proxy:
    build:
      context: {state}/build/adopt
      args: {{PLUGIN_VERSION: "{v}"}}
    image: recorder-plugin/adopt:{v}
    container_name: rec-adopt-proxy
    restart: unless-stopped
    networks: [default{element_net_list}]
    extra_hosts:
      - "host.docker.internal:host-gateway"
    environment:
      UPSTREAM_API: "{up_api}"
      UPSTREAM_PORTAL: "{up_portal}"
      INSTALL_LANGUAGE: "{lang}"
"""


def check_routing_prereqs(a, ctx):
    """Verify the loopback ports are publishable and nginx can reach us."""
    if a.b("ADOPT_EXISTING"):
        return
    if not a.b("PUBLISH_LOOPBACK_PORTS", True):
        say("warn", "PUBLISH_LOOPBACK_PORTS=false: nginx cannot reach the plugin "
                    "over 127.0.0.1. Only correct if nginx shares the docker network.")
        return
    for key, default, label in (("CONTROLLER_PORT", 8765, "controller"),
                                ("REC_HTTP_PORT", 8899, "portal")):
        port = a.i(key, default)
        if port_busy(port):
            say("warn", "%s: 127.0.0.1:%d is already in use; set %s to a free port "
                        "in answers and update the nginx server block accordingly."
                        % (label, port, key))
        else:
            say("ok", "%s will publish on 127.0.0.1:%d" % (label, port))


def detect_tls(a, ctx):
    if ctx.get("certresolver"):
        return "certresolver"
    domain = a["DOMAIN"]
    rc, out, _ = sh("echo | openssl s_client -servername '%s' -connect 127.0.0.1:443 2>/dev/null "
                    "| openssl x509 -noout -subject -issuer 2>/dev/null" % domain, timeout=10)
    if rc != 0 or not out:
        return "unknown"
    if "DEFAULT CERT" in out.upper() or "SELF SIGNED" in out.upper():
        return "none"
    return "default-cert"


def resolve_worker_profile_dir(a, user, idx):
    """Preserve existing profile-spk on single-bot/spk setups; per-bot dir otherwise."""
    explicit = a.get("RECORDER_BOT_PROFILE_DIR", "").strip()
    local = user.lstrip("@").split(":", 1)[0]
    if explicit and (idx == 0 and (local == "spk" or os.path.isdir(explicit))):
        return explicit
    user_safe = re.sub(r"[^A-Za-z0-9_.-]", "_", user)
    return os.path.join(a["PLUGIN_DIR"], "profile", user_safe)


def render_compose(a, ctx):
    p = PROJECT
    ca_inside = "/ca/ca.crt" if ctx["ca_file"] else ""
    ca_volume = ("      - %s:/ca/ca.crt:ro\n" % ctx["ca_file"]) if ctx["ca_file"] else ""

    # nginx reaches the plugin over 127.0.0.1 (host nginx) or by service name
    # (nginx in a container). Bound to 127.0.0.1 so it is never exposed to the
    # internet by accident.
    if a.b("PUBLISH_LOOPBACK_PORTS", True):
        ports_api = '    ports:\n      - "127.0.0.1:%d:8765"\n' % a.i("CONTROLLER_PORT", 8765)
        ports_portal = '    ports:\n      - "127.0.0.1:%d:8899"\n' % a.i("REC_HTTP_PORT", 8899)
    else:
        ports_api = ""
        ports_portal = ""

    state = os.path.join(a["PLUGIN_DIR"], "state")
    # When the plugin network differs from Element Web's, attach the services
    # to both so nginx-in-a-container can reach them by name.
    el_net = ctx.get("element_network") or ctx["network"]
    if el_net != ctx["network"]:
        element_net_list = ", %s" % el_net
        element_net_line = ("  %s:\n    name: %s\n    external: true\n"
                            % (el_net, el_net))
    else:
        element_net_list = ""
        element_net_line = ""

    common = dict(
        state=state, root=ROOT, v=PLUGIN_VERSION, network=ctx["network"],
        domain=a["DOMAIN"], lang=a.get("INSTALL_LANGUAGE", "ru") or "ru",
        plugin=a["PLUGIN_DIR"], recordings_dir=a["RECORDINGS_DIR"],
        p=p, ca_inside=ca_inside, ca_volume=ca_volume,
        element_net_list=element_net_list, element_net_line=element_net_line,
        ports_api=ports_api, ports_portal=ports_portal,
        viewpl=a.i("RECORDINGS_MIN_POWER_LEVEL", 50),
    )

    parts = [COMPOSE_HEAD.format(**common)]
    if a.b("ADOPT_EXISTING"):
        up_api = a.get("ADOPT_CONTROLLER_URL") or "http://host.docker.internal:8765"
        up_portal = a.get("ADOPT_PORTAL_URL") or "http://host.docker.internal:8899"
        parts.append(ADOPT.format(up_api=up_api, up_portal=up_portal, **common))
    else:
        # Workers run with network_mode: host; the controller lives on a
        # bridge network. 127.0.0.1 inside the controller is the controller
        # itself, so it must reach the workers through host-gateway.
        urls = ",".join("http://host.docker.internal:%d" % port
                        for port in ctx["ports"])
        parts.append(CONTROLLER.format(
            urls=urls, token=ctx["token"], accounts=",".join(ctx["users"]),
            minpl=a.i("RECORDING_MIN_POWER_LEVEL", 50),
            synapse=ctx["synapse_url"], **common))
        parts.append(PORTAL.format(synapse=ctx["synapse_url"], **common))
        parts.append(PUBLISHER.format(**common))
        for i, port in enumerate(ctx["ports"]):
            user = ctx["users"][i % len(ctx["users"])]
            pwd = ctx["passwords"][i % len(ctx["passwords"])] if ctx["passwords"] else ""
            prof_dir = resolve_worker_profile_dir(a, user, i)
            os.makedirs(prof_dir, exist_ok=True)
            parts.append(WORKER.format(
                idx=i + 1, port=port, token=ctx["token"], user=user, password=pwd,
                profile_host=prof_dir, call_url=ctx["call_url"],
                spki=(a.get("SPKI_HASH", "").strip()
                      if a.get("SPKI_HASH", "").strip() != "auto" else ctx.get("spki_auto", "")),
                synapse_host=a.get("SYNAPSE_HOST_URL", "").strip()
                             or ("http://127.0.0.1:%d" % a.i("SYNAPSE_LOCAL_PORT", 18008)),
                lk_internal=a.get("LK_INTERNAL_URL", "").strip()
                            or ("127.0.0.1:%d" % a.i("LIVEKIT_LOCAL_PORT", 17880)),
                pulse_src=a.get("PULSE_SRC", "").strip() or "virtual_speaker.monitor",
                chrome_path=a.get("CHROME_PATH", "").strip()
                            or "/ms-playwright/chromium-1148/chrome-linux/chrome",
                resolution=a.get("RECORD_RESOLUTION", "").strip() or "1280x720",
                framerate=a.get("RECORD_FRAMERATE", "").strip() or "15",
                crf=a.get("RECORD_CRF", "").strip() or "28",
                record_secs=a.get("RECORD_MAX_SECONDS", "").strip() or "7200",
                **common))

    return "".join(parts) + COMPOSE_TAIL.format(
        network=ctx["network"], element_net_line=element_net_line)




# --------------------------------------------------------------------------
# nginx server block generation
# --------------------------------------------------------------------------

NGINX_SNIPPET_TMPL = """\
# >>> recorder-plugin {v} >>>
# ===========================================================================
#  Matrix Element Call Recorder - nginx locations
#  Generated by recorder-plugin {v}. Safe to re-generate: everything between
#  the >>> and <<< markers is replaced on --install / --repair.
#
#  Paste into the `server {{ }}` block that already serves your Element Web
#  domain, or keep the generated file and include it from there:
#      include /etc/nginx/snippets/recorder.conf;
#  Then: nginx -t && systemctl reload nginx
# ===========================================================================

# The controller serves /api/record/* and /health; the trailing slash on
# proxy_pass strips the /recording prefix. Without it every call 404s.
location = {api_prefix} {{
    return 308 {api_prefix}/;
}}

location {api_prefix}/ {{
    proxy_pass http://{api_upstream}/;

    proxy_http_version 1.1;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # Authorization: Bearer <matrix access token> must reach the controller;
    # nginx forwards it by default. Do not strip it here: the controller
    # resolves the caller through /_matrix/client/v3/account/whoami and
    # checks the room power level before accepting a start.
}}

location = {portal_prefix} {{
    return 308 {portal_prefix}/;
}}

location {portal_prefix}/ {{
    proxy_pass http://{portal_upstream}/;

    proxy_http_version 1.1;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # MP4 files are streamed, never buffered to a temp file.
    proxy_buffering          off;
    proxy_max_temp_file_size 0;
    send_timeout             3600s;
}}

# Legacy root-absolute download links produced by older portal builds.
location /files/ {{
    proxy_pass http://{portal_upstream}/files/;

    proxy_set_header Host              $host;
    proxy_buffering          off;
    proxy_max_temp_file_size 0;
    send_timeout             3600s;
}}
# <<< recorder-plugin {v} <<<
"""

SNIPPET_BEGIN = "# >>> recorder-plugin"
SNIPPET_END = "# <<< recorder-plugin"


def nginx_upstreams(a, ctx):
    """Return (api, portal) upstream hosts as seen from the nginx side."""
    api_port = a.i("CONTROLLER_PORT", 8765)
    portal_port = a.i("REC_HTTP_PORT", 8899)
    if ctx.get("proxy_kind") == "container":
        # nginx shares the docker network: talk to the service by name.
        return ("rec-controller:8765", "rec-portal:8899")
    return ("127.0.0.1:%d" % api_port, "127.0.0.1:%d" % portal_port)


def render_nginx_snippet(a, ctx):
    api_up, portal_up = nginx_upstreams(a, ctx)
    return NGINX_SNIPPET_TMPL.format(
        v=PLUGIN_VERSION,
        api_prefix=NGINX_PREFIX, portal_prefix=NGINX_PORTAL_PREFIX,
        api_upstream=api_up, portal_upstream=portal_up)


def strip_snippet(blob):
    """Remove a previously written recorder block, header included.

    The block is delimited by SNIPPET_BEGIN / SNIPPET_END, both emitted by
    render_nginx_snippet(). Start is located by the exact marker line so a
    stray mention of the plugin name in surrounding config is never matched.
    """
    start = blob.find(SNIPPET_BEGIN)
    if start < 0:
        return blob, False
    end_marker = blob.find(SNIPPET_END, start)
    if end_marker < 0:
        return blob, False
    end = blob.find(chr(10), end_marker)
    if end < 0:
        end = len(blob)
    else:
        end += 1
    return blob[:start] + blob[end:], True


def write_nginx_snippet(a, ctx):
    """Write the snippet next to the plugin and, when possible, into nginx.

    Never edits a server block blindly: it either includes the snippet from a
    conf.d file it owns, or leaves the file for the operator. Returns the path.
    """
    plugin_dir = a["PLUGIN_DIR"]
    snippet = render_nginx_snippet(a, ctx)

    local = os.path.join(plugin_dir, "nginx", "recorder.conf")
    os.makedirs(os.path.dirname(local), exist_ok=True)
    with open(local, "w", encoding="utf-8") as fh:
        fh.write(snippet)
    os.chmod(local, 0o644)
    say("ok", "nginx snippet written: %s" % local)

    kind = ctx.get("proxy_kind", "")
    name = ctx.get("proxy_name", "")
    if kind == "host":
        target_dir = host_nginx_confdir()
        target = os.path.join(target_dir, "recorder-plugin.conf")
        existed = os.path.isfile(target)
        if existed:
            with open(target, encoding="utf-8", errors="replace") as fh:
                old = fh.read()
            cleaned, had = strip_snippet(old)
            if had:
                shutil.copyfile(target, target + ".bak")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(snippet)
        os.chmod(target, 0o644)
        say("ok", "nginx config %s: %s"
            % ("updated" if existed else "created", target))

        rc, out, err = sh("nginx -t 2>&1", timeout=60)
        if rc != 0:
            if existed:
                shutil.copyfile(target + ".bak", target)
                sh("nginx -t 2>&1", timeout=60)
                say("err", "nginx -t failed, previous config restored:\n" + (out or err))
            else:
                os.unlink(target)
                say("err", "nginx -t failed, generated file removed: %s" % target)
        else:
            say("ok", "nginx -t passed")
            rc2, out2, _ = sh("systemctl reload nginx 2>&1 || nginx -s reload 2>&1",
                              timeout=60)
            say("ok" if rc2 == 0 else "warn",
                "nginx reload: %s" % ("done" if rc2 == 0 else (out2 or "failed")))
    elif kind == "container" and name:
        target = os.path.join(plugin_dir, "nginx", "recorder.conf")
        say("info", "nginx runs in container %s: mount %s into its config dir "
                    "or copy it there, then reload nginx" % (name, target))
    else:
        say("info", "no nginx detected: snippet left at %s" % local)

    return local


# --------------------------------------------------------------------------
# actions & phase handlers
# --------------------------------------------------------------------------

def prepare_dirs(a):
    base = a["PLUGIN_DIR"]
    for sub in ("output", "public", "profile", "state", "backups", "ca"):
        os.makedirs(os.path.join(base, sub), exist_ok=True)
    os.makedirs(a["RECORDINGS_DIR"], exist_ok=True)
    os.makedirs(a["RECORDER_BOT_PROFILE_DIR"], exist_ok=True)
    os.chmod(base, 0o700)
    os.chmod(os.path.join(base, "backups"), 0o700)
    ca = a.get("TLS_CA_FILE", "").strip()
    if ca and os.path.isfile(ca):
        dest = os.path.join(base, "ca", "ca.crt")
        shutil.copyfile(ca, dest)
        os.chmod(dest, 0o644)
        say("ok", "CA copied to %s" % dest)
    say("ok", "plugin directories ready: %s" % base)


def find_observer_source():
    repo_root = os.path.dirname(os.path.dirname(ROOT))
    for cand in (
        os.path.join(ROOT, "vendor", "p63-observer.js"),
        os.path.join(repo_root, "src", "recorder", "p63-observer.js"),
        os.path.join(repo_root, "installer", "compat", "src", "recorder", "p63-observer.js"),
    ):
        if os.path.isfile(cand) and os.path.getsize(cand) > 0:
            return cand
    return ""


def stage_build_contexts(a, ctx):
    obs = ""
    if not a.b("ADOPT_EXISTING"):
        obs = find_observer_source()
        if not obs:
            die("p63-observer.js not found in vendor/ or repo src/recorder/.\n"
                "Run: bash tools/fetch-upstream.sh")
        say("ok", "recorder observer core: %s (%d bytes)" % (obs, os.path.getsize(obs)))

    st = os.path.join(a["PLUGIN_DIR"], "state", "build")
    os.makedirs(st, exist_ok=True)
    plan = {
        "controller": ["Dockerfile", "app.py", "requirements.txt"],
        "portal":     ["Dockerfile", "index.py", "requirements.txt"],
        "publisher":  ["Dockerfile", "publish.py"],
        "recorder":   ["Dockerfile", "server.js", "entrypoint.sh", "package.json"],
        "adopt":      ["Dockerfile", "proxy.py"],
    }
    if a.b("ADOPT_EXISTING"):
        plan.pop("recorder", None)
    for svc, files in plan.items():
        dest = os.path.join(st, svc)
        os.makedirs(dest, exist_ok=True)
        for f in files:
            src = os.path.join(ROOT, "src", svc, f)
            if not os.path.isfile(src):
                die("missing plugin source: src/%s/%s" % (svc, f))
            shutil.copyfile(src, os.path.join(dest, f))
        if svc == "recorder" and obs:
            shutil.copyfile(obs, os.path.join(dest, "p63-observer.js"))
    say("ok", "build contexts staged: %s" % st)
    return st


def write_state(a, ctx, compose_path):
    state = {
        "plugin_version": PLUGIN_VERSION,
        "installed_at": int(time.time()),
        "domain": a["DOMAIN"],
        "reverse_proxy": ctx.get("proxy_kind", ""),
        "nginx_container": ctx.get("proxy_name", ""),
        "network": ctx["network"],
        "element_network": ctx.get("element_network", ctx["network"]),
        "nginx_prefix": NGINX_PREFIX,
        "nginx_portal_prefix": NGINX_PORTAL_PREFIX,
        "controller_port": a.i("CONTROLLER_PORT", 8765),
        "portal_port": a.i("REC_HTTP_PORT", 8899),
        "element": ctx["element"],
        "element_root": ctx["element_root"],
        "synapse_url": ctx["synapse_url"],
        "ports": ctx["ports"],
        "workers": ctx["workers"],
        "accounts": ctx["users"],
        "ui_integration": "module",
        "module_entrypoint": MODULE_REL_ENTRYPOINT,
    }
    path = os.path.join(a["PLUGIN_DIR"], "state", "runtime.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.chmod(path, 0o600)
    say("ok", "state saved: " + path)


def account_exists(ctx, user):
    if not ctx["synapse_url"]:
        return None
    quoted = urllib.parse.quote(user, safe="")
    _, out, _ = sh("curl -sk --max-time 8 -o /dev/null -w '%%{http_code}' "
                   "'%s/_matrix/client/v3/profile/%s'"
                   % (ctx["synapse_url"], quoted), timeout=20)
    if out.strip() == "200":
        return True
    if out.strip() in ("404", "403"):
        return False
    return None


def check_accounts(a, ctx):
    if not ctx["synapse_url"]:
        say("warn", "cannot verify recorder bot accounts (SYNAPSE_URL unknown)")
        return []
    missing = []
    for u in ctx["users"]:
        ex = account_exists(ctx, u)
        if ex is True:
            say("ok", "recorder bot account exists: %s" % u)
        elif ex is False:
            say("warn", "recorder bot account MISSING in Synapse: %s" % u)
            missing.append(u)
        else:
            say("warn", "could not verify bot account %s" % u)
    if missing:
        print()
        print("  To create missing bot account(s), run:")
        print("  docker exec -it %s register_new_matrix_user \\"
              % (ctx["synapse_container"] or "matrix-synapse"))
        print("      -c /data/homeserver.yaml -u <localpart> --no-admin http://127.0.0.1:8008")
        print()
    return missing


HEALTH_WAIT = int(os.environ.get("RECORDER_HEALTH_WAIT", "120"))


def wait_http(url, seconds=None):
    if seconds is None:
        seconds = HEALTH_WAIT
    deadline = time.time() + seconds
    while time.time() < deadline:
        rc, out, _ = sh("curl -sk --max-time 3 -o /dev/null -w '%%{http_code}' '%s'" % url,
                        timeout=10)
        if rc == 0 and out.strip() in ("200", "204"):
            return True
        time.sleep(2)
    return False


def compose(a, compose_path, *args, **kw):
    return sh(["docker", "compose", "-f", compose_path, "-p", PROJECT] + list(args), **kw)


def cmd_check(a):
    verify_host_components(a, allow_install=False)
    ctx = discover(a)
    print_plan(a, ctx)
    check_routing_prereqs(a, ctx)
    if nginx_routes_present(a, ctx):
        say("ok", "nginx already serves %s" % NGINX_PREFIX)
    else:
        say("warn", "nginx does not serve %s yet; --install will generate a snippet"
            % NGINX_PREFIX)
    check_accounts(a, ctx)
    say("ok", tr(a, "check_ok"))
    return 0


def cmd_install(a):
    verify_host_components(a, allow_install=True)
    ctx = discover(a)
    print_plan(a, ctx)
    check_routing_prereqs(a, ctx)
    check_accounts(a, ctx)
    prepare_dirs(a)
    stage_build_contexts(a, ctx)

    compose_path = os.path.join(a["PLUGIN_DIR"], "state", "compose.yaml")
    with open(compose_path, "w", encoding="utf-8") as fh:
        fh.write(render_compose(a, ctx))
    os.chmod(compose_path, 0o600)
    say("ok", "compose written: " + compose_path)
    write_state(a, ctx, compose_path)

    if not a.b("ADOPT_EXISTING"):
        say("info", "building plugin images...")
        rc, out, err = compose(a, compose_path, "build", timeout=5400)
        if rc != 0:
            print(out[-4000:])
            die("image build failed:\n" + err[-3000:])
        say("ok", "images built")

    rc, out, err = compose(a, compose_path, "up", "-d", "--remove-orphans", timeout=900)
    if rc != 0:
        print(out[-4000:])
        die("compose up failed:\n" + err[-3000:])
    say("ok", "overlay stack started")

    if not a.b("ADOPT_EXISTING"):
        for i, port in enumerate(ctx["ports"]):
            ok = wait_http("http://127.0.0.1:%d/health" % port)
            say("ok" if ok else "err",
                "worker %d on :%d %s" % (i + 1, port, "healthy" if ok else "NOT healthy"))

    snippet = write_nginx_snippet(a, ctx)
    if not nginx_routes_present(a, ctx):
        say("warn", "nginx does not serve %s yet. Add the generated snippet:\n"
                    "  include %s;\n  nginx -t && systemctl reload nginx"
            % (NGINX_PREFIX, snippet))

    install_element_module(a, ctx)
    print()
    say("ok", tr(a, "install_ok"))
    return 0


def cmd_repair(a):
    verify_host_components(a, allow_install=True)
    ctx = discover(a)
    prepare_dirs(a)
    write_nginx_snippet(a, ctx)
    install_element_module(a, ctx)
    compose_path = os.path.join(a["PLUGIN_DIR"], "state", "compose.yaml")
    if os.path.isfile(compose_path):
        compose(a, compose_path, "up", "-d", timeout=300)
    say("ok", tr(a, "repair_ok"))
    return 0


def cmd_uninstall(a):
    compose_path = os.path.join(a["PLUGIN_DIR"], "state", "compose.yaml")
    if os.path.isfile(compose_path):
        compose(a, compose_path, "down", "--remove-orphans", timeout=300)
        say("ok", "overlay stack stopped and removed")

    el = find_element(a)
    remove_element_module(a, el)
    print()
    say("ok", tr(a, "uninstall_ok"))
    print("  Preserved recordings : %s" % a["RECORDINGS_DIR"])
    print("  Preserved E2EE bot   : %s" % a["RECORDER_BOT_PROFILE_DIR"])
    return 0


def main():
    ap = argparse.ArgumentParser(description="Matrix Element Call Recorder Plugin v2.0 installer")
    ap.add_argument("--phase", required=True,
                    choices=["check", "install", "repair", "reapply-ui", "uninstall"])
    ap.add_argument("--answers", required=True)
    ap.add_argument("--yes", action="store_true", help="Non-interactive mode")
    args = ap.parse_args()

    phase = "repair" if args.phase == "reapply-ui" else args.phase
    a = load_answers(args.answers, assume_yes=args.yes)

    handler = {
        "check": cmd_check,
        "install": cmd_install,
        "repair": cmd_repair,
        "uninstall": cmd_uninstall,
    }[phase]
    sys.exit(handler(a) or 0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die("interrupted")
