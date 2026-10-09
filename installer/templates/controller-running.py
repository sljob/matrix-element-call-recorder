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
def _save_state(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f)
def make_service_token():
    now = int(time.time())
    payload = {"iss": LIVEKIT_KEY, "sub": "recording-controller", "iat": now, "nbf": now, "exp": now + 600, "video": {"roomRecord": True}}
    return jwt.encode(payload, LIVEKIT_SECRET, algorithm="HS256")
def _reconcile(room, state):
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
        rr = requests.post(f"{RECORDER_URL}/start", json={"room": room}, timeout=20)
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
        rr = requests.post(f"{RECORDER_URL}/stop", json={"room": room}, timeout=60)
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
if __name__ == "__main__":
    os.makedirs("/data", exist_ok=True)
    app.run(host="0.0.0.0", port=8765)
