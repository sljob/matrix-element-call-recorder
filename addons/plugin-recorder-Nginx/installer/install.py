#!/usr/bin/env python3
"""Orchestrator for the Nginx edition of the Matrix Element Call recorder plugin.

Phases:
  check      read-only: discovery + preflight report, exit 2 on blockers
  install    preflight, build images, start stack, write nginx config, register UI
  repair     re-apply nginx config, mounts and Element registration (no rebuild)
  verify     end-to-end probe of every hop the button uses, read-only
  doctor     deep diagnostics with no changes (for "it does not work" tickets)
  uninstall  remove containers, nginx files and UI registration, keep data

Deliberate non-goals: this installer never touches Traefik, never reconfigures
Synapse, never edits another service's nginx `server{}` unless the operator
chose NGINX_MODE=external, and never deletes recordings or the bot's E2EE
profile.
"""

import argparse
import json
import os
import subprocess
import sys
import time

import core
import nginxconf
import preflight
from core import (B, C, E, G, MODULE_REL_ENTRYPOINT, NGINX_PORTAL_PREFIX,
                  NGINX_PREFIX, PLUGIN_VERSION, R, Y, die, say, sh, tr)


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

def discover(a):
    """Fill ctx with everything later phases need. Read-only."""
    ctx = {}
    core.docker_ready()

    element = core.find_element(a)
    if not element:
        die("Element Web container not found.\n"
            '  Set ELEMENT_WEB_CONTAINER="<name>" in answers (see: docker ps)')
    ctx["element"] = element
    ctx["element_root"] = core.detect_element_root(element) or "/app"
    say("ok", "Element Web: %s (root=%s)" % (element, ctx["element_root"]))

    domain = core.autodetect_domain(a, element)
    if not domain or "." not in domain:
        die("DOMAIN could not be determined.\n"
            '  Set DOMAIN="meet.example.com" in answers')
    a["DOMAIN"] = domain
    say("ok", "Domain: %s" % domain)

    ctx["element_call"] = core.find_element_call(a)
    syn_url, syn_cnt = core.find_synapse(a)
    ctx["synapse_url"] = syn_url
    ctx["synapse_container"] = syn_cnt
    if not syn_url:
        say("warn", "Synapse URL not detected; set SYNAPSE_INTERNAL_URL in answers")

    users, passwords, workers = core.resolve_bot_accounts(a, domain)
    ctx["users"] = users
    ctx["passwords"] = passwords
    ctx["workers"] = workers
    say("ok", "recorder bot(s): %s" % ", ".join(users))

    el_nets = core.container_networks(element)
    ctx["element_network"] = el_nets[0] if el_nets else "bridge"
    net = a.get("DOCKER_ADDONS_NETWORK", "").strip()
    if not net or net == "auto":
        front = core.find_nginx_container(a.get("NGINX_CONTAINER", ""))
        nets = core.container_networks(front) if front else []
        net = (nets[0] if nets else ctx["element_network"]) or "bridge"
    ctx["network"] = net
    say("ok", "docker network: %s" % net)

    mode = (a.get("NGINX_MODE", "standalone") or "standalone").strip().lower()
    a["NGINX_MODE"] = mode
    ctx["front_container"] = core.find_nginx_container(a.get("NGINX_CONTAINER", ""))
    ctx["nginx_kind"] = "host" if mode == "external" else "standalone"
    ctx["standalone_port"] = a.i("STANDALONE_PORT", nginxconf.STANDALONE_PORT_DEFAULT)
    say("ok", "nginx mode: %s%s" % (mode, (" (front: %s)" % ctx["front_container"])
                                    if ctx["front_container"] and mode == "external" else ""))

    ctx["call_url"] = (a.get("ELEMENT_CALL_URL", "").strip()
                       or "http://127.0.0.1:%d" % a.i("ELEMENT_CALL_LOCAL_PORT", 8090))
    ctx["token"] = a.get("RECORDER_TOKEN", "").strip() or _stable_token(a)
    ctx["ports"] = ([] if a.b("ADOPT_EXISTING", False)
                    else core.pick_ports(a, workers))
    if ctx["ports"]:
        say("ok", "worker port(s): %s" % ", ".join(str(p) for p in ctx["ports"]))

    ca = a.get("TLS_CA_FILE", "").strip() or a.get("CUSTOM_CA_CERT", "").strip()
    if ca and ca != "auto" and os.path.isfile(ca):
        dst = os.path.join(a["PLUGIN_DIR"], "ca", "ca.crt")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        import shutil
        shutil.copyfile(ca, dst)
        a["TLS_CA_FILE"] = dst
        ctx["ca_file"] = dst
    else:
        ctx["ca_file"] = ""
    ctx["tls_mode"] = core.detect_tls(a, ctx)
    return ctx


