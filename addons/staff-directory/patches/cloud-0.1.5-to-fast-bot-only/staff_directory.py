#!/usr/bin/env python3
"""Matrix Staff Directory, Python 3.10+, standard library only. AGPL-3.0-or-later."""
import argparse
import base64
import hashlib
import hmac
import html
import json
import logging
import os
from pathlib import Path
import signal
import ssl
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPSHandler, ProxyHandler
import uuid

from server_support import https_handler, NoRedirect, RecentActivity, effective_status
VERSION = "0.1.5-cloud-candidate"

LOG = logging.getLogger('staff-directory')

def enc(value):
    return quote(value, safe='')

def atomic(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        os.chmod(tmp, 0o600)
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

class APIError(Exception):
    def __init__(self, status, code):
        super().__init__(f'HTTP {status}, {code}')
        self.status, self.code = status, code

class API:
    def __init__(self, url, token='', ca=None, timeout=20):
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Invalid API URL')
        self.url, self.token, self.timeout = url.rstrip('/'), token, timeout
        context = ssl.create_default_context()
        if ca:
            context.load_verify_locations(cafile=ca)
        self.opener = build_opener(ProxyHandler({}), https_handler(context, url), NoRedirect())

    def call(self, method, path, data=None, token=None):
        headers = {'Accept': 'application/json'}
        bearer = self.token if token is None else token
        if bearer:
            headers['Authorization'] = 'Bearer ' + bearer
        body = None
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode()
            headers['Content-Type'] = 'application/json'
        request = Request(self.url + path, body, headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise APIError(502, 'RESPONSE_TOO_LARGE')
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            # Do not print response bodies: reverse proxies may include credentials.
            raise APIError(exc.code, 'API_REQUEST_FAILED') from None
        except (URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, 'reason', exc)
            code = type(reason).__name__
            if isinstance(reason, ssl.SSLCertVerificationError):
                code += ':' + str(reason.verify_code)
            raise APIError(0, code) from None

MATRIX = '/_matrix/client/v3'

def jwt_token(key, secret, grants):
    def b64(value):
        return base64.urlsafe_b64encode(value).rstrip(b'=').decode()
    now = int(time.time())
    header = b64(b'{"alg":"HS256","typ":"JWT"}')
    payload = b64(json.dumps({'iss': key, 'sub': 'staff-directory', 'nbf': now - 10,
                             'exp': now + 60, 'video': grants}, separators=(',', ':')).encode())
    msg = header + '.' + payload
    return msg + '.' + b64(hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest())

def resolve_identity(identity, users, mapping):
    if identity in mapping and mapping[identity] in users:
        return mapping[identity]
    # lk-jwt-service legacy /sfu/get uses MXID:deviceID. Longest MXID first,
    # including server ports; never match display names or localpart alone.
    for uid in sorted(users, key=len, reverse=True):
        if identity == uid or identity.startswith(uid + ':'):
            return uid
    return None

def status_for(presence, busy, call_ok, enabled, recent=False):
    icon, text, order = effective_status(presence, busy, recent)
    if enabled and not call_ok:
        text += ' · статус звонка недоступен'
    return icon, text, order


def render(rows, warning=''):
    rows = sorted(rows, key=lambda r: (r[1].casefold(), r[0]))
    heading = f'Сотрудники: {len(rows)}'
    lines = [heading]
    parts = ['<h3>' + html.escape(heading) + '</h3>']
    if warning:
        lines.append(warning)
        parts.append('<p>' + html.escape(warning) + '</p>')
    parts.append('<ul>')
    for uid, name, icon, text, _ in rows:
        lines.append(f'{icon} {name} — {text} ({uid})')
        parts.append(f'<li>{icon} <b>{html.escape(name)}</b> — '
                     f'{html.escape(text)} <code>{html.escape(uid)}</code></li>')
    parts.append('</ul>')
    return {'msgtype': 'm.notice', 'body': '\n'.join(lines),
            'format': 'org.matrix.custom.html', 'formatted_body': ''.join(parts)}

class Directory:
    def __init__(self, cfg):
        self.cfg = cfg
        self.matrix = API(cfg['homeserver'], cfg['access_token'], cfg.get('ca_file'))
        from device_snapshot import DeviceSnapshot
        self.devices = DeviceSnapshot(cfg)
        self.room = cfg['room_id']
        self.prefix = MATRIX + '/rooms/' + enc(self.room)
        self.statefile = Path(cfg['state_dir']) / 'state.json'
        self.state = json.loads(self.statefile.read_text()) if self.statefile.exists() else {}
        if self.state.get('room_id') not in (None, self.room):
            raise ValueError('State belongs to another room')
        self.state['room_id'] = self.room
        self.last = None
        self.last_semantic = None
        self.last_published_at = 0
        self.running = True

    def stop(self, *unused):
        self.running = False

    def publish(self, content):
        digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        if digest == self.last:
            return
        event_id = self.state.get('event_id')
        payload = content
        if event_id:
            payload = dict(content)
            payload['body'] = '* ' + content['body']
            payload['m.new_content'] = content
            payload['m.relates_to'] = {'rel_type': 'm.replace', 'event_id': event_id}
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 58000:
            raise ValueError('Directory event too large; reduce membership or name lengths')
        result = self.matrix.call('PUT', self.prefix + '/send/m.room.message/' + uuid.uuid4().hex, payload)
        if not event_id:
            self.state['event_id'] = result['event_id']
        self.state['last_edit_event_id'] = result['event_id']
        self.state['last_publish_ts'] = time.time()
        atomic(self.statefile, self.state)
        try:
            pins = self.matrix.call('GET', self.prefix + '/state/m.room.pinned_events/')
        except APIError as exc:
            if exc.status != 404:
                raise
            pins = {}
        ids = pins.get('pinned', [])
        if self.state['event_id'] not in ids:
            self.matrix.call('PUT', self.prefix + '/state/m.room.pinned_events/',
                             {'pinned': ids + [self.state['event_id']]})
        self.last = digest

    def calls(self, users):
        cfg = self.cfg.get('livekit')
        if not cfg or not cfg.get('url'):
            return set(), True, 0
        endpoint = cfg['url']
        mapping = dict(cfg.get('identity_map', {}))
        if cfg.get('adapter_file'):
            try:
                snapshot = json.loads(Path(cfg['adapter_file']).read_text())
                if not 0 <= time.time() - snapshot['updated_at'] <= 180:
                    raise ValueError('Stale adapter')
                mapping.update(snapshot.get('identities', {}))
                if endpoint == 'docker-adapter':
                    endpoint = snapshot['livekit_url']
            except (OSError, ValueError, KeyError):
                raise APIError(0, 'ADAPTER_UNAVAILABLE') from None
        api = API(endpoint, ca=self.cfg.get('ca_file'))
        def call(method, data, grants):
            token = jwt_token(cfg['api_key'], cfg['api_secret'], grants)
            return api.call('POST', '/twirp/livekit.RoomService/' + method, data, token)
        rooms = call('ListRooms', {}, {'roomList': True}).get('rooms', [])
        busy, unknown = set(), 0
        for room in rooms:
            name = room['name']
            data = call('ListParticipants', {'room': name}, {'roomAdmin': True, 'room': name})
            for participant in data.get('participants', []):
                # protobuf JSON encodes this enum as ACTIVE or integer 2.
                if participant.get('state') not in ('ACTIVE', 2):
                    continue
                identity = participant.get('identity', '')
                uid = resolve_identity(identity, users, mapping)
                if uid:
                    busy.add(uid)
                elif identity not in cfg.get('ignore_identities', []):
                    unknown += 1
        return busy, True, unknown

    def tick(self):
        # Reject accidental use in an encrypted room (bot does not implement E2EE).
        try:
            self.matrix.call('GET', self.prefix + '/state/m.room.encryption/')
        except APIError as exc:
            if exc.status != 404:
                raise
        else:
            raise ValueError('Encrypted room is unsupported; create the dedicated room with setup.py')
        members = self.matrix.call('GET', self.prefix + '/joined_members')['joined']
        excluded = set(self.cfg.get('exclude_users', [])) | {self.cfg['bot_user_id']}
        users = {uid: value for uid, value in members.items() if uid not in excluded}
        if len(users) > self.cfg.get('max_members', 300):
            raise ValueError('Room exceeds max_members; increase capacity explicitly')
        warning = ''
        try:
            busy, call_ok, unknown = self.calls(users)
            if unknown:
                warning = ('LiveKit: есть участники без сопоставления с сотрудниками. '
                           'Статус конференции для них не определён.')
        except APIError:
            busy, call_ok = set(), False
            warning = 'LiveKit недоступен: участие в конференциях сейчас не подтверждено.'
        enabled = bool(self.cfg.get('livekit', {}).get('url'))
        if not enabled:
            warning = 'Определение видеоконференций не подключено.'
        warning += ' · Недавно в сети — обращение устройства за 10 мин; возможна фоновая синхронизация. Не проверка блокировки экрана.'
        rows = []
        for uid, member in users.items():
            try:
                data = self.matrix.call('GET', MATRIX + '/presence/' + enc(uid) + '/status')
                presence = data.get('presence')
            except APIError:
                presence = None
            icon, text, order = self.devices.status(uid, presence, uid in busy, call_ok, enabled, status_for)
            from device_origin import suffix
            text += suffix(Path(self.cfg['state_dir']) / 'devices.json', uid, time.time())
            rows.append((uid, member.get('display_name') or uid, icon, text, order))
        semantic = hashlib.sha256(json.dumps(render(rows, warning), sort_keys=True).encode()).hexdigest()
        if semantic == self.last_semantic and time.time() - self.last_published_at < int(self.cfg.get('heartbeat_seconds', 900)):
            return
        stamp = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())
        warning += f'\nПроверено: {stamp}. Плановое подтверждение не реже 15 минут; старше 20 минут — данные устарели.'
        self.publish(render(rows, warning))
        self.last_semantic = semantic
        self.last_published_at = time.time()
        LOG.info('Updated directory: %d members; LiveKit=%s', len(users), 'ok' if call_ok and enabled else 'not available')

    def run(self, once=False):
        failures = 0
        while self.running:
            start = time.monotonic()
            try:
                self.tick()
                failures = 0
            except (APIError, ValueError, KeyError, OSError) as exc:
                failures += 1
                LOG.error('Update failed (%s). Last message timestamp remains unchanged.', str(exc) if isinstance(exc, APIError) else type(exc).__name__)
                if once:
                    return 1
            if once:
                return 0
            delay = max(int(self.cfg.get('poll_seconds', 60)), min(300, failures * 30))
            while self.running and time.monotonic() - start < delay:
                time.sleep(1)
        return 0

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/etc/matrix-staff-directory/config.json')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    cfg = json.loads(Path(args.config).read_text())
    from credentials import publisher_config
    cfg = publisher_config(cfg)
    worker = Directory(cfg)
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    return worker.run(args.once)

if __name__ == '__main__':
    sys.exit(main())
