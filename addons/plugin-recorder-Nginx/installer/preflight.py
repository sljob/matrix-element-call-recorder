#!/usr/bin/env python3
"""Read-only conflict detection, run before anything is touched.

Every finding carries a stable code (NET, PORT, IMG, CNT, NGX, TPL, CUR, RES,
ANS, FRONT) so an operator can grep a log, and every blocker carries a hint
that is an actionable command, not a comment. `--check` prints this report and
changes nothing; `--install` runs exactly the same checks and refuses to
continue while a blocker is present unless FORCE_INSTALL=true.
"""

import os
import re
import shutil
import socket

import core
from core import E, G, R, Y

SEV_ORDER = {"FATAL": 0, "WARN": 1, "INFO": 2, "OK": 3}


class Report(object):
    def __init__(self, lang="ru"):
        self.lang = "ru" if lang not in ("ru", "en") else lang
        self.rows = []

    # -- recording ---------------------------------------------------------
    def _add(self, sev, code, msg, hint=""):
        self.rows.append({"sev": sev, "code": code, "msg": msg, "hint": hint})
        return self

    def fatal(self, code, msg, hint=""):
        return self._add("FATAL", code, msg, hint)

    def warn(self, code, msg, hint=""):
        return self._add("WARN", code, msg, hint)

    def info(self, code, msg, hint=""):
        return self._add("INFO", code, msg, hint)

    def ok(self, code, msg):
        return self._add("OK", code, msg)

    # -- queries -----------------------------------------------------------
    def of(self, sev):
        return [r for r in self.rows if r["sev"] == sev]

    def blocked(self):
        return bool(self.of("FATAL"))

    def codes(self, sev):
        return sorted({r["code"] for r in self.of(sev)})

    # -- output ------------------------------------------------------------
    def report(self):
        title = ("ПРОВЕРКА ПЕРЕД УСТАНОВКОЙ" if self.lang == "ru"
                 else "PRE-INSTALL CONFLICT CHECK")
        print()
        print(core.B + "=" * 76 + core.R)
        print(core.B + "  " + title + core.R)
        print(core.B + "=" * 76 + core.R)
        for sev, colour, label in (("FATAL", E, "БЛОКЕРЫ" if self.lang == "ru" else "BLOCKERS"),
                                   ("WARN", Y, "ПРЕДУПРЕЖДЕНИЯ" if self.lang == "ru" else "WARNINGS")):
            items = self.of(sev)
            if not items:
                continue
            print("%s%s (%d)%s" % (colour, label, len(items), R))
            for r in items:
                print("  %s%-5s %-5s%s %s" % (colour, "XX" if sev == "FATAL" else "!!",
                                              r["code"], R, r["msg"]))
                if r["hint"]:
                    for line in str(r["hint"]).splitlines():
                        print("             %s" % line)
        oks = self.of("OK")
        print("%sПРОВЕРЕНО (%d)%s" % (G, len(oks), R))
        for r in oks:
            print("  OK    %-5s %s" % (r["code"], r["msg"]))
        print("-" * 76)
        print("  блокеров %d · предупреждений %d · проверено %d"
              % (len(self.of("FATAL")), len(self.of("WARN")), len(oks)))
        if self.blocked():
            print("%s  Установка заблокирована. Исправь блокеры или задай "
                  "FORCE_INSTALL=\"true\" осознанно.%s" % (E, R))
        print()


# ---------------------------------------------------------------------------
# individual checks
# ---------------------------------------------------------------------------

def check_binaries(pf, answers):
    required = [("docker", "docker.io", "Docker Engine"),
                ("curl", "curl", "HTTP client"),
                ("openssl", "openssl", "OpenSSL CLI")]
    for binary, pkg, label in required:
        if shutil.which(binary):
            pf.ok("HOST", "%s (%s)" % (label, binary))
        else:
            pf.fatal("HOST", "%s missing (binary %r)" % (label, binary),
                     "apt-get install -y %s" % pkg)
    if not shutil.which("python3"):
        pf.fatal("HOST", "python3 missing", "apt-get install -y python3")


def check_docker(pf):
    rc, out, err = core.sh(["docker", "info"], timeout=30)
    if rc != 0:
        pf.fatal("DKR", "docker daemon unreachable", err[-300:] or "systemctl start docker")
        return False
    pf.ok("DKR", "docker daemon reachable")
    rc, out, _err = core.sh(["docker", "compose", "version"], timeout=30)
    if rc == 0 and out:
        pf.ok("DKR", "docker compose v2: %s" % out.splitlines()[0])
    else:
        pf.fatal("DKR", "docker compose v2 plugin missing",
                 "apt-get install -y docker-compose-v2")
    return True


