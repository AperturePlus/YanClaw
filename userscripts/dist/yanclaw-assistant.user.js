// ==UserScript==
// @name         Yanclaw Assistant
// @namespace    https://github.com/yanclaw
// @version      1.0.0
// @description  Human-assisted crawler frontend for Yanclaw
// @match        *://*.edu.cn/*
// @match        *://*.ac.cn/*
// @connect      127.0.0.1
// @connect      localhost
// @grant        GM_addStyle
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_xmlhttpRequest
// ==/UserScript==

(function () {
  'use strict';

  const d=new Set;const importCSS = async e=>{d.has(e)||(d.add(e),(t=>{typeof GM_addStyle=="function"?GM_addStyle(t):(document.head||document.documentElement).appendChild(document.createElement("style")).append(t);})(e));};

  const styleCss = '#ycl-panel{position:fixed;bottom:16px;right:16px;z-index:2147483647;width:380px;max-height:80vh;overflow-y:auto;background:#1e1e2e;color:#cdd6f4;border-radius:12px;box-shadow:0 8px 32px #00000073;font:13px/1.5 system-ui,sans-serif;-webkit-user-select:none;user-select:none;transition:all .2s}#ycl-panel.ycl-minimized{width:48px;height:48px;overflow:hidden;border-radius:50%;cursor:pointer;display:flex;align-items:center;justify-content:center}#ycl-panel.ycl-minimized:after{content:"🦀";font-size:22px}#ycl-panel.ycl-minimized *{display:none!important}#ycl-header{display:flex;justify-content:space-between;align-items:center;padding:8px 12px;background:#313244;border-radius:12px 12px 0 0;cursor:move}#ycl-header span{font-weight:600;font-size:14px}#ycl-header button{background:none;border:none;color:#cdd6f4;cursor:pointer;font-size:16px;padding:0 4px}.ycl-section{padding:8px 12px;border-top:1px solid #45475a}.ycl-label{color:#a6adc8;font-size:11px;text-transform:uppercase;letter-spacing:.5px}.ycl-url{color:#89b4fa;word-break:break-all;font-size:12px}.ycl-intent{color:#f9e2af;margin:4px 0}.ycl-hint{color:#94e2d5;font-size:12px}.ycl-btn-row{display:flex;flex-wrap:wrap;gap:6px;padding:8px 12px}.ycl-btn{padding:5px 10px;border:none;border-radius:6px;cursor:pointer;font-size:12px;font-weight:500;transition:filter .15s}.ycl-btn:hover{filter:brightness(1.15)}.ycl-btn-primary{background:#89b4fa;color:#1e1e2e}.ycl-btn-success{background:#a6e3a1;color:#1e1e2e}.ycl-btn-warn{background:#f9e2af;color:#1e1e2e}.ycl-btn-danger{background:#f38ba8;color:#1e1e2e}.ycl-btn-muted{background:#585b70;color:#cdd6f4}.ycl-toggle{display:flex;align-items:center;gap:6px;padding:4px 12px}.ycl-toggle input{accent-color:#89b4fa}.ycl-history{max-height:120px;overflow-y:auto}.ycl-history-item{display:flex;justify-content:space-between;font-size:11px;padding:2px 0;color:#a6adc8}.ycl-status-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px;vertical-align:middle}.ycl-dot-on{background:#a6e3a1}.ycl-dot-off{background:#f38ba8}.ycl-match-banner{background:#a6e3a1;color:#1e1e2e;text-align:center;padding:6px;font-weight:600;font-size:12px}#ycl-toast{position:fixed;top:16px;right:16px;z-index:2147483647;background:#f38ba8;color:#1e1e2e;padding:8px 16px;border-radius:8px;font:13px system-ui,sans-serif;display:none}';
  importCSS(styleCss);
  var _GM_getValue = (() => typeof GM_getValue != "undefined" ? GM_getValue : void 0)();
  var _GM_setValue = (() => typeof GM_setValue != "undefined" ? GM_setValue : void 0)();
  var _GM_xmlhttpRequest = (() => typeof GM_xmlhttpRequest != "undefined" ? GM_xmlhttpRequest : void 0)();
  const API_BASE = "http://127.0.0.1:21520/api";
  const TIMEOUT = 1e4;
  function request(method, path, data) {
    return new Promise((resolve, reject) => {
      _GM_xmlhttpRequest({
        method,
        url: API_BASE + path,
        headers: { "Content-Type": "application/json" },
        data: data ? JSON.stringify(data) : void 0,
        timeout: TIMEOUT,
        onload(res) {
          if (res.status === 204) return resolve(null);
          try {
            resolve(JSON.parse(res.responseText));
          } catch {
            resolve(null);
          }
        },
        onerror: () => reject(new Error("network")),
        ontimeout: () => reject(new Error("timeout"))
      });
    });
  }
  async function fetchNextJob() {
    return request("GET", "/jobs/next");
  }
  async function completeJob(id, html, url, title) {
    return request("POST", `/jobs/${id}/complete`, { html, url, title });
  }
  async function failJob(id, message) {
    await request("POST", `/jobs/${id}/fail`, { message });
  }
  async function skipJob(id) {
    await request("POST", `/jobs/${id}/skip`);
  }
  async function overrideJobUrl(id, newUrl) {
    return request("POST", `/jobs/${id}/override`, { new_url: newUrl });
  }
  async function fetchStatus() {
    return request("GET", "/status");
  }
  const MAX_HISTORY = 20;
  const STORAGE_KEY = "ycl_state";
  const listeners = [];
  function loadPersisted() {
    try {
      const raw = _GM_getValue(STORAGE_KEY, "");
      if (!raw) return {};
      const p = JSON.parse(raw);
      return {
        currentJob: p.currentJob ?? null,
        autoMode: p.autoMode ?? false,
        paused: p.paused ?? false,
        minimized: p.minimized ?? false,
        history: (p.history ?? []).map((h) => ({
          ...h,
          status: h.status,
          time: new Date(h.time)
        }))
      };
    } catch {
      return {};
    }
  }
  function savePersisted() {
    const p = {
      currentJob: state.currentJob,
      autoMode: state.autoMode,
      paused: state.paused,
      minimized: state.minimized,
      history: state.history.map((h) => ({
        id: h.id,
        url: h.url,
        status: h.status,
        time: h.time.toISOString()
      }))
    };
    _GM_setValue(STORAGE_KEY, JSON.stringify(p));
  }
  const persisted = loadPersisted();
  const state = {
    currentJob: persisted.currentJob ?? null,
    autoMode: persisted.autoMode ?? false,
    paused: persisted.paused ?? false,
    connected: false,
    minimized: persisted.minimized ?? false,
    history: persisted.history ?? []
  };
  function subscribe(fn) {
    listeners.push(fn);
  }
  function notify() {
    savePersisted();
    for (const fn of listeners) fn();
  }
  function setJob(job) {
    state.currentJob = job;
    notify();
  }
  function clearJob() {
    state.currentJob = null;
    notify();
  }
  function addHistory(job, status) {
    state.history.unshift({ id: job.id, url: job.url, status, time: new Date() });
    if (state.history.length > MAX_HISTORY) state.history.pop();
  }
  function toggle(key) {
    state[key] = !state[key];
    notify();
  }
  let toastEl = null;
  let hideTimer = null;
  function mountToast() {
    toastEl = document.createElement("div");
    toastEl.id = "ycl-toast";
    document.body.appendChild(toastEl);
  }
  function showToast(msg, duration = 3e3) {
    if (!toastEl) return;
    toastEl.textContent = msg;
    toastEl.style.display = "block";
    if (hideTimer) clearTimeout(hideTimer);
    hideTimer = setTimeout(() => {
      if (toastEl) toastEl.style.display = "none";
    }, duration);
  }
  function urlMatches(a, b) {
    try {
      const u1 = new URL(a);
      const u2 = new URL(b);
      return u1.hostname === u2.hostname && u1.pathname.replace(/\/+$/, "") === u2.pathname.replace(/\/+$/, "");
    } catch {
      return false;
    }
  }
  function truncUrl(url, max = 40) {
    try {
      return new URL(url).pathname.slice(0, max);
    } catch {
      return url.slice(0, max);
    }
  }
  function timeAgo(date) {
    const s = Math.round((Date.now() - date.getTime()) / 1e3);
    if (s < 60) return `${s}s ago`;
    return `${Math.round(s / 60)}m ago`;
  }
  const STATUS_ICONS = {
    completed: "✅",
    skipped: "⏭",
    failed: "❌"
  };
  function statusIcon(status) {
    return STATUS_ICONS[status] ?? "❓";
  }
  const POLL_INTERVAL = 2e3;
  const AUTO_CHECK_INTERVAL = 1e3;
  const AUTO_SUBMIT_DELAY = 2e3;
  let autoCheckTimer = null;
  let submitting = false;
  async function recoverState() {
    try {
      const status = await fetchStatus();
      if (!status) return;
      state.connected = true;
      if (status.current_job) {
        setJob(status.current_job);
      } else if (state.currentJob) {
        clearJob();
      }
    } catch {
      state.connected = false;
    }
    notify();
  }
  function startPolling() {
    setInterval(pollNext, POLL_INTERVAL);
  }
  function startAutoWatcher() {
    if (autoCheckTimer !== null) return;
    autoCheckTimer = setInterval(autoCheck, AUTO_CHECK_INTERVAL);
  }
  let matchedSince = null;
  function autoCheck() {
    const job = state.currentJob;
    if (!job || !state.autoMode || state.paused || submitting) {
      matchedSince = null;
      return;
    }
    if (urlMatches(window.location.href, job.url)) {
      if (matchedSince === null) {
        matchedSince = Date.now();
      } else if (Date.now() - matchedSince >= AUTO_SUBMIT_DELAY) {
        matchedSince = null;
        submitCurrent();
      }
    } else {
      matchedSince = null;
    }
  }
  async function pollNext() {
    if (state.paused || state.currentJob) return;
    try {
      const job = await fetchNextJob();
      state.connected = true;
      if (job) assignJob(job);
    } catch {
      state.connected = false;
    }
    notify();
  }
  function assignJob(job) {
    setJob(job);
    if (state.autoMode) {
      window.location.href = job.url;
    }
  }
  async function submitCurrent() {
    const job = state.currentJob;
    if (!job || submitting) return;
    submitting = true;
    const html = document.documentElement.outerHTML;
    try {
      const res = await completeJob(job.id, html, window.location.href, document.title);
      addHistory(job, "completed");
      clearJob();
      if (res == null ? void 0 : res.next_job) {
        setTimeout(() => assignJob(res.next_job), 100);
      }
    } catch (e) {
      showToast(`提交失败: ${e instanceof Error ? e.message : e}`);
    }
    submitting = false;
    notify();
  }
  async function skipCurrent() {
    const job = state.currentJob;
    if (!job) return;
    try {
      await skipJob(job.id);
      addHistory(job, "skipped");
    } catch {
    }
    clearJob();
  }
  async function failCurrent(msg) {
    const job = state.currentJob;
    if (!job) return;
    try {
      await failJob(job.id, msg || "手动标记失败");
      addHistory(job, "failed");
    } catch {
    }
    clearJob();
  }
  async function overrideUrl() {
    const job = state.currentJob;
    if (!job) return;
    const url = prompt("输入正确的 URL:", job.url);
    if (!url) return;
    try {
      const updated = await overrideJobUrl(job.id, url);
      if (updated) setJob(updated);
    } catch {
    }
  }
  let panelEl = null;
  function mountPanel() {
    panelEl = document.createElement("div");
    panelEl.id = "ycl-panel";
    panelEl.addEventListener("click", () => {
      if (state.minimized) {
        toggle("minimized");
      }
    });
    document.body.appendChild(panelEl);
  }
  function renderPanel() {
    if (!panelEl) return;
    if (state.minimized) {
      panelEl.className = "ycl-minimized";
      panelEl.innerHTML = "";
      return;
    }
    panelEl.className = "";
    const job = state.currentJob;
    const matched = job != null && urlMatches(window.location.href, job.url);
    panelEl.innerHTML = [
      renderHeader(),
      matched ? renderMatchBanner() : "",
      job ? renderJobDetail(job) : renderEmpty(),
      job ? renderActions() : "",
      renderToggles(),
      renderHistory()
    ].join("");
    bindEvents();
  }
  function renderHeader() {
    const dot = state.connected ? "ycl-dot-on" : "ycl-dot-off";
    return `<div id="ycl-header">
    <span><span class="ycl-status-dot ${dot}"></span> Yanclaw Assistant</span>
    <button id="ycl-min" title="最小化">─</button>
  </div>`;
  }
  function renderMatchBanner() {
    return `<div class="ycl-match-banner">✅ 检测到目标页面 — 点击提交或等待自动提交</div>`;
  }
  function renderJobDetail(job) {
    var _a;
    const c = job.context;
    return `<div class="ycl-section">
    <div class="ycl-label">当前任务 #${job.id}</div>
    <div>大学: <b>${c.university_name || "-"}</b></div>
    <div>阶段: ${c.agent_state || "-"}</div>
    ${c.org_unit_name ? `<div>学院: ${c.org_unit_name}</div>` : ""}
    ${c.intent ? `<div class="ycl-intent">💡 ${c.intent}</div>` : ""}
    <div class="ycl-label" style="margin-top:4px">目标 URL</div>
    <div class="ycl-url">${job.url}</div>
    ${c.parent_url ? `<div style="margin-top:2px"><span class="ycl-label">来源</span> <span class="ycl-url">${truncUrl(c.parent_url, 60)}</span></div>` : ""}
    ${c.depth != null ? `<div>深度: ${c.depth}</div>` : ""}
    ${((_a = c.hints) == null ? void 0 : _a.length) ? `<div class="ycl-hint">💡 ${c.hints.join(" | ")}</div>` : ""}
  </div>`;
  }
  function renderEmpty() {
    const msg = state.connected ? "⏳ 等待新任务..." : "🔴 未连接到后端";
    return `<div class="ycl-section" style="text-align:center;padding:16px 12px;">${msg}</div>`;
  }
  function renderActions() {
    return `<div class="ycl-btn-row">
    <button class="ycl-btn ycl-btn-primary" id="ycl-copy">📋 复制URL</button>
    <button class="ycl-btn ycl-btn-primary" id="ycl-open">🔗 打开URL</button>
    <button class="ycl-btn ycl-btn-success" id="ycl-submit">✅ 提交当前页</button>
    <button class="ycl-btn ycl-btn-warn" id="ycl-skip">⏭ 跳过</button>
    <button class="ycl-btn ycl-btn-muted" id="ycl-override">✏️ 修改URL</button>
    <button class="ycl-btn ycl-btn-danger" id="ycl-fail">❌ 失败</button>
  </div>`;
  }
  function renderToggles() {
    return `<div class="ycl-toggle">
    <input type="checkbox" id="ycl-auto" ${state.autoMode ? "checked" : ""}>
    <label for="ycl-auto">自动模式 (自动导航+提交)</label>
  </div>
  <div class="ycl-toggle">
    <input type="checkbox" id="ycl-pause" ${state.paused ? "checked" : ""}>
    <label for="ycl-pause">暂停轮询</label>
  </div>`;
  }
  function renderHistory() {
    if (!state.history.length) return "";
    const items = state.history.map(
      (h) => `<div class="ycl-history-item"><span>${statusIcon(h.status)} ${truncUrl(h.url, 35)}</span><span>${timeAgo(h.time)}</span></div>`
    ).join("");
    return `<div class="ycl-section">
    <div class="ycl-label">历史 (${state.history.length})</div>
    <div class="ycl-history">${items}</div>
  </div>`;
  }
  function bindEvents() {
    const bind = (id, event, fn) => {
      var _a;
      (_a = document.getElementById(id)) == null ? void 0 : _a.addEventListener(event, fn);
    };
    const job = state.currentJob;
    bind("ycl-min", "click", () => toggle("minimized"));
    bind("ycl-copy", "click", () => {
      if (job) navigator.clipboard.writeText(job.url);
    });
    bind("ycl-open", "click", () => {
      if (job) window.location.href = job.url;
    });
    bind("ycl-submit", "click", submitCurrent);
    bind("ycl-skip", "click", skipCurrent);
    bind("ycl-fail", "click", () => failCurrent());
    bind("ycl-override", "click", overrideUrl);
    bind("ycl-auto", "change", () => {
      state.autoMode = !state.autoMode;
      notify();
    });
    bind("ycl-pause", "change", () => {
      state.paused = !state.paused;
      notify();
    });
  }
  mountToast();
  mountPanel();
  subscribe(renderPanel);
  recoverState().then(() => {
    renderPanel();
    startPolling();
    startAutoWatcher();
  });

})();