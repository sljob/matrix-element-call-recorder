import puppeteer from 'puppeteer-core';
import fs from 'fs';
import { spawn, execSync } from 'child_process';

let __sigReceived = false;
process.on('SIGINT', () => { __sigReceived = true; console.log('[SIGNAL] SIGINT'); });
process.on('SIGTERM', () => { __sigReceived = true; console.log('[SIGNAL] SIGTERM'); });

const EC = process.env.EC_URL || 'http://127.0.0.1:8090';
const OUT = process.env.OUT_DIR || '/out';
const HS = process.env.MATRIX_HS || 'https://meet.milorada.ru';
const SYN = process.env.SYNAPSE_URL || 'http://element-synapse-1:8008';
const LK_INT = process.env.LK_INTERNAL || '172.21.0.6:7880';
const REC_USER = process.env.REC_USER || 'recorder';
const REC_PASS = process.env.REC_PASS || 'Milorada2026!';
const REQ_REMOTE = process.env.REQUIRE_REMOTE === '1';
const MIN_WAIT = parseInt(process.env.MIN_WAIT || '0', 10);
const WAIT_V = parseInt(process.env.WAIT_VID || '20', 10);
const ROOM_ID = process.env.ROOM_ID;
const OUT_FILE = process.env.OUT_FILE;
const sleep = ms => new Promise(r=>setTimeout(r,ms));


// Fresh Matrix login; persistent browser crypto store is created by Element Call.
async function matrixRequest(path, method='GET', body, token) {
  const r = await fetch(SYN + path, {
    method,
    headers: {'Content-Type':'application/json',
      ...(token ? {Authorization:'Bearer ' + token} : {})},
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(20000)
  });
  const data = await r.json();
  if (!r.ok) throw new Error('Matrix HTTP ' + r.status + ': ' + JSON.stringify(data));
  return data;
}
const login = await matrixRequest('/_matrix/client/v3/login', 'POST', {
  type:'m.login.password',
  identifier:{type:'m.id.user', user:REC_USER},
  password:REC_PASS,
  device_id:'RECORDER001',
  initial_device_display_name:'Conference recorder'
});
const accessToken = login.access_token;
const userId = login.user_id;
const deviceId = login.device_id;
if (!ROOM_ID) throw new Error('ROOM_ID required');
await matrixRequest('/_matrix/client/v3/join/' + encodeURIComponent(ROOM_ID),
                    'POST', {}, accessToken);
console.log('[AUTH] Login and room join OK: ' + userId);
try {
  const events = await matrixRequest('/_matrix/client/v3/rooms/'
       + encodeURIComponent(ROOM_ID) + '/state', 'GET', undefined, accessToken);
  for (const ev of events) {
    if (ev.type === 'org.matrix.msc3401.call.member'
        && ev.sender === userId && Object.keys(ev.content || {}).length) {
      await matrixRequest('/_matrix/client/v3/rooms/'
        + encodeURIComponent(ROOM_ID) + '/state/org.matrix.msc3401.call.member/'
        + encodeURIComponent(ev.state_key), 'PUT', {}, accessToken);
    }
  }
} catch (e) { console.log('[CLEAN] ' + e.message); }

// BROWSER
const browser = await puppeteer.launch({
  // APP_OWNS_BROWSER_SIGNALS_V1
  handleSIGINT: false,
  handleSIGTERM: false,

  executablePath:'/usr/bin/chromium', headless:false,
  env:{...process.env, PULSE_SERVER:'unix:/tmp/pulse/native', DISPLAY:':99'},
  args:['--unsafely-treat-insecure-origin-as-secure=http://127.0.0.1:8090',
    '--allow-running-insecure-content','--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage',
    '--user-data-dir=/work/profile-spk','--autoplay-policy=no-user-gesture-required',
    '--disable-audio-output-muting','--alsa-output-device=default',
    '--disable-features=AutoplayIgnoreWebAudio','--use-fake-ui-for-media-stream',
    '--window-size=1280,720','--kiosk','--start-fullscreen',
    '--disable-infobars','--disable-session-crashed-bubble',
    '--hide-crash-restore-bubble','--no-first-run','--no-default-browser-check',
    '--window-position=0,0','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader','--ignore-gpu-blocklist','--disable-gpu-sandbox'],
});
const page = await browser.newPage();
await page.setViewport({width:1280,height:720});
const logs=[];
page.on('console', m=>logs.push(`[${m.type()}] ${m.text()}`));
page.on('pageerror', e=>logs.push(`[PAGEERROR] ${e.message}`));