def check_ports(pf, answers, mode):
    """Only the ports this mode will actually bind."""
    wanted = []
    if (answers.get("NGINX_MODE", "standalone") or "standalone") == "standalone":
        wanted.append(("standalone nginx", answers.i("STANDALONE_PORT", 18085)))
    if answers.b("PUBLISH_LOOPBACK_PORTS", True):
        wanted.append(("rec-controller", answers.i("CONTROLLER_PORT", 8765)))
        wanted.append(("rec-portal", answers.i("REC_HTTP_PORT", 8899)))
    for label, port in wanted:
        in_use = port_in_use(port)
        if not in_use:
            pf.ok("PORT", "%s: %d свободен" % (label, port))
            continue
        owner = core.port_owner_container(port)
        if owner in ("rec-controller", "rec-portal", "record-plugin-static"):
            pf.info("PORT", "%s: %d занят своим же контейнером %s" % (label, port, owner),
                    "будет пересоздан при --install")
        else:
            who = ("контейнер %s" % owner) if owner else "посторонний процесс"
            pf.fatal("PORT", "%s: порт %d занят (%s)" % (label, port, who),
                     "освободи порт или задай другой в answers:\n"
                     "  %s" % port_key(label))


def port_key(label):
    if label.endswith("nginx"):
        return 'STANDALONE_PORT="18086"'
    if label == "rec-controller":
        return 'CONTROLLER_PORT="8766"'
    return 'REC_HTTP_PORT="8900"'


def port_in_use(port):
    for addr in ("127.0.0.1", "0.0.0.0"):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((addr, port))
        except OSError:
            return True
        finally:
            s.close()
    return False


def check_containers(pf, answers):
    """Foreign lookalikes are the #1 cause of "the button talks to the wrong thing"."""
    names = core.running_containers()
    all_names = names + [ln.split()[0] for ln in
                         core.sh(["docker", "ps", "-a", "--format", "{{.Names}}"],
                                 timeout=20)[1].splitlines() if ln.strip()]
    for foreign in ("recording-controller", "rec-controller-legacy"):
        if foreign in all_names:
            pf.fatal("CNT", "посторонний контейнер %s уже существует" % foreign,
                     "он не принадлежит этому плагину и перехватывает /api/.\n"
                     "  docker rm -f %s" % foreign)
    ours = [n for n in all_names if n in ("rec-controller", "rec-portal",
                                          "rec-publisher", "record-plugin-static")
            or re.fullmatch(r"rec-worker-\d+", n)]
    if ours:
        pf.info("CNT", "контейнеры прежней установки: %s" % ", ".join(sorted(ours)),
                "будут пересозданы при --install")
    else:
        pf.ok("CNT", "следов прежней установки нет")

    st = core.inspect("rec-controller")
    if st and (st.get("State") or {}).get("Status") == "restarting":
        pf.fatal("CNT", "rec-controller в цикле перезапуска",
                 "docker logs --tail 30 rec-controller\n"
                 "почти всегда причина — пустые обязательные переменные окружения")


def check_stack_containers(pf, answers, ctx):
    for key, value, label in (("ELEMENT_WEB_CONTAINER", ctx.get("element"), "Element Web"),
                              ("ELEMENT_CALL_CONTAINER", ctx.get("element_call"), "Element Call"),
                              ("SYNAPSE_CONTAINER", ctx.get("synapse_container"), "Synapse")):
        if value:
            pf.ok("STK", "%s: %s" % (label, value))
        else:
            pf.fatal("STK", "%s container not found" % label,
                     "укажи имя явно в answers: %s=\"<docker ps имя>\"" % key)


def check_network(pf, ctx):
    net = ctx.get("network") or ""
    rc, out, _err = core.sh(["docker", "network", "ls", "--format", "{{.Name}}"], timeout=20)
    names = set(out.split())
    if not net:
        pf.fatal("NET", "не удалось определить docker-сеть плагина",
                 'DOCKER_ADDONS_NETWORK="<сеть Element Web>"')
    elif net in names:
        pf.ok("NET", "сеть плагина: %s" % net)
    else:
        pf.fatal("NET", "сети %s не существует" % net, "docker network create %s" % net)
    el_net = ctx.get("element_network") or ""
    if el_net and el_net != net:
        if el_net in names:
            pf.ok("NET", "сеть Element Web: %s (плагин подключится к обеим)" % el_net)
        else:
            pf.warn("NET", "сеть Element Web %s отсутствует" % el_net)


