import http from 'http';
import { spawn, execFile, execSync } from 'child_process';
import fs from 'fs';
const PORT = parseInt(process.env.PORT || '8788', 10);
const BIND = process.env.BIND || '0.0.0.0';
const OUT  = process.env.OUT_DIR || '/out';
const SYN = process.env.SYNAPSE_URL || 'http://element-synapse-1:8008';
const REC_USER = process.env.REC_USER || 'recorder';
const REC_PASS = process.env.REC_PASS || 'Milorada2026!';
const jobs = new Map();
const TRACE = `${OUT}/stop-trace.log`;
const trace = (...a) => { const line = `[${new Date().toISOString()}] [SRV] ${a.join(' ')}\n`; try { fs.appendFileSync(TRACE, line); } catch {} process.stdout.write(line); };
const json = (res, code, obj) => { if (res.headersSent) return; res.writeHead(code, {'Content-Type':'application/json'}); res.end(JSON.stringify(obj)); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const sh = (cmd, args) => new Promise(r => execFile(cmd, args, {timeout:10000}, (e, so) => r(String(so || '').trim())));
const ffPids = async (file) => { const base = file.split('/').pop(); const out = await sh('pgrep', ['-f', `ffmpeg.*${base}`]); return out ? out.split('\n').map(s => parseInt(s,10)).filter(Boolean) : []; };
const fileDuration = async (file) => { const d = await sh('ffprobe', ['-v','error','-show_entries','format=duration','-of','default=nk=1:nw=1', file]); const n = parseFloat(d); return n > 0 ? n : 0; };
const alive = pid => { try { process.kill(pid, 0); return true; } catch { return false; } };

// Matrix API: login + clear call.member

// MATRIX_HANGUP_V06
async function matrixLogin() {
  for (let _a = 0; _a < 3; _a++) {
    const r = await fetch(SYN + '/_matrix/client/v3/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        type: 'm.login.password',
        identifier: {type: 'm.id.user', user: REC_USER},
        password: REC_PASS,
        device_id: 'RECORDER001'
      }),
      signal: AbortSignal.timeout(10000)
    });
    const d = await r.json();
    if (r.ok) return {token: d.access_token, userId: d.user_id};
    if (r.status === 429 && _a < 2) { trace('hangup: 429, retry 3s'); await sleep(3000); continue; }
    throw new Error('Login failed: ' + r.status);
  }
}

async function matrixHangup(room) {
  trace('hangup: clearing call.member for room=' + room);
  try {
    const auth = await matrixLogin();
    const statePath = '/_matrix/client/v3/rooms/'
      + encodeURIComponent(room) + '/state';
    const r = await fetch(SYN + statePath, {
      headers: {Authorization: 'Bearer ' + auth.token}
    });
    const events = await r.json();
    let cleared = 0;
    for (const ev of events) {
      if (ev.type === 'org.matrix.msc3401.call.member'
          && ev.sender === auth.userId
          && Object.keys(ev.content || {}).length) {
        await fetch(SYN + statePath + '/org.matrix.msc3401.call.member/'
          + encodeURIComponent(ev.state_key), {
          method: 'PUT',
          headers: {Authorization: 'Bearer ' + auth.token,
                    'Content-Type': 'application/json'},
          body: '{}'
        });
        cleared++;
      }
    }
    trace('hangup: cleared ' + cleared + ' call.member entries');
  } catch (e) {
    trace('hangup: ERROR ' + e.message);
  }
}

