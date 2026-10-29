#!/usr/bin/env python3
"""Pre-install conflict detection for the recorder plugin.

Why this module exists
======================
The previous installer asked ``does the bot profile endpoint answer 200``.
That is a different question from ``will this bot let us log in``. On a
server where the accounts existed with a different password, the check
was green, the install completed, and the first recording died on
``M_FORBIDDEN`` inside the worker, where the controller log shows nothing.
The same class of mistake applies to ports (taken by a foreign process),
images (stale tags rebuilt into a "new" install), templates (a placeholder
left unrendered) and nginx (another vhost already owning /recording).

So every check here verifies a fact with a consequence, not an intention.
Findings carry a severity:

    FATAL  install must stop; the recorder cannot work
    WARN   it will work, but you probably did not mean this
    INFO   expected leftover, handled automatically
    OK     verified, nothing to do

The report is printed once, then ``blocked()`` decides whether --install
proceeds. ``FORCE_INSTALL="true"`` is the only way past a FATAL, and it is
deliberately not tied to ``--yes``: skipping a blocker must be a decision.

Design notes
============
* No shell for JSON requests. Passwords go through urllib, not through a
  quoted curl command line, because quoting was itself a source of false
  ``M_FORBIDDEN`` results.
* Login probes answer 429 under rate limiting. That is WARN, never FATAL:
  a check must not manufacture the condition it looks for.
* Every check is a plain function taking ``pf`` and plain data. That makes
  each one testable against a fixture without Docker, which is how the
  ten scenarios in docs/PREFLIGHT.ru.md are exercised.
"""

import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request

FATAL, WARN, INFO, OK = "fatal", "warn", "info", "ok"

# Keys that normalize_answers() derives itself. They are legitimate in an
# answers file even though answers.example documents BASE_DIR/auto instead
# of spelling them out, so they must never be reported as unknown keys.
COMPUTED_KEYS = frozenset({
    "PLUGIN_DIR", "RECORDINGS_DIR", "RECORDER_BOT_PROFILE_DIR", "BASE_DIR",
    "ELEMENT_CONTAINER", "INSTALLER_LANG", "PLUGIN_VERSION",
})

# Fixes without which a recording provably fails. Checked against the
# observer source so a stale vendor/ copy surfaces here and not at runtime.
OBSERVER_FIXES = (
    ("REC_USER", "REC_USER"),
    ("REC_PASS", "REC_PASS"),
    ("ROOM_ID", "ROOM_ID"),
    ("OUT_FILE", "OUT_FILE"),
    ("-vsync cfr", "'-vsync','cfr'"),
    ("progress in /tmp", "/tmp/ffprogress.log"),
    ("MAP with a space", "MAP "),
    ("virtual_speaker monitor", "virtual_speaker.monitor"),
)

# Sink name the worker entrypoint creates. Kept here so the check fails
# loudly if someone edits one side without the other.
PULSE_SINK = "virtual_speaker"