def check_nginx(pf, answers, ctx):
    mode = answers.get("NGINX_MODE", "standalone") or "standalone"
    if mode == "standalone":
        pf.ok("NGX", "режим standalone: собственный nginx-контейнер, чужие конфиги "
                     "не трогаются")
        return
    if mode != "external":
        pf.fatal("NGX", "NGINX_MODE=%r неизвестен" % mode,
                 'NGINX_MODE="standalone"  # или "external"')
        return
    front = ctx.get("front_container") or ""
    if front:
        pf.ok("NGX", "фронт-прокси (контейнер): %s" % front)
        host_dir = core.container_mount_for(front, "/etc/nginx")
        if not host_dir:
            pf.fatal("NGX", "%s не имеет bind-mount на /etc/nginx" % front,
                     "интеграция невозможна без хостового каталога конфигов;\n"
                     'используй NGINX_MODE="standalone"')
        else:
            pf.ok("NGX", "конфиги фронта на хосте: %s" % host_dir)
    elif core.host_has_nginx():
        pf.ok("NGX", "фронт-прокси: nginx на хосте")
    else:
        pf.fatal("NGX", "NGINX_MODE=external, но nginx не найден ни в контейнере, "
                        "ни на хосте", 'NGINX_MODE="standalone"')


def check_observer(pf):
    path = os.path.join(core.ROOT, "vendor", "p63-observer.js")
    if not os.path.isfile(path):
        pf.fatal("OBS", "vendor/p63-observer.js отсутствует",
                 "переустанови архивом; исходник обязателен для сборки worker'а")
        return
    size = os.path.getsize(path)
    if size < 5000:
        pf.fatal("OBS", "vendor/p63-observer.js подозрительно мал (%d байт)" % size,
                 "файл обрезан — перекачай архив")
        return
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    markers = ["puppeteer", "ffmpeg", "storageState"]
    missing = [m for m in markers if m not in text]
    if missing:
        pf.warn("OBS", "в observer не найдены маркеры: %s" % ", ".join(missing),
                "возможно, это не та версия ядра")
    else:
        pf.ok("OBS", "observer корректен (%d байт)" % size)


def check_resources(pf, workers):
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            mem = fh.read()
        total_kb = int(re.search(r"MemTotal:\s+(\d+)", mem).group(1))
        avail_kb = int(re.search(r"MemAvailable:\s+(\d+)", mem).group(1))
        need_gb = 1.5 * max(1, workers) + 1.0
        avail_gb = avail_kb / 1048576.0
        if avail_gb >= need_gb:
            pf.ok("RES", "RAM: доступно %.1f GiB (нужно ~%.1f для %d worker)"
                  % (avail_gb, need_gb, workers))
        else:
            pf.warn("RES", "RAM: доступно %.1f GiB, рекомендуется %.1f"
                    % (avail_gb, need_gb), "закрой лишние контейнеры")
    except (OSError, AttributeError, ValueError):
        pf.info("RES", "RAM: проверить не удалось")

    st = os.statvfs("/var/lib/docker") if os.path.isdir("/var/lib/docker") else os.statvfs("/")
    free_gb = st.f_bavail * st.f_frsize / 1073741824.0
    if free_gb >= 8:
        pf.ok("RES", "диск: %.1f GiB свободно" % free_gb)
    elif free_gb >= 3:
        pf.warn("RES", "диск: %.1f GiB свободно (образы ~2.5 GiB)" % free_gb,
                "docker image prune -f")
    else:
        pf.fatal("RES", "диск: %.1f GiB свободно — недостаточно" % free_gb,
                 "docker system prune -af --volumes  # осторожно, удалит неиспользуемое")


