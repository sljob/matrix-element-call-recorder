"""Server-only optional activity and explicit local HTTPS transport. No device agents."""
import http.client
import json
import os
from pathlib import Path
import ssl
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler

class NoRedirect(HTTPRedirectHandler):
    # Never forward Matrix credentials to a redirect target.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def https_handler(context, url):
    path = Path('/etc/matrix-staff-directory/local-https.json')
    if not path.exists():
        return HTTPSHandler(context=context)
    opt = json.loads(path.read_text())
    parsed = urlsplit(url)
    if parsed.hostname != opt.get('hostname') or (parsed.port or 443) != opt.get('port', 443):
        return HTTPSHandler(context=context)
    ip = opt.get('connect_ip')
    if ip not in ('127.0.0.1', '::1'):
        raise ValueError('Only loopback local HTTPS overrides are allowed')
    if opt.get('legacy_ca'):
        # Explicit existing per-host choice; never disable hostname/certificate checks.
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    class LocalConnection(http.client.HTTPSConnection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            connect = self._create_connection
            def local(address, *args, **kwargs):
                host, port = address
                if host == opt['hostname']:
                    host = ip
                return connect((host, port), *args, **kwargs)
            self._create_connection = local
    class Handler(HTTPSHandler):
        def https_open(self, req):
            return self.do_open(LocalConnection, req, context=self._context)
    return Handler(context=context)

def effective_status(presence, busy, recent=False):
    if busy:
        return '🔴', 'Занят — видеоконференция', 0
    if presence == 'online':
        return '🟢', 'В сети', 1
    if recent:
        return '🔵', 'Недавно активен', 3
    if presence == 'unavailable':
        return '🟡', 'Отошёл', 2
    if presence == 'offline':
        return '⚪', 'Не в сети (по Matrix)', 4
    return '❔', 'Нет данных о присутствии', 5

class RecentActivity:
    """Tracks only recent message event sender/time in explicitly opted-in joined rooms."""
    def __init__(self, cfg, api):
        self.cfg, self.api = cfg, api
        self.rooms = sorted(set(cfg.get('recent_activity_rooms', [])))
        self.enabled = bool(self.rooms)
        self.path = Path(cfg['state_dir']) / 'recent-server-v1.json'
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}
        if self.data.get('rooms') != self.rooms:
            self.data = {'rooms': self.rooms, 'recent': {}}
        self.warning = ''

    def process(self, response, now):
        recent = self.data.setdefault('recent', {})
        allowed = set(self.rooms)
        joined = response.get('rooms', {}).get('join', {})
        limited = False
        for room, value in joined.items():
            if room not in allowed:
                continue
            timeline = value.get('timeline', {})
            limited |= bool(timeline.get('limited'))
            for event in timeline.get('events', []):
                if event.get('type') not in ('m.room.message', 'm.room.encrypted'):
                    continue
                sender, ts = event.get('sender'), event.get('origin_server_ts')
                if not isinstance(sender, str) or not sender.startswith('@') or sender == self.cfg['bot_user_id']:
                    continue
                if not isinstance(ts, (int, float)) or not now-300 <= ts/1000 <= now:
                    continue
                content = event.get('content', {})
                if not isinstance(content, dict):
                    continue
                if content.get('m.relates_to', {}).get('rel_type') == 'm.replace':
                    continue
                recent[sender] = max(recent.get(sender, 0), ts/1000)
        self.data['recent'] = {uid: ts for uid, ts in recent.items() if now-300 <= ts <= now}
        self.data['since'] = response['next_batch']
        self.warning = 'Часть событий активности пропущена при синхронизации.' if limited else ''

    def update(self):
        if not self.enabled:
            return
        filt = {'room': {'rooms': self.rooms, 'timeline': {'limit': 200, 'types': ['m.room.message', 'm.room.encrypted']},
                         'state': {'types': []}, 'ephemeral': {'types': []}, 'account_data': {'types': []}},
                'presence': {'types': []}, 'account_data': {'types': []}}
        params = {'timeout': 0, 'set_presence': 'offline', 'filter': json.dumps(filt)}
        if self.data.get('since'):
            params['since'] = self.data['since']
        result = self.api.call('GET', '/_matrix/client/v3/sync?' + urlencode(params))
        self.process(result, time.time())
        tmp = self.path.with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(self.data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def is_recent(self, uid):
        age = time.time() - self.data.get('recent', {}).get(uid, 0)
        return self.enabled and 0 <= age <= 300
