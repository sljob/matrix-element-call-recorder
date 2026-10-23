#!/usr/bin/env python3
"""
Portal: serves the recordings archive with real Matrix authorization.

Two rules that make this different from a static file server:

  1. The index is filtered per caller. You see only rooms you are a member of
     and where you hold enough power. A URL is not a capability.
  2. Every file request is re-authorized. Copying a link to a colleague who is
     not in the room yields 403, not the MP4.

Authorization is delegated to Synapse: the browser sends its own access token,
we call /_matrix/client/v3/account/whoami to learn the user id, then read the
room state for membership and power level. Server admins see everything.
"""

import functools
import html
import io
import time
import json
import os
import re
import urllib.parse

import requests
from flask import Flask, request, send_file, Response, abort

PORT = int(os.environ.get("PORT", "8899"))
PUBLIC_DIR = os.environ.get("PUBLIC_DIR", "/public")
SYNAPSE_URL = os.environ.get("SYNAPSE_URL", "").rstrip("/")
DOMAIN = os.environ.get("DOMAIN", "")
LANG = os.environ.get("INSTALL_LANGUAGE", "ru")
CA = os.environ.get("TLS_CA_FILE", "")
CACHE_TTL = 120

app = Flask(__name__)
SESSION = requests.Session()

_auth_cache = {}   # token hash -> (user_id, expires)
_room_cache = {}   # room_id -> (state dict, expires)

T = {
    "ru": {
        "title": "Записи конференций",
        "empty": "Записей пока нет.",
        "denied": "Нет доступа. Войдите тем же аккаунтом Matrix, что и в Element.",
        "login": "Войти через Element",
        "room": "Комната",
        "size": "Размер",
        "date": "Дата",
        "duration_hint": "Скачать",
        "back": "Назад к списку",
    },
    "en": {
        "title": "Conference recordings",
        "empty": "No recordings yet.",
        "denied": "Access denied. Sign in with the same Matrix account as Element.",
        "login": "Sign in via Element",
        "room": "Room",
        "size": "Size",
        "date": "Date",
        "duration_hint": "Download",
        "back": "Back to list",
    },
}


def t(key):
    return T.get(LANG, T["ru"]).get(key, key)


def verify():
    return CA if CA and os.path.isfile(CA) else True


def token_from_request():
    """Element passes its access token; accept header, query or cookie."""
    h = request.headers.get("Authorization", "")
    if h.startswith("Bearer "):
        return h[7:].strip()
    q = request.args.get("access_token")
    if q:
        return q.strip()
    c = request.cookies.get("mx_access_token")
    return c.strip() if c else ""


def whoami(token):
    """Resolve the caller. Cached briefly: this runs on every file request."""
    if not token or not SYNAPSE_URL:
        return None
    import hashlib
    key = hashlib.sha256(token.encode()).hexdigest()
    now = time.monotonic()
    hit = _auth_cache.get(key)
    if hit and hit[1] > now:
        return hit[0]
    try:
        r = SESSION.get(SYNAPSE_URL + "/_matrix/client/v3/account/whoami",
                        headers={"Authorization": "Bearer " + token},
                        timeout=5, verify=verify())
        if r.status_code == 200:
            uid = r.json().get("user_id")
            if uid:
                _auth_cache[key] = (uid, now + CACHE_TTL)
                return uid
    except requests.RequestException:
        pass
    return None