async function stopJob(job, room) {
  const file = job.file;
  trace(`STOP room=${room} file=${file}`);
  try { job.proc.kill('SIGINT'); trace('SIGINT -> observer', job.proc.pid); } catch (e) { trace('SIGINT observer ОШИБКА:', e.message); }
  await sleep(2000);
  let pids = await ffPids(file);
  trace('ffmpeg pids:', pids.join(',') || '(нет)');
  if (!pids.length) {
    try { fs.statSync(file); } catch {
      trace('ffmpeg не запущен, файл не существует — быстрый выход');
      if (alive(job.proc.pid)) { try { job.proc.kill('SIGTERM'); } catch {} for (let _w=0; _w<10; _w++) { await sleep(1000); if (!alive(job.proc.pid)) break; } if (alive(job.proc.pid)) { try { job.proc.kill('SIGKILL'); trace('observer SIGKILL'); } catch {} } }
      await matrixHangup(room);
      return 0;
    }
    let dur = await fileDuration(file);
    if (dur) { trace(`файл уже закрыт, длительность ${dur}s`); await matrixHangup(room); return dur; }
  }
  for (const p of pids) { try { process.kill(p, 'SIGINT'); trace('SIGINT -> ffmpeg', p); } catch (e) { trace('ffmpeg', p, 'ОШИБКА:', e.message); } }
  let dur = 0;
  for (let i = 1; i <= 30; i++) {
    await sleep(1000);
    pids = await ffPids(file);
    if (!pids.length) { dur = await fileDuration(file); if (dur) { trace(`файл закрыт за ${i}s, длительность ${dur}s`); break; } }
    if (i === 15 && pids.length) { for (const p of pids) { try { process.kill(p,'SIGTERM'); trace('SIGTERM ->', p); } catch {} } }
  }
  if (!dur) trace('ВНИМАНИЕ: файл не закрылся за 30s');
  if (alive(job.proc.pid)) { trace('observer жив, SIGTERM'); try { job.proc.kill('SIGTERM'); } catch {} await sleep(3000); if (alive(job.proc.pid)) { try { job.proc.kill('SIGKILL'); trace('observer SIGKILL'); } catch {} } }
  await matrixHangup(room);
  return dur;
}
const server = http.createServer((req, res) => { let body = ''; req.on('data', c => body += c); req.on('end', async () => { const url = req.url.split('?')[0]; if (url !== '/health') trace(`REQ ${req.method} ${req.url} from=${req.socket.remoteAddress} body=${body.slice(0,200)}`); if (url === '/health') { const act = []; for (const [r, j] of jobs) act.push({room:r, file:j.file, seconds: Math.round((Date.now()-j.startedAt)/1000), observerAlive: alive(j.proc.pid)}); return json(res, 200, {status:'ok', active: act}); } let data = {}; try { data = body ? JSON.parse(body) : {}; } catch {} const room = data.room;
if (url !== '/health' &&
    req.headers.authorization !== 'Bearer ' + process.env.RECORDER_API_TOKEN)
  return json(res, 401, {error:'unauthorized'});
if (room !== undefined &&
    (typeof room !== 'string' || !/^![^\s/]+$/.test(room)))
  return json(res, 400, {error:'invalid Matrix room ID'}); if (url === '/start' && req.method === 'POST') { if (!room) return json(res, 400, {error:'room is required'}); if (jobs.size) return json(res, 409, {error:'recorder is busy'}); const file = `${OUT}/rec-${Date.now()}.mp4`; 
{
  const metaPath = file.replace(/\.mp4$/, '.meta');
  const temporary = metaPath + '.tmp';
  try {
    fs.writeFileSync(temporary, JSON.stringify({
      room_id: room,
      started_at: Math.floor(Date.now() / 1000)
    }), {mode: 0o644});
    fs.chmodSync(temporary, 0o644);
    fs.renameSync(temporary, metaPath);
  } catch (error) {
    try { fs.unlinkSync(temporary); } catch {}
    console.error('[META] ' + error.message);
    return json(res, 500, {
      error: 'cannot persist recording metadata'
    });
  }
}

fs.writeFileSync(file + '.active', ''); const proc = spawn('node', ['/app/p63-observer.js'], { env: {...process.env, DISPLAY: ':99', PULSE_SERVER: 'unix:/tmp/pulse/native', OUT_DIR: OUT, EC_URL: process.env.EC_URL || 'http://127.0.0.1:8090', MATRIX_HS: process.env.MATRIX_HS || 'https://meet.milorada.ru', LK_INTERNAL: process.env.LK_INTERNAL || '172.21.0.7:7880', ROOM_ID: room, OUT_FILE: file, RECORD_SECS: String(data.maxSeconds || process.env.RECORD_SECS || 7200), WAIT_VID: process.env.WAIT_VID || '30', REQUIRE_REMOTE: process.env.REQUIRE_REMOTE || '0', NO_AUDIO: process.env.NO_AUDIO || '0', PULSE_SRC: process.env.PULSE_SRC || 'recsink.monitor', MIN_WAIT: process.env.MIN_WAIT || '12'}, stdio: ['ignore','pipe','pipe'], detached: false }); const log = fs.createWriteStream(`${OUT}/job-${Date.now()}.log`); proc.stdout.on('data', d => { const m = String(d).match(/\[REC_FILE\]\s+(\S+)/); if (m && jobs.has(room)) jobs.get(room).file = m[1]; }); proc.stdout.pipe(log); proc.stderr.pipe(log); proc.on('exit', code => { trace(`observer exit=${code} room=${room}`); jobs.delete(room); try { fs.unlinkSync(file + '.active'); } catch {} log.end(); }); jobs.set(room, {proc, file, startedAt: Date.now()}); trace(`START room=${room} file=${file} pid=${proc.pid}`); return json(res, 200, {status:'started', room, file}); } if (url === '/stop' && req.method === 'POST') { const job = jobs.get(room); if (!job) return json(res, 404, {error:'no recording for this room'}); const elapsed = Math.round((Date.now()-job.startedAt)/1000); const dur = await stopJob(job, room); jobs.delete(room); return json(res, 200, {status:'stopped', room, elapsedSeconds: elapsed, file: job.file, durationSeconds: dur, closed: dur > 0}); } if (url === '/status') return json(res, 200, {room, recording: jobs.has(room)}); json(res, 404, {error:'not found'}); }); });
server.listen(PORT, BIND, () => trace(`listening on ${BIND}:${PORT}`));
