#!/usr/bin/env python3
"""Authenticated, serialized admission to independent Matrix recording workers."""
import concurrent.futures
import hmac
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ['RECORDER_API_TOKEN']
WORKERS = json.loads(os.environ['POOL_WORKERS'])
SYNAPSE = os.environ.get('SYNAPSE_URL', 'http://synapse:8008')
LOCK = threading.RLock()
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def request(url, data=None, token=TOKEN, timeout=5):
    req = urllib.request.Request(url, data=None if data is None else json.dumps(data).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
    try:
        with OPENER.open(req, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try: result = json.load(error)
        except Exception: result = {'error': 'HTTP ' + str(error.code)}
        return error.code, result
    except Exception:
        return 502, {'error': 'upstream unavailable'}

def snapshot():
    def read(w):
        code, data = request(w['url'] + '/health')
        if code != 200 or data.get('status') != 'ok' or not isinstance(data.get('active'), list):
            return w, None
        return w, data
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(WORKERS)) as pool:
        return list(pool.map(read, WORKERS))

def invite(w, room, authorization):
    if not authorization.startswith('Bearer '):
        return 403, {'error': 'Matrix authorization required'}
    token = authorization[7:]
    path = SYNAPSE + '/_matrix/client/v3/rooms/' + urllib.parse.quote(room, safe='')
    code, data = request(path + '/state/m.room.member/' + urllib.parse.quote(w['user_id'], safe=''), token=token, timeout=10)
    if code == 200 and data.get('membership') in ('join', 'invite'):
        return 200, {}
    return request(path + '/invite', {'user_id': w['user_id']}, token=token, timeout=10)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def reply(self, code, data):
        raw = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try: self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError): pass
    def authorized(self):
        return hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + TOKEN)
    def do_GET(self):
        if self.path != '/health': return self.reply(404, {'error': 'not found'})
        if not self.authorized(): return self.reply(401, {'error': 'unauthorized'})
        with LOCK:
            states = snapshot()
        ready = sum(data is not None for _, data in states)
        active = [dict(job, worker=w['name']) for w, data in states if data for job in data['active']]
        self.reply(200 if ready == len(WORKERS) else 503,
            {'status': 'ok' if ready == len(WORKERS) else 'degraded', 'active': active,
             'workers': len(WORKERS), 'ready': ready})
    def do_POST(self):
        if not self.authorized(): return self.reply(401, {'error': 'unauthorized'})
        if self.path not in ('/start', '/stop'): return self.reply(404, {'error': 'not found'})
        try:
            n = int(self.headers.get('Content-Length', '0'))
            if not 0 < n <= 8192: raise ValueError()
            obj = json.loads(self.rfile.read(n))
            room = obj.get('room')
            if not isinstance(room, str) or not room.startswith('!') or '/' in room or any(c.isspace() for c in room): raise ValueError()
        except Exception: return self.reply(400, {'error': 'invalid room or JSON'})
        # Admission, invitation and start form one critical section. State is
        # reconstructed from workers on every operation, including after restart.
        with LOCK:
            states = snapshot()
            owners = [w for w, data in states if data and any(j.get('room') == room for j in data['active'])]
            if self.path == '/start':
                if owners: return self.reply(409, {'error': 'recording already active'})
                if any(data is None for _, data in states):
                    return self.reply(503, {'error': 'worker unavailable; allocation suspended'})
                free = [w for w, data in states if not data['active']]
                if not free: return self.reply(409, {'error': 'all recording workers are busy'})
                worker = free[0]
                code, result = invite(worker, room, self.headers.get('X-Matrix-Authorization', ''))
                if code != 200: return self.reply(code, {'error': 'cannot invite selected recorder', 'detail': result})
                code, result = request(worker['url'] + '/start', obj, timeout=20)
                # Never retry a timed-out start on another worker.
                return self.reply(code, result)
            if len(owners) > 1: return self.reply(409, {'error': 'multiple workers own this room'})
            if not owners:
                code = 503 if any(data is None for _, data in states) else 404
                return self.reply(code, {'error': 'recording not located'})
            code, result = request(owners[0]['url'] + '/stop', obj, timeout=80)
            if code == 200 and result.get('closed') is not True:
                return self.reply(502, {'error': 'recording did not finalize', 'detail': result})
            return self.reply(code, result)

if __name__ == '__main__':
    if not WORKERS or len({w['user_id'] for w in WORKERS}) != len(WORKERS):
        raise SystemExit('Worker identities must be unique')
    ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8788'))), Handler).serve_forever()