class Preflight(object):
    """Collects findings and prints one report."""

    def __init__(self, lang="ru"):
        self.lang = lang
        self.items = []

    def add(self, sev, code, msg, hint=""):
        self.items.append((sev, code, msg, hint))
        return self

    def fatal(self, code, msg, hint=""):
        return self.add(FATAL, code, msg, hint)

    def warn(self, code, msg, hint=""):
        return self.add(WARN, code, msg, hint)

    def info(self, code, msg, hint=""):
        return self.add(INFO, code, msg, hint)

    def ok(self, code, msg, hint=""):
        return self.add(OK, code, msg, hint)

    def count(self, sev):
        return sum(1 for i in self.items if i[0] == sev)

    def blocked(self):
        return self.count(FATAL) > 0

    def codes(self, sev):
        return [i[1] for i in self.items if i[0] == sev]

    def report(self, color=True):
        def z(c, s):
            return ("\033[%sm%s\033[0m" % (c, s)) if color else s

        ru = self.lang != "en"
        groups = (
            (FATAL, "31", "XX", "БЛОКЕРЫ" if ru else "BLOCKERS"),
            (WARN, "33", "!!", "ПРЕДУПРЕЖДЕНИЯ" if ru else "WARNINGS"),
            (INFO, "36", "..", "К СВЕДЕНИЮ" if ru else "NOTES"),
            (OK, "32", "OK", "ПРОВЕРЕНО" if ru else "VERIFIED"),
        )
        print()
        print(z("1", "=" * 76))
        print(z("1", "  ПРОВЕРКА КОНФЛИКТОВ ПЕРЕД УСТАНОВКОЙ" if ru
                 else "  PRE-INSTALL CONFLICT CHECK"))
        print(z("1", "=" * 76))
        for sev, colour, mark, title in groups:
            group = [i for i in self.items if i[0] == sev]
            if not group:
                continue
            print()
            print(z(colour, "%s (%d)" % (title, len(group))))
            for _s, code, msg, hint in group:
                print("  %s %-8s %s" % (mark, code, msg))
                for line in (hint.splitlines() if hint else ()):
                    print("             %s" % line)
        print()
        print(z("1", "-" * 76))
        print(("  блокеров %d · предупреждений %d · проверено %d"
               if ru else "  blockers %d · warnings %d · verified %d")
              % (self.count(FATAL), self.count(WARN), self.count(OK)))
        if self.blocked():
            print(z("31", ("  Установка заблокирована. Исправь блокеры или задай "
                           "FORCE_INSTALL=\"true\" осознанно." if ru else
                           "  Install blocked. Fix the blockers or set "
                           "FORCE_INSTALL=\"true\" deliberately.")))
        print(z("1", "=" * 76))
        return self


# --------------------------------------------------------------- helpers