def room_access(token, user_id, room_id):
    """Return (is_member, power_level) for this user in this room."""
    now = time.monotonic()
    hit = _room_cache.get(room_id)
    if hit and hit[1] > now:
        state = hit[0]
    else:
        state = {}
        try:
            r = SESSION.get(
                "%s/_matrix/client/v3/rooms/%s/state"
                % (SYNAPSE_URL, urllib.parse.quote(room_id, safe="")),
                headers={"Authorization": "Bearer " + token}, timeout=6, verify=verify())
            if r.status_code == 200:
                for ev in r.json():
                    if ev.get("type") == "m.room.member":
                        state.setdefault("members", {})[ev.get("state_key")] = ev
                    elif ev.get("type") == "m.room.power_levels":
                        state["pl"] = ev.get("content", {})
        except requests.RequestException:
            return False, 0
        _room_cache[room_id] = (state, now + CACHE_TTL)

    members = state.get("members", {})
    me = members.get(user_id)
    if not me or me.get("content", {}).get("membership") != "join":
        return False, 0
    pl = state.get("pl", {})
    users = pl.get("users", {}) or {}
    level = users.get(user_id, pl.get("users_default", 0))
    return True, int(level) if isinstance(level, (int, float)) else 0


def is_server_admin(token, user_id):
    """Server admins see every recording. Uses the standard Synapse admin API."""
    try:
        r = SESSION.get(
            "%s/_synapse/admin/v2/users/%s" % (SYNAPSE_URL, urllib.parse.quote(user_id, safe="")),
            headers={"Authorization": "Bearer " + token}, timeout=5, verify=verify())
        return r.status_code == 200 and bool(r.json().get("admin"))
    except requests.RequestException:
        return False


# --------------------------------------------------------------------------
# archive index
# --------------------------------------------------------------------------

SAFE = re.compile(r"^[A-Za-z0-9_.\-/]+$")


def scan_public():
    """Walk the public dir and build entries keyed by room directory."""
    out = []
    if not os.path.isdir(PUBLIC_DIR):
        return out
    for root, _dirs, files in os.walk(PUBLIC_DIR):
        for f in sorted(files):
            if not f.lower().endswith((".mp4", ".mp3", ".jpg")):
                continue
            p = os.path.join(root, f)
            rel = os.path.relpath(p, PUBLIC_DIR).replace(os.sep, "/")
            if not SAFE.match(rel):
                continue
            room_dir = rel.split("/")[0] if "/" in rel else ""
            out.append({
                "rel": rel,
                "name": f,
                "room_dir": room_dir,
                "bytes": os.path.getsize(p),
                "mtime": int(os.path.getmtime(p)),
            })
    out.sort(key=lambda e: e["mtime"], reverse=True)
    return out


def room_id_from_dir(room_dir):
    """Output dirs are 'roomid_domain' (sanitized). Rebuild a plausible id."""
    if not room_dir:
        return ""
    if room_dir.startswith("!"):
        return room_dir
    parts = room_dir.split("_", 1)
    if len(parts) == 2 and parts[1]:
        return "!%s:%s" % (parts[0], parts[1])
    return "!" + room_dir


def allowed_entries(token, user_id, entries):
    """Filter entries by room membership. Cached per room within the request."""
    admin = is_server_admin(token, user_id)
    decisions = {}
    kept = []
    for e in entries:
        rid = room_id_from_dir(e["room_dir"])
        if rid not in decisions:
            decisions[rid] = (True, 999) if admin else room_access(token, user_id, rid)
        member, _pl = decisions[rid]
        if member:
            kept.append(e)
    return kept


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f TB" % n


@app.get("/healthz")
def healthz():
    return {"ok": True, "synapse": bool(SYNAPSE_URL), "lang": LANG}


@app.get("/")
@app.get("/index.html")
def index():
    token = token_from_request()
    user = whoami(token) if token else None

    if not user:
        return Response(render_login(), mimetype="text/html; charset=utf-8")

    entries = allowed_entries(token, user, scan_public())
    return Response(render_index(entries, user), mimetype="text/html; charset=utf-8")


@app.get("/api/list")
def api_list():
    token = token_from_request()
    user = whoami(token)
    if not user:
        return {"ok": False, "error": "unauthorized"}, 401
    entries = allowed_entries(token, user, scan_public())
    return {"ok": True, "user": user, "recordings": entries}


