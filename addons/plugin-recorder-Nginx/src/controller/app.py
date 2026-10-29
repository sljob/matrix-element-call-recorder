#!/usr/bin/env python3
"""
Recording controller for the overlay plugin.

Improvements over the reference controller:

  _reconcile() is a NOOP here, on purpose.
  The reference implementation polls LiveKit Egress to decide whether a
  recording is still running. Overlay installs must not assume Egress exists
  (many Element Call deployments run LiveKit without egress at all, and the
  plugin never calls LiveKit). Worker state is therefore derived from the
  workers themselves: /status is the single source of truth, and a recording
  is only reported "stopped" once the worker has finalized the file.

  This removes a whole class of "button stuck on Stop" bugs where Egress
  and the recorder disagreed.

Job state is kept in memory plus a JSON file, so a controller restart does not
lose track of an in-flight recording.
"""

import json
import os
import threading
import time

import requests
from flask import Flask, jsonify, request

PORT = int(os.environ.get("PORT", "8765"))
RECORDER_URLS = [u.strip() for u in os.environ.get("RECORDER_URLS", "").split(",") if u.strip()]
TOKEN = os.environ.get("RECORDER_TOKEN", "")
ACCOUNTS = [a.strip() for a in os.environ.get("RECORDER_ACCOUNTS", "").split(",") if a.strip()]
MIN_POWER = int(os.environ.get("RECORDING_MIN_POWER_LEVEL", "50"))
DOMAIN = os.environ.get("DOMAIN", "")
LANG = os.environ.get("INSTALL_LANGUAGE", "ru")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/output")
PUBLIC_DIR = os.environ.get("PUBLIC_DIR", "/public")
CA = os.environ.get("TLS_CA_FILE", "")
# Read by whoami() and room_power(). The compose template always passes it;
# without this line the lookup raises NameError and /start answers 500.
SYNAPSE_URL = os.environ.get("SYNAPSE_URL", "").rstrip("/")
ADOPT = os.environ.get("ADOPT_MODE", "false").lower() == "true"
ADOPT_URL = os.environ.get("ADOPT_CONTROLLER_URL", "")
ADOPT_PORTAL = os.environ.get("ADOPT_PORTAL_URL", "")
STATE_FILE = "/tmp/controller-state.json"

SESSION = requests.Session()

app = Flask(__name__)
_lock = threading.Lock()
_jobs = {}          # room_id -> job dict
_worker_busy = {}   # base url -> room_id


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def verify_tls():
    if CA and os.path.isfile(CA):
        return CA
    return True


# --------------------------------------------------------------------------
# caller authorization
# --------------------------------------------------------------------------

_auth_cache = {}   # sha256(token) -> (user_id, expires_at)
_AUTH_TTL = 60


def caller_token():
    """The Matrix access token the button sends. Never trusted as a claim,
    only as a key to ask Synapse who the caller is."""
    h = request.headers.get("Authorization", "")
    if h.startswith("Bearer "):
        return h[7:].strip()
    return ""


def whoami(token):
    import hashlib
    key = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    hit = _auth_cache.get(key)
    if hit and hit[1] > now:
        return hit[0]
    try:
        r = SESSION.get(SYNAPSE_URL + "/_matrix/client/v3/account/whoami",
                        headers={"Authorization": "Bearer " + token},
                        timeout=6, verify=verify_tls())
        if r.status_code == 200:
            uid = r.json().get("user_id")
            if uid:
                _auth_cache[key] = (uid, now + _AUTH_TTL)
                return uid
    except requests.RequestException as exc:
        app.logger.warning("whoami failed: %s", exc)
    return None


def room_power(token, room_id, user_id):
    """
    Return (is_member, power_level) or None when it cannot be determined.

    This is the check that actually protects /start. The button computes a
    power level locally for a nicer error message, but that number comes from
    the browser and is trivially forged -- it must never be the only gate.
    None means "could not verify", which callers must treat as a refusal.
    """
    import urllib.parse
    quoted = urllib.parse.quote(room_id, safe="")
    try:
        state = SESSION.get(
            "%s/_matrix/client/v3/rooms/%s/state/m.room.power_levels"
            % (SYNAPSE_URL, quoted),
            headers={"Authorization": "Bearer " + token}, timeout=6,
            verify=verify_tls())
        if state.status_code == 404:
            return (False, 0)
        if state.status_code != 200:
            return None
        levels = state.json() or {}

        member = SESSION.get(
            "%s/_matrix/client/v3/rooms/%s/state/m.room.member/%s"
            % (SYNAPSE_URL, quoted, urllib.parse.quote(user_id, safe="")),
            headers={"Authorization": "Bearer " + token}, timeout=6,
            verify=verify_tls())
        if member.status_code != 200:
            return (False, 0)
        if (member.json() or {}).get("membership") != "join":
            return (False, 0)

        users = levels.get("users") or {}
        pl = users.get(user_id)
        if not isinstance(pl, (int, float)):
            pl = levels.get("users_default", 0)
        return (True, int(pl))
    except (requests.RequestException, ValueError) as exc:
        app.logger.warning("power level check failed: %s", exc)
        return None


