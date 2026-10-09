import puppeteer from 'puppeteer-core';
import fs from 'fs';
import { spawn } from 'child_process';

const EC     = process.env.EC_URL  || 'http://127.0.0.1:8090';
const OUT    = process.env.OUT_DIR || '/out';
const HS     = process.env.MATRIX_HS || 'https://meet.milorada.ru';
const LK_INT = process.env.LK_INTERNAL || '172.21.0.7:7880';
const WAIT_P = parseInt(process.env.WAIT_PUB || '90', 10);
const REQ_REMOTE = process.env.REQUIRE_REMOTE === '1';
const MIN_WAIT  = parseInt(process.env.MIN_WAIT || '12', 10);
const WAIT_V = parseInt(process.env.WAIT_VID || '120', 10);
const sleep  = ms => new Promise(r=>setTimeout(r,ms));

const browser = await puppeteer.launch({
  executablePath:'/usr/bin/chromium', headless:false,
  env:{...process.env, PULSE_SERVER:'unix:/tmp/pulse/native', DISPLAY:':99'},
  args:['--ignore-certificate-errors-spki-list=d8Som6y88M8XM7SBHE5JqV9wANkBAsw4GnYsxQxnLRQ=','--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage',
    '--user-data-dir=/profile-spk','--autoplay-policy=no-user-gesture-required',
    '--disable-audio-output-muting',
    '--alsa-output-device=default',
    '--disable-features=AutoplayIgnoreWebAudio','--use-fake-ui-for-media-stream',
    '--window-size=1280,720','--kiosk','--start-fullscreen',
    '--disable-infobars','--disable-session-crashed-bubble',
    '--hide-crash-restore-bubble','--no-first-run','--no-default-browser-check',
    '--disable-features=TranslateUI,AutoplayIgnoreWebAudio',
    '--window-position=0,0','--disable-gpu'],
});
const page = await browser.newPage();
await page.setViewport({width:1280,height:720});

const logs=[];
page.on('console', m=>logs.push(`[${m.type()}] ${m.text()}`));
page.on('pageerror', e=>logs.push(`[PAGEERROR] ${e.message}`));
const shot = (n) => page.screenshot({path:`${OUT}/p56-${n}.png`}).catch(()=>{});

