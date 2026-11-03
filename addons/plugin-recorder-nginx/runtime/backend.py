"""Matrix-authorized API and archive. All durable job ownership is stored in SQLite."""
import hashlib
from contextlib import contextmanager
import html
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import secrets
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

STATE = Path(os.environ.get('STATE_DIR', '/state'))
PUBLIC = Path(os.environ.get('PUBLIC_DIR', '/public'))
HS = os.environ.get('SYNAPSE_URL', 'http://127.0.0.1:8008').rstrip('/')
WORKER = os.environ.get('WORKER_URL', 'http://127.0.0.1:8789')
BOT = os.environ.get('RECORDER_USER', '')
TOKEN = os.environ.get('RECORDER_TOKEN', '')
MIN_POWER = int(os.environ.get('RECORDING_MIN_POWER_LEVEL', '50'))
VIEW_POWER = int(os.environ.get('RECORDINGS_MIN_POWER_LEVEL', '50'))
CA = os.environ.get('TLS_CA_FILE') or None
TLS = ssl.create_default_context(cafile=CA)
LOCK = threading.RLock()


class APIError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def remote(url, method='GET', body=None, headers=None):
    request = urllib.request.Request(url, method=method, headers=headers or {},
        data=json.dumps(body).encode() if body is not None else None)
    request.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(request, timeout=20, context=TLS) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise APIError(error.code, 'upstream HTTP ' + str(error.code)) from error
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise APIError(503, 'upstream unavailable') from error


def matrix(token, path, method='GET', body=None):
    return remote(HS + '/_matrix/client/v3/' + path, method, body,
                  {'Authorization': 'Bearer ' + token})


def worker(path, method='GET', body=None):
    return remote(WORKER + path, method, body, {'X-Recorder-Token': TOKEN})