def worker_health(url, timeout=3):
    try:
        r = SESSION.get(url + "/health", timeout=timeout, verify=verify_tls())
        if r.status_code == 200:
            return r.json()
    except requests.RequestException:
        pass
    return None


def worker_status(url, timeout=3):
    try:
        r = SESSION.get(url + "/status", timeout=timeout, verify=verify_tls(),
                        headers={"X-Recorder-Token": TOKEN})
        if r.status_code == 200:
            return r.json()
    except requests.RequestException:
        pass
    return None


def worker_post(url, path, payload=None, timeout=8):
    try:
        r = SESSION.post(url + path, json=payload or {}, timeout=timeout,
                         verify=verify_tls(), headers={"X-Recorder-Token": TOKEN})
        return r.status_code, (r.json() if r.content else {})
    except requests.RequestException as exc:
        return 0, {"error": str(exc)}


def ensure_membership(token, room_id, user_id):
    """Invite the recorder bots and join them, using the caller's own token.

    The observer does a plain /join before recording. A private room answers
    M_FORBIDDEN ("You are not invited to this room") and the recording never
    starts (#10). The caller is a room member with enough power here, so their
    token can issue the invite; each bot then joins with its own credentials.
    Returns (ok, detail).
    """
    import urllib.parse

    if not ACCOUNTS:
        return True, "no accounts configured"
    quoted = urllib.parse.quote(room_id, safe="")
    for account in ACCOUNTS:
        uid = account if account.startswith("@") else None
        local = account.lstrip("@").split(":", 1)[0]
        if not uid and DOMAIN:
            uid = "@%s:%s" % (local, DOMAIN)
        if not uid:
            continue
        member = SESSION.get(
            "%s/_matrix/client/v3/rooms/%s/state/m.room.member/%s"
            % (SYNAPSE_URL, quoted, urllib.parse.quote(uid, safe="")),
            headers={"Authorization": "Bearer " + token}, timeout=6,
            verify=verify_tls())
        if member.status_code == 200:
            if (member.json() or {}).get("membership") == "join":
                continue
        # Not in the room yet: invite. 403 here means the caller cannot invite,
        # which is a real permission problem and worth reporting verbatim.
        inv = SESSION.post(
            "%s/_matrix/client/v3/rooms/%s/invite" % (SYNAPSE_URL, quoted),
            json={"user_id": uid},
            headers={"Authorization": "Bearer " + token}, timeout=8,
            verify=verify_tls())
        if inv.status_code not in (200, 403):
            # 403 = already joined, or an invite is pending; keep going.
            return False, "invite %s failed: HTTP %s %s" % (uid, inv.status_code, inv.text[:160])
    return True, "ok"


def pick_worker(room_id):
    """Return (url, health) for an idle worker, preferring the least loaded."""
    free = []
    for url in RECORDER_URLS:
        h = worker_health(url)
        if not h:
            continue
        if h.get("busy"):
            if _worker_busy.get(url) == room_id:
                return url, h
            continue
        free.append((url, h))
    if not free:
        return None, None
    # a worker whose profile already has storageState is faster on first frame
    free.sort(key=lambda item: (not item[1].get("profile", {}).get("exists", False), item[0]))
    return free[0]


def persist():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(_jobs, fh)
        os.chmod(STATE_FILE, 0o600)
    except OSError:
        pass


