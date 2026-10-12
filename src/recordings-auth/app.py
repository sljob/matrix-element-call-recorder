#!/usr/bin/env python3
"""Recordings web server with Matrix authentication.

Access control:
- Server admin: sees all recordings
- Room admin (power level >= 50): sees recordings of rooms where they are admin
- Others: see nothing
"""
import os, json, time, secrets, datetime, re, subprocess
from urllib.parse import quote
from flask import Flask, request, redirect, send_from_directory, abort, session
import requests as http

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
SYNAPSE_URL = os.environ.get("SYNAPSE_URL", "http://synapse:8008")
PUB_DIR = os.environ.get("PUB_DIR", "/public")
_power_cache = {}
_room_name_cache = {}
_probe_cache = {}
CACHE_TTL = 60

def get_power_level(tok, uid, room_id):
    """Get user's power level in a room. Returns int (0 if unknown)."""
    if not room_id:
        return 0
    now = time.time()
    key = (uid, room_id)
    c = _power_cache.get(key)
    if c and now - c[0] < CACHE_TTL:
        return c[1]
    level = 0
    try:
        r = http.get(
            f"{SYNAPSE_URL}/_matrix/client/v3/rooms/{quote(room_id, safe='')}/state/m.room.power_levels/",
            headers={"Authorization": f"Bearer {tok}"}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            users = data.get("users", {})
            level = users.get(uid, data.get("users_default", 0))
            if not isinstance(level, int):
                try:
                    level = int(level)
                except Exception:
                    level = 0
    except Exception:
        pass
    _power_cache[key] = (now, level)
    return level

def check_admin(tok, uid):
    try:
        r = http.get(f"{SYNAPSE_URL}/_synapse/admin/v2/users/{uid}",
                      headers={"Authorization": f"Bearer {tok}"}, timeout=10)
        if r.status_code == 200:
            return r.json().get("admin", False)
    except Exception:
        pass
    return False

def get_room_name(tok, room_id):
    if not room_id:
        return "Без комнаты"
    now = time.time()
    c = _room_name_cache.get(room_id)
    if c and now - c[0] < 300:
        return c[1]
    name = room_id
    try:
        r = http.get(
            f"{SYNAPSE_URL}/_matrix/client/v3/rooms/{quote(room_id, safe='')}/state/m.room.name/",
            headers={"Authorization": f"Bearer {tok}"}, timeout=10)
        if r.status_code == 200:
            n = r.json().get("name", "").strip()
            if n:
                name = n
    except Exception:
        pass
    if name == room_id:
        try:
            r = http.get(
                f"{SYNAPSE_URL}/_matrix/client/v3/rooms/{quote(room_id, safe='')}/state/m.room.canonical_alias/",
                headers={"Authorization": f"Bearer {tok}"}, timeout=10)
            if r.status_code == 200:
                a = r.json().get("alias", "").strip()
                if a:
                    name = a
        except Exception:
            pass
    _room_name_cache[room_id] = (now, name)
    return name

def get_meta(base):
    p = os.path.join(PUB_DIR, base + ".meta")
    if os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass
    return None

def probe(fpath):
    if fpath in _probe_cache:
        return _probe_cache[fpath]
    info = {"duration": 0.0, "audio": "", "video": ""}
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-show_entries", "stream=codec_name,codec_type,width,height",
             "-of", "json", fpath],
            capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            d = json.loads(r.stdout)
            try:
                info["duration"] = float(d.get("format", {}).get("duration", 0))
            except Exception:
                pass
            for s in d.get("streams", []):
                if s.get("codec_type") == "audio":
                    info["audio"] = s.get("codec_name", "")
                elif s.get("codec_type") == "video":
                    info["video"] = str(s.get("width", "?")) + "x" + str(s.get("height", "?"))
    except Exception:
        pass
    _probe_cache[fpath] = info
    return info

def is_authed():
    return "access_token" in session and "user_id" in session

def can_access(base, tok, uid, adm):
    """Check if user can access a recording.
    Server admin: yes.
    Room admin (power >= 50): yes.
    Others: no.
    """
    if adm:
        return True
    m = get_meta(base)
    if not m or not m.get("room_id"):
        return False
    level = get_power_level(tok, uid, m["room_id"])
    return level >= 50