def _stable_token(a):
    """Reuse the token from a previous install so running workers stay valid."""
    state = os.path.join(a["PLUGIN_DIR"], "state", "runtime.json")
    if os.path.isfile(state):
        try:
            with open(state, encoding="utf-8") as fh:
                old = json.load(fh).get("recorder_token")
            if old:
                return old
        except (OSError, ValueError):
            pass
    import secrets
    return secrets.token_hex(24)


# ---------------------------------------------------------------------------
# plan output
# ---------------------------------------------------------------------------

def print_plan(a, ctx):
    print()
    print(core.B + "План установки (v%s, nginx edition)" % PLUGIN_VERSION + R)
    rows = [
        ("домен", a["DOMAIN"]),
        ("каталог плагина", a["PLUGIN_DIR"]),
        ("каталог записей", a["RECORDINGS_DIR"]),
        ("профиль E2EE бота", a["RECORDER_BOT_PROFILE_DIR"]),
        ("язык установщика/UI", "%s / %s" % (a.get("INSTALLER_LANG", "ru"),
                                             a.get("BUTTON_LANG", "ru"))),
        ("режим nginx", a["NGINX_MODE"] + (
            " (порт %d, %s)" % (ctx["standalone_port"], nginxconf.CONTAINER_NAME)
            if a["NGINX_MODE"] == "standalone" else "")),
        ("docker сеть", ctx["network"]),
        ("Element Web", "%s (root: %s)" % (ctx["element"], ctx["element_root"])),
        ("Synapse", ctx["synapse_url"] or "не определён"),
        ("Element Call", ctx["call_url"]),
        ("боты записи", ", ".join(ctx["users"])),
        ("worker(ы) / порты", "%d -> %s" % (ctx["workers"],
                                            ", ".join(map(str, ctx["ports"])) or "—")),
        ("UI", "ESM Module API (%s)" % MODULE_REL_ENTRYPOINT),
        ("маршруты", "%s/* (UI+API), %s/* (записи)" % (NGINX_PREFIX, NGINX_PORTAL_PREFIX)),
    ]
    width = max(len(k) for k, _ in rows) + 1
    for key, value in rows:
        print("  %-*s: %s" % (width, key, value))
    print()


# ---------------------------------------------------------------------------
# phases
# ---------------------------------------------------------------------------

def cmd_check(a):
    ctx = discover(a)
    print_plan(a, ctx)
    pf = preflight.run(a, ctx, a["__answers_path"])
    pf.report()
    say("ok", tr(a, "check_ok"))
    return 2 if pf.blocked() else 0


def cmd_doctor(a):
    ctx = discover(a)
    print_plan(a, ctx)
    pf = preflight.run(a, ctx, a["__answers_path"])
    pf.report()
    doctor(a, ctx)
    say("ok", "диагностика завершена (изменений не вносилось)")
    return 3 if pf.blocked() else 0


def doctor(a, ctx):
    print(core.B + "Глубокий разбор маршрута кнопки записи" + R)
    port = ctx["standalone_port"]
    api_port = a.i("CONTROLLER_PORT", 8765)

    checks = []

    state_file = os.path.join(a["PLUGIN_DIR"], "state", "runtime.json")
    checks.append(("state/runtime.json", os.path.isfile(state_file),
                   state_file if os.path.isfile(state_file) else "нет — плагин не установлен"))

    api_ip = core.container_ip_on("rec-controller", ctx["network"])
    checks.append(("rec-controller в сети %s" % ctx["network"], bool(api_ip),
                   api_ip or "контейнер не найден/не подключён"))

    if api_ip:
        code = _http_code("http://%s:%d/api/health" % (api_ip, api_port))
        checks.append(("controller /api/health напрямую", str(code) == "200",
                       "HTTP %s" % code))

    if a["NGINX_MODE"] == "standalone":
        code = _http_code("http://127.0.0.1:%d/healthz" % port)
        checks.append(("nginx /healthz", str(code) == "200", "HTTP %s" % code))
        code = _http_code("http://127.0.0.1:%d%s/" % (port, NGINX_PREFIX))
        checks.append(("nginx %s/ (UI)" % NGINX_PREFIX, str(code) == "200", "HTTP %s" % code))
        code = _http_code("http://127.0.0.1:%d%s/api/version" % (port, NGINX_PREFIX))
        checks.append(("nginx %s/api/version" % NGINX_PREFIX, str(code) == "200",
                       "HTTP %s" % code))
        from_inside = _http_code("http://%s:%d/api/version" % (api_ip or "127.0.0.1", api_port),
                                 in_container=nginxconf.CONTAINER_NAME)
        checks.append(("nginx -> controller (изнутри контейнера)", str(from_inside) == "200",
                       "HTTP %s" % from_inside))
        connected = ctx["network"] in core.container_networks(nginxconf.CONTAINER_NAME)
        checks.append(("nginx в сети %s" % ctx["network"], connected,
                       "нет — docker network connect %s %s"
                       % (ctx["network"], nginxconf.CONTAINER_NAME) if not connected else "да"))

    worker_port = ctx["ports"][0] if ctx["ports"] else a.i("RECORDER_HOST_PORT", 8788)
    code = _http_code("http://127.0.0.1:%d/health" % worker_port)
    checks.append(("worker :%d /health" % worker_port, str(code) == "200",
                   "HTTP %s (worker поднимается ~40 c)" % code))

    for label, ok, detail in checks:
        say("ok" if ok else "warn", "%-42s %s" % (label, detail))
    print()


