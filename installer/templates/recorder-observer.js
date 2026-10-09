import puppeteer from 'puppeteer-core';
import fs from 'fs';
import { spawn, execSync } from 'child_process';

let __sigReceived = false;
process.on('SIGINT', () => { __sigReceived = true; console.log('[SIGNAL] SIGINT'); });
process.on('SIGTERM', () => { __sigReceived = true; console.log('[SIGNAL] SIGTERM'); });

const EC = process.env.EC_URL || 'http://127.0.0.1:8090';
const OUT = process.env.OUT_DIR || '/out';
const HS = process.env.MATRIX_HS || 'https://host.example.org';
const SYN = process.env.SYNAPSE_URL || 'http://172.18.0.3:8008';
const LK_INT = process.env.LK_INTERNAL || '172.21.0.6:7880';
const REC_USER = process.env.REC_USER || 'recorder';
const REC_PASS = process.env.REC_PASS || 'Milorada2026!';
const REQ_REMOTE = process.env.REQUIRE_REMOTE === '1';
const MIN_WAIT = parseInt(process.env.MIN_WAIT || '12', 10);
const WAIT_V = parseInt(process.env.WAIT_VID || '120', 10);
const ROOM_ID = process.env.ROOM_ID;
const OUT_FILE = process.env.OUT_FILE;
const sleep = ms => new Promise(r=>setTimeout(r,ms));

// LOGIN
console.log('[AUTH] Login via curl to ' + SYN);
let accessToken=null, userId=null, deviceId=null;
try {
  const cmd = `curl -s -X POST '${SYN}/_matrix/client/v3/login' -H 'Content-Type: application/json' -d '{"type":"m.login.password","user":"${REC_USER}","password":"${REC_PASS}","device_id":"RECORDER001"}'`;
  const data = JSON.parse(execSync(cmd, {encoding:'utf8', timeout:10000}).trim());
  if (data.access_token) { accessToken=data.access_token; userId=data.user_id; deviceId=data.device_id; console.log('[AUTH] OK user='+userId+' device='+deviceId); }
  else { console.log('[AUTH] FAIL '+JSON.stringify(data).slice(0,200)); }
} catch(e) { console.log('[AUTH] Error: '+e.message); }
if (!accessToken) process.exit(1);

// CLEAN stale call.member
try {
  const stateRaw = execSync(`curl -s '${SYN}/_matrix/client/v3/rooms/${ROOM_ID}/state' -H 'Authorization: Bearer ${accessToken}'`, {encoding:'utf8', timeout:10000});
  const events = JSON.parse(stateRaw);
  if (!Array.isArray(events)) { console.log('[CLEAN] state response not array: ' + stateRaw.slice(0,200)); }
  let cleaned=0;
  for (const ev of (Array.isArray(events) ? events : [])) {
    if (ev.type==='org.matrix.msc3401.call.member' && ev.sender && ev.sender.includes('recorder') && Object.keys(ev.content||{}).length>0) {
      const sk=encodeURIComponent(ev.state_key);
      execSync(`curl -s -X PUT '${SYN}/_matrix/client/v3/rooms/${ROOM_ID}/state/org.matrix.msc3401.call.member/${sk}' -H 'Authorization: Bearer ${accessToken}' -H 'Content-Type: application/json' -d '{}'`, {encoding:'utf8', timeout:10000});
      cleaned++;
    }
  }
  console.log('[CLEAN] Очищено '+cleaned+' stale call.member');
} catch(e) { console.log('[CLEAN] Error: '+e.message); }

// BROWSER
const browser = await puppeteer.launch({
  executablePath:'/usr/bin/chromium', headless:false,
  env:{...process.env, PULSE_SERVER:'unix:/tmp/pulse/native', DISPLAY:':99'},
  args:['--ignore-certificate-errors','--unsafely-treat-insecure-origin-as-secure=http://127.0.0.1:8090',
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
      console.log('[WS_REWRITE] ' + newUrl.slice(0,120));
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

// LOAD
console.log('[NAV] Loading Element Call: ' + EC);
await page.goto(EC+'/', {waitUntil:'networkidle2', timeout:60000});
await sleep(parseInt(process.env.W_BOOT||'3000',10));
console.log('[CONFIG] LiveKit internal: ' + LK_INT);

// MATRIX API
const mx = (p,o={}) => page.evaluate(async(hs,p,t,o)=>{
  try{ const r=await fetch(hs+p,{method:o.method||'GET', headers:{Authorization:'Bearer '+t,'Content-Type':'application/json'}, body:o.body?JSON.stringify(o.body):undefined}); let b=null; try{b=await r.json();}catch{} return {status:r.status, body:b}; }catch(e){return{error:String(e)};}
}, HS,p,accessToken,o);
const f = encodeURIComponent(JSON.stringify({room:{timeline:{limit:1}}}));
const sync = await mx(`/_matrix/client/v3/sync?timeout=0&filter=${f}`);
console.log('[SYNC] status=' + (sync.status||sync.error));
const ROOM = ROOM_ID || Object.keys(sync.body?.rooms?.join||{})[0];
console.log('[ROOM] ' + ROOM);
if (!ROOM) { console.log('[FATAL] No room'); process.exit(1); }

// NAVIGATE
const P = new URLSearchParams({roomId:ROOM, skipLobby:'true', hideHeader:'true', showControls:'false', displayName:'Recorder', returnToLobby:'false'});
console.log('[NAV] Going to room...');
await page.goto(`${EC}/room/#?${P}`,{waitUntil:'networkidle2',timeout:60000}).catch(()=>{});
await sleep(parseInt(process.env.W_ROOM||'8000',10));
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
await sleep(3000);

// Wait for page navigation after join (Element Call changes view)
try {
  await page.waitForNavigation({waitUntil:'networkidle2', timeout:15000});
  console.log('[NAV] Post-join navigation done');
} catch(e) {
  console.log('[NAV] No post-join navigation (or timeout): ' + e.message.slice(0,100));
}

// Re-get page reference in case of navigation
let currentPage = page;
try {
  const pages = await browser.pages();
  if (pages.length > 0) {
    currentPage = pages[pages.length - 1];
    console.log('[NAV] Using page: ' + pages.length + ' pages available');
  }
} catch(e) {
  console.log('[NAV] Page lookup error: ' + e.message.slice(0,100));
}

await sleep(parseInt(process.env.W_GATE||'3000',10));
await clickJoinButtons();
await sleep(3000);

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
for (let i = 0; i < WAIT_V; i += 5) {
  await sleep(parseInt(process.env.W_POLL||'1000',10));
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
if (gotRemote) {
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
await browser.close();