def get_visible_recordings(tok, uid, adm):
    """Get recordings visible to this user."""
    recs = []
    try:
        files = sorted(os.listdir(PUB_DIR), reverse=True)
    except Exception:
        files = []
    for name in files:
        if not re.fullmatch(r"rec-\d+_web\.mp4", name):
            continue
        base = name[:-8]
        meta = get_meta(base)
        room_id = meta.get("room_id") if meta else None
        if not adm:
            if not room_id:
                continue
            level = get_power_level(tok, uid, room_id)
            if level < 50:
                continue
        fpath = os.path.join(PUB_DIR, name)
        info = probe(fpath)
        m = re.search(r"(\d{13})", base)
        ts = (datetime.datetime.fromtimestamp(int(m.group(1)) / 1000)
              .strftime("%d.%m.%Y %H:%M:%S") if m else base)
        recs.append({
            "base": base, "timestamp": ts,
            "duration": info["duration"],
            "size_mb": os.path.getsize(fpath) / 1048576,
            "audio": info["audio"], "video": info["video"],
            "has_audio": os.path.exists(os.path.join(PUB_DIR, base + "_audio.mp3")),
            "has_frame": os.path.exists(os.path.join(PUB_DIR, base + "_frame.jpg")),
            "room_id": room_id,
        })
    return recs

# ── HTML ──

LOGIN_PAGE = """<!doctype html><html lang=ru><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Записи — вход</title>
<style>
body{background:#14161a;color:#e8e8e8;font:15px/1.7 system-ui,sans-serif;
display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}
.box{background:#1a1d23;padding:32px;border-radius:12px;max-width:360px;width:90%}
h2{margin:0 0 20px;font-weight:600}
label{display:block;margin:12px 0 4px;color:#9aa0a8;font-size:13px}
input{width:100%;box-sizing:border-box;padding:10px;border:1px solid #2a2d33;
border-radius:8px;background:#22252b;color:#e8e8e8;font-size:14px}
button{width:100%;margin-top:20px;padding:12px;border:none;border-radius:8px;
background:#0dbd8b;color:#fff;font-size:15px;cursor:pointer}
button:hover{background:#0bbb9a}
.err{color:#e06c6c;margin-top:12px;text-align:center}
.hint{color:#6b7078;font-size:12px;margin-top:16px;text-align:center}
</style>
<div class=box>
<h2>🔐 Записи звонков</h2>
<form method=post action=login>
<label>Пользователь Matrix</label>
<input name=username placeholder=admin required autofocus>
<label>Пароль</label>
<input type=password name=password required>
<button type=submit>Войти</button>
</form>
__ERROR__
<p class=hint>Используйте учётную запись Matrix (Element)</p>
</div></html>"""

def render_login(error=None):
    err = '<p class=err>' + error + '</p>' if error else ""
    return LOGIN_PAGE.replace("__ERROR__", err)

def _esc(s):
    if not s:
        return ""
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))