def _http_code(url, in_container=""):
    cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "8", url]
    if in_container:
        rc, out, _err = sh(["docker", "exec", in_container] + cmd, timeout=20)
    else:
        rc, out, _err = sh(cmd, timeout=20)
    return out.strip() if rc == 0 else "000"


def cmd_install(a):
    ctx = discover(a)
    print_plan(a, ctx)
    pf = preflight.run(a, ctx, a["__answers_path"])
    pf.report()
    if pf.blocked():
        if not a.b("FORCE_INSTALL", False):
            say("err", "установка остановлена проверкой конфликтов")
            print('  Осознанный обход: FORCE_INSTALL="true" bash install.sh --install <answers>')
            return 3
        say("warn", "FORCE_INSTALL=true: блокеры игнорируются осознанно")

    core.prepare_dirs(a)
    core.check_accounts(a, ctx)
    core.stage_build_contexts(a, ctx)

    compose_path = os.path.join(a["PLUGIN_DIR"], "state", "compose.yaml")
    core.write_file(compose_path, core.render_compose(a, ctx), mode=0o600)
    say("ok", "compose: %s" % compose_path)
    core.write_state(a, ctx, compose_path)

    if not a.b("ADOPT_EXISTING", False):
        say("info", "сборка образов (первый раз 5-15 минут)...")
        rc, out, err = core.compose(a, compose_path, "build", timeout=5400)
        if rc != 0:
            print(out[-3000:])
            die("сборка образов не удалась:\n" + err[-2500:])
        say("ok", "образы собраны")

    rc, out, err = core.compose(a, compose_path, "up", "-d", "--remove-orphans", timeout=900)
    if rc != 0:
        print(out[-3000:])
        die("docker compose up не удался:\n" + err[-2500:])
    say("ok", "стек запущен")

    api_port = a.i("CONTROLLER_PORT", 8765)
    ok = core.wait_http("http://127.0.0.1:%d/api/health" % api_port) or \
        core.wait_http("http://127.0.0.1:%d/health" % api_port)
    say("ok" if ok else "err", "controller /api/health %s" % ("отвечает" if ok else "НЕ отвечает"))
    if not ok:
        rc, out, _err = core.sh(["docker", "logs", "--tail", "40", "rec-controller"], timeout=30)
        print(out)

    for idx, port in enumerate(ctx["ports"]):
        ok = core.wait_http("http://127.0.0.1:%d/health" % port)
        say("ok" if ok else "warn", "worker %d на :%d %s"
            % (idx + 1, port, "healthy" if ok else "пока не отвечает"))

    apply_nginx(a, ctx)
    core.install_element_module(a, ctx)
    smoke(a, ctx)
    print()
    say("ok", tr(a, "install_ok"))
    return 0


def apply_nginx(a, ctx):
    """Own the nginx layer, in whichever mode the operator picked."""
    # Both containers must share a network before we hand an IP to nginx.
    for name in ("rec-controller", "rec-portal"):
        ok, detail = core.connect_network(name, ctx["network"])
        if not ok:
            say("warn", "%s: не удалось подключить к сети %s: %s"
                % (name, ctx["network"], detail))

    if a["NGINX_MODE"] == "external":
        conf_root = _front_conf_root(ctx)
        return nginxconf.apply_external(a, ctx, conf_root)

    port = nginxconf.apply_standalone(a, ctx, {"standalone_port": ctx["standalone_port"]})
    ok, detail = core.connect_network(nginxconf.CONTAINER_NAME, ctx["network"])
    say("ok" if ok else "warn", "nginx в сети %s: %s" % (ctx["network"], detail))
    return port


