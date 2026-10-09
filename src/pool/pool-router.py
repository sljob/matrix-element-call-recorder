
import hmac
import json
import os
import re
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlsplit

TOKEN = os.environ["RECORDER_API_TOKEN"]
SYN = os.environ["SYNAPSE_URL"].rstrip("/")
WORKERS = json.loads(os.environ["POOL_WORKERS"])
LOCK = threading.RLock()
OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def http(url, data=None, headers=None, timeout=10):
    req = urllib.request.Request(
        url, data=None if data is None else json.dumps(data).encode(),
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with OPEN.open(req, timeout=timeout) as res:
            return res.status, json.load(res)
    except urllib.error.HTTPError as error:
        try:
            data = json.loads(error.read())
        except Exception:
            data = {"error": "upstream HTTP error"}
        return error.code, data

def snapshot():
    result = []
    for worker in WORKERS:
        code, data = http(worker["url"] + "/health")
        if code != 200 or data.get("status") != "ok":
            raise RuntimeError("worker unavailable: " + worker["name"])
        active = data.get("active")
        if not isinstance(active, list):
            raise RuntimeError("invalid worker health")
        result.append((worker, active))
    return result

def dispatch(action, data, caller=""):
    room = data.get("room")
    if not isinstance(room, str) or not re.fullmatch(r"![^\s/]+", room):
        return 400, {"error": "invalid Matrix room ID"}
    with LOCK:
        states = snapshot()
        owners = [w for w, jobs in states
                  if any(j.get("room") == room for j in jobs)]
        if len(owners) > 1:
            return 503, {"error": "duplicate room ownership"}
        if action == "start":
            if owners:
                return 409, {"error": "already recording"}
            free = [w for w, jobs in states if not jobs]
            if not free:
                return 409, {"error": "all recorder workers are busy"}
            if not caller.startswith("Bearer "):
                return 401, {"error": "Matrix authorization required"}
            worker = free[0]
            encoded = quote(room, safe="")
            headers = {"Authorization": caller}
            code, membership = http(
                SYN + "/_matrix/client/v3/rooms/" + encoded
                + "/state/m.room.member/" + quote(worker["user_id"], safe=""),
                headers=headers)
            if code != 200 or membership.get("membership") not in ("join", "invite"):
                code, response = http(
                    SYN + "/_matrix/client/v3/rooms/" + encoded + "/invite",
                    {"user_id": worker["user_id"]}, headers)
                if code != 200:
                    return 403, {"error": "cannot invite selected recorder"}
            # Не допускаем одинаковых timestamp-имён при быстрых стартах.
            time.sleep(0.005)
            return http(worker["url"] + "/start", data,
                        {"Authorization": "Bearer " + TOKEN}, 25)
        if action == "stop":
            if not owners:
                return 404, {"error": "no recording for this room"}
            return http(owners[0]["url"] + "/stop", data,
                        {"Authorization": "Bearer " + TOKEN}, 75)
        if action == "status":
            return 200, {"room": room, "recording": bool(owners)}
        return 404, {"error": "not found"}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Не пишем заголовки/токены/тело запроса в журнал.
        print("POOL", self.command, urlsplit(self.path).path, flush=True)

    def reply(self, code, data):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if urlsplit(self.path).path != "/health":
            return self.reply(404, {"error": "not found"})
        try:
            with LOCK:
                states = snapshot()
                active = []
                for worker, jobs in states:
                    for job in jobs:
                        active.append({**job, "worker": worker["name"]})
                self.reply(200, {
                    "status": "ok", "capacity": len(WORKERS),
                    "active": active,
                })
        except Exception:
            self.reply(503, {"error": "recorder pool unavailable"})

    def do_POST(self):
        auth = self.headers.get("Authorization", "")
        if not hmac.compare_digest(auth, "Bearer " + TOKEN):
            return self.reply(401, {"error": "unauthorized"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > 16384:
                return self.reply(400, {"error": "invalid request size"})
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                return self.reply(400, {"error": "invalid JSON"})
            action = urlsplit(self.path).path.lstrip("/")
            code, response = dispatch(
                action, data, self.headers.get("X-Matrix-Authorization", ""))
            self.reply(code, response)
        except (ValueError, TypeError):
            self.reply(400, {"error": "invalid request"})
        except Exception:
            self.reply(503, {
                "error": "worker response not confirmed; check recording status"
            })

if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ["PORT"])),
                        Handler).serve_forever()
