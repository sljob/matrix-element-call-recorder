import os, time, json, uuid, jwt, requests, hashlib, base64
from flask import Flask, request, jsonify
app = Flask(__name__)
LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "http://matrix-livekit-server:7880")
LIVEKIT_KEY = os.environ["LIVEKIT_KEY"]
LIVEKIT_SECRET = os.environ["LIVEKIT_SECRET"]
SYNAPSE_URL = os.environ.get("SYNAPSE_URL", "http://matrix-synapse:8008")
RECORDER_URL = os.environ.get("RECORDER_URL", "http://172.21.0.1:8788")
MIN_POWER_LEVEL = int(os.environ.get("RECORDING_MIN_POWER_LEVEL", "50"))
STATE_FILE = "/data/state.json"
RECORDER_API_TOKEN = os.environ["RECORDER_API_TOKEN"]
RECORDER_USER_ID = os.environ["RECORDER_USER_ID"]
OUT_DIR = "/out"
def livekit_room_name(matrix_room_id):
    local_part = matrix_room_id.split(':')[0]
    slot_id = "m.call#ROOM"
    raw = json.dumps([local_part, slot_id], separators=(',', ':'), ensure_ascii=False)
    digest = hashlib.sha256(raw.encode('utf-8')).digest()
    return base64.b64encode(digest).decode().rstrip('=')
def _load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f: return json.load(f)
    return {}

def _installer_write_metadata(state):
    import re
    import tempfile

    for room, entry in state.items():
        if not isinstance(entry, dict):
            continue
        filename = os.path.basename(entry.get("file") or "")
        if not re.fullmatch(r"rec-\d+\.mp4", filename):
            continue
        if not isinstance(room, str) or not room.startswith("!"):
            raise ValueError("Invalid recording room ID")

        destination = os.path.join(OUT_DIR, filename[:-4] + ".meta")
        data = {
            "room_id": room,
            "started_at": entry.get("started_at"),
            "ended_at": entry.get("ended_at"),
            "started_by": entry.get("started_by"),
        }
        fd, temporary = tempfile.mkstemp(
            prefix=".recording-meta-", dir=OUT_DIR
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
                handle.flush()
                os.fchmod(handle.fileno(), 0o644)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

def _save_state(s):
    _installer_write_metadata(s)
    with open(STATE_FILE + ".tmp", "w") as f: json.dump(s, f)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)
def make_service_token():
    now = int(time.time())
    payload = {"iss": LIVEKIT_KEY, "sub": "recording-controller", "iat": now, "nbf": now, "exp": now + 600, "video": {"roomRecord": True}}
    return jwt.encode(payload, LIVEKIT_SECRET, algorithm="HS256")
def _reconcile(room, state):
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
    return state
def check_auth(room_id):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "): return False, "missing matrix access token"
    token = auth.split(" ", 1)[1]
    try:
        who = requests.get(f"{SYNAPSE_URL}/_matrix/client/v3/account/whoami", headers={"Authorization": f"Bearer {token}"}, timeout=10)
        if who.status_code != 200: return False, "invalid matrix access token"
        user_id = who.json().get("user_id")
        state_ev = requests.get(f"{SYNAPSE_URL}/_matrix/client/v3/rooms/{room_id}/state", headers={"Authorization": f"Bearer {token}"}, timeout=10)
        if state_ev.status_code == 200:
            for ev in state_ev.json():
                if ev.get("type") == "m.room.create":
                    creator = ev.get("sender")
                    if creator == user_id: return True, user_id
                    break
        pl = requests.get(f"{SYNAPSE_URL}/_matrix/client/v3/rooms/{room_id}/state/m.room.power_levels", headers={"Authorization": f"Bearer {token}"}, timeout=10)
        if pl.status_code != 200: return False, "cannot read power levels"
        data = pl.json()
        users = data.get("users", {})
        default_pl = data.get("users_default", 0)
        user_pl = users.get(user_id, default_pl)
        if user_pl < MIN_POWER_LEVEL: return False, f"insufficient power level ({user_pl} < {MIN_POWER_LEVEL})"
        return True, user_id
    except requests.RequestException as e: return False, f"auth check failed: {e}"
