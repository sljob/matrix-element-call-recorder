// ==========================================================================
//  recorder worker — HTTP shim around the pinned p63-observer.js
//
//  One container per worker. Runs with --network=host, so the only thing
//  keeping this port from being an open "start recording" button is
//  RECORDER_TOKEN, checked on every state-changing call.
//
//  Endpoints (all under the token check except /health):
//    GET  /health              liveness + profile/login state
//    POST /start               {roomId, roomName, lang} -> spawn observer
//    POST /stop                graceful finalize
//    GET  /status              {state, startedAt, output, error}
// ==========================================================================
'use strict';

const http = require('http');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const PORT = parseInt(process.env.PORT || '8788', 10);
const TOKEN = process.env.RECORDER_TOKEN || '';
const USER = process.env.RECORDER_USER || '';
// Password is read once here and handed to the observer below. The observer
// expects REC_PASS; compose provides RECORDER_PASSWORD. Passing it through
// explicitly is what makes login work (#06).
const PASSWORD = process.env.RECORDER_PASSWORD || '';
// Host-network worker cannot use docker DNS (#07): compose passes a loopback
// Synapse URL, the observer needs it as SYNAPSE_URL with a scheme (#09).
const SYNAPSE_URL = process.env.SYNAPSE_HOST_URL || process.env.SYNAPSE_URL || '';
const PULSE_SRC = process.env.PULSE_SRC || 'virtual_speaker.monitor';
const SPKI = process.env.SPKI_HASH || '';
const PROFILE_DIR = process.env.PROFILE_DIR || '/profile';
const OUTPUT_DIR = process.env.OUTPUT_DIR || '/output';
const CALL_URL = process.env.ELEMENT_CALL_URL || '';
const DOMAIN = process.env.DOMAIN || '';
const CA = process.env.TLS_CA_FILE || '';
const HOST_RESOLVER = process.env.HOST_RESOLVER || '';
const OBSERVER = process.env.OBSERVER_JS || '/app/p63-observer.js';

let current = null; // {proc, roomId, file, startedAt, stopRequested}

function log(...a) {
  console.log(new Date().toISOString(), '[' + USER + ']', ...a);
}

function json(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store',
  });
  res.end(body);
}

function authorized(req) {
  if (!TOKEN) return false; // fail closed: a worker with no token is not usable
  // Accept both spellings: the controller sends X-Recorder-Token, but manual
  // curl checks and older builds used Authorization: Bearer (#08).
  let h = req.headers['x-recorder-token'] || '';
  if (!h) {
    const auth = req.headers['authorization'] || '';
    if (auth.startsWith('Bearer ')) h = auth.slice(7).trim();
  }
  // constant-time-ish compare; timing here is not a realistic attack surface
  // but there is no reason to leak length either
  if (h.length !== TOKEN.length) return false;
  let diff = 0;
  for (let i = 0; i < TOKEN.length; i++) diff |= h.charCodeAt(i) ^ TOKEN.charCodeAt(i);
  return diff === 0;
}

function profileState() {
  const p = path.join(PROFILE_DIR, 'storageState.json');
  try {
    const st = fs.statSync(p);
    return { exists: true, bytes: st.size, mtime: st.mtime.toISOString() };
  } catch {
    return { exists: false };
  }
}

function safeRoomDir(roomId) {
  // "!abc:domain" -> "abc_domain"; never let a room id escape OUTPUT_DIR
  const cleaned = String(roomId).replace(/^!/, '').replace(/[^A-Za-z0-9_.-]/g, '_');
  const dir = path.join(OUTPUT_DIR, cleaned);
  if (!dir.startsWith(path.resolve(OUTPUT_DIR))) throw new Error('bad room id');
  return dir;
}