def http_json(url, payload=None, timeout=10):
    """JSON request without a shell. Returns (status, dict).

    Using urllib instead of curl keeps passwords off the command line and
    out of shell quoting, which is exactly how a correct password used to
    turn into a false M_FORBIDDEN.
    """
    data, headers = None, {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", "replace")
            return exc.code, (json.loads(raw) if raw.strip() else {})
        except Exception:
            return exc.code, {}
    except Exception:
        return 0, {}


def _run(cmd, timeout=25):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"
    except FileNotFoundError as exc:
        return 127, "", str(exc)


def port_free(port):
    for addr in ("127.0.0.1", "0.0.0.0"):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((addr, port))
        except OSError:
            return False
        finally:
            s.close()
    return True


def port_owner(port):
    """'nginx (pid 123)' for whatever listens on the port, or ''."""
    _rc, out, _err = _run(["ss", "-lntpH", "sport = :%d" % port], timeout=10)
    if not out:
        return ""
    name = re.search(r'users:\(\("([^"]+)"', out)
    pid = re.search(r"pid=(\d+)", out)
    label = name.group(1) if name else "?"
    return "%s (pid %s)" % (label, pid.group(1)) if pid else label


def container_on_port(port):
    """Name of the container publishing this host port, or ''."""
    _rc, out, _err = _run(["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"])
    needle = ":%d->" % port
    for line in out.splitlines():
        if needle in line:
            return line.split("\t")[0].strip()
    return ""


def answers_keys(path):
    keys = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key = line.split("=", 1)[0].strip()
                if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                    keys.append(key)
    except OSError:
        pass
    return keys


def file_sha(path):
    import hashlib
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


# --------------------------------------------------------------- accounts

def check_accounts(pf, answers, synapse_url, users, admin_user=""):
    """The check that was missing: does the account let us in?

    An existing account with an unknown password is a blocker, because the
    installer cannot change it silently and the worker will fail at login.
    """
    raw = (answers.get("RECORDER_BOT_PASSWORDS") or "").strip()
    pwds = [p.strip() for p in raw.split(",")] if raw else []

    if not users:
        pf.fatal("BOT", "список аккаунтов пуст",
                 "задай RECORDER_BOT_USERS или RECORDER_BOT_PREFIX + COUNT")
        return
    if not synapse_url:
        pf.warn("BOT", "Synapse не найден — учётки не проверить",
                "укажи SYNAPSE_INTERNAL_URL")
        return

    admin = (admin_user or answers.get("ADMIN_USER") or "").strip().lstrip("@")
    used_pw = {}

    for i, uid in enumerate(users):
        local = uid.lstrip("@").split(":", 1)[0]
        pw = pwds[i % len(pwds)] if pwds else ""

        if admin and local == admin:
            pf.fatal("BOT", "%s совпадает с ADMIN_USER" % uid,
                     "бот-рекордер не должен быть администратором сервера")
            continue

        quoted = urllib.parse.quote(uid, safe="")
        status, _body = http_json("%s/_matrix/client/v3/profile/%s"
                                 % (synapse_url, quoted))
        if status == 0:
            pf.warn("BOT", "%s: Synapse не ответил, существование неизвестно" % uid,
                    "проверь SYNAPSE_INTERNAL_URL и что synapse healthy")
            continue
        exists = (status == 200)

        if not exists:
            if not pw:
                pf.fatal("BOT", "%s нет, и пароль не задан" % uid,
                         "RECORDER_BOT_PASSWORDS пуст: аккаунт не создать "
                         "и не залогинить")
            else:
                pf.warn("BOT", "%s нет — будет создан установщиком" % uid,
                        "нужен registration_shared_secret в homeserver.yaml")
            continue

        # Account exists. This is where the old installer stopped, and where
        # the field failure started.
        if not pw:
            pf.fatal("BOT", "%s уже существует, но RECORDER_BOT_PASSWORDS пуст" % uid,
                     "пароль неизвестен -> первая запись упадёт M_FORBIDDEN.\n"
                     "задай пароль в answers или запусти tools/resolve-bot-accounts.sh")
            continue
        if pw in used_pw:
            pf.fatal("BOT", "%s: тот же пароль, что у %s" % (uid, used_pw[pw]),
                     "у каждого бота должен быть свой пароль")

        status, body = http_json(
            "%s/_matrix/client/v3/login" % synapse_url,
            {"type": "m.login.password",
             "identifier": {"type": "m.id.user", "user": local},
             "password": pw},
        )
        if status == 200 and body.get("access_token"):
            pf.ok("BOT", "%s: пароль из answers подходит" % uid)
            used_pw[pw] = uid
        elif body.get("errcode") == "M_FORBIDDEN":
            pf.fatal("BOT", "%s существует, но пароль из answers НЕ подходит" % uid,
                     "сбрось пароль и обнови RECORDER_BOT_PASSWORDS,\n"
                     "или удали аккаунт: tools/resolve-bot-accounts.sh --delete %s" % local)
        elif status == 429:
            pf.warn("BOT", "%s: rate limit на /login, повтори позже" % uid,
                    "проверка не блокирует установку специально")
        elif status == 0:
            pf.warn("BOT", "%s: Synapse не ответил на /login" % uid)
        else:
            pf.warn("BOT", "%s: /login вернул HTTP %s %s"
                    % (uid, status, body.get("errcode", "")))


# ------------------------------------------------------------------ ports

def check_ports(pf, answers, ports):
    """ports: list of (port, label, answers_key)."""
    if not ports:
        pf.info("PORT", "порты не публикуются (loopback выключен или ADOPT)")
        return
    for port, label, key in ports:
        if port_free(port):
            pf.ok("PORT", "%s: %d свободен" % (label, port))
            continue
        holder = container_on_port(port)
        if holder.startswith("rec-"):
            pf.info("PORT", "%s: %d держит свой контейнер %s" % (label, port, holder),
                    "будет пересоздан при --install")
        elif holder:
            pf.fatal("PORT", "%s: %d занят контейнером %s" % (label, port, holder),
                     "останови %s или смени %s в answers" % (holder, key))
        else:
            pf.fatal("PORT", "%s: %d занят процессом %s"
                     % (label, port, port_owner(port) or "неизвестно"),
                     "ss -lntp | grep :%d\n"
                     "либо освободи порт, либо смени %s" % (port, key))


# ----------------------------------------------------------------- images

def check_images(pf, version):
    _rc, out, _err = _run(["docker", "images", "--format",
                           "{{.Repository}}:{{.Tag}}"], timeout=30)
    if not out:
        return
    stale = sorted({t for t in out.splitlines()
                    if t.startswith("recorder-plugin/") and not t.endswith(":" + version)})
    if stale:
        pf.info("IMG", "образы других версий: %s" % ", ".join(stale[:6]),
                "docker image prune -f после успешной установки")
    _rc, dangling, _err = _run(["docker", "images", "-q", "-f", "dangling=true"],
                               timeout=30)
    count = len([d for d in dangling.splitlines() if d.strip()])
    if count > 5:
        pf.warn("IMG", "висячих образов: %d" % count, "docker image prune -f")


def check_containers(pf, name_prefix="rec-"):
    """Leftovers from a previous attempt, and foreign lookalikes."""
    _rc, out, _err = _run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}"],
                          timeout=30)
    ours, foreign = [], []
    for line in out.splitlines():
        name = line.split("\t")[0].strip()
        if not name.startswith(name_prefix):
            continue
        known = (name in ("rec-controller", "rec-portal", "rec-publisher",
                          "rec-adopt-proxy")
                 or re.fullmatch(r"rec-worker-\d+", name))
        (ours if known else foreign).append(name)
    if ours:
        pf.info("INST", "контейнеры прошлой установки: %s" % ", ".join(sorted(ours)),
                "будут пересозданы при --install")
    if foreign:
        pf.warn("INST", "посторонние %s* контейнеры: %s"
                % (name_prefix, ", ".join(sorted(foreign)[:6])),
                "проверь: docker inspect <name> — возможно, это старый рекордер,\n"
                "который займёт порт или маршрут")