// INJECT
await page.evaluateOnNewDocument((hs, tok, uid, did, lkInt) => {
  localStorage.clear();
  const session = {user_id: uid, device_id: did, access_token: tok, passwordlessUser: false, homeserver: hs, baseUrl: hs};
  localStorage.setItem('matrix-auth-store', JSON.stringify(session));
  localStorage.setItem('mx_access_token', tok);
  localStorage.setItem('mx_user_id', uid);
  localStorage.setItem('mx_device_id', did);
  localStorage.setItem('mx_hs_url', hs);
  localStorage.setItem('mx_base_url', hs);
  localStorage.setItem('mx_is_guest', 'false');
  console.log('[INJECT] Token for ' + uid);
  window.__localTracks = new Set();
  window.__lkStats = {rewrites:0, wsOpen:0, wsErr:0};
  window.__LK_INTERNAL_URL = lkInt;
  window.__pcs = [];
  const OrigPC = window.RTCPeerConnection;
  window.RTCPeerConnection = function(...args) {
    const pc = new OrigPC(...args);
    window.__pcs.push(pc);
    pc.addEventListener('iceconnectionstatechange', () => console.log('[ICE] ' + pc.iceConnectionState));
    pc.addEventListener('connectionstatechange', () => console.log('[PC] ' + pc.connectionState));
    return pc;
  };
  window.RTCPeerConnection.prototype = OrigPC.prototype;
  const origWS = WebSocket;
  window.WebSocket = function(url, protocols) {
    let newUrl = url;
    if (/livekit|rtc/.test(url)) {
      newUrl = url.replace(/^wss?:\/\/[^\/]+\/livekit-server/, 'ws://' + window.__LK_INTERNAL_URL);
      newUrl = newUrl.replace(/^wss?:\/\/[^\/]+(:\d+)?\/rtc/, 'ws://' + window.__LK_INTERNAL_URL + '/rtc');
      window.__lkStats.rewrites++;
      console.log('[WS_REWRITE] internal LiveKit');
    }
    const ws = new origWS(newUrl, protocols);
    ws.addEventListener('open', () => { window.__lkStats.wsOpen++; console.log('[WS_OPEN]'); });
    ws.addEventListener('error', () => { window.__lkStats.wsErr++; console.log('[WS_ERROR]'); });
    ws.addEventListener('close', (e) => console.log('[WS_CLOSE] code=' + e.code));
    return ws;
  };
  window.WebSocket.prototype = origWS.prototype;
  window.WebSocket.CONNECTING = origWS.CONNECTING;
  window.WebSocket.OPEN = origWS.OPEN;
  window.WebSocket.CLOSING = origWS.CLOSING;
  window.WebSocket.CLOSED = origWS.CLOSED;
  const md = navigator.mediaDevices;
  if (md) for (const fn of ['getUserMedia','getDisplayMedia']) {
    const o = md[fn]?.bind(md); if(!o) continue;
    md[fn] = async (...a) => { const s = await o(...a); s.getTracks().forEach(t=>window.__localTracks.add(t.id)); return s; };
  }
}, HS, accessToken, userId, deviceId, LK_INT);

// LOAD V08 — сразу в комнату, без главной страницы
console.log('[CONFIG] LiveKit internal: ' + LK_INT);

// MATRIX API V08 — sync пропущен, ROOM_ID задан
const ROOM = ROOM_ID;
console.log('[ROOM] ' + ROOM);
if (!ROOM) { console.log('[FATAL] No room'); process.exit(1); }