// evaluateOnNewDocument persists across navigations
await page.evaluateOnNewDocument((lkInt) => {
  window.__localTracks = new Set();
  window.__lkStats = {rewrites:0, wsOpen:0, wsErr:0};
  window.__LK_INTERNAL_URL = lkInt;

  try { for (const k of ['livekit','livekit-room','livekit-participant',
                         'livekit-engine','livekit-track','lk-e2ee'])
    localStorage.setItem('loglevel:'+k,'DEBUG'); } catch {}

  const origWS = WebSocket;
  window.WebSocket = function(url, protocols) {
    let newUrl = url;
    if (/livekit|rtc/.test(url)) {
      newUrl = url.replace(/^wss?:\/\/[^\/]+\/livekit-server/, 'ws://' + window.__LK_INTERNAL_URL);
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
    md[fn] = async (...a) => { const s = await o(...a);
      s.getTracks().forEach(t=>window.__localTracks.add(t.id)); return s; };
  }
}, LK_INT);

// Load home first to establish session
await page.goto(EC+'/', {waitUntil:'networkidle2', timeout:60000});
await sleep(parseInt(process.env.W_BOOT||'1500',10));
console.log('[CONFIG] LiveKit internal: ' + LK_INT);

const tok = await page.evaluate(()=>{
  for (const k of Object.keys(localStorage)) {
    const r=localStorage.getItem(k)||'';
    if (/access_?token/i.test(k)&&r.length>20) return r;
    try{const o=JSON.parse(r); if(o?.access_token)return o.access_token;
        if(o?.accessToken)return o.accessToken;}catch{}
  }
  return null;
});
console.log('[TOKEN]', tok ? 'found' : 'MISSING');

const mx = (p,o={}) => page.evaluate(async(hs,p,t,o)=>{
  try{ const r=await fetch(hs+p,{method:o.method||'GET',
    headers:{Authorization:'Bearer '+t,'Content-Type':'application/json'},
    body:o.body?JSON.stringify(o.body):undefined});
    let b=null; try{b=await r.json();}catch{}
    return {status:r.status, body:b};
  }catch(e){return{error:String(e)};}}, HS,p,tok,o);

const f = encodeURIComponent(JSON.stringify({room:{timeline:{limit:1}}}));
const sync = await mx(`/_matrix/client/v3/sync?timeout=0&filter=${f}`);
const ROOM = process.env.ROOM_ID || Object.keys(sync.body?.rooms?.join||{})[0];
console.log(`[ROOM] ${ROOM}`);

// Navigate to call room
const P=new URLSearchParams({roomId:ROOM, skipLobby:'true', hideHeader:'true',
  showControls:'false', displayName:'Recorder', returnToLobby:'false'});
console.log('[NAV] Going to room...');
await page.goto(`${EC}/room/#?${P}`,{waitUntil:'networkidle2',timeout:60000}).catch(()=>{});
await sleep(parseInt(process.env.W_ROOM||'3000',10));
shot('10-lobby');

// Click Join call by text
console.log('[JOIN] Looking for Join button...');
const joinResult = await page.evaluate(()=>{
  const btns=[...document.querySelectorAll('button,[role=button]')].filter(b=>b.offsetParent!==null);
  const joinBtn = btns.find(b=>/join call/i.test(b.innerText||''));
  if(joinBtn) { joinBtn.click(); return 'clicked: ' + joinBtn.innerText.trim(); }
  return 'NOT FOUND. Buttons: ' + btns.map(b=>b.innerText.trim().slice(0,20)).join(' | ');
});
console.log('[JOIN] ' + joinResult);
await sleep(parseInt(process.env.W_GATE||'2000',10));
shot('20-joined');

// Check WS status
const wsStatus = logs.filter(l => /WS_REWRITE|WS_OPEN|WS_ERROR|WS_CLOSE/.test(l));
console.log('[WS] Events: ' + wsStatus.length);
wsStatus.slice(0,10).forEach(l => console.log('  ' + l.slice(0,150)));

// Wait for remote video
console.log(`\n[VIDEO] Waiting ${WAIT_V}s...\n`);
let gotRemote=false, finalSnap=null;
for(let i=0;i<WAIT_V;i+=5){
  await sleep(parseInt(process.env.W_POLL||'1000',10));
  const snap = await page.evaluate(()=>{
    const loc=window.__localTracks||new Set();
    const vids=[...document.querySelectorAll('video')].map((v,idx)=>{
      const ids=(v.srcObject?.getVideoTracks?.()||[]).map(t=>t.id);
      return {idx,w:v.videoWidth,h:v.videoHeight,
              alive:v.videoWidth>0&&v.readyState>=2,
              local:ids.length>0&&ids.every(id=>loc.has(id))};
    });
    return {remote:vids.filter(v=>v.alive&&!v.local).length,
            local:vids.filter(v=>v.alive&&v.local).length,
            total:vids.length, stats:window.__lkStats, vids};
  });

  console.log(`[t+${i+5}s] REMOTE=${snap.remote} local=${snap.local} total=${snap.total} ws_rw=${snap.stats.rewrites} ws_ok=${snap.stats.wsOpen} ws_err=${snap.stats.wsErr}`);
  if(snap.remote>0){ gotRemote=true; finalSnap=snap; break; }

  if(!REQ_REMOTE && (i+5)>=MIN_WAIT){

    console.log('[GATE] remote video нет, пишу экран+звук (REQUIRE_REMOTE=0)');

    gotRemote=true; finalSnap=snap; break;

  }
  finalSnap=snap;
}
shot('40-final');
// AUDIO_PROBE: состояние удалённого звука в браузере рекордера
try {
  const ap = await page.evaluate(() => {
    const els = Array.from(document.querySelectorAll('audio,video'));
    return {
      count: els.length,
      details: els.slice(0, 8).map(e => ({
        tag: e.tagName, paused: e.paused, muted: e.muted,
        volume: e.volume, readyState: e.readyState,
        tracks: e.srcObject ? e.srcObject.getAudioTracks().map(t => ({
          enabled: t.enabled, muted: t.muted, state: t.readyState, label: t.label
        })) : []
      }))
    };
  });
  console.log('[AUDIO] элементов: ' + ap.count);
  for (const d of ap.details) {
    console.log('[AUDIO] ' + d.tag + ' paused=' + d.paused + ' muted=' + d.muted +
      ' vol=' + d.volume + ' ready=' + d.readyState +
      ' audioTracks=' + JSON.stringify(d.tracks));
  }
  // принудительно снимаем пауза/мьют
  const fixed = await page.evaluate(async () => {
    let n = 0;
    for (const e of document.querySelectorAll('audio,video')) {
      e.muted = false; e.volume = 1.0;
      if (e.paused) { try { await e.play(); n++; } catch (err) {} }
    }
    return n;
  });
  console.log('[AUDIO] запущено принудительно: ' + fixed);

// RMS_PROBE: измеряем реальную энергию удалённого аудиотрека 8 секунд
try {
  const rms = await page.evaluate(async () => {
    const el = Array.from(document.querySelectorAll('audio'))
      .find(e => e.srcObject && e.srcObject.getAudioTracks().length);
    if (!el) return { error: 'audio элемент с треком не найден' };
    const ctx = new AudioContext();
    if (ctx.state === 'suspended') await ctx.resume();
    const src = ctx.createMediaStreamSource(el.srcObject);
    const an = ctx.createAnalyser();
    an.fftSize = 2048;
    src.connect(an);
    const buf = new Float32Array(an.fftSize);
    const samples = [];
    for (let i = 0; i < 16; i++) {
      await new Promise(r => setTimeout(r, 500));
      an.getFloatTimeDomainData(buf);
      let sum = 0;
      for (const v of buf) sum += v * v;
      samples.push(Math.sqrt(sum / buf.length));
    }
    const peak = Math.max(...samples);
    return {
      ctxState: ctx.state, sampleRate: ctx.sampleRate,
      peakRms: peak,
      peakDb: peak > 0 ? (20 * Math.log10(peak)).toFixed(1) : '-inf',
      nonZero: samples.filter(v => v > 1e-6).length,
      total: samples.length
    };
  });
  console.log('[RMS] ' + JSON.stringify(rms));
} catch (e) { console.log('[RMS] ошибка: ' + e.message); }

} catch (e) { console.log('[AUDIO] проба не удалась: ' + e.message); }


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
if(gotRemote){
  console.log('DONE (remote video: ' + ((finalSnap && finalSnap.remote) ? 'да' : 'нет') + ')');
} else {
  console.log('FAIL: NO REMOTE VIDEO');
  console.log('WS rewrites:', finalSnap?.stats?.rewrites, 'opens:', finalSnap?.stats?.wsOpen, 'errors:', finalSnap?.stats?.wsErr);
}
console.log('════════════════════════════════════════════════\n');

fs.writeFileSync(`${OUT}/p56-result.json`, JSON.stringify({gotRemote, snap:finalSnap,
  wsLogs: logs.filter(l=>/WS_|LiveKit|livekit|Failed to connect/i.test(l)).slice(0,30)}, null, 2));
// BTNDUMP: печатаем все кнопки, чтобы найти реальный селектор
try {
  const btns = await page.evaluate(() => [...document.querySelectorAll('button')]
    .map(b => ({t:(b.getAttribute('data-testid')||''),
                a:(b.getAttribute('aria-label')||''),
                x:(b.textContent||'').trim().slice(0,25)})));
  console.log('[BTNDUMP] ' + JSON.stringify(btns));
} catch(e) { console.log('[BTNDUMP] ошибка ' + e); }
// HANGUP2: клик «Завершить» + пауза, чтобы снялся m.rtc.member
try {
  const sels=['[data-testid="incall_hangup"]','button[aria-label*="Leave"]',
              'button[aria-label*="Hang"]','button[aria-label*="End"]',
              'button[aria-label*="Покинуть"]','button[aria-label*="Завершить"]'];
  for (const sel of sels) {
    const el = await page.$(sel).catch(()=>null);
    if (el) { await el.click().catch(()=>{}); console.log('[HANGUP] клик '+sel); break; }
  }
  await new Promise(r=>setTimeout(r,4000));
} catch(e) { console.log('[HANGUP] ошибка '+e); }
await browser.close();