def _front_conf_root(ctx):
    front = ctx.get("front_container") or ""
    if front:
        host_dir = core.container_mount_for(front, "/etc/nginx")
        if not host_dir:
            die("%s has no bind mount for /etc/nginx, so its config cannot be "
                "edited from the host.\n  Use NGINX_MODE=\"standalone\"" % front)
        return host_dir
    return core.host_nginx_confdir()


def smoke(a, ctx):
    print()
    print(core.B + "Дымовые проверки" + R)
    if a["NGINX_MODE"] == "external":
        say("info", "режим external: проверь свой фронт-прокси, "
                    "маршрут %s/ должен отдавать 200" % NGINX_PREFIX)
        return
    port = ctx["standalone_port"]
    for path, expect in (("/healthz", "200"),
                         ("%s/" % NGINX_PREFIX, "200"),
                         ("%s/api/health" % NGINX_PREFIX, "200"),
                         ("%s/api/version" % NGINX_PREFIX, "200")):
        code = _http_code("http://127.0.0.1:%d%s" % (port, path))
        say("ok" if code == expect else "warn", "%-28s -> %s" % (path, code))
    print()
    print("  Плагин доступен локально: http://127.0.0.1:%d%s/" % (port, NGINX_PREFIX))
    print("  Кнопка записи в Element Call появится после перезагрузки страницы.")


def cmd_repair(a):
    ctx = discover(a)
    core.prepare_dirs(a)
    compose_path = os.path.join(a["PLUGIN_DIR"], "state", "compose.yaml")
    if os.path.isfile(compose_path):
        rc, out, err = core.compose(a, compose_path, "up", "-d", timeout=600)
        say("ok" if rc == 0 else "warn", "стек %s" % ("поднят" if rc == 0 else err[-300:]))
    apply_nginx(a, ctx)
    core.install_element_module(a, ctx)
    smoke(a, ctx)
    say("ok", tr(a, "repair_ok"))
    return 0


def cmd_verify(a):
    ctx = discover(a)
    smoke(a, ctx)
    doctor(a, ctx)
    return 0


def cmd_uninstall(a):
    ctx = {}
    compose_path = os.path.join(a["PLUGIN_DIR"], "state", "compose.yaml")
    if os.path.isfile(compose_path):
        rc, _out, err = core.compose(a, compose_path, "down", "--remove-orphans", timeout=300)
        say("ok" if rc == 0 else "warn", "стек остановлен" + ("" if rc == 0 else ": " + err[-200:]))
    sh(["docker", "rm", "-f", nginxconf.CONTAINER_NAME], timeout=120)

    conf_root = os.path.join(a["PLUGIN_DIR"], "nginx", "conf.d")
    for rel in (nginxconf.LOCATIONS_FILE, nginxconf.DEFAULT_SERVER_FILE):
        p = os.path.join(conf_root, rel)
        if os.path.isfile(p):
            os.unlink(p)
            say("ok", "удалён %s" % p)

    el = core.find_element(a)
    if el:
        core.remove_element_module(a, el)
    print()
    say("ok", tr(a, "uninstall_ok"))
    print("  Записи сохранены      : %s" % a["RECORDINGS_DIR"])
    print("  E2EE профиль бота     : %s" % a["RECORDER_BOT_PROFILE_DIR"])
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

PHASES = {
    "check": cmd_check,
    "install": cmd_install,
    "repair": cmd_repair,
    "verify": cmd_verify,
    "doctor": cmd_doctor,
    "uninstall": cmd_uninstall,
}


def main():
    ap = argparse.ArgumentParser(
        description="Matrix Element Call Recorder — nginx plugin v%s" % PLUGIN_VERSION)
    ap.add_argument("--phase", required=True,
                    choices=["check", "install", "repair", "verify", "doctor", "uninstall"])
    ap.add_argument("--answers", required=True)
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    a = core.load_answers(args.answers, assume_yes=args.yes)
    a["__answers_path"] = os.path.abspath(args.answers)
    try:
        sys.exit(PHASES[args.phase](a) or 0)
    except KeyboardInterrupt:
        die("прервано пользователем")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die("прервано пользователем")