// NAVIGATE
const P = new URLSearchParams({roomId:ROOM, skipLobby:'true', hideHeader:'true', showControls:'false', displayName:'Recorder', returnToLobby:'false'});
console.log('[NAV] Going to room...');
await page.goto(`${EC}/room/#?${P}`,{waitUntil:'domcontentloaded',timeout:60000}).catch(()=>{});
// OBSERVER_SPEEDUP_V06
await page.waitForFunction(() => { const btns = [...document.querySelectorAll('button,[role=button]')].filter(b => b.offsetParent !== null); return btns.some(b => /join/i.test((b.textContent||'').trim()) || /join/i.test(b.getAttribute('aria-label')||'')); }, {timeout: 15000, polling: 200}).catch(() => console.log('[NAV] Join button not found in 15s'));
await sleep(500);
const roomText = await page.evaluate(() => document.body?.innerText?.slice(0,500) || 'EMPTY');
console.log('[ROOM] Text: ' + roomText.slice(0,300));

// JOIN
console.log('[JOIN] Looking for Join button...');
async function clickJoinButtons() {
  try {
    const result = await page.evaluate(() => {
      const btns = [...document.querySelectorAll('button,[role=button]')].filter(b => b.offsetParent !== null);
      const clicked = [];
      for (const b of btns) {
        const t = (b.textContent || '').trim().toLowerCase();
        const a = (b.getAttribute('aria-label') || '').toLowerCase();
        if (/^join call$/i.test(t) || /^join$/i.test(t) || /join call/i.test(a)) {
          b.click();
          clicked.push(t || a);
        }
      }
      return clicked.length ? 'clicked: ' + clicked.join(', ') : 'NOT FOUND. Buttons: ' + btns.map(b => (b.textContent||'').trim().slice(0,25)).join(' | ');
    });
    console.log('[JOIN] ' + result);
    return result;
  } catch(e) {
    console.log('[JOIN] Error (frame detached?): ' + e.message);
    return 'error';
  }
}
await clickJoinButtons();
await sleep(1000);

// OBSERVER_FAST_JOIN_V07 — без повторного клика
let currentPage = page;
try {
  const pages = await browser.pages();
  if (pages.length > 0) currentPage = pages[pages.length - 1];
} catch(e) {}

await sleep(2000);

// Screenshot
try {
  await currentPage.screenshot({path:`${OUT}/p56-01-call.png`});
} catch(e) {
  console.log('[SNAP] Error: ' + e.message.slice(0,100));
}

const wsEvents = logs.filter(l => /WS_|ICE|PC_|INJECT/i.test(l));
console.log('[WS+ICE] Events: ' + wsEvents.length);
wsEvents.slice(0, 20).forEach(l => console.log('  ' + l.slice(0, 200)));

// WAIT FOR VIDEO — try-catch on EVERY evaluate (frame can detach)

// GRID - DO NOT CLICK - stay in default Grid view
// Back to Speaker Mode button goes TO speaker mode, not away from it
console.log('[GRID] Staying in default view (no clicks)');
await sleep(1000);

console.log(`\n[VIDEO] Waiting ${WAIT_V}s...\n`);
let gotRemote = false, finalSnap = null;
for (let i = 0; i < WAIT_V && !__sigReceived; i += 5) {
  await sleep(parseInt(process.env.W_POLL||'200',10));
  let snap;
  try {
    snap = await currentPage.evaluate(() => {
      const loc = window.__localTracks || new Set();
      const vids = [...document.querySelectorAll('video')].map(v => {
        const ids = (v.srcObject?.getVideoTracks?.() || []).map(t => t.id);
        const tracks = v.srcObject?.getTracks?.() || [];
        return {w: v.videoWidth, h: v.videoHeight, alive: v.videoWidth > 0 && v.readyState >= 2,
                local: ids.length > 0 && ids.every(id => loc.has(id)),
                trackCount: tracks.length, readyState: v.readyState, muted: v.muted};
      });
      const iceStates = (window.__pcs||[]).map(pc => pc.iceConnectionState);
      const pcStates = (window.__pcs||[]).map(pc => pc.connectionState);
      return {remote: vids.filter(v => v.alive && !v.local).length,
              local: vids.filter(v => v.alive && v.local).length,
              total: vids.length, stats: window.__lkStats,
              iceStates, pcStates, vidDetails: vids};
    });
  } catch(e) {
    console.log(`[t+${i+5}s] EVAL ERROR: ${e.message.slice(0,100)}`);
    // Try to re-get page
    try {
      const pages = await browser.pages();
      if (pages.length > 0) currentPage = pages[pages.length - 1];
      console.log(`[t+${i+5}s] Switched to page ${pages.length}`);
    } catch(e2) {}
    continue;
  }
  if (!snap) { console.log(`[t+${i+5}s] snap=null`); continue; }
  console.log(`[t+${i+5}s] R=${snap.remote} L=${snap.local} T=${snap.total} ws=${snap.stats.rewrites}/${snap.stats.wsOpen}/${snap.stats.wsErr} ice=[${snap.iceStates.join(',')}] pc=[${snap.pcStates.join(',')}]`);
  if (snap.vidDetails && snap.vidDetails.length > 0 && i < 15) {
    snap.vidDetails.forEach((v,j) => console.log(`  v[${j}]: ${v.w}x${v.h} alive=${v.alive} local=${v.local} tr=${v.trackCount} rdy=${v.readyState} m=${v.muted}`));
  }
  if (snap.remote > 0) { gotRemote = true; finalSnap = snap; break; }
  if (!REQ_REMOTE && (i+5) >= MIN_WAIT) {
    console.log('[GATE] remote video нет, пишу экран+звук');
    gotRemote = true; finalSnap = snap; break;
  }
  finalSnap = snap;
}