def load_state():
    global _jobs
    if os.path.isfile(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                _jobs = json.load(fh)
        except (OSError, json.JSONDecodeError):
            _jobs = {}


def _reconcile():
    """Intentionally a NOOP.

    Kept as a named hook so nobody 'fixes' the missing LiveKit Egress sync by
    adding one back: this plugin never talks to LiveKit Egress, and worker
    /status already carries the truth. See module docstring.
    """
    return None


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

@app.get("/health")
def health():
    workers = []
    for url in RECORDER_URLS:
        h = worker_health(url)
        workers.append({"url": url, "ok": bool(h),
                        "busy": (h or {}).get("busy"),
                        "profile": (h or {}).get("profile", {}).get("exists")})
    return jsonify({
        "ok": True,
        "mode": "adopt" if ADOPT else "overlay",
        "domain": DOMAIN,
        "lang": LANG,
        "min_power_level": MIN_POWER,
        "workers": workers,
        "active_jobs": sum(1 for j in _jobs.values() if j.get("state") == "recording"),
    })


@app.get("/api/record/status")
def status():
    # Accept every historical spelling: the button sends roomId, older
    # callers sent room_id, and the legacy compat controller used room.
    room_id = (request.args.get("roomId") or request.args.get("room_id")
               or request.args.get("room") or "")
    _reconcile()
    if ADOPT:
        return jsonify({"state": "unknown", "mode": "adopt", "room": room_id})
    if room_id and room_id in _jobs:
        return jsonify(poll_job(room_id))
    return jsonify({
        "rows": list(_jobs.values()),
        "active": [j for j in _jobs.values() if j.get("state") == "recording"],
    })


def poll_job(room_id):
    job = _jobs.get(room_id)
    if not job:
        return {"state": "idle", "room": room_id}
    url = job.get("worker")
    st = worker_status(url) if url else None
    if st:
        job["state"] = {"recording": "recording", "stopping": "stopping",
                        "finalized": "processing"}.get(st.get("state"), job.get("state"))
        job["elapsed"] = st.get("elapsed")
        job["file"] = st.get("file") or job.get("file")
        if st.get("exitCode") is not None and job["state"] == "processing":
            job["state"] = "published"
        _worker_busy[url] = room_id if st.get("state") == "recording" else None
        persist()
    elif job.get("state") == "recording":
        job["state"] = "error"
        job["error"] = "worker unreachable"
        persist()
    return job


@app.post("/api/record/start")
def start():
    data = request.get_json(silent=True) or {}
    room_id = data.get("roomId") or data.get("room_id")
    if not room_id:
        return jsonify({"ok": False, "error": "roomId required"}), 400

    # Authorize against Synapse, not against the request body. The body's
    # "powerLevel" field is ignored: it comes from the browser.
    token = caller_token()
    if not token:
        return jsonify({"ok": False, "error": "not signed in"}), 401
    user_id = whoami(token)
    if not user_id:
        return jsonify({"ok": False, "error": "invalid or expired session"}), 401

    verdict = room_power(token, room_id, user_id)
    if verdict is None:
        # Synapse unreachable: fail closed. A recorder that starts for an
        # unverified caller is worse than a recording that does not start.
        return jsonify({"ok": False, "error": "cannot verify permissions"}), 503
    is_member, power = verdict
    if not is_member:
        return jsonify({"ok": False, "error": "not a room member"}), 403
    if power < MIN_POWER:
        return jsonify({"ok": False, "error": "power level too low",
                        "required": MIN_POWER, "actual": power}), 403

    with _lock:
        existing = _jobs.get(room_id)
        if existing and existing.get("state") in ("recording", "stopping", "processing"):
            return jsonify({"ok": True, "state": existing["state"], "note": "already active"}), 200

        member_ok, member_detail = ensure_membership(token, room_id, user_id)
        if not member_ok:
            return jsonify({"ok": False, "error": "cannot invite recorder bot",
                            "detail": member_detail}), 403

        url, h = pick_worker(room_id)
        if not url:
            free = [u for u in RECORDER_URLS if (worker_health(u) or {}).get("busy") is False]
            busy = [u for u in RECORDER_URLS if (worker_health(u) or {}).get("busy")]
            return jsonify({
                "ok": False,
                "error": "no idle recorder",
                "workers_busy": busy,
                "workers_free": free,
            }), 503

        code, body = worker_post(url, "/start", {
            "roomId": room_id,
            "roomName": data.get("roomName", ""),
            "lang": LANG,
        })
        if code not in (200, 202):
            return jsonify({"ok": False, "error": "worker rejected start",
                            "worker": url, "detail": body}), 502

        _worker_busy[url] = room_id
        _jobs[room_id] = {
            "room": room_id,
            "roomName": data.get("roomName", ""),
            "worker": url,
            "file": body.get("file"),
            "state": "recording",
            "startedAt": int(time.time()),
            "startedBy": user_id,
        }
        persist()
        app.logger.info("recording started room=%s worker=%s", room_id, url)
        return jsonify({"ok": True, "state": "recording", "worker": url,
                        "file": body.get("file")}), 202


@app.post("/api/record/stop")
def stop():
    data = request.get_json(silent=True) or {}
    room_id = data.get("roomId") or data.get("room_id")
    if not room_id:
        return jsonify({"ok": False, "error": "roomId required"}), 400

    token = caller_token()
    if not token:
        return jsonify({"ok": False, "error": "not signed in"}), 401
    user_id = whoami(token)
    if not user_id:
        return jsonify({"ok": False, "error": "invalid or expired session"}), 401
    verdict = room_power(token, room_id, user_id)
    if verdict is None:
        return jsonify({"ok": False, "error": "cannot verify permissions"}), 503
    is_member, power = verdict
    if not is_member:
        return jsonify({"ok": False, "error": "not a room member"}), 403

    with _lock:
        job = _jobs.get(room_id)
        if not job:
            return jsonify({"ok": False, "error": "no active recording for this room"}), 404
        # Whoever started it may stop it; anyone else needs the same power
        # level that start requires. Knowing the room id is not enough.
        if job.get("startedBy") not in (user_id, "") and power < MIN_POWER:
            return jsonify({"ok": False, "error": "power level too low"}), 403
        url = job.get("worker")
        code, body = worker_post(url, "/stop", {})
        if code not in (200, 202):
            return jsonify({"ok": False, "error": "worker rejected stop",
                            "detail": body}), 502
        job["state"] = "stopping"
        persist()
        app.logger.info("recording stopping room=%s worker=%s", room_id, url)
        return jsonify({"ok": True, "state": "stopping"}), 202


if __name__ == "__main__":
    load_state()
    app.logger.info("controller: %d workers, min power %d, mode %s",
                    len(RECORDER_URLS), MIN_POWER, "adopt" if ADOPT else "overlay")
    if ADOPT:
        app.logger.info("adopt mode: forwarding is done by the proxy container, "
                        "this instance only serves /health")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