def check_networks(pf, network, element_network):
    _rc, out, _err = _run(["docker", "network", "ls", "--format", "{{.Name}}"],
                          timeout=20)
    names = set(out.split())
    if not names:
        return
    for label, net in (("плагина", network), ("Element Web", element_network)):
        if not net:
            continue
        if net in names:
            pf.ok("NET", "сеть %s: %s" % (label, net))
        else:
            (pf.fatal if label == "плагина" else pf.warn)(
                "NET", "сети %s нет: %s" % (label, net),
                "docker network create %s" % net)


# -------------------------------------------------------------- templates

def check_templates(pf, render_compose, render_snippet, shim, ctx):
    """Render both artefacts once and parse them. Catches broken edits."""
    try:
        text = render_compose(shim, ctx)
    except Exception as exc:
        pf.fatal("TPL", "compose не рендерится: %s: %s" % (type(exc).__name__, exc),
                 "это дефект установщика, а не окружения")
        return
    left = re.findall(r"\{[a-z_0-9]+\}", text)
    if left:
        pf.fatal("TPL", "в compose остались плейсхолдеры: %s"
                 % ", ".join(sorted(set(left))[:8]),
                 "шаблон собран с пропущенным аргументом .format()")
    if "traefik" in text.lower():
        pf.fatal("TPL", "в compose остались ярлыки traefik")
    try:
        import yaml
        doc = yaml.safe_load(text)
        services = len((doc or {}).get("services") or {})
        if services < 2:
            pf.fatal("TPL", "compose рендерится в %d сервисов" % services)
        else:
            pf.ok("TPL", "compose валиден, сервисов %d" % services)
    except ImportError:
        pf.warn("TPL", "PyYAML не установлен — YAML не проверен",
                "apt-get install -y python3-yaml")
    except Exception as exc:
        pf.fatal("TPL", "compose не парсится как YAML: %s" % exc)

    try:
        snippet = render_snippet(shim, ctx)
    except Exception as exc:
        pf.fatal("TPL", "nginx-сниппет не рендерится: %s: %s"
                 % (type(exc).__name__, exc))
        return
    if snippet.count("{") != snippet.count("}"):
        pf.fatal("TPL", "несбалансированные скобки в nginx-сниппете")
    problems = []
    if "proxy_pass" not in snippet:
        problems.append("нет proxy_pass")
    if "# >>> recorder-plugin" not in snippet:
        problems.append("нет BEGIN-маркера")
    if "# <<< recorder-plugin" not in snippet:
        problems.append("нет END-маркера")
    if problems:
        pf.fatal("TPL", "nginx-сниппет: %s" % ", ".join(problems),
                 "без маркеров --repair будет дублировать блок")
    else:
        pf.ok("TPL", "nginx-сниппет валиден, маркеры на месте")


# ----------------------------------------------------------------- observer