// Screenshot before recording
try { await currentPage.screenshot({path:`${OUT}/p56-02-before-rec.png`}); } catch(e) {
  console.log('[SNAP2] Error: ' + e.message.slice(0,100));
}

// RECORD — ВАРИАНТ B: композиция видео через page.screenshot, x11grab removed
// ==== E3: ЗАХВАТ В ФАЙЛ ====
if (gotRemote && !__sigReceived) {
  const SECS = parseInt(process.env.RECORD_SECS || '30', 10);
  const outFile = process.env.OUT_FILE || `${OUT}/rec-${Date.now()}.mp4`;
  console.log(`[REC] Запись ${SECS}s -> ${outFile}`);
  console.log(`[REC_FILE] ${outFile}`);
  const ff = spawn('ffmpeg', [
    '-nostats','-loglevel','warning','-progress','/out/ffprogress.log','-thread_queue_size','1024','-f','x11grab','-draw_mouse','0','-video_size','1280x720',
    '-framerate',(process.env.FPS || '15'),'-i',':99.0',
    ...(process.env.NO_AUDIO === '1'
      ? ['-f','lavfi','-i','anullsrc=r=48000:cl=stereo']
      : ['-thread_queue_size','1024','-f','pulse','-ac','2','-i',(process.env.PULSE_SRC || 'recsink.monitor')]),
    '-threads','0','-fps_mode','cfr','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p','-crf',(process.env.CRF || '28'),
    '-af','aresample=async=1:first_pts=0','-c:a','aac','-b:a','128k',
    '-movflags','+frag_keyframe+empty_moov+default_base_moof','-fflags','+genpts',
    '-y',outFile
  ], {stdio:['pipe','ignore','pipe']});

  // FF_DRAIN: без чтения пайпов ffmpeg блокируется на 64 КБ буфере
  if (ff.stdout) { ff.stdout.resume(); ff.stdout.on('data', () => {}); }
  if (ff.stderr) {
    ff.stderr.resume();
    ff.stderr.on('data', d => {
      const t = d.toString().trim();
      if (t) console.log('[ffmpeg] ' + t.slice(0, 200));
    });
  }
  ff.stderr.on('data', d => {
    const s = d.toString();
    const m = s.match(/frame=\s*(\d+)/);
    if (m && parseInt(m[1],10) % 100 === 0) console.log(`[REC] frame=${m[1]}`);
  });
  // ==== ОСТАНОВКА: единая точка, без дублей ====
  const stopLog = (m) => {
    const s = `[${new Date().toISOString()}] [OBS] ${m}\n`;
    try { fs.appendFileSync('/out/stop-trace.log', s); } catch (e) {}
    console.log('[REC] ' + m);
  };
  let stopping = false;
  const stopAll = (why) => {
    if (stopping) return; stopping = true;
    stopLog('СТОП: ' + why);
    try { ff.stdin.write('q'); stopLog('ffmpeg <- q'); }
    catch (e) { stopLog('stdin q ОШИБКА: ' + e.message); }
    setTimeout(() => { try { ff.kill('SIGINT');  stopLog('ffmpeg <- SIGINT');  } catch (e) {} }, 1000);
    setTimeout(() => { try { ff.kill('SIGTERM'); stopLog('ffmpeg <- SIGTERM'); } catch (e) {} }, 10000);
  };
  process.on('SIGINT',  () => stopAll('SIGINT'));
  process.on('SIGTERM', () => stopAll('SIGTERM'));
  const hardStop = setTimeout(() => stopAll('лимит ' + SECS + 's'), SECS * 1000);
await new Promise(r => ff.on('exit', c => { clearTimeout(hardStop); console.log(`[REC] ffmpeg exit=${c}`); r(); }));
  try {
    const sz = fs.statSync(outFile).size;
    console.log(`[REC] Файл: ${outFile} (${Math.round(sz/1024)} KB)`);
    fs.writeFileSync(`${OUT}/last-recording.txt`, outFile);
  } catch(e) { console.log('[REC] ФАЙЛ НЕ СОЗДАН: ' + e.message); }
}

