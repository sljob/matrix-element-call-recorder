
import argparse
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


SOURCE_ROOT = Path(__file__).resolve().parent.parent

def source_text(path):
    return (SOURCE_ROOT / path).read_text(encoding="utf-8")

SOURCE = {name: source_text("installer/templates/" + name) for name in ['button.js', 'controller-running.py', 'recorder-server.js', 'recorder-observer.js', 'recorder-package.json', 'recorder-package-lock.json', 'rec-publish.py']}
os.umask(0o077)

# INSTALL_LANGUAGE_V1: deterministic source localization; never translate user data.
INSTALL_LANGUAGE = "ru"
EN_TRANSLATIONS = json.loads(source_text("locales/en.json"))

def localize(text):
    if INSTALL_LANGUAGE != "en" or not isinstance(text, str):
        return text
    for source, target in sorted(EN_TRANSLATIONS.items(), key=lambda pair: -len(pair[0])):
        text = text.replace(source, target)
    text = text.replace("lang=ru", "lang=en").replace('lang="ru"', 'lang="en"')
    text = text.replace("%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S")
    return text

# Translate our diagnostic output, not subprocess output or opaque protocol fields.
import builtins as _language_builtins

def print(*args, **kwargs):
    return _language_builtins.print(*(localize(arg) for arg in args), **kwargs)

def fail(message):
    raise SystemExit("ERROR: " + localize(message))

# COMPOSE_PULL_RETRY_V1
def run(args, **kwargs):
    args = list(args)
    is_pull = (
        args[:2] == ["docker", "compose"]
        and "pull" in args[2:]
    )
    if not is_pull:
        print("+", args[0], args[1] if len(args) > 1 else "", flush=True)
        return subprocess.run(args, check=True, **kwargs)

    # Не запрашивать заново уже имеющиеся образы.
    # Недостающие образы обязаны скачаться успешно.
    pos = args.index("pull", 2)
    if "--policy" not in args[pos+1:]:
        args[pos+1:pos+1] = ["--policy", "missing"]

    options = dict(kwargs)
    options["env"] = dict(options.get("env") or os.environ)
    options["env"]["COMPOSE_PARALLEL_LIMIT"] = "1"
    options.setdefault("timeout", 1800)

    for attempt in range(1, 5):
        print("+ docker compose pull: попытка %s/4" % attempt, flush=True)
        try:
            return subprocess.run(args, check=True, **options)
        except (subprocess.CalledProcessError,
                subprocess.TimeoutExpired):
            if attempt == 4:
                print("ERROR: загрузка образов не завершена; "
                      "установка остановлена.", flush=True)
                raise
            delay = attempt * 20
            print("Повтор через %s секунд; уже скачанное сохраняется."
                  % delay, flush=True)
            time.sleep(delay)


def output(args):
    return subprocess.check_output(args, text=True).strip()

def write(path, text, mode=0o600):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # SELF_CONTAINED_BUTTON_V04
    if str(path).endswith("/config/record-button.js"):
        text = source_text("config/record-button.js")
    elif str(path).endswith("/config/element-index.html"):
        import re as _button_re
        text, _n = _button_re.subn('record-button\\.js(?:\\?[^"\'\\s<>]*)?', ("record-button.js?v=" + hashlib.sha256(p.with_name("record-button.js").read_bytes()).hexdigest()), text)
        if _n != 1:
            raise RuntimeError("Expected one record-button.js reference")
    # INSTALLER_ENTRYPOINT_FIX_V06
    if str(path).endswith("/src/recorder/entrypoint.sh"):
        if '# RECORDER_STARTUP_FIX' not in text:
            anchor = "set -euo pipefail"
            text = text.replace(anchor, anchor + "\n"
                + "# RECORDER_STARTUP_FIX\n"
                + "rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 /tmp/pulse/native\n", 1)
    p.write_text(text, encoding="utf-8")
    p.chmod(mode)




def jwrite(path, data, mode=0o600):
    write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", mode)

def replace_once(text, old, new):
    count = text.count(old)
    if count != 1:
        fail("Несовпадение исходника при патче: " + old[:90]
             + " (совпадений " + str(count) + ")")
    return text.replace(old, new, 1)

parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=["--check", "--install"])
parser.add_argument("answers")
# Parse explicitly because mode begins with '--'.
if len(sys.argv) != 3 or sys.argv[1] not in ("--check", "--install"):
    fail("Использование: bash install.sh --check|--install /path/answers.conf")
mode, answers_path = sys.argv[1], Path(sys.argv[2]).resolve()

if os.geteuid() != 0:
    fail("Запустите от root.")

cfg = {}
for raw in answers_path.read_bytes().splitlines():
    raw = raw.strip()
    if not raw or raw.startswith(b"#"):
        continue
    try:
        line = raw.decode("utf-8")
    except UnicodeDecodeError:
        fail("Значения answers.conf должны быть UTF-8.")
    if not re.match(r"^[A-Z][A-Z0-9_]*=", line):
        fail("Недопустимый синтаксис answers.conf.")
    key, value = line.split("=", 1)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if key in cfg:
        fail("Повтор параметра: " + key)
    cfg[key] = value

# Supported deployment languages: ru/en. Preserve ru for existing answers files.
cfg.setdefault("INSTALL_LANGUAGE", "ru")
INSTALL_LANGUAGE = cfg["INSTALL_LANGUAGE"].strip().lower()
if INSTALL_LANGUAGE not in ("ru", "en"):
    fail("INSTALL_LANGUAGE must be ru or en")
cfg["INSTALL_LANGUAGE"] = INSTALL_LANGUAGE
print("INSTALL_LANGUAGE:", INSTALL_LANGUAGE, flush=True)

# Defaults come from the supplied working server manifest.
for key, value in {'ELEMENT_WEB_IMAGE': 'docker.io/vectorim/element-web@sha256:7050130b263bbcf0e4ad8da875a2821cd89dc55b2f04e93aaa13f682d013019e', 'ELEMENT_CALL_IMAGE': 'ghcr.io/element-hq/element-call@sha256:6c4563f42365bf7361f976e71304eb1e8574ef10a228b5524712b5c077f8b5ac', 'SYNAPSE_IMAGE': 'docker.io/matrixdotorg/synapse@sha256:78de1d10bef02e375f861d1cc99f8bedd9381d4f9083ea8b2c22a053477b205f', 'POSTGRES_IMAGE': 'postgres@sha256:6c538e7206ea40ff740ef27883529390a690b6ead6ba96b44c67a9f7c638e8fd', 'LIVEKIT_IMAGE': 'livekit/livekit-server@sha256:e37d68f172556d02aa77968b9fc55ef481468c0315fa38e4fa6c56ce72e3a815', 'TRAEFIK_IMAGE': 'traefik@sha256:1c32e7c368204fd72812152ebdd2ac0425993df6fd982317deb02e48f2d5423c', 'JWT_IMAGE': 'ghcr.io/element-hq/lk-jwt-service@sha256:822f0c03a3bdd924da92afc2e8ec59de5dda17af42d32e71e11f269c3517abf7', 'ADMIN_IMAGE': 'etkecc/synapse-admin@sha256:797327e3c77ab811066609d9e73ad4dbe8f6d17de8d6608d1bfdb80bf823dc69', 'NGINX_IMAGE': 'nginx@sha256:f91bdb7aee4cba26f89b1c5c3aa12742ec3c91c6d70fd7007c6dc797e9676c45'}.items(): cfg.setdefault(key, value)
cfg.setdefault("HOSTNAME", cfg.get("DOMAIN", ""))
cfg.setdefault("SYNAPSE_SERVER_NAME", cfg.get("DOMAIN", ""))
cfg.setdefault("ADMIN_USER", "admin")
cfg.setdefault("RECORDER_USER", "recorder")
cfg.setdefault("POSTGRES_USER", "synapse")
cfg.setdefault("POSTGRES_DB", "synapse")
bootstrap_path = answers_path.with_name(answers_path.name + ".generated.json")
if bootstrap_path.exists():
    bootstrap = json.loads(bootstrap_path.read_text())