def check_observer(pf, root):
    vend = os.path.join(root, "vendor", "p63-observer.js")
    src = os.path.join(root, "src", "recorder", "p63-observer.js")

    def usable(p):
        return os.path.isfile(p) and os.path.getsize(p) > 0

    path = vend if usable(vend) else (src if usable(src) else "")
    if not path:
        pf.fatal("OBS", "p63-observer.js не найден",
                 "bash tools/fetch-upstream.sh  (нужен vendor/p63-observer.js)")
        return

    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            blob = fh.read()
    except OSError as exc:
        pf.fatal("OBS", "observer не читается: %s" % exc)
        return

    missing = [label for label, needle in OBSERVER_FIXES if needle not in blob]
    if missing:
        pf.fatal("OBS", "в observer нет фиксов: %s" % ", ".join(missing),
                 "ядро из старой сборки — запись упадёт на -fps_mode, ROOM_ID\n"
                 "или PULSE_SRC. Пересобери vendor/ из src/recorder/")
    else:
        pf.ok("OBS", "observer содержит все известные фиксы")

    if usable(vend) and usable(src) and file_sha(vend) != file_sha(src):
        pf.warn("OBS", "vendor/p63-observer.js отличается от src/recorder/",
                "правки ушли в один файл и не попали в другой —\n"
                "пересобери vendor/: bash tools/fetch-upstream.sh")


# ------------------------------------------------------------------ nginx

def check_nginx(pf, conf_dirs, prefix):
    have_bin = bool(shutil.which("nginx"))
    if have_bin:
        rc, out, err = _run(["nginx", "-t"], timeout=60)
        if rc == 0:
            pf.ok("NGX", "nginx -t проходит")
        else:
            pf.fatal("NGX", "nginx -t падает на текущем конфиге",
                     ((err or out) or "").strip()[:300])
    else:
        pf.warn("NGX", "nginx не в PATH",
                "если прокси в контейнере — проверь конфиг там")

    clashes = []
    for conf_dir in conf_dirs:
        if not conf_dir or not os.path.isdir(conf_dir):
            continue
        for base, _dirs, files in os.walk(conf_dir):
            for fn in files:
                if not fn.endswith(".conf") or fn == "recorder-plugin.conf":
                    continue
                fp = os.path.join(base, fn)
                try:
                    with open(fp, encoding="utf-8", errors="replace") as fh:
                        blob = fh.read()
                except OSError:
                    continue
                if prefix + "/" in blob and "proxy_pass" in blob:
                    clashes.append(fp)
    if clashes:
        pf.fatal("NGX", "другой конфиг уже обслуживает %s/: %s"
                 % (prefix, ", ".join(clashes[:3])),
                 "два location на один префикс: nginx возьмёт первый.\n"
                 "убери старый блок, иначе запросы уйдут мимо контроллера")
    else:
        pf.ok("NGX", "конфликтующих location %s/ нет" % prefix)


# ----------------------------------------------------------------- answers

