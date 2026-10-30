// HTTP lifecycle adapter; browser/Matrix/media behavior lives in the pinned observer.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {spawn, execFile} from 'node:child_process';
import {promisify} from 'node:util';
const exec = promisify(execFile);
const OUT = process.env.OUT_DIR || '/out';
const stateFile = path.join(OUT, 'worker-state.json');
const token = process.env.RECORDER_TOKEN || '';
const port = Number(process.env.PORT || 8789);
const observer = process.env.OBSERVER_JS || '/app/observer.nginx.mjs';
const publisher = process.env.PUBLISHER_PY || '/plugin/publish.py';
let job = null;
let child = null;
let shuttingDown = false;
fs.mkdirSync(OUT, {recursive:true});
function save() {
  fs.writeFileSync(stateFile + '.tmp', JSON.stringify(job), {mode:0o600});
  fs.renameSync(stateFile + '.tmp', stateFile);
}
try {
  job = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
  if (job && ['starting','recording','stopping'].includes(job.state)) {
    job.state = 'error'; job.error = 'worker restarted before finalization'; save();
  }
} catch (e) { if (e.code !== 'ENOENT') throw e; }
const busy = () => !!child || job?.state === 'processing';
function auth(req) {
  const supplied = req.headers['x-recorder-token'] || '';
  return token && Buffer.byteLength(supplied) === Buffer.byteLength(token) &&
    crypto.timingSafeEqual(Buffer.from(supplied), Buffer.from(token));
}
function reply(res, code, body) {
  res.writeHead(code, {'Content-Type':'application/json','Cache-Control':'no-store'});
  res.end(JSON.stringify(body));
}
function sanitize(text) {
  return text.replace(/\beyJ[\w-]+\.[\w-]+\.[\w-]+/g,'<JWT>')
    .replace(/(Bearer\s+)\S+/gi,'$1<SECRET>');
}
async function recoverPublications() {
  for (const f of fs.readdirSync(OUT).filter(n => n.endsWith('.mp4.done'))) {
    const source = path.join(OUT, f.slice(0, -5));
    try { await exec('python3', [publisher, source], {timeout:1800000});
      if(job?.file === source) {job.state='finalized';delete job.error;save();} }
    catch { console.error('[publish] pending:', path.basename(source)); }
  }
}
await recoverPublications();
async function finalized(proc, file, code) {
  if (child !== proc) return;
  // close fires only after observer exits and its inherited pipes close.
  job.state='processing';save();
  child = null;
  try {
    if (code !== 0) throw new Error('observer exit ' + code);
    const {stdout} = await exec('ffprobe', ['-v','error','-show_entries',
      'format=duration','-of','default=nk=1:nw=1',file], {timeout:30000});
    if (!(Number(stdout.trim()) > 0)) throw new Error('no valid recording');
    job.state = 'processing'; save();
    const meta = {room_id:job.room, room_name:job.roomName, started_by:job.startedBy,
      started_at:job.startedAt, source_bytes:fs.statSync(file).size};
    fs.writeFileSync(file + '.done.tmp', JSON.stringify(meta));
    fs.renameSync(file + '.done.tmp', file + '.done');
    await exec('python3', [publisher, file], {timeout:1800000});
    job.state = 'finalized'; job.endedAt = new Date().toISOString();
  } catch (error) {
    job.state = 'error'; job.error = error.message;
  }
  save();
}
const server = http.createServer(async(req,res) => {
  try {
    const url = new URL(req.url, 'http://localhost');
    if (url.pathname === '/health' && req.method === 'GET') {
      return reply(res,200,{ok:true,busy:busy(),state:job?.state || 'idle'});
    }
    if (!auth(req)) return reply(res,403,{error:'worker authentication failed'});
    if (url.pathname === '/status' && req.method === 'GET') {
      if (job?.state === 'starting' && child && fs.existsSync(job.file) && fs.statSync(job.file).size > 4096) {
        job.state = 'recording'; save();
      }
      return reply(res,200,job || {state:'idle'});
    }
    if (req.method !== 'POST') return reply(res,405,{error:'method not allowed'});
    let body = ''; for await (const chunk of req) {
      body += chunk; if (body.length > 8192) return reply(res,413,{error:'body too large'});
    }
    const data = JSON.parse(body || '{}');
    if (url.pathname === '/start') {
      if (shuttingDown || busy()) return reply(res,409,{error:'worker busy'});
      if (!/^![^\s/]+:[^\s/]+$/.test(data.roomId || '')) return reply(res,400,{error:'invalid roomId'});
      const file = path.join(OUT, 'rec-' + new Date().toISOString().replace(/[:.]/g,'-') + '-' + crypto.randomBytes(4).toString('hex') + '.mp4');
      job = {state:'starting',room:data.roomId, roomName:String(data.roomName || '').slice(0,500),
        startedBy:String(data.startedBy || ''), startedAt:new Date().toISOString(),file};
      save();
      child = spawn(process.execPath, [observer], {
        env:{...process.env,ROOM_ID:data.roomId,OUT_FILE:file}, stdio:['ignore','pipe','pipe']
      });
      const proc = child;
      for (const stream of [proc.stdout,proc.stderr]) {
        stream.setEncoding('utf8'); stream.on('data',d => process.stdout.write(sanitize(d)));
      }
      proc.on('error', () => { job.state='error';job.error='observer spawn failed';save(); });
      proc.on('close', code => finalized(proc,file,code));
      return reply(res,202,job);
    }
    if (url.pathname === '/stop') {
      if (data.roomId !== job?.room) return reply(res,409,{error:'room does not own worker'});
      if (!child) return reply(res,200,job || {state:'idle'});
      job.state='stopping';save();child.kill('SIGINT');
      return reply(res,202,job);
    }
    return reply(res,404,{error:'not found'});
  } catch (e) {
    if (!res.headersSent) reply(res,400,{error:'invalid request'});
  }
});
server.requestTimeout=15000;
server.headersTimeout=10000;
if (!token) throw new Error('RECORDER_TOKEN required');
server.listen(port,'127.0.0.1',()=>console.log('[worker] listening on loopback:',port));
for (const signal of ['SIGINT','SIGTERM']) process.on(signal,()=>{
  shuttingDown=true;
  if(child) {job.state='stopping';save();child.kill('SIGINT');}
  server.close();
  const timer=setInterval(()=>{if(!busy()){clearInterval(timer);process.exit(0);}},500);
  setTimeout(()=>process.exit(1),110000).unref();
});