KNOWN_KEYS = set("""
INSTALL_ACTION INSTALLER_LANG INSTALL_MISSING_COMPONENTS FORCE_INSTALL
BUTTON_LANG BUTTON_ALWAYS_VISIBLE BUTTON_SHOW_RECORDINGS_LINK
RECORDING_API_PREFIX RECORDINGS_PUBLIC_PATH BUTTON_POLL_INTERVAL_MS UI_INTEGRATION_MODE
DOMAIN BASE_DIR PLUGIN_DIR RECORDINGS_DIR RECORDER_BOT_PROFILE_DIR
ELEMENT_WEB_CONTAINER ELEMENT_CALL_CONTAINER SYNAPSE_CONTAINER NGINX_CONTAINER
ELEMENT_CALL_INTERNAL_URL ELEMENT_CALL_URL SYNAPSE_INTERNAL_URL SYNAPSE_URL
DOCKER_ADDONS_NETWORK NGINX_MODE STANDALONE_PORT
RECORDING_MIN_POWER_LEVEL RECORDINGS_MIN_POWER_LEVEL
PUBLISH_LOOPBACK_PORTS RECORDER_BOT_COUNT RECORDER_BOT_USERS RECORDER_BOT_PREFIX
RECORDER_BOT_PASSWORDS RECORDER_BOT_DISPLAYNAME RECORDER_ACCOUNTS RECORDING_WORKERS
BACKUP_ELEMENT_WEB ENABLE_APPSERVICE_AUTO_INVITE
SYNAPSE_REGISTRATION_SHARED_SECRET SYNAPSE_CONFIG_PATH CUSTOM_CA_CERT TLS_CA_FILE
CHROMIUM_SPKI_HASH SPKI_HASH RECORDER_HOST_PORT CONTROLLER_PORT REC_HTTP_PORT
APPSERVICE_PORT RECORDER_BASE_PORT RECORD_RESOLUTION RECORD_FRAMERATE
RECORD_VIDEO_BITRATE RECORD_AUDIO_BITRATE PRESERVE_DATA_ON_UNINSTALL
RECORDER_TOKEN ADOPT_EXISTING ADOPT_CONTROLLER_URL ADOPT_PORTAL_URL
SYNAPSE_HOST_URL SYNAPSE_LOCAL_PORT LK_INTERNAL_URL LIVEKIT_LOCAL_PORT
PULSE_SRC CHROME_PATH RECORD_CRF RECORD_MAX_SECONDS INSTALL_LANGUAGE
""".split())


def check_answers(pf, answers, path):
    if not path or not os.path.isfile(path):
        pf.fatal("ANS", "answers-файл не найден: %s" % path,
                 "cp answers.example /root/answers.env && chmod 600 /root/answers.env")
        return
    mode = os.stat(path).st_mode & 0o777
    if mode & 0o077:
        pf.fatal("ANS", "answers виден другим пользователям (mode %o)" % mode,
                 "chmod 600 %s" % path)
    unknown, dupes = [], []
    seen = set()
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                pf.fatal("ANS", "строка %d не является KEY=VALUE" % lineno)
                continue
            key = line.split("=", 1)[0].strip()
            if key in seen:
                dupes.append(key)
            seen.add(key)
            if key not in KNOWN_KEYS:
                unknown.append(key)
    if dupes:
        pf.fatal("ANS", "дублирующиеся ключи: %s" % ", ".join(sorted(set(dupes))),
                 "оставь по одному вхождению каждого ключа")
    if unknown:
        pf.info("ANS", "неизвестные ключи (игнорируются): %s"
                % ", ".join(sorted(unknown)))
    if not answers.get("DOMAIN") or answers.get("DOMAIN") == "auto":
        pf.info("ANS", "DOMAIN=auto — будет определён из Element Web")
    if answers.i("RECORDER_BOT_COUNT", 1) > 1:
        pf.warn("ANS", "RECORDER_BOT_COUNT=%d: нужен отдельный Matrix-аккаунт на "
                       "каждый worker и RAM на каждый Chromium"
                % answers.i("RECORDER_BOT_COUNT", 1))


def check_worker_port(pf, answers):
    port = answers.i("RECORDER_HOST_PORT", 8788)
    if port_in_use(port):
        owner = core.port_owner_container(port)
        if owner and owner.startswith("rec-worker"):
            pf.info("WRK", "worker порт %d занят своим контейнером %s" % (port, owner),
                    "будет пересоздан")
        else:
            pf.fatal("WRK", "worker порт %d занят (%s)"
                    % (port, owner or "неизвестный процесс"),
                    "worker использует network_mode=host; освободи порт или\n"
                    '  задай RECORDER_HOST_PORT="8790"')
    else:
        pf.ok("WRK", "worker порт %d свободен" % port)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def run(answers, ctx, answers_path=""):
    pf = Report(answers.get("INSTALLER_LANG", "ru"))
    check_binaries(pf, answers)
    if check_docker(pf):
        check_ports(pf, answers, answers.get("NGINX_MODE", "standalone"))
        check_worker_port(pf, answers)
        check_containers(pf, answers)
        check_stack_containers(pf, answers, ctx)
        check_network(pf, ctx)
        check_nginx(pf, answers, ctx)
    check_observer(pf)
    check_answers(pf, answers, answers_path)
    check_resources(pf, answers.i("RECORDER_BOT_COUNT", 1))
    return pf