@contextmanager
def database():
    db = sqlite3.connect(STATE / 'plugin.sqlite', timeout=20)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize():
    STATE.mkdir(parents=True, exist_ok=True)
    with database() as db:
        db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS owners(room TEXT PRIMARY KEY, uid TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, token TEXT NOT NULL, expires REAL NOT NULL);
        ''')
    (STATE / 'plugin.sqlite').chmod(0o600)


def permissions(token, uid, room):
    q = urllib.parse.quote(room, safe='')
    try:
        events = matrix(token, 'rooms/' + q + '/state')
    except APIError as error:
        if error.code in (401, 403):
            return False, 0
        raise
    state = {(e['type'], e.get('state_key', '')): e for e in events}
    member = state.get(('m.room.member', uid), {}).get('content', {}).get('membership') == 'join'
    pl = state.get(('m.room.power_levels', ''))
    create = state.get(('m.room.create', ''), {})
    content = create.get('content', {})
    creator = create.get('sender') or content.get('creator')
    if content.get('room_version') == '12' and uid in [creator] + content.get('additional_creators', []):
        level = float('inf')
    elif pl is None:
        level = 100 if uid == creator else 0
    else:
        power = pl.get('content', {})
        level = power.get('users', {}).get(uid, power.get('users_default', 0))
    return member, level if isinstance(level, (int, float)) else 0


def entries():
    result = []
    for marker in PUBLIC.glob('*/*.json'):
        try:
            item = json.loads(marker.read_text())
            if item.get('room_id') and item.get('file'):
                result.append(item)
        except (ValueError, OSError):
            pass
    return sorted(result, key=lambda e: e.get('started_at', ''), reverse=True)


def page(items, uid):
    groups = {}
    for entry in items:
        groups.setdefault(entry['room_id'], []).append(entry)
    body = '<h1>Записи конференций</h1><p>' + html.escape(uid) + '</p><form method="post" action="/recordings/logout"><button>Выйти</button></form>'
    for room, recordings in groups.items():
        title = recordings[0].get('room_name') or room
        body += '<section><h2>' + html.escape(title) + '</h2><small>' + html.escape(room) + '</small>'
        for entry in recordings:
            url = '/recordings/files/' + urllib.parse.quote(entry['file'])
            poster = '/recordings/files/' + urllib.parse.quote(entry['jpg']) if entry.get('jpg') else ''
            body += '<article><p>' + html.escape(entry['started_at']) + ' · ' + str(entry['duration']) + ' с</p>'
            body += '<video controls preload="metadata" poster="' + html.escape(poster, quote=True) + '" src="' + html.escape(url, quote=True) + '"></video>'
            body += '<p><a download href="' + html.escape(url, quote=True) + '">Скачать MP4</a>'
            if entry.get('mp3'):
                audio = '/recordings/files/' + urllib.parse.quote(entry['mp3'])
                body += ' · <a download href="' + html.escape(audio, quote=True) + '">Скачать MP3</a><br><audio controls preload="none" src="' + html.escape(audio, quote=True) + '"></audio>'
            body += '</p></article>'
        body += '</section>'
    if not groups:
        body += '<p>Доступных записей пока нет.</p>'
    return '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Записи</title><style>body{font:16px sans-serif;background:#10151d;color:#eee;max-width:1000px;margin:30px auto;padding:16px}a{color:#70baff}video{width:100%;max-height:560px}article{background:#1c2531;padding:15px;margin:15px 0;border-radius:10px}small{overflow-wrap:anywhere}audio{margin-top:12px}</style>' + body + '</html>'


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        # No access tokens, URL query strings or credentials in logs.
        return

    def send(self, code, data, content_type='application/json', extra=None):
        payload = json.dumps(data).encode() if content_type == 'application/json' else data.encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD': self.wfile.write(payload)

    def read_body(self):
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 <= length <= 16384: raise APIError(413, 'body too large')
        return self.rfile.read(length).decode()

    def token(self):
        auth = self.headers.get('Authorization', '')
        if auth.startswith('Bearer '): return auth[7:]
        c = cookies.SimpleCookie()
        try: c.load(self.headers.get('Cookie', ''))
        except cookies.CookieError: return ''
        sid = c.get('rec_session')
        if sid:
            with database() as db:
                row = db.execute('SELECT token FROM sessions WHERE id=? AND expires>?', (sid.value, time.time())).fetchone()
            if row: return row['token']
        return ''

    def identity(self):
        token = self.token()
        if not token: raise APIError(401, 'login required')
        try: uid = matrix(token, 'account/whoami')['user_id']
        except (APIError, KeyError): raise APIError(401, 'invalid Matrix session')
        return token, uid

    def check_origin(self):
        origin = self.headers.get('Origin')
        expected = 'https://' + os.environ.get('DOMAIN', '')
        if origin and origin != expected: raise APIError(403, 'cross-origin request denied')

    def do_HEAD(self): self.dispatch()
    def do_GET(self): self.dispatch()
    def do_POST(self): self.dispatch()

    def dispatch(self):
        try: self.route()
        except APIError as error: self.send(error.code, {'error': error.message})
        except (ValueError, TypeError, KeyError, json.JSONDecodeError): self.send(400, {'error':'invalid request'})
        except Exception:
            self.send(500, {'error':'internal error'})

    def route(self):
        url = urllib.parse.urlsplit(self.path)
        if self.command == 'POST': self.check_origin()
        if url.path == '/health':
            state = worker('/health')
            return self.send(200 if state.get('ok') else 503, {'ok':state.get('ok', False), 'worker':state})
        if url.path == '/recordings/login' and self.command == 'POST':
            values = urllib.parse.parse_qs(self.read_body())
            login = remote(HS + '/_matrix/client/v3/login', 'POST', {
                'type':'m.login.password', 'identifier':{'type':'m.id.user','user':values['user'][0]},
                'password':values['password'][0], 'initial_device_display_name':'Recorder archive'})
            sid = secrets.token_urlsafe(32)
            with database() as db:
                db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
                db.execute('INSERT INTO sessions VALUES(?,?,?)', (sid, login['access_token'], time.time()+28800))
            return self.send(303, '', 'text/plain', {'Location':'/recordings/', 'Set-Cookie':'rec_session='+sid+'; Path=/recordings/; Secure; HttpOnly; SameSite=Strict; Max-Age=28800'})
        if url.path == '/recordings/logout' and self.command == 'POST':
            token = self.token()
            if token:
                matrix(token, 'logout', 'POST', {})
                with database() as db: db.execute('DELETE FROM sessions WHERE token=?', (token,))
            return self.send(303, '', 'text/plain', {'Location':'/recordings/', 'Set-Cookie':'rec_session=; Path=/recordings/; Secure; HttpOnly; SameSite=Strict; Max-Age=0'})
        if url.path in ('/recordings', '/recordings/'):
            try: token, uid = self.identity()
            except APIError:
                return self.send(200, '<!doctype html><meta charset="utf-8"><h1>Архив записей</h1><form method="post" action="/recordings/login"><input name="user" required placeholder="Matrix login" autocomplete="username"><input name="password" type="password" required autocomplete="current-password"><button>Войти</button></form>', 'text/html; charset=utf-8')
            allowed = []
            rights = {}
            for entry in entries():
                room = entry['room_id']
                if room not in rights: rights[room] = permissions(token, uid, room)
                member, power = rights[room]
                if member and power >= VIEW_POWER: allowed.append(entry)
            return self.send(200, page(allowed, uid), 'text/html; charset=utf-8')
        if url.path.startswith('/recordings/files/'):
            token, uid = self.identity()
            rel = urllib.parse.unquote(url.path[len('/recordings/files/'):])
            entry = next((e for e in entries() if rel in [e.get('file'), e.get('mp3'), e.get('jpg')]), None)
            if not entry: raise APIError(404, 'not found')
            member, power = permissions(token, uid, entry['room_id'])
            if not member or power < VIEW_POWER: raise APIError(403, 'room access denied')
            return self.file(PUBLIC / rel)
        if not url.path.startswith('/api/record/'): raise APIError(404, 'not found')
        token, uid = self.identity()
        action = url.path.rsplit('/', 1)[-1]
        if action == 'status' and self.command in ('GET','HEAD'):
            args = urllib.parse.parse_qs(url.query)
            room = (args.get('roomId') or args.get('room') or [''])[0]
            member, _ = permissions(token, uid, room)
            if not member: raise APIError(403, 'not a room member')
            current = worker('/status')
            state = current.get('state','idle') if current.get('room') == room else 'idle'
            return self.send(200, {'state':state, 'room':room, 'detail':current.get('error') if state == 'error' else None})
        if self.command != 'POST' or action not in ('start','stop'): raise APIError(405, 'method not allowed')
        data = json.loads(self.read_body())
        room = data.get('roomId') or data.get('room') or ''
        if not room.startswith('!') or ':' not in room: raise APIError(400,'roomId required')
        member, power = permissions(token, uid, room)
        if not member: raise APIError(403,'not a room member')
        with LOCK:
            if action == 'start':
                if power < MIN_POWER: raise APIError(403,'power level too low')
                current = worker('/status')
                if current.get('room') == room and current.get('state') in ('starting','recording','stopping','processing'):
                    return self.send(200, {'state':current['state']})
                q = urllib.parse.quote(room, safe='')
                membership = None
                try: membership = matrix(token, 'rooms/'+q+'/state/m.room.member/'+urllib.parse.quote(BOT,safe='')).get('membership')
                except APIError as error:
                    if error.code not in (403,404): raise
                if membership not in ('join','invite'):
                    matrix(token, 'rooms/'+q+'/invite', 'POST', {'user_id':BOT})
                room_name = room
                try: room_name = matrix(token,'rooms/'+q+'/state/m.room.name').get('name', room)
                except APIError: pass
                result = worker('/start','POST', {'roomId':room,'roomName':room_name,'startedBy':uid})
                with database() as db: db.execute('INSERT OR REPLACE INTO owners VALUES(?,?)', (room,uid))
            else:
                with database() as db: owner = db.execute('SELECT uid FROM owners WHERE room=?',(room,)).fetchone()
                if power < MIN_POWER and (not owner or owner['uid'] != uid): raise APIError(403,'cannot stop recording')
                result = worker('/stop','POST',{'roomId':room})
            return self.send(202, {'state':result['state']})

    def file(self, target):
        target = target.resolve()
        if not target.is_relative_to(PUBLIC.resolve()) or not target.is_file(): raise APIError(404,'not found')
        size = target.stat().st_size
        start, end, code = 0, size-1, 200
        requested = self.headers.get('Range')
        if requested:
            import re
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
            if not match or not any(match.groups()):
                return self.send(416, '', 'text/plain', {'Content-Range':'bytes */'+str(size)})
            a,b = match.groups()
            if a: start, end = int(a), min(int(b) if b else size-1, size-1)
            else: start = max(0,size-int(b))
            if start > end or start >= size:
                return self.send(416,'','text/plain',{'Content-Range':'bytes */'+str(size)})
            code=206
        self.send_response(code)
        self.send_header('Content-Type',mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
        self.send_header('Accept-Ranges','bytes')
        self.send_header('Content-Length',str(end-start+1))
        self.send_header('Cache-Control','private, no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        if code==206: self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        self.end_headers()
        if self.command=='HEAD': return
        with target.open('rb') as f:
            f.seek(start)
            remaining=end-start+1
            while remaining:
                chunk=f.read(min(65536,remaining))
                if not chunk: break
                self.wfile.write(chunk);remaining-=len(chunk)


def main():
    initialize()
    server = ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('PORT','8765'))), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == '__main__': main()