def _render_card(r):
    d = r["duration"]
    mm = str(int(d // 60)) + ":" + str(int(d % 60)).zfill(2) if d else "?"
    sz = "{:.1f}".format(r["size_mb"]) + " МБ"
    badge = ('<span class=ok>звук ' + r["audio"] + '</span>' if r["audio"]
             else '<span class=no>ЗВУКА НЕТ</span>')
    mp3 = (' · <a href="' + r["base"] + '_audio.mp3" download>только звук</a>'
           if r["has_audio"] else "")
    poster = ('poster="' + r["base"] + '_frame.jpg"' if r["has_frame"] else "")
    return (
        '<div class=card><h3>' + r["timestamp"] + '</h3>'
        '<p class=meta>' + mm + ' · ' + r["video"] + ' · ' + sz + ' · ' + badge + '</p>'
        '<video controls preload=metadata ' + poster + '>'
        '<source src="' + r["base"] + '_web.mp4" type="video/mp4"></video>'
        '<p><a href="' + r["base"] + '_web.mp4" download>скачать видео</a>' + mp3 + '</p></div>'
    )

def render_list(recs, uid, adm, tok):
    # Группировка по room_id
    groups = {}
    no_room = []
    for r in recs:
        rid = r.get("room_id")
        if rid:
            groups.setdefault(rid, []).append(r)
        else:
            no_room.append(r)

    # Получить названия комнат
    room_names = {}
    for rid in groups:
        room_names[rid] = get_room_name(tok, rid)

    sections = []
    for rid in sorted(groups.keys(), key=lambda x: room_names.get(x, x)):
        rname = room_names.get(rid, rid)
        cards = [_render_card(r) for r in groups[rid]]
        sections.append(
            '<div class=room-section>'
            '<h2 class=room-name>💬 ' + _esc(rname) + '</h2>'
            '<p class=room-meta>' + str(len(groups[rid])) + ' запис(ей) · <code>' + _esc(rid) + '</code></p>'
            + "".join(cards) + '</div>'
        )

    if no_room:
        cards = [_render_card(r) for r in no_room]
        sections.append(
            '<div class=room-section>'
            '<h2 class=room-name>⚠ Записи без привязки к комнате</h2>'
            '<p class=room-meta>' + str(len(no_room)) + ' запис(ей) · только для администратора</p>'
            + "".join(cards) + '</div>'
        )

    role = "👑 Серверный админ" if adm else "👥 Админ комнаты"
    n = len(recs)
    now = datetime.datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    body = "".join(sections) if sections else "<p>Нет доступных записей.</p>"
    return """<!doctype html><html lang=ru><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Записи E2EE-звонков</title>
<style>
body{background:#14161a;color:#e8e8e8;font:15px/1.7 system-ui,sans-serif;
max-width:940px;margin:36px auto;padding:0 20px}
h2{font-weight:600} h3{margin:0 0 6px;font-size:17px}
video{width:100%;border-radius:10px;background:#000;margin:8px 0}
.meta{color:#9aa0a8;font-size:13px;margin:0 0 8px}
.ok{color:#5ec27a} .no{color:#e06c6c}
.card{border-top:1px solid #2a2d33;padding-top:20px;margin-top:28px}
a{color:#7cc4ff;text-decoration:none} a:hover{text-decoration:underline}
code{background:#22252b;padding:2px 6px;border-radius:4px;font-size:12px}
.upd{color:#6b7078;font-size:12px;margin-top:40px}
.bar{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}
.room-section{margin-bottom:40px}
.room-name{font-size:20px;font-weight:600;color:#7cc4ff;margin-bottom:4px;
border-bottom:2px solid #2a2d33;padding-bottom:8px}
.room-meta{color:#6b7078;font-size:13px;margin:0 0 16px}
</style>
<div class=bar>
<h2>📹 Записи звонков</h2>
<div><span class=upd>""" + role + " · " + _esc(uid) + """</span> · <a href=logout>выйти</a></div>
</div>
<p class=meta>всего записей: """ + str(n) + """</p>
""" + body + """
<p class=upd>обновлено """ + now + """</p>
</html>"""

# ── Routes ──

@app.after_request
def set_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp

@app.route("/")
def index():
    if not is_authed():
        return render_login(request.args.get("error"))
    tok = session["access_token"]
    uid = session["user_id"]
    adm = session.get("is_admin", False)
    recs = get_visible_recordings(tok, uid, adm)
    return render_list(recs, uid, adm, tok)

@app.route("/login", methods=["POST"])
def login():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    if username.startswith("@"):
        username = username.split(":")[0][1:]
    if not username or not password:
        return redirect("./?error=" + quote("Введите логин и пароль"))
    try:
        r = http.post(f"{SYNAPSE_URL}/_matrix/client/v3/login", json={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": username},
            "password": password
        }, timeout=10)
        if r.status_code != 200:
            try:
                err = r.json().get("error", "Неверный логин или пароль")
            except Exception:
                err = "Неверный логин или пароль"
            return redirect("./?error=" + quote(err))
        data = r.json()
        tok = data["access_token"]
        uid = data["user_id"]
        adm = check_admin(tok, uid)
        session["access_token"] = tok
        session["user_id"] = uid
        session["is_admin"] = adm
        return redirect("./")
    except Exception as e:
        return redirect("./?error=" + quote("Ошибка сервера: " + str(e)))

@app.route("/logout")
def logout():
    if "access_token" in session:
        try:
            http.post(f"{SYNAPSE_URL}/_matrix/client/v3/logout",
                      headers={"Authorization": f"Bearer {session['access_token']}"},
                      timeout=5)
        except Exception:
            pass
    session.clear()
    return redirect("./")

@app.route("/<path:filename>")
def serve_file(filename):
    if filename in ("index.html", "index.htm"):
        return redirect("./")
    if not is_authed():
        return redirect("./")
    base = None
    if filename.endswith("_web.mp4"):
        base = filename[:-8]
    elif filename.endswith("_audio.mp3"):
        base = filename[:-10]
    elif filename.endswith("_frame.jpg"):
        base = filename[:-10]
    else:
        abort(403)
    if not can_access(base, session["access_token"], session["user_id"],
                      session.get("is_admin", False)):
        abort(403)
    try:
        return send_from_directory(PUB_DIR, filename)
    except Exception:
        abort(404)

@app.errorhandler(403)
def forbidden(e):
    if is_authed():
        return "Доступ запрещён", 403
    return redirect("./")



import requests as _acl_http
from urllib.parse import quote as _acl_quote
from flask import abort as _acl_abort, g as _acl_g

def _acl_get(token, path):
    try:
        response = _acl_http.get(
            SYNAPSE_URL.rstrip("/") + path,
            headers={"Authorization": "Bearer " + token},
            timeout=10,
        )
    except _acl_http.RequestException:
        _acl_abort(503, description="Matrix temporarily unavailable")
    if response.status_code == 401:
        _acl_abort(401, description="Matrix session expired")
    return response

def _acl_identity(token):
    cached = getattr(_acl_g, "_recording_identity", None)
    if cached and cached[0] == token:
        return cached[1], cached[2]

    response = _acl_get(token, "/_matrix/client/v3/account/whoami")
    if response.status_code != 200:
        _acl_abort(503, description="Cannot verify Matrix identity")
    try:
        uid = response.json()["user_id"]
        if not isinstance(uid, str) or not uid.startswith("@"):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        _acl_abort(503, description="Invalid Matrix identity response")

    response = _acl_get(
        token, "/_synapse/admin/v2/users/" + _acl_quote(uid, safe="")
    )
    if response.status_code == 200:
        try:
            admin = response.json().get("admin") is True
        except (ValueError, AttributeError):
            _acl_abort(503, description="Invalid administrator response")
    elif response.status_code == 403:
        admin = False
    else:
        _acl_abort(503, description="Cannot verify server administrator")

    _acl_g._recording_identity = (token, uid, admin)
    return uid, admin

def _acl_joined(token):
    cached = getattr(_acl_g, "_recording_joined", None)
    if cached and cached[0] == token:
        return cached[1]
    response = _acl_get(token, "/_matrix/client/v3/joined_rooms")
    if response.status_code != 200:
        _acl_abort(503, description="Cannot verify room membership")
    try:
        rooms = response.json()["joined_rooms"]
        if not isinstance(rooms, list):
            raise ValueError()
        if not all(isinstance(room, str) for room in rooms):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        _acl_abort(503, description="Invalid membership response")
    result = set(rooms)
    _acl_g._recording_joined = (token, result)
    return result

def _acl_power(token, uid, room):
    if not isinstance(room, str) or not room.startswith("!"):
        return 0
    # Не использовать исторический state после выхода из комнаты.
    if room not in _acl_joined(token):
        return 0

    cache = getattr(_acl_g, "_recording_powers", None)
    if cache is None:
        cache = {}
        _acl_g._recording_powers = cache
    key = (token, uid, room)
    if key in cache:
        return cache[key]

    response = _acl_get(
        token, "/_matrix/client/v3/rooms/"
        + _acl_quote(room, safe="") + "/state"
    )
    if response.status_code == 403:
        return 0
    if response.status_code != 200:
        _acl_abort(503, description="Cannot verify room permissions")
    try:
        events = response.json()
        if not isinstance(events, list):
            raise ValueError()
        state = {
            (event["type"], event.get("state_key", "")): event
            for event in events if isinstance(event, dict)
        }
        create = state.get(("m.room.create", ""), {})
        content = create.get("content", {})
        version = str(content.get("room_version", "1"))
        creator = create.get("sender") or content.get("creator")
        power_event = state.get(("m.room.power_levels", ""))
        creators = {creator}
        if version == "12":
            creators.update(content.get("additional_creators", []))

        if version == "12" and uid in creators:
            level = float("inf")
        elif power_event is None:
            level = 100 if uid == creator else 0
        else:
            power = power_event["content"]
            level = power.get("users", {}).get(
                uid, power.get("users_default", 0)
            )
            if type(level) is not int:
                if isinstance(level, str) and level.lstrip("-").isdigit():
                    level = int(level)
                else:
                    level = 0
    except (ValueError, KeyError, TypeError, AttributeError):
        _acl_abort(503, description="Invalid room permissions")
    # Кэш действует только в пределах текущего HTTP-запроса.
    cache[key] = level
    return level

from html import escape as _html_escape

app.config.update(
    SESSION_COOKIE_NAME="recordings_session",
    SESSION_COOKIE_PATH="/recordings",
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=28800,
)

def get_power_level(tok, uid, room_id):
    return _acl_power(tok, uid, room_id)

def check_admin(tok, uid):
    actual_uid, admin = _acl_identity(tok)
    return actual_uid == uid and admin

def render_login(error=None):
    message = (
        '<p class=err>' + _html_escape(str(error)) + '</p>'
        if error else ""
    )
    return LOGIN_PAGE.replace("__ERROR__", message)

def get_meta(base):
    if not re.fullmatch(r"rec-\d+", base):
        return None
    path = os.path.join(PUB_DIR, base + ".meta")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return None
        room = data.get("room_id")
        if not isinstance(room, str) or not room.startswith("!"):
            return None
        return data
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        app.logger.exception("Cannot read metadata: %s", path)
        return None

_old_room_name = get_room_name

def get_room_name(tok, room_id):
    uid, admin = _acl_identity(tok)
    if not admin:
        return _old_room_name(tok, room_id)
    try:
        response = http.get(
            SYNAPSE_URL + "/_synapse/admin/v1/rooms/"
            + quote(room_id, safe=""),
            headers={"Authorization": "Bearer " + tok},
            timeout=10,
        )
        if response.status_code == 200:
            data = response.json()
            return data.get("name") or data.get("canonical_alias") or room_id
    except (http.RequestException, ValueError, AttributeError):
        app.logger.warning("Cannot fetch room name: %s", room_id)
    return room_id

_old_visible_recordings = get_visible_recordings

def get_visible_recordings(tok, uid, adm):
    recordings = _old_visible_recordings(tok, uid, adm)
    room = request.args.get("room")
    if room:
        return [item for item in recordings if item["room_id"] == room]
    return recordings

@app.before_request
def _installer_protect_recordings():
    if request.path in ("/login", "/logout"):
        return

    if request.path != "/" and not re.fullmatch(
        r"/rec-\d+(?:_web\.mp4|_audio\.mp3|_frame\.jpg)",
        request.path,
    ):
        abort(403)

    if not is_authed():
        if request.path == "/":
            return
        abort(401)

    token = session["access_token"]
    uid, admin = _acl_identity(token)
    if uid != session["user_id"]:
        session.clear()
        abort(401)

    # Перепроверяем admin при каждом запросе.
    session["is_admin"] = admin

    if request.path == "/" and not admin:
        room = request.args.get("room")
        if room:
            if _acl_power(token, uid, room) < 50:
                abort(403)
        elif not any(
            _acl_power(token, uid, rid) >= 50
            for rid in _acl_joined(token)
        ):
            abort(403)

@app.after_request
def _installer_private_response(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "media-src 'self'; img-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    return response


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80, threaded=True)