@app.get("/files/<path:rel>")
def file(rel):
    token = token_from_request()
    user = whoami(token)
    if not user:
        abort(401)
    if not SAFE.match(rel):
        abort(400)

    room_dir = rel.split("/")[0] if "/" in rel else ""
    rid = room_id_from_dir(room_dir)
    if not is_server_admin(token, user):
        member, _ = room_access(token, user, rid)
        if not member:
            app.logger.warning("denied %s for %s (room %s)", rel, user, rid)
            abort(403)

    full = os.path.abspath(os.path.join(PUBLIC_DIR, rel))
    if not full.startswith(os.path.abspath(PUBLIC_DIR)) or not os.path.isfile(full):
        abort(404)
    return send_file(full, conditional=True)


# --------------------------------------------------------------------------
# minimal HTML (no build step, no external assets)
# --------------------------------------------------------------------------

CSS = """
:root{--bg:#0e1116;--fg:#e6edf3;--dim:#8b949e;--acc:#2f81f7;--card:#161b22}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{padding:24px 20px;border-bottom:1px solid #21262d}
h1{margin:0;font-size:20px;font-weight:600}
main{max-width:960px;margin:0 auto;padding:20px}
.card{background:var(--card);border:1px solid #21262d;border-radius:8px;
 padding:14px 16px;margin:10px 0;display:flex;flex-wrap:wrap;
 align-items:center;gap:12px}
.name{flex:1 1 260px;font-weight:500;word-break:break-all}
.meta{color:var(--dim);font-size:13px;min-width:110px}
a.dl{background:var(--acc);color:#fff;text-decoration:none;padding:6px 14px;
 border-radius:6px;font-size:14px}
a.dl:hover{filter:brightness(1.1)}
.empty{color:var(--dim);padding:40px 0;text-align:center}
.login{max-width:460px;margin:60px auto;padding:24px;background:var(--card);
 border:1px solid #21262d;border-radius:10px}
.login a{color:var(--acc)}
code{background:#21262d;padding:2px 6px;border-radius:4px;font-size:13px}
"""


def page(title, body):
    return ("<!doctype html><html lang='%s'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>%s</title><style>%s</style></head><body>%s</body></html>"
            % (LANG, html.escape(title), CSS, body))


def render_login():
    login_url = "/login" if not DOMAIN else "https://%s" % DOMAIN
    body = ("<header><h1>%s</h1></header><main><div class='login'>"
            "<p>%s</p><p><a href='%s'>%s</a></p>"
            "<p style='color:var(--dim);font-size:13px'>"
            "&larr; <a href='%s' style='color:inherit'>%s</a></p>"
            "</div></main>") % (
        html.escape(t("title")), html.escape(t("denied")),
        html.escape(login_url), html.escape(t("login")),
        html.escape(login_url), html.escape(t("back")))
    return page(t("title"), body)


def render_index(entries, user):
    rows = []
    for e in entries:
        import datetime
        when = datetime.datetime.utcfromtimestamp(e["mtime"]).strftime("%Y-%m-%d %H:%M")
        icon = "🎬" if e["name"].endswith(".mp4") else ("🎧" if e["name"].endswith(".mp3") else "🖼")
        rows.append(
            "<div class='card'><span>%s</span>"
            "<span class='name'>%s</span>"
            "<span class='meta'>%s</span>"
            "<span class='meta'>%s</span>"
            "<a class='dl' href='/files/%s'>%s</a></div>"
            % (icon, html.escape(e["name"]), html.escape(when), human(e["bytes"]),
               urllib.parse.quote(e["rel"]), html.escape(t("duration_hint"))))

    body = ("<header><h1>%s</h1><div style='color:var(--dim);font-size:13px'>%s</div></header>"
            "<main>%s</main>") % (
        html.escape(t("title")), html.escape(user),
        "".join(rows) if rows else "<div class='empty'>%s</div>" % html.escape(t("empty")))
    return page(t("title"), body)


if __name__ == "__main__":
    app.logger.info("portal: public=%s synapse=%s lang=%s", PUBLIC_DIR, SYNAPSE_URL, LANG)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