else:
    bootstrap = {key: secrets.token_urlsafe(32) for key in ("ADMIN_PASSWORD", "RECORDER_PASSWORD", "POSTGRES_PASSWORD", "LIVEKIT_KEY", "LIVEKIT_SECRET")}
    fd = os.open(str(bootstrap_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f: json.dump(bootstrap, f, indent=2)
for key, value in bootstrap.items(): cfg.setdefault(key, value)

cfg.setdefault("RECORDING_WORKERS", "4")
worker_count = int(cfg["RECORDING_WORKERS"])
if worker_count not in (2, 4): fail("RECORDING_WORKERS must be 2 or 4")
required = [
    "DOMAIN", "HOSTNAME", "SERVER_IP", "SYNAPSE_SERVER_NAME",
    "ADMIN_USER", "ADMIN_PASSWORD", "RECORDER_USER", "RECORDER_PASSWORD",
    "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB",
    "LIVEKIT_KEY", "LIVEKIT_SECRET",
    "ELEMENT_WEB_IMAGE", "ELEMENT_CALL_IMAGE", "SYNAPSE_IMAGE",
    "POSTGRES_IMAGE", "LIVEKIT_IMAGE", "TRAEFIK_IMAGE",
    "JWT_IMAGE", "ADMIN_IMAGE", "NGINX_IMAGE",
    "TLS_CERT_FILE", "TLS_KEY_FILE", "CA_CERT_FILE",
]
for key in required:
    if not cfg.get(key):
        fail("Нет параметра: " + key)

domain = cfg["DOMAIN"]
if not re.fullmatch(r"[a-zA-Z0-9.-]+", domain):
    fail("DOMAIN должен быть DNS-именем без схемы и пути.")
if cfg["SYNAPSE_SERVER_NAME"] != domain:
    fail("В v0.1 SYNAPSE_SERVER_NAME должен совпадать с DOMAIN.")
for key in ("ADMIN_USER", "RECORDER_USER"):
    if not re.fullmatch(r"[a-z0-9._=-]+", cfg[key]):
        fail(key + ": задайте локальную часть Matrix ID, без @ и домена.")
if cfg["ADMIN_USER"] == cfg["RECORDER_USER"]:
    fail("Admin и recorder должны быть разными пользователями.")

import ipaddress
address = ipaddress.IPv4Address(cfg["SERVER_IP"])
if address.is_loopback or address.is_unspecified or address.is_multicast:
    fail("SERVER_IP must be the new server LAN address")
if not shutil.which("ip"):
    fail("Install iproute2 first")
interfaces = json.loads(output(["ip", "-j", "-4", "address", "show"]))
local_ips = {a.get("local") for iface in interfaces for a in iface.get("addr_info", [])}
if cfg["SERVER_IP"] not in local_ips:
    fail("SERVER_IP=" + cfg["SERVER_IP"] + " is not assigned to this server. Local IPs: " + ", ".join(sorted(local_ips)))
print("OK: media IP belongs to this server:", cfg["SERVER_IP"], flush=True)


ports = {
    "rec": int(cfg.get("RECORDER_API_PORT", "8788")),
    "ec": int(cfg.get("SOCAT_PORT", "8090")),
    "pub": int(cfg.get("RECORDINGS_WEB_PORT", "8899")),
    "syn": int(cfg.get("SYNAPSE_LOCAL_PORT", "18008")),
    "lk": int(cfg.get("LIVEKIT_LOCAL_PORT", "17880")),
}
fixed_tcp = [80, 443, 8448, 5350, 7881]
all_tcp = fixed_tcp + list(ports.values())
if len(set(all_tcp)) != len(all_tcp) or any(p < 1 or p > 65535 for p in all_tcp):
    fail("Повтор или недопустимое значение TCP-портов.")

root = Path(cfg.get("INSTALL_DIR", "/opt/element-stack")).resolve()
if root == Path("/") or not str(root).startswith("/opt/"):
    fail("INSTALL_DIR должен быть отдельным каталогом внутри /opt.")
marker = root / ".installer-config-sha256"
fingerprint = hashlib.sha256(
    json.dumps(cfg, sort_keys=True).encode()
).hexdigest()
existing_install = marker.is_file()
release_marker = root / ".installer-release"
if existing_install and (not release_marker.is_file() or release_marker.read_text().strip() != "v2"):
    fail("Fresh-install release v2 cannot overwrite a legacy/production installation")

if existing_install:
    if marker.read_text().strip() != fingerprint:
        fail("answers.conf отличается от первого запуска. "
             "v0.1 не выполняет миграцию параметров существующей установки.")
else:
    if root.exists():
        fail("INSTALL_DIR уже существует и не принадлежит этому установщику.")
    if Path("/matrix").exists():
        fail("Обнаружен /matrix. Этот установщик предназначен для чистого сервера.")

release = Path("/etc/os-release").read_text()
if not re.search(r'^ID="?ubuntu"?$', release, re.M):
    fail("v0.1 предназначена для Ubuntu.")

certs = {}
for key in ("TLS_CERT_FILE", "TLS_KEY_FILE", "CA_CERT_FILE"):
    p = Path(cfg[key])
    if not p.is_absolute():
        p = answers_path.parent / p
    p = p.resolve()
    if not p.is_file():
        fail("Нет локального файла " + key + ": " + str(p))
    certs[key] = p

if not shutil.which("openssl"):
    fail("Нужен openssl: apt-get update && apt-get install -y openssl")


# Normalize PEM, DER and concatenated mixed certificate input before copying.
import tempfile

def tls_cmd(args, data):
    r = subprocess.run(['openssl'] + args, input=data, capture_output=True, timeout=30)
    if r.returncode:
        fail('TLS: OpenSSL rejected input for ' + args[0])
    return r.stdout

def split_certs(raw):
    parts, pos = [], 0
    while pos < len(raw):
        if raw[pos:pos+1] in (b' ', b'\r', b'\n', b'\t'):
            pos += 1
            continue
        if raw[pos:pos+3] == b'\xef\xbb\xbf':
            pos += 3
            continue
        if raw.startswith(b'-----BEGIN CERTIFICATE-----', pos):
            end = raw.find(b'-----END CERTIFICATE-----', pos)
            if end < 0: fail('TLS: incomplete PEM certificate')
            end += len(b'-----END CERTIFICATE-----')
            fmt = 'PEM'
        else:
            if raw[pos:pos+1] != b'\x30' or pos + 2 > len(raw):
                fail('TLS: unknown bytes at offset ' + str(pos))
            size, header = raw[pos+1], 2
            if size & 128:
                n = size & 127
                if not 1 <= n <= 4 or pos + 2 + n > len(raw):
                    fail('TLS: invalid DER length')
                size = int.from_bytes(raw[pos+2:pos+2+n], 'big')
                header += n
            end = pos + header + size
            if end > len(raw): fail('TLS: truncated DER')
            fmt = 'DER'
        parts.append(tls_cmd(['x509','-inform',fmt,'-outform','PEM'],raw[pos:end]))
        pos = end
    if not parts: fail('TLS: no certificates')
    return parts

def normalize_tls(certs, domain):
    chain = split_certs(certs['TLS_CERT_FILE'].read_bytes())
    ca = split_certs(certs['CA_CERT_FILE'].read_bytes())
    raw = certs['TLS_KEY_FILE'].read_bytes()
    fmt = 'PEM' if b'-----BEGIN ' in raw else 'DER'
    key = tls_cmd(['pkey','-inform',fmt,'-outform','PEM','-passin','pass:'],raw)
    pub = tls_cmd(['pkey','-pubout'],key)
    matches = [i for i,c in enumerate(chain) if tls_cmd(['x509','-pubkey','-noout'],c)==pub]
    if len(matches)!=1: fail('TLS: unique leaf matching private key not found')
    leaf = chain.pop(matches[0])
    ordered = [leaf]
    while chain:
        issuer = tls_cmd(['x509','-noout','-issuer','-nameopt','RFC2253'],ordered[-1]).split(b'=',1)[1].strip()
        idx = [i for i,c in enumerate(chain) if tls_cmd(['x509','-noout','-subject','-nameopt','RFC2253'],c).split(b'=',1)[1].strip()==issuer]
        if len(idx)!=1: fail('TLS: ambiguous or unrelated certificates in chain')
        ordered.append(chain.pop(idx[0]))
    result = {'TLS_CERT_FILE':b''.join(ordered),'CA_CERT_FILE':b''.join(ca),'TLS_KEY_FILE':key}
    with tempfile.TemporaryDirectory(prefix='element-tls-check.') as d:
        d = Path(d)
        for k,v in result.items():
            (d/k).write_bytes(v); (d/k).chmod(0o600)
        (d/'leaf.pem').write_bytes(leaf)
        run(['openssl','x509','-in',str(d/'leaf.pem'),'-noout','-checkend','86400'])
        run(['openssl','verify','-CAfile',str(d/'CA_CERT_FILE'),'-untrusted',str(d/'TLS_CERT_FILE'),'-verify_hostname',domain,'-purpose','sslserver',str(d/'leaf.pem')])
    print('OK: TLS normalized; chain, domain, expiry and private key verified.', flush=True)
    return result

prepared_tls = normalize_tls(certs, domain)

if not existing_install:
    for port in all_tcp:
        with socket.socket() as s:
            try:
                s.bind(("0.0.0.0", port))
            except OSError:
                fail("TCP-порт занят: " + str(port))

print("CHECK OK: конфигурация, сертификаты и базовые условия проверены.")
print("Доступность образов и E2EE-запись эта проверка не подтверждает.")
if mode == "--check":
    raise SystemExit(0)

env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
run(["apt-get", "update"], env=env)
packages = ["ca-certificates", "curl", "openssl"]
if not shutil.which("docker"):
    packages += ["docker.io", "docker-compose-v2"]
run(["apt-get", "install", "-y", "--no-install-recommends"] + packages, env=env)
if subprocess.run(["docker", "compose", "version"],
                  stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL).returncode:
    fail("Нет Docker Compose v2. Установите совместимый Compose plugin.")
run(["systemctl", "enable", "--now", "docker"])

if not existing_install:
    containers = output(["docker", "ps", "-aq"])
    if containers:
        fail("На сервере уже есть контейнеры. v0.1 требует чистый Docker.")
    root.mkdir(mode=0o700)
    write(marker, fingerprint + "\n")
    write(release_marker, "v2\n")

for directory in ("certs", "config", "data/postgres", "data/synapse",
                  "data/controller", "data/profile", "out", "public",
                  "src/recorder", "src/controller", "well-known/.well-known/matrix"):
    (root / directory).mkdir(parents=True, exist_ok=True)

# FIX_PUBLIC_DIRECTORY_PERMISSIONS
for public_path in (
    root / "public",
    root / "well-known",
    root / "well-known/.well-known",
    root / "well-known/.well-known/matrix",
):
    public_path.mkdir(parents=True, exist_ok=True)
    public_path.chmod(0o755)

for key, name in (("TLS_CERT_FILE", "fullchain.pem"),
                  ("TLS_KEY_FILE", "privkey.pem"),
                  ("CA_CERT_FILE", "ca.crt")):
    (root / "certs" / name).write_bytes(prepared_tls[key])
    (root / "certs" / name).chmod(0o600)

secret_path = root / "config/generated-secrets.json"
if secret_path.exists():
    internal = json.loads(secret_path.read_text())
else:
    internal = {k: secrets.token_hex(32) for k in
                ("registration", "macaroon", "form", "recorder_api")}
    jwrite(secret_path, internal)

# ELEMENT_WEB_EXACT_DIGEST_MIRROR_V1
# Same manifest digest; only the registry/repository changes.
if cfg['ELEMENT_WEB_IMAGE'] == 'ghcr.io/element-hq/element-web@sha256:7050130b263bbcf0e4ad8da875a2821cd89dc55b2f04e93aaa13f682d013019e':
    cfg['ELEMENT_WEB_IMAGE'] = 'docker.io/vectorim/element-web@sha256:7050130b263bbcf0e4ad8da875a2821cd89dc55b2f04e93aaa13f682d013019e'

# SYNAPSE_EXACT_DIGEST_MIRROR_V1
if cfg['SYNAPSE_IMAGE'] == 'ghcr.io/element-hq/synapse@sha256:78de1d10bef02e375f861d1cc99f8bedd9381d4f9083ea8b2c22a053477b205f':
    cfg['SYNAPSE_IMAGE'] = 'docker.io/matrixdotorg/synapse@sha256:78de1d10bef02e375f861d1cc99f8bedd9381d4f9083ea8b2c22a053477b205f'

# ----- Adapt supplied controller, without dependence on old custom image -----
controller = SOURCE["controller-running.py"]
controller = replace_once(
    controller, 'STATE_FILE = "/data/state.json"',
    'STATE_FILE = "/data/state.json"\n'
    'RECORDER_API_TOKEN = os.environ["RECORDER_API_TOKEN"]\n'
    'RECORDER_USER_ID = os.environ["RECORDER_USER_ID"]'
)
controller = replace_once(
    controller,
    'with open(STATE_FILE, "w") as f: json.dump(s, f)',
    'with open(STATE_FILE + ".tmp", "w") as f: json.dump(s, f)\n'
    '    os.replace(STATE_FILE + ".tmp", STATE_FILE)'
)
controller = replace_once(
    controller,
    'def _reconcile(room, state):\n    return state',
    """def _reconcile(room, state):
    entry = state.get(room)
    if entry and entry.get("status") == "active":
        rr = requests.get(RECORDER_URL + "/health",
            headers={"Authorization": "Bearer " + RECORDER_API_TOKEN},
            timeout=5)
        rr.raise_for_status()
        active = {j.get("room") for j in rr.json().get("active", [])}
        if room not in active:
            entry["status"] = "finished"
            _save_state(state)
    return state"""
)
controller = replace_once(
    controller,
    'rr = requests.post(f"{RECORDER_URL}/start", json={"room": room}, timeout=20)',
    """encoded_room = requests.utils.quote(room, safe="")
        caller_headers = {"Authorization": request.headers["Authorization"]}
        member_url = (SYNAPSE_URL + "/_matrix/client/v3/rooms/" + encoded_room
                      + "/state/m.room.member/"
                      + requests.utils.quote(RECORDER_USER_ID, safe=""))
        member = requests.get(member_url, headers=caller_headers, timeout=10)
        membership = member.json().get("membership") if member.status_code == 200 else None
        if membership not in ("join", "invite"):
            invitation = requests.post(
                SYNAPSE_URL + "/_matrix/client/v3/rooms/" + encoded_room + "/invite",
                headers=caller_headers, json={"user_id": RECORDER_USER_ID}, timeout=10)
            if invitation.status_code != 200:
                return jsonify({"error": "cannot invite recorder",
                                "detail": invitation.text}), 403
        rr = requests.post(f"{RECORDER_URL}/start", json={"room": room},
            headers={"Authorization": "Bearer " + RECORDER_API_TOKEN}, timeout=20)"""
)
controller = replace_once(
    controller,
    'rr = requests.post(f"{RECORDER_URL}/stop", json={"room": room}, timeout=60)',
    'rr = requests.post(f"{RECORDER_URL}/stop", json={"room": room}, '
    'headers={"Authorization": "Bearer " + RECORDER_API_TOKEN}, timeout=60)'
)
controller = replace_once(
    controller,
    'app.run(host="0.0.0.0", port=8765)',
    'app.run(host="0.0.0.0", port=8765, threaded=False)'
)

# ── Patch: _write_meta for recordings-auth ──
controller = controller.replace('def make_service_token():',
    'def _write_meta(room, entry):\n'
    '    base = (entry.get("file") or "").replace(".mp4", "")\n'
    '    if not base: return\n'
    '    try:\n'
    '        meta_path = os.path.join(OUT_DIR, base + ".meta")\n'
    '        with open(meta_path, "w") as f:\n'
    '            json.dump({"room_id": room, "started_at": entry.get("started_at"),\n'
    '                   "ended_at": entry.get("ended_at"), "started_by": entry.get("started_by")}, f)\n'
    '        os.chmod(meta_path, 0o644)\n'
    '    except Exception: pass\n\n'
    'def make_service_token():', 1)
controller = controller.replace(
    '            entry["status"] = "finished"\n            _save_state(state)',
    '            entry["status"] = "finished"\n            _write_meta(room, entry)\n            _save_state(state)', 1)
controller = controller.replace(
    '    entry["ended_at"] = int(time.time())\n    state[room] = entry\n    _save_state(state)',
    '    entry["ended_at"] = int(time.time())\n    _write_meta(room, entry)\n    state[room] = entry\n    _save_state(state)', 1)

write(root / "src/controller/app.py", controller)
write(root / "src/controller/Dockerfile", """FROM python:3.12-slim-bookworm
WORKDIR /app
RUN pip install --no-cache-dir Flask==3.1.2 requests==2.32.5 PyJWT==2.10.1
COPY app.py /app/app.py
CMD ["python", "/app/app.py"]
""")

# ----- Recorder: fresh login/join without shell interpolation -----
observer = SOURCE["recorder-observer.js"]
start = observer.index("// LOGIN")
end = observer.index("// BROWSER", start)
observer = observer[:start] + """
// Fresh Matrix login; persistent browser crypto store is created by Element Call.
async function matrixRequest(path, method='GET', body, token) {
  const r = await fetch(SYN + path, {
    method,
    headers: {'Content-Type':'application/json',
      ...(token ? {Authorization:'Bearer ' + token} : {})},
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(20000)
  });
  const data = await r.json();
  if (!r.ok) throw new Error('Matrix HTTP ' + r.status + ': ' + JSON.stringify(data));
  return data;
}
const login = await matrixRequest('/_matrix/client/v3/login', 'POST', {
  type:'m.login.password',
  identifier:{type:'m.id.user', user:REC_USER},
  password:REC_PASS,
  device_id:'RECORDER001',
  initial_device_display_name:'Conference recorder'
});
const accessToken = login.access_token;
const userId = login.user_id;
const deviceId = login.device_id;
if (!ROOM_ID) throw new Error('ROOM_ID required');
await matrixRequest('/_matrix/client/v3/join/' + encodeURIComponent(ROOM_ID),
                    'POST', {}, accessToken);
console.log('[AUTH] Login and room join OK: ' + userId);
try {
  const events = await matrixRequest('/_matrix/client/v3/rooms/'
       + encodeURIComponent(ROOM_ID) + '/state', 'GET', undefined, accessToken);
  for (const ev of events) {
    if (ev.type === 'org.matrix.msc3401.call.member'
        && ev.sender === userId && Object.keys(ev.content || {}).length) {
      await matrixRequest('/_matrix/client/v3/rooms/'
        + encodeURIComponent(ROOM_ID) + '/state/org.matrix.msc3401.call.member/'
        + encodeURIComponent(ev.state_key), 'PUT', {}, accessToken);
    }
  }
} catch (e) { console.log('[CLEAN] ' + e.message); }

""" + observer[end:]
observer = observer.replace("'--ignore-certificate-errors',", "")
observer = observer.replace(
    "console.log('[WS_REWRITE] ' + newUrl.slice(0,120));",
    "console.log('[WS_REWRITE] internal LiveKit');"
)
# Do not continue launching ffmpeg after a stop signal during preparation.
observer = replace_once(observer, "if (gotRemote) {",
                        "if (gotRemote && !__sigReceived) {")

# APP_OWNS_BROWSER_SIGNALS_V1
if "// APP_OWNS_BROWSER_SIGNALS_V1" not in observer:
    if re.search(r"\bhandleSIG(?:INT|TERM)\s*:", observer):
        fail("Recorder signal options already exist; review required.")
    matches = list(re.finditer(r"puppeteer\.launch\s*\(\s*\{", observer))
    if len(matches) != 1:
        fail("Expected one puppeteer.launch call.")
    pos = matches[0].end()
    observer = (observer[:pos]
        + "\n  // APP_OWNS_BROWSER_SIGNALS_V1\n"
        + "  handleSIGINT: false,\n  handleSIGTERM: false,\n"
        + observer[pos:])
    observer = replace_once(observer, "await browser.close();",
        "console.log('[SHUTDOWN] closing browser');\n"
        "await browser.close();\n"
        "console.log('[SHUTDOWN] browser closed');")

# CLEAR_RECORDER_CALL_MEMBERSHIP_V1
observer = replace_once(observer, "console.log('[SHUTDOWN] browser closed');", "console.log('[SHUTDOWN] browser closed');\n\n// CLEAR_RECORDER_CALL_MEMBERSHIP_V1\ntry {\n  const statePath = '/_matrix/client/v3/rooms/'\n    + encodeURIComponent(ROOM_ID) + '/state';\n  const state = await matrixRequest(\n    statePath, 'GET', undefined, accessToken\n  );\n  if (!Array.isArray(state)) throw new Error('Invalid room state response');\n\n  let cleared = 0;\n  for (const ev of state) {\n    if (ev.type !== 'org.matrix.msc3401.call.member'\n        || ev.sender !== userId\n        || typeof ev.state_key !== 'string'\n        || !Object.keys(ev.content || {}).length) continue;\n\n    await matrixRequest(\n      statePath + '/' + encodeURIComponent(ev.type)\n      + '/' + encodeURIComponent(ev.state_key),\n      'PUT', {}, accessToken\n    );\n    cleared++;\n  }\n\n  const checked = await matrixRequest(\n    statePath, 'GET', undefined, accessToken\n  );\n  const remaining = checked.filter(ev =>\n    ev.type === 'org.matrix.msc3401.call.member'\n    && ev.sender === userId\n    && Object.keys(ev.content || {}).length\n  ).length;\n\n  console.log('[HANGUP] cleared=' + cleared + ' remaining=' + remaining);\n  if (remaining) throw new Error('Recorder call membership remains');\n\n  const otherTypes = [...new Set(checked.filter(ev =>\n    ev.sender === userId\n    && /(?:rtc|call).*member/i.test(ev.type)\n    && ev.type !== 'org.matrix.msc3401.call.member'\n    && Object.keys(ev.content || {}).length\n  ).map(ev => ev.type))];\n  if (otherTypes.length)\n    console.log('[HANGUP] other membership types: ' + otherTypes.join(','));\n} catch (e) {\n  console.error('[HANGUP] FAILED: ' + e.message);\n  process.exitCode = 1;\n}\n")
write(root / "src/recorder/p63-observer.js", observer)

server = SOURCE["recorder-server.js"]
a = server.index("async function matrixHangup(room)")
b = server.index("async function stopJob(job, room)", a)
server = server[:a] + """
async function matrixHangup(room) {
  // Browser close removes the participant; stale membership is cleaned next login.
  trace('browser closing for room=' + room);
}

""" + server[b:]
server = replace_once(
    server,
    "const room = data.room;",
    """const room = data.room;
if (url !== '/health' &&
    req.headers.authorization !== 'Bearer ' + process.env.RECORDER_API_TOKEN)
  return json(res, 401, {error:'unauthorized'});
if (room !== undefined &&
    (typeof room !== 'string' || !/^![^\\s/]+:[^\\s/]+$/.test(room)))
  return json(res, 400, {error:'invalid Matrix room ID'});"""
)
server = replace_once(
    server,
    "if (jobs.has(room)) return json(res, 409, {error:'already recording'});",
    "if (jobs.size) return json(res, 409, {error:'recorder is busy'});"
)
server = replace_once(
    server,
    "const file = `${OUT}/rec-${Date.now()}.mp4`;",
    "const file = `${OUT}/rec-${Date.now()}.mp4`; "
    "fs.writeFileSync(file + '.active', ''); "
    "fs.writeFileSync(file + '.meta', JSON.stringify({room_id: room, started_at: Date.now()}));"
)
server = replace_once(
    server,
    "jobs.delete(room); });",
    "jobs.delete(room); try { fs.unlinkSync(file + '.active'); } catch {} "
    "log.end(); });"
)
server = replace_once(
    server,
    "jobs.delete(room); const dur = await stopJob(job, room);",
    "const dur = await stopJob(job, room); jobs.delete(room);"
)
# ── Patch: fallback .meta write ──
if "file + '.meta'" not in server:
    server = server.replace(
        "fs.writeFileSync(file + '.active', '');",
        "fs.writeFileSync(file + '.active', ''); "
        "fs.writeFileSync(file + '.meta', JSON.stringify({room_id: room, started_at: Date.now()})); "
        "try { fs.chmodSync(file + '.meta', 0o644); } catch(e) {}", 1)

write(root / "src/recorder/server.js", server)

package = json.loads(SOURCE["recorder-package.json"])
package["type"] = "module"
jwrite(root / "src/recorder/package.json", package)
write(root / "src/recorder/package-lock.json",
      SOURCE["recorder-package-lock.json"])

publisher = SOURCE["rec-publish.py"]
publisher = publisher.replace(
    'OUT = "/matrix/dev/matrix-e2ee-recorder/out"', 'OUT = "/out"'
).replace(
    'PUB = "/matrix/dev/matrix-e2ee-recorder/public"', 'PUB = "/public"'
)
a = publisher.index("# файлы, в которые")
b = publisher.index("for name in sorted(os.listdir(OUT)):", a)
publisher = publisher[:a] + (
    "busy = {name[:-7] for name in os.listdir(OUT) if name.endswith('.active')}\n\n"
) + publisher[b:]
publisher = replace_once(
    publisher, '"-movflags","+faststart",web]',
    '"-movflags","+faststart",web + ".tmp.mp4"]'
)
publisher = replace_once(
    publisher, '    mp3 = os.path.join(PUB, base + "_audio.mp3")',
    '    os.replace(web + ".tmp.mp4", web)\n'
    '    _meta_src = os.path.join(OUT, base + ".mp4.meta")\n'
    '    if os.path.exists(_meta_src):\n'
    '        shutil.copy2(_meta_src, os.path.join(PUB, base + ".meta"))\n'
    '    mp3 = os.path.join(PUB, base + "_audio.mp3")'
)
publisher = replace_once(
    publisher,
    'open(os.path.join(PUB, "index.html"), "w", encoding="utf-8").write(html)',
    'open(os.path.join(PUB, "index.html.tmp"), "w", encoding="utf-8").write(html)\n'
    'os.replace(os.path.join(PUB, "index.html.tmp"), os.path.join(PUB, "index.html"))'
)
# ── Patch: import shutil for .meta copy ──
if 'shutil' not in publisher:
    publisher = publisher.replace(
        'import os, re, subprocess as sp, datetime, sys',
        'import os, re, subprocess as sp, datetime, sys, shutil', 1)

_publisher_meta_code = source_text('installer/templates/publisher-metadata.py')

# RECORDINGS_INSTALL_FIX_V3: publisher generation
def _pub_replace_once(text, old, new):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(
            "Publisher fix: expected one match, found "
            + str(count) + ": " + repr(old)
        )
    return text.replace(old, new, 1)

_old_meta_copy = (
    '    _meta_src = os.path.join(OUT, base + ".mp4.meta")\n'
    '    if os.path.exists(_meta_src):\n'
    '        shutil.copy2(_meta_src, os.path.join(PUB, base + ".meta"))\n'
)
publisher = _pub_replace_once(publisher, _old_meta_copy, "")
publisher = _pub_replace_once(
    publisher,
    "for name in sorted(os.listdir(OUT)):",
    _publisher_meta_code + "\nfor name in sorted(os.listdir(OUT)):"
)
publisher = _pub_replace_once(
    publisher,
    '    web = os.path.join(PUB, base + "_web.mp4")',
    '    _publish_metadata(base)\n'
    '    web = os.path.join(PUB, base + "_web.mp4")'
)
import ast as _publisher_ast
_publisher_ast.parse(publisher, filename="generated-rec-publish.py")

write(root / "src/recorder/rec-publish.py", publisher)

write(root / "src/recorder/entrypoint.sh", """#!/bin/bash
set -euo pipefail
mkdir -p /tmp/pulse /out /public /work/profile-spk
rm -f /out/*.active
export DISPLAY=:99
export PULSE_SERVER=unix:/tmp/pulse/native
Xvfb :99 -screen 0 1280x720x24 -nolisten tcp &
for i in $(seq 1 30); do
  xdpyinfo -display :99 >/dev/null 2>&1 && break
  sleep 1
done
xdpyinfo -display :99 >/dev/null
pulseaudio --exit-idle-time=-1 --disallow-exit -n \
  --load="module-native-protocol-unix socket=/tmp/pulse/native" \
  --load="module-null-sink sink_name=recsink" \
  --load=module-always-sink --daemonize=yes --log-target=stderr
pactl info >/dev/null
pactl list sinks short | grep -w recsink
exec node /app/server.js
""", 0o755)
shutil.copyfile(root / "certs/ca.crt", root / "src/recorder/ca.crt")
write(root / "src/recorder/Dockerfile", """FROM node:22-bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium xvfb x11-utils pulseaudio pulseaudio-utils ffmpeg \
    tini procps curl ca-certificates libnss3-tools python3 \
    fonts-liberation dbus-x11 \
    && rm -rf /var/lib/apt/lists/*
COPY ca.crt /usr/local/share/ca-certificates/site-ca.crt
RUN update-ca-certificates && mkdir -p /root/.pki/nssdb \
    && certutil -N -d sql:/root/.pki/nssdb --empty-password \
    && certutil -A -d sql:/root/.pki/nssdb -n site-ca -t "C,," \
       -i /usr/local/share/ca-certificates/site-ca.crt
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY server.js p63-observer.js rec-publish.py ./
COPY entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
""")

button = SOURCE["button.js"]
# API_BASE: use cfg["DOMAIN"], skip if not found (source may use relative paths)
_api_old = 'const API_BASE = "https://' + cfg["DOMAIN"] + '/recording";'
if _api_old in button:
    button = button.replace(_api_old, 'const API_BASE = "/recording";', 1)
button = replace_once(
    button, 'stopBtn.style.display = "inline-block";',
    'stopBtn.disabled = false; stopBtn.style.display = "inline-block";'
)
button = button.replace(
    'badge.textContent = "Готов";',
    'badge.textContent = "Готов"; '
    'const gallery = document.createElement("a"); '
    'gallery.href="/recordings/"; gallery.target="_blank"; '
    'gallery.rel="noopener"; gallery.textContent="Записи"; c.appendChild(gallery);'
)
write(root / "config/record-button.js", button, 0o644)

# ----- Configurations: JSON is also valid YAML -----
baseurl = "https://" + domain
jwrite(root / "config/element.json", {
    "brand": "Element",
    "default_server_config": {
        "m.homeserver": {"base_url": baseurl, "server_name": domain}
    },
    "disable_custom_urls": True, "disable_guests": True,
    "default_theme": "light", "show_labs_settings": True,
    "element_call": {"url": baseurl + "/call", "use_exclusively": True,
                     "participant_limit": 8, "brand": "Element Call"},
    "features": {"feature_element_call_video_rooms": True,
                 "feature_group_calls": True, "feature_video_rooms": True}
}, 0o644)
jwrite(root / "config/call.json", {
    "default_server_config": {
        "m.homeserver": {"base_url": baseurl, "server_name": domain}
    },
    "livekit": {"livekit_service_url": baseurl + "/livekit-jwt-service"}
}, 0o644)
jwrite(root / "well-known/.well-known/matrix/client", {
    "m.homeserver": {"base_url": baseurl},
    "org.matrix.msc4143.rtc_foci": [{
        "type": "livekit",
        "livekit_service_url": baseurl + "/livekit-jwt-service"
    }]
}, 0o644)
jwrite(root / "well-known/.well-known/matrix/server",
       {"m.server": domain + ":8448"}, 0o644)

jwrite(root / "config/livekit.yaml", {
    "port": 7880, "bind_addresses": ["0.0.0.0"],
    "keys": {cfg["LIVEKIT_KEY"]: cfg["LIVEKIT_SECRET"]},
    "room": {"auto_create": False},
    "rtc": {"tcp_port": 7881, "udp_port": 7882,
            "use_external_ip": False, "node_ip": cfg["SERVER_IP"]},
    "turn": {"enabled": True, "domain": domain, "external_tls": True,
             "tls_port": 5350, "udp_port": 3479,
             "relay_range_start": 30000, "relay_range_end": 30020},
    "logging": {"level": "info"}
}, 0o644)

write(root / "config/static-nginx.conf", """server {
 listen 80;
 root /usr/share/nginx/html;
 location / {
   default_type application/json;
   add_header Access-Control-Allow-Origin "*" always;
   try_files $uri =404;
 }
}
""", 0o644)
write(root / "config/recordings-nginx.conf", """server {
 listen 80;
 root /usr/share/nginx/html;
 index index.html;
 location / { try_files $uri $uri/ =404; }
}
""", 0o644)

static_traefik = {
    "entryPoints": {
        "web": {"address": ":80", "http": {"redirections": {
            "entryPoint": {"to": "secure", "scheme": "https"}}}},
        "secure": {"address": ":443", "transport": {
            "respondingTimeouts": {"readTimeout": "300s", "writeTimeout": "0s"}}},
        "federation": {"address": ":8448"},
        "turn": {"address": ":5350"}
    },
    "providers": {"file": {"filename": "/config/dynamic.yml", "watch": True}},
    "log": {"level": "INFO"},
    "accessLog": {},
    "api": {"dashboard": False}
}
jwrite(root / "config/traefik.json", static_traefik)

routers, services, middlewares = {}, {}, {}
def route(name, rule, host, port, priority, strip=None):
    services[name] = {"loadBalancer": {
        "servers": [{"url": "http://" + host + ":" + str(port)}]}}
    router = {
        "rule": "Host(`" + domain + "`) && (" + rule + ")",
        "entryPoints": ["secure"], "tls": {},
        "service": name, "priority": priority
    }
    if strip:
        middlewares[name + "-strip"] = {"stripPrefix": {"prefixes": [strip]}}
        router["middlewares"] = [name + "-strip"]
    routers[name] = router

route("element", "PathPrefix(`/`)", "element", 8080, 1)
route("synapse", "PathPrefix(`/_matrix`) || PathPrefix(`/_synapse`)",
      "synapse", 8008, 100)
route("call", "PathPrefix(`/call`)", "call", 8080, 50, "/call")
route("jwt", "PathPrefix(`/livekit-jwt-service`)", "jwt", 8080, 50,
      "/livekit-jwt-service")
route("livekit", "PathPrefix(`/livekit-server`)", "livekit", 7880, 50,
      "/livekit-server")
route("controller", "PathPrefix(`/recording/`) || Path(`/recording`)",
      "controller", 8765, 50, "/recording")
route("recordings", "PathPrefix(`/recordings`)", "recordings", 80, 100,
      "/recordings")
route("admin", "PathPrefix(`/synapse-admin`)", "admin", 8080, 100,
      "/synapse-admin")
route("wellknown", "PathPrefix(`/.well-known/matrix/`)",
      "wellknown", 80, 100)
middlewares["trailing"] = {"redirectRegex": {
    "regex": r"^(https?://[^/]+/(?:recordings|synapse-admin|call))$",
    "replacement": "${1}/", "permanent": True}}
for name in ("recordings", "admin", "call"):
    routers[name]["middlewares"].insert(0, "trailing")
routers["federation"] = {
    "rule": "Host(`" + domain + "`) && PathPrefix(`/_matrix`)",
    "entryPoints": ["federation"], "tls": {}, "service": "synapse"
}
jwrite(root / "config/dynamic.yml", {
    "http": {"routers": routers, "services": services, "middlewares": middlewares},
    "tcp": {
        "routers": {"turn": {
            "rule": "HostSNI(`" + domain + "`)", "entryPoints": ["turn"],
            "tls": {}, "service": "turn"}},
        "services": {"turn": {"loadBalancer": {
            "servers": [{"address": "livekit:5350"}]}}}
    },
    "tls": {
        "certificates": [{"certFile": "/certs/fullchain.pem",
                          "keyFile": "/certs/privkey.pem"}],
        "stores": {"default": {"defaultCertificate": {
            "certFile": "/certs/fullchain.pem", "keyFile": "/certs/privkey.pem"}}}
    }
})

# ----- Compose -----
def vol(src, dst, ro=False):
    return str(root / src) + ":" + dst + (":ro" if ro else "")

def service(image, **extra):
    return dict(image=image, restart="unless-stopped",
                logging={"driver": "json-file",
                         "options": {"max-size": "10m", "max-file": "3"}},
                **extra)

rec_image = "local/element-e2ee-recorder:v01"
ctrl_image = "local/element-recording-controller:v01"
s = {}

s["postgres"] = service(cfg["POSTGRES_IMAGE"],
    environment={"POSTGRES_USER": cfg["POSTGRES_USER"],
                 "POSTGRES_PASSWORD": cfg["POSTGRES_PASSWORD"],
                 "POSTGRES_DB": cfg["POSTGRES_DB"],
                 "POSTGRES_INITDB_ARGS": "--encoding=UTF8 --locale=C",
                 "PGDATA": "/var/lib/postgresql/data"},
    volumes=[vol("data/postgres", "/var/lib/postgresql")])

s["synapse"] = service(cfg["SYNAPSE_IMAGE"],
    environment={"SYNAPSE_SERVER_NAME": domain,
                 "SYNAPSE_REPORT_STATS": "no", "UID": "991", "GID": "991"},
    volumes=[vol("data/synapse", "/data")],
    ports=[f"127.0.0.1:{ports['syn']}:8008"])

s["element"] = service(cfg["ELEMENT_WEB_IMAGE"],
    environment={"ELEMENT_WEB_PORT": "8080"},
    volumes=[vol("config/element.json", "/app/config.json", True),
             vol("config/element-index.html", "/usr/share/nginx/html/index.html", True),
             vol("config/record-button.js", "/usr/share/nginx/html/record-button.js", True)])

s["call"] = service(cfg["ELEMENT_CALL_IMAGE"],
    volumes=[vol("config/call.json", "/app/config.json", True)],
    ports=[f"127.0.0.1:{ports['ec']}:8080"])

s["livekit"] = service(cfg["LIVEKIT_IMAGE"],
    command=["--config", "/config.yaml"],
    volumes=[vol("config/livekit.yaml", "/config.yaml", True)],
    ports=[f"127.0.0.1:{ports['lk']}:7880", "7881:7881",
           "7882:7882/udp", "3479:3479/udp", "30000-30020:30000-30020/udp"])

s["jwt"] = service(cfg["JWT_IMAGE"],
    environment={
        "LIVEKIT_URL": "wss://" + domain + "/livekit-server",
        "LIVEKIT_KEY": cfg["LIVEKIT_KEY"],
        "LIVEKIT_SECRET": cfg["LIVEKIT_SECRET"],
        "LIVEKIT_FULL_ACCESS_HOMESERVERS": domain,
        "LIVEKIT_CS_API_URL_OVERRIDES": domain + "=http://synapse:8008",
        "SSL_CERT_FILE": "/site-ca.crt"
    },
    volumes=[vol("certs/ca.crt", "/site-ca.crt", True)],
    extra_hosts=[domain + ":host-gateway"])

worker_secret_file = root / "config/worker-accounts.json"
if worker_secret_file.exists():
    worker_accounts = json.loads(worker_secret_file.read_text())
else:
    worker_accounts = [{"user": cfg["RECORDER_USER"], "password": cfg["RECORDER_PASSWORD"]}]
    for i in range(2, worker_count + 1):
        worker_accounts.append({"user": "recpool_" + secrets.token_hex(8), "password": secrets.token_urlsafe(32)})
    jwrite(worker_secret_file, worker_accounts)
if len(worker_accounts) != worker_count: fail("Stored worker count differs")
for i in range(1, worker_count + 1):
    (root / ("data/profile-worker-%d" % i)).mkdir(parents=True, exist_ok=True)
recenv = {
    "EC_URL": "http://127.0.0.1:" + str(ports["ec"]),
    "SYNAPSE_URL": "http://127.0.0.1:" + str(ports["syn"]),
    "MATRIX_HS": baseurl,
    "LK_INTERNAL": "127.0.0.1:" + str(ports["lk"]),
    "REC_USER": cfg["RECORDER_USER"], "REC_PASS": cfg["RECORDER_PASSWORD"],
    "RECORDER_API_TOKEN": internal["recorder_api"],
    "PORT": str(ports["rec"]), "BIND": "0.0.0.0",
    "W_GATE": "15000", "W_ROOM": "5000",
    "RECORD_SECS": "7200", "WAIT_VID": "120",
    "REQUIRE_REMOTE": "0", "NODE_EXTRA_CA_CERTS": "/usr/local/share/ca-certificates/site-ca.crt"
}
s["recorder"] = service("local/element-recorder-pool:v04",
    build={"context": str(root / "src/pool")},
    ports=["127.0.0.1:%s:8788" % ports["rec"]],
    environment={"RECORDER_API_TOKEN": internal["recorder_api"], "SYNAPSE_URL": "http://synapse:8008",
        "POOL_WORKERS": json.dumps([{"name": "rec-worker-%d" % i, "url": "http://rec-worker-%d:8788" % i,
        "user_id": "@" + worker_accounts[i-1]["user"] + ":" + domain} for i in range(1, worker_count+1)])})
for i, account in enumerate(worker_accounts, 1):
    worker_env = dict(recenv, EC_URL="http://127.0.0.1:8090", SYNAPSE_URL="http://synapse:8008",
        LK_INTERNAL="livekit:7880", PORT="8788", REC_USER=account["user"], REC_PASS=account["password"],
        REC_DEVICE_ID="RECPOOL%03d" % i, WORKER_INDEX=str(i))
    s["rec-worker-%d" % i] = service(rec_image,
        build={"context": str(root / "src/recorder")},
        entrypoint=["/usr/bin/tini", "--", "python3", "/app/pool-loopback-supervisor.py"],
        shm_size="1gb", environment=worker_env,
        extra_hosts=[domain + ":host-gateway"],
        volumes=[vol("out", "/out"), vol("public", "/public"), vol("data/profile-worker-%d" % i, "/work/profile-spk")],
        stop_grace_period="120s")

s["controller"] = service(ctrl_image,
    build={"context": str(root / "src/controller")},
    environment={
        "SYNAPSE_URL": "http://synapse:8008",
        "LIVEKIT_URL": "http://livekit:7880",
        "LIVEKIT_KEY": cfg["LIVEKIT_KEY"],
        "LIVEKIT_SECRET": cfg["LIVEKIT_SECRET"],
        "RECORDER_URL": "http://recorder:8788",
        "RECORDER_USER_ID": "@" + cfg["RECORDER_USER"] + ":" + domain,
        "RECORDER_API_TOKEN": internal["recorder_api"],
        "RECORDING_MIN_POWER_LEVEL": cfg.get("RECORDING_MIN_POWER_LEVEL", "50")
    },
    extra_hosts=["host.docker.internal:host-gateway"],
    volumes=[vol("data/controller", "/data"), vol("public", "/out")])

s["publisher"] = service(rec_image,
    entrypoint=["/bin/sh", "-c"],
    command=["while true; do python3 /app/rec-publish.py; sleep 15; done"],
    volumes=[vol("out", "/out", True), vol("public", "/public")])

# Recordings Flask app with Matrix auth (power level >= 50)
recordings_flask = source_text('src/recordings-auth/app.py')
write(root / "src/recordings-auth/app.py", recordings_flask)
write(root / "src/recordings-auth/Dockerfile", """FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir flask requests gunicorn
COPY app.py /app/app.py
WORKDIR /app
CMD gunicorn -w 1 --threads 4 -b 0.0.0.0:80 app:app
""", 0o644)

s["recordings"] = service("element-recordings",
    ports=[f"127.0.0.1:{ports['pub']}:80"],
    volumes=[vol("public", "/public", True)])
s["recordings"]["build"] = str(root / "src" / "recordings-auth")

s["wellknown"] = service(cfg["NGINX_IMAGE"],
    volumes=[vol("well-known", "/usr/share/nginx/html", True),
             vol("config/static-nginx.conf", "/etc/nginx/conf.d/default.conf", True)])
s["admin"] = service(cfg["ADMIN_IMAGE"])
s["traefik"] = service(cfg["TRAEFIK_IMAGE"],
    command=["--configFile=/config/traefik.json"],
    ports=["80:80", "443:443", "8448:8448", "5350:5350"],
    volumes=[vol("config", "/config", True), vol("certs", "/certs", True)])

# Escape dollar signs for Compose interpolation; original passwords remain literal.
def compose_escape(value):
    if isinstance(value, str):
        return value.replace("$", "$$")
    if isinstance(value, list):
        return [compose_escape(x) for x in value]
    if isinstance(value, dict):
        return {k: compose_escape(v) for k, v in value.items()}
    return value

compose_file = root / "compose.json"
jwrite(compose_file, compose_escape({"name": "element", "services": s}))
dc = ["docker", "compose", "-f", str(compose_file)]
run(dc + ["config", "--quiet"])

official = [name for name in s if name not in ("recorder", "controller", "publisher", "recordings") and not name.startswith("rec-worker-")]
run(dc + ["pull"] + official)

# Extract stock Element HTML from its image, then inject the supplied button.
temp_name = "element-installer-html-" + secrets.token_hex(4)
try:
    run(["docker", "create", "--name", temp_name, cfg["ELEMENT_WEB_IMAGE"]])
    candidates = ["/app/index.html", "/usr/share/nginx/html/index.html"]
    extracted = root / "config/element-index.html"
    found = False
    for path in candidates:
        r = subprocess.run(["docker", "cp", temp_name + ":" + path, str(extracted)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode == 0:
            found = True
            break
    if not found:
        fail("Не найден index.html внутри Element image.")
finally:
    subprocess.run(["docker", "rm", "-v", temp_name],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
html = extracted.read_text()
if "</body>" not in html:
    fail("Не найден </body> в Element index.html.")
write(extracted, html.replace("</body>",
      '<script src="/record-button.js"></script></body>', 1), 0o644)

# INSTALLER_FIXED_FILES_V09
_fixed_v09 = {name: source_text("installer/compat/" + name.removeprefix("/opt/element-stack/")) for name in ['/opt/element-stack/src/recorder/server.js', '/opt/element-stack/src/recorder/p63-observer.js', '/opt/element-stack/src/recorder/entrypoint.sh', '/opt/element-stack/src/controller/app.py']}
for _p, _b in _fixed_v09.items():
    _p = str(root / Path(_p).relative_to('/opt/element-stack'))
    try:
        _c = _b
        Path(_p).parent.mkdir(parents=True, exist_ok=True)
        Path(_p).write_text(_c)
        print("OK:", _p)
    except Exception as _e:
        raise RuntimeError("Cannot write final source: " + _p) from _e


# PARALLEL_RECORDING_RELEASE_V1
_final_pool_sources = {name: source_text(name) for name in ['src/recorder/server.js', 'src/recorder/p63-observer.js', 'src/controller/app.py', 'src/recorder/entrypoint.sh', 'src/recorder/pool-loopback-supervisor.py', 'src/pool/pool-router.py', 'src/pool/Dockerfile']}
for rel, text in _final_pool_sources.items():
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    p.chmod(0o755 if rel.endswith(".sh") else 0o600)
p = root / "src/recorder/Dockerfile"
p.write_text(p.read_text().replace("COPY entrypoint.sh /entrypoint.sh", "COPY pool-loopback-supervisor.py /app/pool-loopback-supervisor.py\nCOPY entrypoint.sh /entrypoint.sh"))

# Localize only generated application sources after all embedded fixes.
# Room names, usernames, API routes and status codes are never localized.
for _language_relative in (
    "config/record-button.js", "src/recordings-auth/app.py",
    "src/recorder/rec-publish.py", "src/controller/app.py",
    "src/recorder/server.js", "src/recorder/p63-observer.js",
    "src/pool/pool-router.py",
):
    _language_file = root / _language_relative
    _language_text = _language_file.read_text(encoding="utf-8")
    # Replace fallback test-server URL restored from the known-good embedded source.
    _language_text = _language_text.replace("https://meet.milorada.ru", baseurl)
    _language_file.write_text(localize(_language_text), encoding="utf-8")
write(root / "config/install-language.json", json.dumps({"language": INSTALL_LANGUAGE}) + "\n")

# RECORDINGS_INSTALL_FIX_V3: validate final sources, then build
import ast as _final_ast

for _relative in (
    "src/controller/app.py",
    "src/recordings-auth/app.py",
    "src/recorder/rec-publish.py",
):
    _file = root / _relative
    _final_ast.parse(
        _file.read_text(encoding="utf-8"), filename=str(_file)
    )

run(dc + ["build", "controller", "recorder", "recordings", "rec-worker-1"])

run(dc + [
    "run", "--rm", "--no-deps", "--entrypoint", "node",
    "rec-worker-1", "--check", "/app/server.js",
])
run(dc + [
    "run", "--rm", "--no-deps", "--entrypoint", "node",
    "rec-worker-1", "--check", "/app/p63-observer.js",
])
run(dc + [
    "run", "--rm", "--no-deps", "--entrypoint", "python3",
    "rec-worker-1", "-m", "py_compile", "/app/rec-publish.py",
])
run(dc + [
    "run", "--rm", "--no-deps", "--entrypoint", "python",
    "controller", "-m", "py_compile", "/app/app.py",
])
run(dc + [
    "run", "--rm", "--no-deps", "--entrypoint", "python",
    "recordings", "-m", "py_compile", "/app/app.py",
])



# BUTTON_RELEASE_CHECKS_V1
_button_path = root / "config/record-button.js"
_button_source = _button_path.read_text(encoding="utf-8")
run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "node",
     "--mount", "type=bind,src=" + str(_button_path) + ",dst=/button.js,readonly",
     rec_image, "--check", "/button.js"])
_api_start = "  async function api(action, id) {"
_api_end = "  async function sync() {"
if _button_source.count(_api_start) != 1 or _button_source.count(_api_end) != 1:
    fail("Button: cannot identify api() for testing")
_api_part = _api_start + _button_source.split(_api_start, 1)[1].split(_api_end, 1)[0]
_button_test = '\n(async () => {\n  const API = "/recording/api/record/";\n  const token = () => "test-token";\n  let nextResponse;\n  const fetch = async () => nextResponse;\n__API_FUNCTION__\n  let tests = 0;\n  for (const action of ["status", "start", "stop"]) {\n    for (const payload of ["<html>Forbidden</html>", \'{"error":"denied"}\']) {\n      nextResponse = {status:403, ok:false, text:async () => payload};\n      let message = "";\n      try { await api(action, "!test:example.invalid"); }\n      catch (e) { message = e.message; }\n      if (message !== "Недостаточно прав для управления записью")\n        throw new Error("Wrong 403 handling: " + action + ": " + message);\n      tests++;\n    }\n  }\n  for (const status of ["inactive", "active", "stopped"]) {\n    nextResponse = {status:200,ok:true,text:async () => JSON.stringify({status})};\n    if ((await api("status", "!test:example.invalid")).status !== status)\n      throw new Error("Success response changed");\n    tests++;\n  }\n  nextResponse = {status:500,ok:false,text:async () => "<html>Error</html>"};\n  let rejected = false;\n  try { await api("status", "!test:example.invalid"); }\n  catch (e) { rejected = e.message.includes("500"); }\n  if (!rejected) throw new Error("HTTP 500 hidden");\n  console.log("BUTTON API TESTS PASSED: " + (++tests));\n})().catch(e => {console.error(e);process.exitCode=1;});\n'.replace("__API_FUNCTION__", _api_part)
_button_test = localize(_button_test)
run(["docker", "run", "--rm", "-i", "--network", "none", "--entrypoint", "node",
     rec_image, "--input-type=commonjs"], input=_button_test, text=True)

# FIX_POSTGRES_OWNER_V1
pg_uid = int(output([
    "docker", "run", "--rm", "--user", "0",
    "--entrypoint", "sh", cfg["POSTGRES_IMAGE"],
    "-c", "id -u postgres"
]))
pg_gid = int(output([
    "docker", "run", "--rm", "--user", "0",
    "--entrypoint", "sh", cfg["POSTGRES_IMAGE"],
    "-c", "id -g postgres"
]))
pg_dir = root / "data/postgres"
os.chown(pg_dir, pg_uid, pg_gid)
os.chmod(pg_dir, 0o700)

run(dc + ["up", "-d", "postgres"])

def wait_for(label, check, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check():
                print("READY:", label, flush=True)
                return
        except Exception:
            pass
        time.sleep(2)
    fail("Таймаут готовности: " + label +
         ". Логи: docker compose -f " + str(compose_file) + " logs --tail=100")

wait_for("PostgreSQL", lambda: subprocess.run(
    dc + ["exec", "-T", "postgres", "pg_isready",
          "-U", cfg["POSTGRES_USER"], "-d", cfg["POSTGRES_DB"]],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0)

synpath = root / "data/synapse"
os.chown(synpath, 991, 991)
if not (synpath / (domain + ".signing.key")).exists():
    run(dc + ["run", "--rm", "--no-deps", "synapse", "generate"])

synconfig = {
    "server_name": domain, "public_baseurl": baseurl + "/",
    "report_stats": False, "enable_registration": False,
    "user_directory": {"enabled": True, "search_all_users": True, "prefer_local_users": True},
    "registration_shared_secret": internal["registration"],
    "macaroon_secret_key": internal["macaroon"], "form_secret": internal["form"],
    "signing_key_path": "/data/" + domain + ".signing.key",
    "log_config": "/data/log.config",
    "media_store_path": "/data/media_store",
    "database": {"name": "psycopg2", "args": {
        "user": cfg["POSTGRES_USER"], "password": cfg["POSTGRES_PASSWORD"],
        "database": cfg["POSTGRES_DB"], "host": "postgres", "port": 5432,
        "cp_min": 5, "cp_max": 10}},
    "listeners": [{"port": 8008, "bind_addresses": ["0.0.0.0"],
                   "type": "http", "tls": False, "x_forwarded": True,
                   "resources": [{"names": ["client", "federation", "openid"],
                                  "compress": False}]}],
    "max_upload_size": "50M", "max_event_delay_duration": "24h",
    "experimental_features": {"msc4143_enabled": True, "msc4222_enabled": True,
                              "msc4354_enabled": True},
    "matrix_rtc": {"transports": [{
        "type": "livekit", "livekit_service_url": baseurl + "/livekit-jwt-service"}]},
    "rc_message": {"per_second": 0.5, "burst_count": 30},
    "rc_delayed_event_mgmt": {"per_second": 1, "burst_count": 20},
}
jwrite(synpath / "homeserver.yaml", synconfig)
jwrite(synpath / "log.config", {
    "version": 1, "disable_existing_loggers": False,
    "formatters": {"basic": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler",
                             "formatter": "basic", "stream": "ext://sys.stdout"}},
    "root": {"level": "INFO", "handlers": ["console"]}
})
for p in (synpath / "homeserver.yaml", synpath / "log.config"):
    os.chown(p, 991, 991)

run(dc + ["up", "-d", "synapse"])

# Ignore shell proxy environment for local bootstrap API calls.
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
local = "http://127.0.0.1:" + str(ports["syn"])

def api(path, data=None):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(local + path, data=body,
                                 headers={"Content-Type": "application/json"})
    with opener.open(req, timeout=15) as response:
        return json.load(response)

wait_for("Synapse", lambda: bool(api("/_matrix/client/versions")))

for user, password, admin in [(cfg["ADMIN_USER"], cfg["ADMIN_PASSWORD"], True)] + [(a["user"], a["password"], False) for a in worker_accounts]:
    nonce = api("/_synapse/admin/v1/register")["nonce"]
    message = "\0".join([nonce, user, password, "admin" if admin else "notadmin"])
    mac = hmac.new(internal["registration"].encode(),
                   message.encode(), hashlib.sha1).hexdigest()
    try:
        api("/_synapse/admin/v1/register", {
            "nonce": nonce, "username": user, "password": password,
            "admin": admin, "mac": mac})
    except urllib.error.HTTPError as e:
        error = json.loads(e.read())
        if error.get("errcode") != "M_USER_IN_USE":
            fail("Создание пользователя " + user + ": " + str(error))
    # Confirm credentials; do not print access tokens.
    login = api("/_matrix/client/v3/login", {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": user},
        "password": password, "device_id": "INSTALL_CHECK"
    })
    token = login["access_token"]
    req = urllib.request.Request(local + "/_matrix/client/v3/logout", data=b"{}",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    with opener.open(req, timeout=15):
        pass
    print("USER OK:", user)

run(dc + ["up", "-d"])

# Docker restart policies supervise detached containers after boot.
docker_bin = shutil.which("docker")
unit = f"""[Unit]
Description=Element conference and recording stack
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory={root}
ExecStart={docker_bin} compose -f {compose_file} up -d
ExecStop={docker_bin} compose -f {compose_file} stop -t 60
TimeoutStartSec=600
TimeoutStopSec=300

[Install]
WantedBy=multi-user.target
"""
write("/etc/systemd/system/element-stack.service", unit, 0o644)
run(["systemctl", "daemon-reload"])
run(["systemctl", "enable", "--now", "element-stack.service"])

def https_ok(path):
    return subprocess.run([
        "curl", "--silent", "--show-error", "--fail", "--noproxy", "*",
        "--connect-timeout", "3", "--max-time", "10",
        "--cacert", str(root / "certs/ca.crt"),
        "--resolve", domain + ":443:127.0.0.1",
        "-o", "/dev/null", baseurl + path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

for path in ("/_matrix/client/versions", "/.well-known/matrix/client",
             "/", "/call/", "/synapse-admin/", "/recordings/", "/recording/health"):
    wait_for("HTTPS " + path, lambda p=path: https_ok(p), 180)

def rec_ready():
    req = urllib.request.Request("http://127.0.0.1:" + str(ports["rec"]) + "/health", headers={"Authorization": "Bearer " + internal["recorder_api"]})
    with opener.open(req, timeout=5) as response:
        return json.load(response).get("status") == "ok"
wait_for("Recorder API", rec_ready)
for i in range(1, worker_count + 1):
    run(dc + ["exec", "-T", "rec-worker-%d" % i, "pactl", "--server=unix:/tmp/pulse/native", "list", "sinks", "short"])
print("PARALLEL WORKERS READY:", worker_count)


# Verify actual HTTPS delivery, not merely HTTP 200.
def _release_https_bytes(path):
    return subprocess.check_output([
        "curl", "--silent", "--show-error", "--fail", "--noproxy", "*",
        "--connect-timeout", "5", "--max-time", "30",
        "--cacert", str(root / "certs/ca.crt"),
        "--resolve", domain + ":443:127.0.0.1",
        "-H", "Cache-Control: no-cache", baseurl + path,
    ])
_release_js = (root / "config/record-button.js").read_bytes()
_release_hash = hashlib.sha256(_release_js).hexdigest()
_release_url = "/record-button.js?v=" + _release_hash
_release_html = (root / "config/element-index.html").read_bytes()
if ('src="' + _release_url + '"').encode() not in _release_html:
    fail("Generated HTML does not reference the current button hash")
if _release_https_bytes("/?installer_verify=" + secrets.token_hex(8)) != _release_html:
    fail("HTTPS serves different Element HTML; installation NOT accepted")
if _release_https_bytes(_release_url) != _release_js:
    fail("HTTPS serves different recording JavaScript; installation NOT accepted")
print("VERIFIED: HTTPS HTML and recording JavaScript match generated files")
print("BUTTON SHA256: " + _release_hash)

print("\nINSTALLATION HTTP CHECKS PASSED")
print("Element:   " + baseurl + "/")
print("Admin:     " + baseurl + "/synapse-admin/")
print("Recordings:" + baseurl + "/recordings/")
print("Logs: docker compose -f " + str(compose_file) + " logs --tail=100")
print("E2EE RECORDING IS NOT YET VERIFIED: perform a real conference recording test.")


# 
