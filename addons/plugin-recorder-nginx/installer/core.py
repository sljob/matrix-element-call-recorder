#!/usr/bin/env python3
"""Shared primitives for the Nginx edition of the recorder plugin installer.

Everything here is host-facing: answers parsing, tiny shell helpers, docker
introspection, discovery of the existing Element/Synapse/Element Call stack,
compose rendering and the Element Web Module API registration.

Rule of thumb for this file: no phase logic. If a function decides whether to
change something, it belongs in install.py or nginxconf.py.
"""

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

PLUGIN_VERSION = "3.1.0"
PROJECT = "recorder-plugin"
EDITION = "nginx"

NGINX_PREFIX = "/recording"
NGINX_PORTAL_PREFIX = "/recordings"
MODULE_CONFIG_KEY = "io.github.matrix-element-call-recorder"
MODULE_REL_ENTRYPOINT = "/modules/recorder/index.js"
LEGACY_MARKER_BEGIN = "<!-- recorder-plugin:begin -->"
LEGACY_MARKER_END = "<!-- recorder-plugin:end -->"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

HEALTH_WAIT = int(os.environ.get("RECORDER_HEALTH_WAIT", "150"))

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


HOST_REQUIREMENTS = [
    ("docker",  "docker.io",               "Docker Engine"),
    ("curl",    "curl",                    "HTTP client (curl)"),
    ("jq",      "jq",                      "JSON processor (jq)"),
    ("openssl", "openssl",                 "OpenSSL CLI (TLS & SPKI checks)"),
]


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


# --------------------------------------------------------------------------
# discovery helpers added for the nginx edition
# --------------------------------------------------------------------------

def find_nginx_container(explicit=""):
    """Locate a container that terminates HTTP for the stack.

    Returns the name of a running nginx container (front proxy or our own
    standalone one) or "" when nginx runs directly on the host / not at all.
    """
    explicit = (explicit or "").strip()
    if explicit and explicit != "auto":
        return explicit
    for name in running_containers():
        if name.startswith("rec-"):
            continue
        d = inspect(name) or {}
        image = ((d.get("Config") or {}).get("Image") or "").lower()
        ports = (d.get("NetworkSettings") or {}).get("Ports") or {}
        publishes_http = any(p in ports and ports[p] for p in ("80/tcp", "443/tcp"))
        if "nginx" in image or "nginx" in name.lower():
            if publishes_http:
                return name
    return ""


def host_has_nginx():
    rc, out, _err = sh("command -v nginx >/dev/null 2>&1 && nginx -v 2>&1", timeout=15)
    return rc == 0 and bool(out.strip())


def host_nginx_confdir():
    for cand in ("/etc/nginx/conf.d", "/etc/nginx/sites-enabled"):
        if os.path.isdir(cand):
            return cand
    return "/etc/nginx/conf.d"


def container_mount_for(name, destination):
    """Host path bind-mounted at `destination` inside `name`, or ""."""
    d = inspect(name) or {}
    for m in d.get("Mounts") or []:
        if (m.get("Destination") or "").rstrip("/") == destination.rstrip("/"):
            return m.get("Source") or ""
    return ""


def container_ip_on(name, network):
    d = inspect(name) or {}
    nets = ((d.get("NetworkSettings") or {}).get("Networks") or {})
    entry = nets.get(network) or {}
    return (entry.get("IPAddress") or "").strip()


def port_owner_container(port):
    """Name of the container publishing host `port`, or ""."""
    rc, out, _err = sh(["docker", "ps", "--format", "{{.Names}}"], timeout=20)
    for name in [x for x in out.splitlines() if x.strip()]:
        d = inspect(name) or {}
        ports = (d.get("NetworkSettings") or {}).get("Ports") or {}
        for spec, bindings in ports.items():
            for b in bindings or []:
                if str(b.get("HostPort")) == str(port):
                    return name
    return ""


def connect_network(container, network):
    rc, _out, err = sh(["docker", "network", "connect", network, container], timeout=60)
    if rc == 0:
        return True, "connected"
    if "already exists" in err or "already attached" in err:
        return True, "already attached"
    return False, err[-300:]

def write_file(path, content, mode=0o644):
    """Atomic write: a half-written nginx config is worse than none."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
