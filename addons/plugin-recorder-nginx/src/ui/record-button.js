/* ==========================================================================
 *  record-button.js — plugin edition
 *
 *  Injected into Element Web by install.sh. Adds a record button to the
 *  Element Call / MatrixRTC conference UI and wires it to the controller at
 *  /recording/api/record/start.
 *
 *  Design choices worth knowing before you edit this file:
 *
 *  1. No matrix-js-sdk import. Element Web is a bundled SPA; reaching into its
 *     internal modules breaks on every release. Everything this script needs
 *     (access token, user id, room id) is read from localStorage and the URL,
 *     and the room power level is queried from Synapse over plain HTTP. That
 *     keeps it working across Element Web versions.
 *
 *  2. A MutationObserver, not a setInterval-only approach. Element Call mounts
 *     and unmounts its control bar as the user joins/leaves a call and as the
 *     layout changes; a button inserted once disappears. The observer
 *     re-inserts it, but only when it is actually missing, and it is debounced
 *     so a busy DOM does not cause a re-render loop.
 *
 *  3. Fail loud in the console, quiet in the UI. When something is wrong
 *     (no token, insufficient power, controller down) the user gets one short
 *     toast; the console gets the detail. Silent failure in a recorder is the
 *     worst outcome — you think you are recording and you are not.
 * ========================================================================== */

