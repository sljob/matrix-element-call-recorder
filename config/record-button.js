(function () {
  "use strict";
  if (window.self !== window.top) return;
  const API = "/recording/api/record/";
  let panel, start, stop, badge;
  let busy = false, checking = false, current = null, state = null;
  let revision = 0;

  function room() {
    const m = (location.hash || "").match(/#\/room\/(![^/?#]+)/);
    if (!m) return null;
    try { return decodeURIComponent(m[1]); } catch (_) { return m[1]; }
  }

  function token() {
    try {
      const t = window.mxMatrixClientPeg?.get?.()?.getAccessToken?.();
      if (t) return t;
    } catch (_) {}
    try { return localStorage.getItem("mx_access_token"); }
    catch (_) { return null; }
  }

  function draw(message, error = false) {
    if (!panel) return;
    start.disabled = busy || !current ||
      !["inactive", "finished", "stopped"].includes(state);
    stop.disabled = busy || !current || state !== "active";
    badge.textContent = message;
    badge.style.color = error ? "#b00020" : "#245c24";
  }

  async function api(action, id) {
    const t = token();
    if (!t) throw new Error("Нет авторизации в Element");
    const abort = new AbortController();
    const timer = setTimeout(
      () => abort.abort(), action === "status" ? 12000 : 90000
    );
    let url = API + action;
    const options = {
      headers: {Authorization: "Bearer " + t},
      cache: "no-store", signal: abort.signal
    };
    if (action === "status") {
      url += "?room=" + encodeURIComponent(id);
    } else {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify({room: id});
    }
    try {
      const response = await fetch(url, options);
      // RECORD_API_403_BEFORE_JSON_V1
      if (response.status === 403) {
        throw new Error("Недостаточно прав для управления записью");
      }
      const text = await response.text();
      let data;
      try { data = JSON.parse(text); }
      catch (_) { throw new Error("API: HTTP " + response.status + ", не JSON"); }
      if (!data || typeof data !== "object")
        throw new Error("Некорректный ответ API");
      if (!response.ok || data.error)
        throw new Error(data.error || ("HTTP " + response.status));
      return data;
    } catch (e) {
      if (e.name === "AbortError")
        throw new Error("Истекло время ожидания; состояние не подтверждено");
      throw e;
    } finally { clearTimeout(timer); }
  }

  async function sync() {
    if (busy || checking) return;
    const id = room();
    if (id !== current) {
      current = id; state = null; revision++;
      draw(id ? "Проверка…" : "Откройте комнату");
    }
    if (!id) return;
    const version = revision;
    checking = true;
    try {
      const data = await api("status", id);
      if (version !== revision || busy || room() !== id) return;
      if (!["active", "inactive", "finished", "stopped"].includes(data.status))
        throw new Error("Неизвестное состояние записи");
      state = data.status;
      draw(state === "active" ? "Идёт запись" : "Готов");
    } catch (e) {
      if (version === revision && !busy && room() === id) {
        state = null; draw(e.message, true);
      }
    } finally { checking = false; }
  }

  async function act(action) {
    const id = room();
    if (busy || !id || id !== current) return;
    if (action === "start" &&
        !["inactive", "finished", "stopped"].includes(state)) return;
    if (action === "stop" && state !== "active") return;
    revision++; busy = true;
    draw(action === "start" ? "Запуск…" : "Остановка…");
    try {
      await api(action, id);
      state = null;
      draw("Проверка результата…");
    } catch (e) {
      state = null; draw(e.message, true);
    } finally {
      busy = false;
      setTimeout(sync, 1000);
    }
  }

  function mount() {
    if (!document.body) return;
    if (panel && panel.isConnected) return;
    if (document.getElementById("record-btn-container")) return;

    panel = document.createElement("div");
    panel.id = "record-btn-container";
    panel.style.cssText =
      "position:fixed;top:60px;right:20px;z-index:9999;display:flex;" +
      "gap:8px;align-items:center;background:#f5f5f5;padding:10px;" +
      "border-radius:8px;font:13px sans-serif";
    const link = document.createElement("a");
    link.href = "/recordings/";
    link.target = "_blank"; link.rel = "noopener";
    link.textContent = "Записи";
    start = document.createElement("button");
    start.textContent = "🔴 Запись";
    start.onclick = () => act("start");
    stop = document.createElement("button");
    stop.textContent = "⏹ Стоп";
    stop.onclick = () => act("stop");
    badge = document.createElement("span");
    badge.id = "record-status-badge";
    panel.append(link, start, stop, badge);
    document.body.appendChild(panel);
    draw("Проверка…");
    void sync();
  }

  window.addEventListener("hashchange", () => {
    revision++; current = null; state = null;
    draw("Проверка…"); void sync();
  });
  new MutationObserver(mount).observe(
    document, {childList: true, subtree: true}
  );
  setInterval(() => { mount(); void sync(); }, 5000);
  mount();
})();