console.log('\n════════════════════════════════════════════════');
console.log('DONE (remote: ' + ((finalSnap && finalSnap.remote) ? 'да' : 'нет') + ')');
if (finalSnap) console.log('ICE: ' + (finalSnap.iceStates||[]).join(','));
console.log('════════════════════════════════════════════════\n');
fs.writeFileSync(`${OUT}/p56-result.json`, JSON.stringify({gotRemote, snap: finalSnap, wsLogs: logs.filter(l => /WS_|ICE|PC_|INJECT|AUTH|error|fail|join|sync/i.test(l)).slice(0, 80)}, null, 2));
logs.filter(l => /ICE|PC_|WS_|INJECT|AUTH|JOIN|error|fail|sync/i.test(l)).slice(0, 60).forEach(l => console.log('  ' + l.slice(0, 200)));
console.log('[SHUTDOWN] closing browser');
await browser.close();
console.log('[SHUTDOWN] browser closed');

// GUARANTEED_CLEANUP_V06
if (!gotRemote) { console.log('[CLEANUP] Recording never started, cleaning up'); }
// CLEAR_RECORDER_CALL_MEMBERSHIP_V1
try {
  const statePath = '/_matrix/client/v3/rooms/'
    + encodeURIComponent(ROOM_ID) + '/state';
  const state = await matrixRequest(
    statePath, 'GET', undefined, accessToken
  );
  if (!Array.isArray(state)) throw new Error('Invalid room state response');

  let cleared = 0;
  for (const ev of state) {
    if (ev.type !== 'org.matrix.msc3401.call.member'
        || ev.sender !== userId
        || typeof ev.state_key !== 'string'
        || !Object.keys(ev.content || {}).length) continue;

    await matrixRequest(
      statePath + '/' + encodeURIComponent(ev.type)
      + '/' + encodeURIComponent(ev.state_key),
      'PUT', {}, accessToken
    );
    cleared++;
  }

  const checked = await matrixRequest(
    statePath, 'GET', undefined, accessToken
  );
  const remaining = checked.filter(ev =>
    ev.type === 'org.matrix.msc3401.call.member'
    && ev.sender === userId
    && Object.keys(ev.content || {}).length
  ).length;

  console.log('[HANGUP] cleared=' + cleared + ' remaining=' + remaining);
  if (remaining) throw new Error('Recorder call membership remains');

  const otherTypes = [...new Set(checked.filter(ev =>
    ev.sender === userId
    && /(?:rtc|call).*member/i.test(ev.type)
    && ev.type !== 'org.matrix.msc3401.call.member'
    && Object.keys(ev.content || {}).length
  ).map(ev => ev.type))];
  if (otherTypes.length)
    console.log('[HANGUP] other membership types: ' + otherTypes.join(','));
} catch (e) {
  console.error('[HANGUP] FAILED: ' + e.message);
  process.exitCode = 1;
}