@app.route("/health", methods=["GET"])
def health(): return jsonify({"status": "ok"})
@app.route("/api/record/start", methods=["POST"])
def start_record():
    data = request.json or {}
    room = data.get("room")
    if not room: return jsonify({"error": "room is required"}), 400
    ok, info = check_auth(room)
    if not ok: return jsonify({"error": f"forbidden: {info}"}), 403
    state = _load_state()
    state = _reconcile(room, state)
    if room in state and state[room].get("status") == "active":
        return jsonify({"error": "recording already active", "egress_id": state[room]["egress_id"]}), 409
    ts = int(time.time())
    lk_room = livekit_room_name(room)
    try:
        encoded_room = requests.utils.quote(room, safe="")
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
            headers={"Authorization": "Bearer " + RECORDER_API_TOKEN, "X-Matrix-Authorization": request.headers.get("Authorization", "")}, timeout=20)
        if rr.status_code != 200: return jsonify({"error": "recorder error", "detail": rr.text}), 502
        egress_id = f"rec-{ts}"
        filename = os.path.basename((rr.json() or {}).get("file", f"{ts}.mp4"))
    except requests.RequestException as e:
        return jsonify({"error": f"recorder unreachable: {type(e).__name__}: {e}"}), 502
    state[room] = {"egress_id": egress_id, "status": "active", "file": filename, "started_by": info, "started_at": ts, "livekit_room_name": lk_room}
    _save_state(state)
    return jsonify({"egress_id": egress_id, "file": filename, "status": "started", "livekit_room_name": lk_room})
@app.route("/api/record/stop", methods=["POST"])
def stop_record():
    data = request.json or {}
    room = data.get("room")
    if not room: return jsonify({"error": "room is required"}), 400
    ok, info = check_auth(room)
    if not ok: return jsonify({"error": f"forbidden: {info}"}), 403
    state = _load_state()
    state = _reconcile(room, state)
    entry = state.get(room)
    if not entry: return jsonify({"error": "no recording found for this room"}), 404
    if entry.get("status") != "active":
        return jsonify({"status": entry.get("status", "stopped"), "file": entry.get("file"), "note": "recording was already stopped"})
    try:
        rr = requests.post(f"{RECORDER_URL}/stop", json={"room": room}, headers={"Authorization": "Bearer " + RECORDER_API_TOKEN}, timeout=60)
        status, text = rr.status_code, rr.text
    except requests.RequestException as e:
        status, text = 502, f"recorder unreachable: {type(e).__name__}: {e}"
    if status not in (200, 404):
        return jsonify({"error": "recorder error", "detail": text}), 502
    entry["status"] = "stopped"
    entry["ended_at"] = int(time.time())
    state[room] = entry
    _save_state(state)
    return jsonify({"status": "stopped", "file": entry["file"]})
@app.route("/api/record/status", methods=["GET"])
def status_record():
    room = request.args.get("room")
    state = _load_state()
    if room:
        state = _reconcile(room, state)
        return jsonify(state.get(room, {"status": "inactive"}))
    return jsonify(state)
@app.route("/files/<path:filename>", methods=["GET"])
def get_file(filename):
    from flask import send_from_directory
    return send_from_directory(OUT_DIR, filename, as_attachment=True)


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

def check_auth(room_id):
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        return False, "missing Matrix access token"
    token = authorization[7:].strip()
    if not token:
        return False, "missing Matrix access token"
    uid, admin = _acl_identity(token)
    if admin or _acl_power(token, uid, room_id) >= 50:
        return True, uid
    return False, "room administrator power level >= 50 required"

@app.before_request
def _installer_protect_controller():
    # Старый маршрут позволял обходить recordings-auth.
    if request.path == "/files" or request.path.startswith("/files/"):
        _acl_abort(403)
    if request.path == "/api/record/status":
        room = request.args.get("room")
        if not room:
            _acl_abort(403)
        allowed, reason = check_auth(room)
        if not allowed:
            _acl_abort(403, description=reason)



# CONTROLLER_HTTP_ERRORS_JSON_V1
from werkzeug.exceptions import HTTPException as _ControllerHTTPException

@app.errorhandler(_ControllerHTTPException)
def _controller_http_error(error):
    response = error.get_response()
    code = response.status_code
    messages = {
        400: "Некорректный запрос",
        401: "Сессия недействительна. Войдите в Element заново",
        403: "Недостаточно прав для управления записью",
        404: "Ресурс API не найден",
        405: "Метод запроса не поддерживается",
        503: "Не удалось проверить права. Matrix временно недоступен",
    }
    response.set_data(json.dumps({
        "error": messages.get(code, error.name), "status": code,
    }, ensure_ascii=False))
    response.content_type = "application/json; charset=utf-8"
    response.headers["Cache-Control"] = "no-store"
    return response

if __name__ == "__main__":
    os.makedirs("/data", exist_ok=True)
    app.run(host="0.0.0.0", port=8765, threaded=False)