function startRecording(body) {
  if (current) {
    return { code: 409, obj: { ok: false, error: 'already recording', room: current.roomId } };
  }
  if (!body || !body.roomId) {
    return { code: 400, obj: { ok: false, error: 'roomId required' } };
  }

  const outDir = safeRoomDir(body.roomId);
  fs.mkdirSync(outDir, { recursive: true });

  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const file = path.join(outDir, `rec-${stamp}.mp4`);

  // Names on the left are what vendor/p63-observer.js actually reads. The
  // previous mapping (RECORDER_ROOM / RECORDER_OUTPUT) never matched, so the
  // observer died with "ROOM_ID required" and REC_PASS=PLACEHOLDER (#06).
  const env = Object.assign({}, process.env, {
    REC_USER: USER,
    REC_PASS: PASSWORD,
    ROOM_ID: body.roomId,
    OUT_FILE: file,
    OUT_DIR: OUTPUT_DIR,
    EC_URL: CALL_URL,
    ELEMENT_CALL_URL: CALL_URL,
    SYNAPSE_URL: SYNAPSE_URL,
    MATRIX_HS: 'https://' + (DOMAIN || ''),
    PULSE_SRC: PULSE_SRC,
    PROFILE_DIR: PROFILE_DIR,
    DOMAIN: DOMAIN,
    DISPLAY: process.env.FFMPEG_DISPLAY || ':99',
    IGNORE_HTTPS_ERRORS: '1',
    NODE_EXTRA_CA_CERTS: CA || '',
  });
  if (body.roomName) env.RECORDER_ROOM_NAME = body.roomName;
  if (SPKI) env.SPKI_HASH = SPKI;
  // LK_INTERNAL stays inherited from compose: the observer patches LiveKit
  // websocket URLs to the address the worker reaches on the host network.
  delete env.RECORDER_PASSWORD_EPHEMERAL; // never inherited into the child's env dump

  const args = [];
  if (CA) args.push('--ignore-certificate-errors');
  if (HOST_RESOLVER) {
    // Chromium wants "MAP <host> <ip>" with a SPACE. A colon makes the rule
    // invalid and it is ignored silently, so DNS keeps failing (#18).
    const rule = HOST_RESOLVER.replace(':', ' ');
    args.push('--host-resolver-rules=MAP ' + rule);
  }
  if (SPKI) args.push('--ignore-certificate-errors-spki-list=' + SPKI);

  log('starting observer for room', body.roomId, '->', file);
  const proc = spawn(process.execPath, [OBSERVER, ...args], {
    cwd: '/app',
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  const tag = '[' + path.basename(file) + ']';
  proc.stdout.on('data', (d) => process.stdout.write(tag + ' ' + d));
  proc.stderr.on('data', (d) => process.stderr.write(tag + ' ' + d));

  current = { proc, roomId: body.roomId, file, startedAt: Date.now(), stopRequested: false };

  proc.on('exit', (code, sig) => {
    log('observer exited', code, sig);
    if (current && current.proc === proc) {
      current.exited = true;
      current.exitCode = code;
    }
  });

  return { code: 202, obj: { ok: true, room: body.roomId, file: path.basename(file) } };
}

function stopRecording() {
  if (!current) return { code: 409, obj: { ok: false, error: 'not recording' } };
  if (current.stopRequested) return { code: 202, obj: { ok: true, state: 'stopping' } };

  current.stopRequested = true;
  log('stopping, sending SIGINT to observer');
  try {
    current.proc.kill('SIGINT');
  } catch (e) {
    log('kill failed', e.message);
  }

  return { code: 202, obj: { ok: true, state: 'stopping', file: path.basename(current.file) } };
}

function status() {
  if (!current) return { state: 'idle', profile: profileState(), user: USER };
  const st = {
    state: current.stopRequested ? (current.exited ? 'finalized' : 'stopping') : 'recording',
    room: current.roomId,
    file: path.basename(current.file),
    elapsed: Math.round((Date.now() - current.startedAt) / 1000),
    exitCode: current.exitCode,
    profile: profileState(),
    user: USER,
  };
  if (current.exited) {
    // give the uploader a moment, then free the slot for the next recording
    setTimeout(() => { if (current && current.exited) current = null; }, 5000);
  }
  return st;
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');

  if (url.pathname === '/health') {
    return json(res, 200, {
      ok: true,
      user: USER,
      busy: !!current,
      profile: profileState(),
      callUrl: CALL_URL,
    });
  }

  if (!authorized(req)) {
    log('unauthorized request from', req.socket.remoteAddress, url.pathname);
    return json(res, 401, { ok: false, error: 'unauthorized' });
  }

  if (url.pathname === '/status') return json(res, 200, status());

  if (url.pathname === '/stop' && req.method === 'POST') {
    const r = stopRecording();
    return json(res, r.code, r.obj);
  }

  if (url.pathname === '/start' && req.method === 'POST') {
    let raw = '';
    req.on('data', (c) => {
      raw += c;
      if (raw.length > 1e6) req.destroy();
    });
    req.on('end', () => {
      let body = {};
      try { body = JSON.parse(raw || '{}'); } catch { }
      const r = startRecording(body);
      json(res, r.code, r.obj);
    });
    return;
  }

  json(res, 404, { ok: false, error: 'not found' });
});

server.listen(PORT, '0.0.0.0', () => {
  log('worker listening on', PORT, 'callUrl=' + CALL_URL, 'profile=' + PROFILE_DIR);
  if (!TOKEN) log('WARNING: RECORDER_TOKEN empty — every request will be rejected');
});