(function () {
  'use strict';

  const CFG = Object.assign({
    domain: window.location.hostname,
    minPowerLevel: 50,
    lang: 'ru',
    showRecordings: true,
    controllerPath: '/recording',
  }, window.__RECORDER_PLUGIN__ || {});

  const L = {
    ru: {
      record: 'Запись',
      stop: 'Стоп',
      recordings: 'Записи',
      starting: 'Запуск…',
      stopping: 'Завершение…',
      processing: 'Обработка…',
      needCall: 'Запись доступна только во время звонка',
      needPower: 'Недостаточно прав для записи в этой комнате',
      noRoom: 'Не удалось определить комнату',
      noToken: 'Нет активной сессии Matrix — войдите заново',
      busy: 'Все рекордеры заняты, попробуйте позже',
      started: 'Запись началась',
      stopped: 'Запись остановлена, файл обрабатывается',
      failed: 'Не удалось запустить запись',
      unreachable: 'Сервер записи недоступен',
    },
    en: {
      record: 'Record',
      stop: 'Stop',
      recordings: 'Recordings',
      starting: 'Starting…',
      stopping: 'Finishing…',
      processing: 'Processing…',
      needCall: 'Recording is available only during a call',
      needPower: 'Not enough power to record in this room',
      noRoom: 'Could not determine the room',
      noToken: 'No active Matrix session — sign in again',
      busy: 'All recorders are busy, try again later',
      started: 'Recording started',
      stopped: 'Recording stopped, the file is being processed',
      failed: 'Could not start recording',
      unreachable: 'Recording service unreachable',
    },
  };
  const t = (k) => (L[CFG.lang] || L.ru)[k] || k;

  const state = { roomId: '', powerLevel: null, recording: false, phase: 'idle', timer: null };
  let insertScheduled = false;

  // ------------------------------------------------------------------ utils

  function log(...a) { console.log('%c[recorder]', 'color:#2f81f7', ...a); }
  function warn(...a) { console.warn('[recorder]', ...a); }

  function toast(msg, kind) {
    document.querySelectorAll('.recplugin-toast').forEach((n) => n.remove());
    const el = document.createElement('div');
    el.className = 'recplugin-toast';
    el.textContent = msg;
    el.style.cssText = [
      'position:fixed', 'z-index:2147483000', 'left:50%', 'transform:translateX(-50%)',
      'bottom:96px', 'max-width:min(92vw,520px)', 'padding:12px 18px',
      'border-radius:10px', 'font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif',
      'color:#fff', 'box-shadow:0 6px 24px rgba(0,0,0,.35)',
      'background:' + (kind === 'error' ? '#b3261e' : kind === 'ok' ? '#1f7a3f' : '#333'),
    ].join(';');
    document.body.appendChild(el);
    setTimeout(() => el.remove(), kind === 'error' ? 7000 : 4000);
  }

  // --------------------------------------------------- matrix session facts

  function accessToken() {
    try {
      // Element Web stores the token under a few names depending on version.
      for (const k of Object.keys(localStorage)) {
        if (/^mx_access_token(:|$)/.test(k)) {
          const v = localStorage.getItem(k);
          if (v) return v.replace(/^"|"$/g, '');
        }
      }
    } catch (e) { warn('localStorage unavailable', e); }
    return '';
  }

  function userId() {
    try {
      for (const k of Object.keys(localStorage)) {
        if (/^mx_user_id(:|$)/.test(k)) {
          const v = localStorage.getItem(k);
          if (v) return v.replace(/^"|"$/g, '');
        }
      }
    } catch (e) { /* ignore */ }
    return '';
  }

  /**
   * Room id discovery. Element Web puts it in the hash; standalone Element Call
   * puts it in the query string; an embedded call passes it to the iframe src.
   * We try all three before giving up, because a wrong room id means the
   * recording lands in the wrong archive folder.
   */
  function discoverRoomId() {
    const hash = decodeURIComponent(window.location.hash || '');
    let m = hash.match(/([!#][A-Za-z0-9._=/-]+:[A-Za-z0-9.:-]+)/);
    if (m) return m[1];

    const q = new URLSearchParams(window.location.search || '');
    for (const k of ['roomId', 'room_id', 'room', 'conference']) {
      const v = q.get(k);
      if (v && v.startsWith('!')) return v;
    }

    for (const frame of document.querySelectorAll('iframe')) {
      try {
        const src = decodeURIComponent(frame.getAttribute('src') || '');
        const fm = src.match(/([!#][A-Za-z0-9._=/-]+:[A-Za-z0-9.:-]+)/);
        if (fm) return fm[1];
      } catch (e) { /* cross-origin, ignore */ }
    }
    return '';
  }

  async function fetchPowerLevel(roomId, token) {
    const url = new URL(
      '/_matrix/client/v3/rooms/' + encodeURIComponent(roomId) + '/state/m.room.power_levels',
      window.location.origin);
    const res = await fetch(url.toString(), {
      headers: { Authorization: 'Bearer ' + token },
    });
    if (!res.ok) return null;
    const data = await res.json();
    const uid = userId();
    const users = data.users || {};
    return typeof users[uid] === 'number' ? users[uid]
      : (typeof data.users_default === 'number' ? data.users_default : 0);
  }

  // ------------------------------------------------------------- controller

  function api(path, body) {
    // The access token is sent so the controller can verify the caller's power
    // level against Synapse itself. It is NOT proof of anything on its own --
    // the controller re-checks membership and power_levels server-side and
    // ignores any powerLevel sent in the body.
    const headers = { 'Content-Type': 'application/json', 'Accept': 'application/json' };
    const token = accessToken();
    if (token) headers.Authorization = 'Bearer ' + token;
    return fetch(CFG.controllerPath + path, {
      method: 'POST',
      credentials: 'same-origin',
      headers: headers,
      body: JSON.stringify(Object.assign({
        roomId: state.roomId,
        userId: userId(),
        lang: CFG.lang,
      }, body || {})),
    });
  }

  async function startRecording(btn) {
    const token = accessToken();
    if (!token) { toast(t('noToken'), 'error'); return; }
    if (!state.roomId) { toast(t('noRoom'), 'error'); return; }

    setPhase(btn, 'starting');
    try {
      if (state.powerLevel === null) {
        state.powerLevel = await fetchPowerLevel(state.roomId, token);
      }
      if (state.powerLevel !== null && state.powerLevel < CFG.minPowerLevel) {
        setPhase(btn, 'idle');
        toast(t('needPower') + ' (' + state.powerLevel + '/' + CFG.minPowerLevel + ')', 'error');
        return;
      }

      const res = await api('/api/record/start');
      const data = await res.json().catch(() => ({}));

      if (res.status === 503) { setPhase(btn, 'idle'); toast(t('busy'), 'error'); return; }
      if (!res.ok || data.ok === false) {
        setPhase(btn, 'idle');
        toast(t('failed') + (data.error ? ': ' + data.error : ''), 'error');
        warn('start failed', res.status, data);
        return;
      }

      state.recording = true;
      setPhase(btn, 'recording');
      toast(t('started'), 'ok');
      pollStatus();
    } catch (e) {
      setPhase(btn, 'idle');
      toast(t('unreachable'), 'error');
      warn('start threw', e);
    }
  }

  async function stopRecording(btn) {
    setPhase(btn, 'stopping');
    try {
      const res = await api('/api/record/stop');
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(t('failed') + (data.error ? ': ' + data.error : ''), 'error');
        warn('stop failed', res.status, data);
        setPhase(btn, state.recording ? 'recording' : 'idle');
        return;
      }
      state.recording = false;
      toast(t('stopped'), 'ok');
      setPhase(btn, 'processing');
      pollStatus();
    } catch (e) {
      toast(t('unreachable'), 'error');
      warn('stop threw', e);
      setPhase(btn, 'recording');
    }
  }

  /**
   * Poll until the worker reports the job finished. Without this the button
   * would sit on "Stop" after the recording ended elsewhere (another admin
   * pressed Stop, the worker restarted), and the user would believe a second
   * recording is running.
   */
  function pollStatus() {
    clearInterval(state.timer);
    state.timer = setInterval(async () => {
      if (!state.roomId) return;
      try {
        const res = await fetch(
          CFG.controllerPath + '/api/record/status?roomId=' + encodeURIComponent(state.roomId),
          { credentials: 'same-origin' });
        if (!res.ok) { clearInterval(state.timer); return; }
        const data = await res.json();
        const phase = data.state || 'idle';
        const btn = document.querySelector('.recplugin-btn');
        if (phase === 'idle' || phase === 'published') {
          state.recording = false;
          clearInterval(state.timer);
          if (btn) setPhase(btn, 'idle');
        } else if (btn) {
          state.recording = phase === 'recording';
          setPhase(btn, phase === 'recording' ? 'recording' : phase);
        }
      } catch (e) { /* transient; keep polling */ }
    }, 5000);
  }

  // ------------------------------------------------------------------- UI

  const BTN_STYLE = `
    .recplugin-wrap{display:flex;align-items:center;gap:6px}
    .recplugin-btn{display:inline-flex;align-items:center;gap:8px;height:32px;
      padding:0 12px;border-radius:8px;border:1px solid rgba(255,255,255,.18);
      background:rgba(255,255,255,.08);color:var(--color-fg,#fff);cursor:pointer;
      font:500 14px/1 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
    .recplugin-btn:hover{background:rgba(255,255,255,.16)}
    .recplugin-btn[disabled]{opacity:.55;cursor:default}
    .recplugin-btn.is-recording{background:#b3261e;border-color:#b3261e;color:#fff}
    .recplugin-dot{width:9px;height:9px;border-radius:50%;background:#e5484d;
      box-shadow:0 0 0 0 rgba(229,72,77,.7);animation:recplugin-pulse 1.6s infinite}
    @keyframes recplugin-pulse{
      0%{box-shadow:0 0 0 0 rgba(229,72,77,.7)}
      70%{box-shadow:0 0 0 9px rgba(229,72,77,0)}
      100%{box-shadow:0 0 0 0 rgba(229,72,77,0)}}
    .recplugin-link{font:500 13px/1 inherit;color:var(--color-fg,#fff);opacity:.75;
      text-decoration:none;padding:6px 8px;border-radius:6px}
    .recplugin-link:hover{opacity:1;background:rgba(255,255,255,.1)}
    @media (max-width:520px){.recplugin-btn span.recplugin-label{display:none}}
  `;

  function ensureStyle() {
    if (document.getElementById('recplugin-style')) return;
    const s = document.createElement('style');
    s.id = 'recplugin-style';
    s.textContent = BTN_STYLE;
    document.head.appendChild(s);
  }

  function setPhase(btn, phase) {
    state.phase = phase;
    const label = btn.querySelector('.recplugin-label');
    const dot = btn.querySelector('.recplugin-dot');
    btn.classList.toggle('is-recording', phase === 'recording' || phase === 'stopping');
    btn.disabled = phase === 'starting' || phase === 'stopping' || phase === 'processing';

    const text = {
      idle: t('record'),
      starting: t('starting'),
      recording: t('stop'),
      stopping: t('stopping'),
      processing: t('processing'),
    }[phase] || t('record');

    if (label) label.textContent = text;
    btn.title = text;
    if (dot) dot.style.display = (phase === 'recording') ? '' : 'none';
    btn.setAttribute('aria-label', text);
  }

  function buildControl() {
    const wrap = document.createElement('div');
    wrap.className = 'recplugin-wrap recplugin-control';

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'recplugin-btn';
    btn.innerHTML = '<span class="recplugin-dot" style="display:none"></span>'
      + '<span class="recplugin-label"></span>';
    setPhase(btn, state.recording ? 'recording' : 'idle');

    btn.addEventListener('click', (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      if (state.recording) stopRecording(btn); else startRecording(btn);
    });
    wrap.appendChild(btn);

    if (CFG.showRecordings) {
      const a = document.createElement('a');
      a.className = 'recplugin-link';
      a.href = '/recordings/';
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.textContent = t('recordings');
      wrap.appendChild(a);
    }
    return wrap;
  }

  /**
   * Where to put the button. Element Web renders one of several containers for
   * the call controls; we look for the widest match and fall back to the
   * call view's header. If nothing matches we do nothing rather than injecting
   * a floating button over unrelated UI.
   */
  const ANCHORS = [
    '[data-testid="call-controls"]',
    '.mx_CallView_controls',
    '.mx_CallViewButtons',
    '[class*="CallView"] [class*="controls"]',
    '[class*="call"] [class*="control"][class*="bar"]',
    '[data-testid="room-header-controls"]',
    '.mx_RoomHeader_buttons',
  ];

  function insertionPoint() {
    for (const sel of ANCHORS) {
      const el = document.querySelector(sel);
      if (el && el.offsetParent !== null) return el;
    }
    return null;
  }

  function inCall() {
    // The CallView only exists while a call is on screen.
    return !!document.querySelector(
      '[data-testid="call-view"],[class*="CallView"],[class*="call-view"],[data-testid="call-controls"]');
  }

  function insertButton() {
    insertScheduled = false;
    if (document.querySelector('.recplugin-control')) return;

    const rid = discoverRoomId();
    if (rid && rid !== state.roomId) {
      log('room changed', state.roomId, '->', rid);
      state.roomId = rid;
      state.powerLevel = null;
      state.recording = false;
      clearInterval(state.timer);
    }
    if (!state.roomId) { warn('room id not found yet; will retry on DOM change'); }

    if (!inCall()) return;
    const point = insertionPoint();
    if (!point) return;

    ensureStyle();
    const ctrl = buildControl();
    point.appendChild(ctrl);
    log('button inserted into', point.className || point.tagName, 'for room', state.roomId);
  }

  function scheduleInsert() {
    if (insertScheduled) return;
    insertScheduled = true;
    // Debounce: Element re-renders the control bar many times per second while
    // a call is joining; inserting synchronously on each mutation flickers.
    requestAnimationFrame(() => setTimeout(insertButton, 120));
  }

  function boot() {
    log('plugin active', CFG);
    ensureStyle();

    const observer = new MutationObserver(scheduleInsert);
    observer.observe(document.documentElement, {
      childList: true, subtree: true,
      attributes: true, attributeFilter: ['class', 'data-testid'],
    });

    window.addEventListener('hashchange', scheduleInsert);
    window.addEventListener('popstate', scheduleInsert);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) scheduleInsert();
    });

    scheduleInsert();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