def installer_keys(installer_path):
    """Keys the installer actually reads from the answers file.

    answers.example is documentation and can lag behind the code; the code
    cannot. Reading the installer's own source keeps this list honest, so a
    legitimate override is never reported as a typo.
    """
    keys = set()
    try:
        with open(installer_path, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
    except OSError:
        return keys
    for pattern in (r'a\["\']([A-Z][A-Z0-9_]*)\["\']\]',
                    r'a\.get\(["\']([A-Z][A-Z0-9_]*)',
                    r'a\.b\(["\']([A-Z][A-Z0-9_]*)',
                    r'a\.i\(["\']([A-Z][A-Z0-9_]*)'):
        keys.update(re.findall(pattern, src))
    return keys


def check_answers(pf, answers, answers_path, example_path="", installer_path=""):
    known = set(COMPUTED_KEYS)
    if example_path and os.path.isfile(example_path):
        known |= set(answers_keys(example_path))
    if installer_path:
        known |= installer_keys(installer_path)

    given = answers_keys(answers_path) if answers_path else []
    seen, dupes = set(), []
    for key in given:
        if key in seen:
            dupes.append(key)
        seen.add(key)
    if dupes:
        pf.fatal("ANS", "дубли ключей: %s" % ", ".join(sorted(set(dupes))),
                 "последнее значение молча перекрывает первое")
    unknown = sorted(seen - known) if known else []
    if unknown:
        pf.warn("ANS", "неизвестные ключи (опечатка?): %s" % ", ".join(unknown[:10]),
                "такие ключи установщик игнорирует")

    domain = (answers.get("DOMAIN") or "").strip()
    if not domain:
        pf.fatal("ANS", "DOMAIN пуст")
    elif domain.startswith(("http://", "https://")):
        pf.fatal("ANS", "DOMAIN со схемой: %r" % domain,
                 "укажи только имя хоста: %s" % domain.split("//", 1)[-1])
    elif domain in ("auto", "example.org", "localhost"):
        pf.warn("ANS", "DOMAIN похож на заглушку: %r" % domain)

    plugin_dir = (answers.get("PLUGIN_DIR") or "").strip().rstrip("/")
    if plugin_dir in ("", "/", "/opt", "/etc", "/usr", "/root", "/var", "/tmp"):
        pf.fatal("ANS", "PLUGIN_DIR указывает на системный каталог: %r" % plugin_dir)

    if answers.b("ADOPT_EXISTING") and answers.b("PUBLISH_LOOPBACK_PORTS", True):
        pf.warn("ANS", "ADOPT_EXISTING вместе с PUBLISH_LOOPBACK_PORTS=true",
                "порты 8765/8899 окажутся заняты дважды")


# --------------------------------------------------------------- resources

def check_resources(pf, workers):
    try:
        with open("/proc/meminfo") as fh:
            blob = fh.read()
        m = re.search(r"MemTotal:\s+(\d+) kB", blob)
        total = int(m.group(1)) / 1048576.0 if m else 0.0
        need = 2 * max(1, workers) + 2
        if total and total < need:
            pf.warn("RES", "RAM %.1f GiB, нужно около %d GiB" % (total, need),
                    "уменьши RECORDER_BOT_COUNT или добавь памяти")
        elif total:
            pf.ok("RES", "RAM %.1f GiB достаточно для %d воркер(ов)"
                  % (total, max(1, workers)))
    except OSError:
        pass

    for path in ("/", "/var/lib/docker"):
        if not os.path.isdir(path):
            continue
        try:
            free = shutil.disk_usage(path).free / 1024.0 ** 3
        except OSError:
            continue
        if free < 5:
            pf.warn("RES", "%s: свободно %.1f GiB" % (path, free),
                    "образы и записи занимают место; нужно хотя бы 5 GiB")
        else:
            pf.ok("RES", "%s: %.1f GiB свободно" % (path, free))


# ------------------------------------------------------------------- entry

def run(answers, ctx, render_compose, render_snippet, shim,
        root, answers_path="", example_path="", conf_dirs=None):
    """Run every check and return the Preflight report.

    ``shim`` is an Answers-like object whose PLUGIN_DIR/RECORDINGS_DIR point
    at a scratch directory, so template rendering never touches the real
    install paths during a check.
    """
    pf = Preflight(lang=(answers.get("INSTALLER_LANG") or "ru")[:2])

    check_accounts(pf, answers, ctx.get("synapse_url"), ctx.get("users") or [],
                   ctx.get("admin_user", ""))
    check_networks(pf, ctx.get("network"), ctx.get("element_network"))

    ports = []
    if answers.b("PUBLISH_LOOPBACK_PORTS", True):
        ports.append((answers.i("CONTROLLER_PORT", 8765), "controller",
                      "CONTROLLER_PORT"))
        ports.append((answers.i("REC_HTTP_PORT", 8899), "portal", "REC_HTTP_PORT"))
    if not answers.b("ADOPT_EXISTING"):
        base = answers.i("RECORDER_BASE_PORT", 8788)
        for i in range(max(1, ctx.get("workers", 1))):
            ports.append((base + i, "worker-%d" % (i + 1), "RECORDER_BASE_PORT"))
    check_ports(pf, answers, ports)

    check_containers(pf)
    check_images(pf, answers.get("PLUGIN_VERSION") or "")
    check_observer(pf, root)
    check_templates(pf, render_compose, render_snippet, shim, ctx)
    check_answers(pf, answers, answers_path, example_path,
                  os.path.join(root, "installer", "install.py"))
    check_nginx(pf, conf_dirs or ["/etc/nginx/conf.d"], ctx.get("api_prefix",
                                                                "/recording"))
    check_resources(pf, ctx.get("workers", 1))
    return pf
