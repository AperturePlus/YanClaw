// ==UserScript==
// @name         Yanclaw Assistant
// @namespace    https://github.com/yanclaw
// @version      1.0.0
// @description  Human-assisted crawler frontend for Yanclaw
// @match        *://*.edu.cn/*
// @match        *://*.ac.cn/*
// @exclude      *://dx.scu.edu.cn/*
// @exclude      *://mail.scu.edu.cn/*
// @connect      127.0.0.1
// @connect      localhost
// @grant        GM.deleteValue
// @grant        GM.getValue
// @grant        GM.listValues
// @grant        GM.setValue
// @grant        GM.xmlHttpRequest
// @grant        GM_addStyle
// @grant        GM_deleteValue
// @grant        GM_getValue
// @grant        GM_listValues
// @grant        GM_setValue
// @grant        GM_xmlhttpRequest
// @run-at       document-idle
// @noframes
// ==/UserScript==

(function () {
  'use strict';

  const d=new Set;const importCSS = async e=>{d.has(e)||(d.add(e),(t=>{typeof GM_addStyle=="function"?GM_addStyle(t):(document.head||document.documentElement).appendChild(document.createElement("style")).append(t);})(e));};

  const styleCss = '#ycl-panel{position:fixed;bottom:16px;right:16px;z-index:2147483647;width:380px;max-height:80vh;overflow-y:auto;background:#1e1e2e;color:#cdd6f4;border-radius:12px;box-shadow:0 8px 32px #00000073;font:13px/1.5 system-ui,sans-serif;-webkit-user-select:none;user-select:none;transition:all .2s}#ycl-panel.ycl-minimized{width:48px;height:48px;overflow:hidden;border-radius:50%;cursor:pointer;display:flex;align-items:center;justify-content:center}#ycl-panel.ycl-minimized:after{content:"🦀";font-size:22px}#ycl-panel.ycl-minimized *{display:none!important}#ycl-header{display:flex;justify-content:space-between;align-items:center;padding:8px 12px;background:#313244;border-radius:12px 12px 0 0;cursor:move}#ycl-header span{font-weight:600;font-size:14px}#ycl-header button{background:none;border:none;color:#cdd6f4;cursor:pointer;font-size:16px;padding:0 4px}.ycl-section{padding:8px 12px;border-top:1px solid #45475a}.ycl-label{color:#a6adc8;font-size:11px;text-transform:uppercase;letter-spacing:.5px}.ycl-url{color:#89b4fa;word-break:break-all;font-size:12px}.ycl-intent{color:#f9e2af;margin:4px 0}.ycl-hint{color:#94e2d5;font-size:12px}.ycl-btn-row{display:flex;flex-wrap:wrap;gap:6px;padding:8px 12px}.ycl-btn{padding:5px 10px;border:none;border-radius:6px;cursor:pointer;font-size:12px;font-weight:500;transition:filter .15s}.ycl-btn:hover{filter:brightness(1.15)}.ycl-btn:disabled{cursor:not-allowed;opacity:.55;filter:none}.ycl-btn-primary{background:#89b4fa;color:#1e1e2e}.ycl-btn-success{background:#a6e3a1;color:#1e1e2e}.ycl-btn-warn{background:#f9e2af;color:#1e1e2e}.ycl-btn-danger{background:#f38ba8;color:#1e1e2e}.ycl-btn-muted{background:#585b70;color:#cdd6f4}.ycl-toggle{display:flex;align-items:center;gap:6px;padding:4px 12px}.ycl-toggle input{accent-color:#89b4fa}.ycl-history{max-height:120px;overflow-y:auto}.ycl-history-item{display:flex;justify-content:space-between;font-size:11px;padding:2px 0;color:#a6adc8}.ycl-status-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px;vertical-align:middle}.ycl-dot-on{background:#a6e3a1}.ycl-dot-off{background:#f38ba8}.ycl-match-banner{background:#a6e3a1;color:#1e1e2e;text-align:center;padding:6px;font-weight:600;font-size:12px}.ycl-standby-banner{background:#fab387;color:#1e1e2e;text-align:center;padding:6px;font-weight:600;font-size:12px}.ycl-role-badge{display:inline-flex;align-items:center;margin-left:6px;padding:0 6px;border-radius:999px;font-size:10px;line-height:16px;vertical-align:middle}.ycl-role-owner{background:#a6e3a1;color:#1e1e2e}.ycl-role-standby{background:#f9e2af;color:#1e1e2e}#ycl-toast{position:fixed;top:16px;right:16px;z-index:2147483647;background:#f38ba8;color:#1e1e2e;padding:8px 16px;border-radius:8px;font:13px system-ui,sans-serif;display:none}';
  importCSS(styleCss);
  var _GM = (() => typeof GM != "undefined" ? GM : void 0)();
  var _GM_deleteValue = (() => typeof GM_deleteValue != "undefined" ? GM_deleteValue : void 0)();
  var _GM_getValue = (() => typeof GM_getValue != "undefined" ? GM_getValue : void 0)();
  var _GM_listValues = (() => typeof GM_listValues != "undefined" ? GM_listValues : void 0)();
  var _GM_setValue = (() => typeof GM_setValue != "undefined" ? GM_setValue : void 0)();
  var _GM_xmlhttpRequest = (() => typeof GM_xmlhttpRequest != "undefined" ? GM_xmlhttpRequest : void 0)();
  const BLOCKED_HOSTS = new Set([
    "dx.scu.edu.cn",
    "mail.scu.edu.cn"
  ]);
  function isAssistantBlockedHost(hostname = window.location.hostname) {
    return BLOCKED_HOSTS.has((hostname || "").toLowerCase());
  }
  const API_BASE = "http://127.0.0.1:21520/api";
  const TIMEOUT = 1e4;
  function parseJson(text) {
    if (!text) return null;
    try {
      return JSON.parse(text);
    } catch {
      return null;
    }
  }
  function requestWithLegacyApi(method, url, body) {
    return new Promise((resolve, reject) => {
      _GM_xmlhttpRequest({
        method,
        url,
        headers: { "Content-Type": "application/json" },
        data: body,
        timeout: TIMEOUT,
        onload(res) {
          if (res.status === 204) return resolve(null);
          resolve(parseJson(res.responseText));
        },
        onerror: () => reject(new Error("network")),
        ontimeout: () => reject(new Error("timeout"))
      });
    });
  }
  async function requestWithModernApi(method, url, body) {
    let timeoutHandle = null;
    const requestPromise = _GM.xmlHttpRequest({
      method,
      url,
      headers: { "Content-Type": "application/json" },
      data: body
    });
    const timeoutPromise = new Promise((_, reject) => {
      timeoutHandle = setTimeout(() => reject(new Error("timeout")), TIMEOUT);
    });
    const res = await Promise.race([requestPromise, timeoutPromise]);
    if (timeoutHandle !== null) {
      clearTimeout(timeoutHandle);
    }
    if (res.status === 204) return null;
    return parseJson(res.responseText);
  }
  function request(method, path, data) {
    if (isAssistantBlockedHost()) {
      return Promise.resolve(null);
    }
    const url = API_BASE + path;
    const body = data ? JSON.stringify(data) : void 0;
    if (typeof _GM_xmlhttpRequest === "function") {
      return requestWithLegacyApi(method, url, body);
    }
    if (typeof (_GM == null ? void 0 : _GM.xmlHttpRequest) === "function") {
      return requestWithModernApi(method, url, body);
    }
    return Promise.reject(new Error("GM_xmlhttpRequest unavailable"));
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
  async function fetchDecision() {
    return request("GET", "/decision");
  }
  async function resolveDecision(id, action) {
    await request("POST", `/decision/${id}/resolve`, { action });
  }
  const YCL_PREFIX = "ycl_";
  const UI_PREFS_KEY = "ycl_ui_prefs_v2";
  const INSTANCE_LOCK_KEY = "ycl_instance_lock_v1";
  const MAX_HISTORY = 20;
  const LEGACY_LOCAL_FALLBACK_KEY = "ycl_state_fallback";
  const listeners = [];
  const prefsFallbackKey = `${UI_PREFS_KEY}_fallback`;
  const cleanupWhitelist = new Set([UI_PREFS_KEY, prefsFallbackKey, INSTANCE_LOCK_KEY]);
  const PREFS_WRITE_DELAY = 150;
  let prefsWriteTimer = null;
  let prefsHydrated = false;
  let hydratePrefsPromise = null;
  let lastPrefsRaw = "";
  const state = {
    currentJob: null,
    autoMode: false,
    paused: false,
    connected: false,
    minimized: false,
    pendingDecision: null,
    history: [],
    instanceRole: "standby"
  };
  function isCleanupTarget(key) {
    return key.startsWith(YCL_PREFIX) && !cleanupWhitelist.has(key);
  }
  function readLocal(key) {
    try {
      return localStorage.getItem(key) ?? "";
    } catch {
      return "";
    }
  }
  function writeLocal(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch {
    }
  }
  function removeLocal(key) {
    try {
      localStorage.removeItem(key);
    } catch {
    }
  }
  function parsePrefs(raw) {
    if (!raw) {
      return { autoMode: false, minimized: false };
    }
    try {
      const parsed = JSON.parse(raw);
      return {
        autoMode: parsed.autoMode ?? false,
        minimized: parsed.minimized ?? false
      };
    } catch {
      return { autoMode: false, minimized: false };
    }
  }
  function snapshotPrefsRaw() {
    const prefs = {
      autoMode: state.autoMode,
      minimized: state.minimized
    };
    return JSON.stringify(prefs);
  }
  function flushPrefs() {
    const raw = snapshotPrefsRaw();
    if (raw === lastPrefsRaw) return;
    lastPrefsRaw = raw;
    if (typeof _GM_setValue === "function") {
      _GM_setValue(UI_PREFS_KEY, raw);
      return;
    }
    if (typeof (_GM == null ? void 0 : _GM.setValue) === "function") {
      void _GM.setValue(UI_PREFS_KEY, raw);
      return;
    }
    writeLocal(prefsFallbackKey, raw);
  }
  function schedulePrefsPersist() {
    if (prefsWriteTimer !== null) return;
    prefsWriteTimer = setTimeout(() => {
      prefsWriteTimer = null;
      flushPrefs();
    }, PREFS_WRITE_DELAY);
  }
  async function loadPrefsRaw() {
    try {
      if (typeof _GM_getValue === "function") {
        return _GM_getValue(UI_PREFS_KEY, "");
      }
      if (typeof (_GM == null ? void 0 : _GM.getValue) === "function") {
        return await _GM.getValue(UI_PREFS_KEY, "");
      }
      return readLocal(prefsFallbackKey);
    } catch {
      return "";
    }
  }
  async function listGMKeys() {
    try {
      if (typeof _GM_listValues === "function") {
        return _GM_listValues();
      }
      if (typeof (_GM == null ? void 0 : _GM.listValues) === "function") {
        return await _GM.listValues();
      }
    } catch {
    }
    return [];
  }
  async function deleteGMKey(key) {
    try {
      if (typeof _GM_deleteValue === "function") {
        _GM_deleteValue(key);
        return;
      }
      if (typeof (_GM == null ? void 0 : _GM.deleteValue) === "function") {
        await _GM.deleteValue(key);
      }
    } catch {
    }
  }
  async function cleanupLegacyStorage() {
    try {
      const keysToDelete = [];
      for (let i = 0; i < localStorage.length; i += 1) {
        const key = localStorage.key(i);
        if (!key) continue;
        if (isCleanupTarget(key)) {
          keysToDelete.push(key);
        }
      }
      keysToDelete.push(LEGACY_LOCAL_FALLBACK_KEY);
      for (const key of keysToDelete) {
        removeLocal(key);
      }
    } catch {
    }
    const gmKeys = await listGMKeys();
    for (const key of gmKeys) {
      if (isCleanupTarget(key)) {
        await deleteGMKey(key);
      }
    }
  }
  async function hydratePrefs() {
    if (prefsHydrated) return;
    if (hydratePrefsPromise) return hydratePrefsPromise;
    hydratePrefsPromise = (async () => {
      const raw = await loadPrefsRaw();
      const prefs = parsePrefs(raw);
      state.autoMode = prefs.autoMode;
      state.minimized = prefs.minimized;
      lastPrefsRaw = snapshotPrefsRaw();
      prefsHydrated = true;
    })();
    return hydratePrefsPromise;
  }
  if (typeof window !== "undefined") {
    window.addEventListener("beforeunload", () => {
      if (prefsWriteTimer !== null) {
        clearTimeout(prefsWriteTimer);
        prefsWriteTimer = null;
      }
      flushPrefs();
    });
  }
  function subscribe(fn) {
    listeners.push(fn);
  }
  function notify() {
    for (const fn of listeners) fn();
  }
  function setInstanceRole(role) {
    if (state.instanceRole === role) return;
    state.instanceRole = role;
    notify();
  }
  function setAutoMode(value) {
    if (state.autoMode === value) return;
    state.autoMode = value;
    schedulePrefsPersist();
    notify();
  }
  function setMinimized(value) {
    if (state.minimized === value) return;
    state.minimized = value;
    schedulePrefsPersist();
    notify();
  }
  function togglePaused() {
    state.paused = !state.paused;
    notify();
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
      return u1.hostname === u2.hostname && u1.pathname.replace(/\/+$/, "") === u2.pathname.replace(/\/+$/, "") && normalizedSearch(u1) === normalizedSearch(u2);
    } catch {
      return false;
    }
  }
  function normalizedSearch(url) {
    if (!url.search) return "";
    const params = [...url.searchParams.entries()].sort(([aKey, aValue], [bKey, bValue]) => {
      const keyOrder = aKey.localeCompare(bKey);
      return keyOrder || aValue.localeCompare(bValue);
    });
    return params.map(([key, value]) => `${key}=${value}`).join("&");
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
  const ERROR_PATTERNS = /502 bad gateway|503 service|504 gateway|500 internal|error occurred|server error|nginx/i;
  function isErrorPage() {
    var _a;
    const title = document.title || "";
    const bodyText = ((_a = document.body) == null ? void 0 : _a.innerText) || "";
    if (bodyText.length < 2e3 && ERROR_PATTERNS.test(title + " " + bodyText)) return true;
    return false;
  }
  const POLL_INTERVAL = 2e3;
  const FAST_POLL_INTERVAL = 500;
  const FAST_POLL_ROUNDS = 4;
  const AUTO_CHECK_INTERVAL = 1500;
  const AUTO_SUBMIT_DELAY = 1500;
  const CAPTURE_STABLE_INTERVAL = 200;
  const CAPTURE_STABLE_ROUNDS = 3;
  const CAPTURE_MAX_WAIT = 8e3;
  const DECISION_POLL_INTERVAL = 5e3;
  const ERROR_RETRY_DELAY = 5e3;
  const MAX_ERROR_RETRIES = 3;
  const DEFAULT_DECISION_ACTION = "switch_failed_to_human";
  let pollTimer = null;
  let autoCheckTimer = null;
  let submitting = false;
  let polling = false;
  let decisionPromptedId = null;
  let resolvingDecision = false;
  let lastDecisionCheckAt = 0;
  async function recoverState() {
    var _a, _b, _c;
    let changed = false;
    try {
      const status = await fetchStatus();
      if (!status) {
        if (state.connected) {
          state.connected = false;
          changed = true;
        }
        if (changed) notify();
        return;
      }
      if (!state.connected) {
        state.connected = true;
        changed = true;
      }
      const pendingId = ((_a = status.pending_decision) == null ? void 0 : _a.id) ?? null;
      if ((((_b = state.pendingDecision) == null ? void 0 : _b.id) ?? null) !== pendingId) {
        changed = true;
      }
      state.pendingDecision = status.pending_decision ?? null;
      if (status.current_job) {
        if ((((_c = state.currentJob) == null ? void 0 : _c.id) ?? null) !== status.current_job.id) {
          state.currentJob = status.current_job;
          changed = true;
        }
      } else if (state.currentJob !== null) {
        state.currentJob = null;
        changed = true;
      }
    } catch {
      if (state.connected) {
        state.connected = false;
        changed = true;
      }
    }
    if (changed) {
      notify();
    }
  }
  function startPolling() {
    if (pollTimer !== null) return;
    void pollNext();
    pollTimer = setInterval(() => {
      void pollNext();
    }, POLL_INTERVAL);
  }
  function stopPolling() {
    if (pollTimer !== null) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }
  function startAutoWatcher() {
    if (autoCheckTimer !== null) return;
    autoCheckTimer = setInterval(autoCheck, AUTO_CHECK_INTERVAL);
  }
  function stopAutoWatcher() {
    if (autoCheckTimer !== null) {
      clearInterval(autoCheckTimer);
      autoCheckTimer = null;
    }
  }
  let matchedSince = null;
  let errorRetries = 0;
  function autoCheck() {
    if (state.instanceRole !== "owner") return;
    const job = state.currentJob;
    if (!job || !state.autoMode || state.paused || submitting) {
      matchedSince = null;
      return;
    }
    if (isErrorPage()) {
      matchedSince = null;
      if (errorRetries < MAX_ERROR_RETRIES) {
        errorRetries++;
        showToast(`错误页面，${ERROR_RETRY_DELAY / 1e3}s 后重试 (${errorRetries}/${MAX_ERROR_RETRIES})`);
        setTimeout(() => {
          window.location.href = job.url;
        }, ERROR_RETRY_DELAY);
      } else {
        showToast("重试次数已用完，请手动处理");
      }
      return;
    }
    errorRetries = 0;
    if (urlMatches(window.location.href, job.url)) {
      if (matchedSince === null) {
        matchedSince = Date.now();
      } else if (Date.now() - matchedSince >= AUTO_SUBMIT_DELAY) {
        matchedSince = null;
        void submitCurrent();
      }
    } else {
      matchedSince = null;
    }
  }
  async function pollNext() {
    if (state.instanceRole !== "owner") return;
    const now = Date.now();
    if (now - lastDecisionCheckAt >= DECISION_POLL_INTERVAL) {
      lastDecisionCheckAt = now;
      await checkPendingDecision();
    }
    if (document.visibilityState === "hidden" && !state.currentJob) return;
    if (state.paused || state.currentJob || polling) return;
    const connectedBefore = state.connected;
    let jobAssigned = false;
    polling = true;
    try {
      const job = await fetchNextJob();
      state.connected = true;
      if (job) {
        assignJob(job);
        jobAssigned = true;
      }
    } catch {
      state.connected = false;
    } finally {
      polling = false;
    }
    if (!jobAssigned && state.connected !== connectedBefore) {
      notify();
    }
  }
  function assignJob(job) {
    errorRetries = 0;
    setJob(job);
    if (state.autoMode) {
      window.location.href = job.url;
    }
  }
  function triggerFastPollBurst() {
    if (state.paused || state.currentJob) return;
    for (let i = 0; i < FAST_POLL_ROUNDS; i += 1) {
      setTimeout(() => {
        void pollNext();
      }, i * FAST_POLL_INTERVAL);
    }
  }
  async function checkPendingDecision() {
    var _a;
    if (state.instanceRole !== "owner") return;
    if (resolvingDecision) return;
    const previousId = ((_a = state.pendingDecision) == null ? void 0 : _a.id) ?? null;
    try {
      const decision = await fetchDecision();
      state.pendingDecision = decision;
      const nextId = (decision == null ? void 0 : decision.id) ?? null;
      if (previousId !== nextId) {
        notify();
      }
      if (!decision) {
        decisionPromptedId = null;
        return;
      }
      if (decisionPromptedId === decision.id) return;
      decisionPromptedId = decision.id;
      showToast(`详情补抓连续失败 ${decision.failure_count} 次，等待人工决策`);
      const accepted = window.confirm(
        [
          `院系：${decision.org_unit_name || "Unknown"}`,
          `后台详情抓取连续失败：${decision.failure_count}`,
          "点击“确定”将失败链接切人工处理。"
        ].join("\n")
      );
      if (accepted) {
        await resolvePendingDecision(decision, DEFAULT_DECISION_ACTION);
      }
    } catch {
    }
  }
  async function resolvePendingDecision(decision, action) {
    if (resolvingDecision) return;
    resolvingDecision = true;
    try {
      await resolveDecision(decision.id, action);
      state.pendingDecision = null;
      decisionPromptedId = null;
      showToast("已切换为人工处理详情失败链接");
    } catch (e) {
      showToast(`决策提交失败: ${e instanceof Error ? e.message : e}`);
    }
    resolvingDecision = false;
    notify();
  }
  async function switchPendingDecisionToHuman() {
    if (state.instanceRole !== "owner") return;
    const decision = state.pendingDecision;
    if (!decision) return;
    await resolvePendingDecision(decision, DEFAULT_DECISION_ACTION);
  }
  async function submitCurrent() {
    if (state.instanceRole !== "owner") return;
    const job = state.currentJob;
    if (!job || submitting) return;
    if (isErrorPage()) {
      showToast("当前是错误页面，无法提交");
      return;
    }
    submitting = true;
    try {
      const html = await captureCurrentHtml();
      const res = await completeJob(job.id, html, window.location.href, document.title);
      addHistory(job, "completed");
      clearJob();
      if (res == null ? void 0 : res.next_job) {
        setTimeout(() => assignJob(res.next_job), 100);
      } else {
        triggerFastPollBurst();
      }
    } catch (e) {
      showToast(`提交失败: ${e instanceof Error ? e.message : e}`);
    }
    submitting = false;
    notify();
  }
  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
  async function captureCurrentHtml() {
    await waitForCaptureReady();
    return serializePageWithoutOverlay();
  }
  async function waitForCaptureReady() {
    var _a;
    const started2 = Date.now();
    let lastSignature = "";
    let stableRounds = 0;
    let scrolled = false;
    while (Date.now() - started2 < CAPTURE_MAX_WAIT) {
      if (document.readyState === "complete") {
        if (!scrolled && Date.now() - started2 >= Math.floor(AUTO_SUBMIT_DELAY / 2)) {
          scrolled = true;
          window.scrollTo({ top: ((_a = document.body) == null ? void 0 : _a.scrollHeight) ?? 0, behavior: "auto" });
        }
        const signature = captureSignature();
        if (signature === lastSignature) {
          stableRounds += 1;
        } else {
          lastSignature = signature;
          stableRounds = 0;
        }
        if (Date.now() - started2 >= AUTO_SUBMIT_DELAY && stableRounds >= CAPTURE_STABLE_ROUNDS) {
          return;
        }
      }
      await sleep(CAPTURE_STABLE_INTERVAL);
    }
  }
  function captureSignature() {
    var _a, _b;
    const textLength = ((_b = (_a = document.body) == null ? void 0 : _a.innerText) == null ? void 0 : _b.length) ?? 0;
    const nodeCount = document.getElementsByTagName("*").length;
    const imageCount = document.images.length;
    return `${textLength}:${nodeCount}:${imageCount}`;
  }
  function serializePageWithoutOverlay() {
    const clone = document.documentElement.cloneNode(true);
    clone.querySelectorAll("#ycl-panel,#ycl-toast,[data-yanclaw-overlay]").forEach((node) => node.remove());
    return clone.outerHTML;
  }
  async function skipCurrent() {
    if (state.instanceRole !== "owner") return;
    const job = state.currentJob;
    if (!job) return;
    try {
      await skipJob(job.id);
      addHistory(job, "skipped");
    } catch {
    }
    clearJob();
    triggerFastPollBurst();
  }
  async function failCurrent(msg) {
    if (state.instanceRole !== "owner") return;
    const job = state.currentJob;
    if (!job) return;
    try {
      await failJob(job.id, msg || "手动标记失败");
      addHistory(job, "failed");
    } catch {
    }
    clearJob();
    triggerFastPollBurst();
  }
  async function overrideUrl() {
    if (state.instanceRole !== "owner") return;
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
  const HEARTBEAT_INTERVAL = 2e3;
  const STALE_TIMEOUT = 7e3;
  const LOCK_VERSION = 1;
  const LOCK_SCOPE = "global";
  const tabId = (() => {
    try {
      if (typeof (crypto == null ? void 0 : crypto.randomUUID) === "function") {
        return crypto.randomUUID();
      }
    } catch {
    }
    return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  })();
  let tickTimer = null;
  let currentRole = "standby";
  let onRoleChange$1 = null;
  let started = false;
  function safeReadLock() {
    try {
      const raw = localStorage.getItem(INSTANCE_LOCK_KEY);
      if (!raw) return null;
      const lock = JSON.parse(raw);
      if (!(lock == null ? void 0 : lock.ownerTabId) || typeof lock.heartbeatAt !== "number") return null;
      return lock;
    } catch {
      return null;
    }
  }
  function safeWriteLock(record) {
    try {
      localStorage.setItem(INSTANCE_LOCK_KEY, JSON.stringify(record));
    } catch {
    }
  }
  function isStale(lock) {
    return Date.now() - lock.heartbeatAt > STALE_TIMEOUT;
  }
  function setRole(nextRole) {
    if (currentRole === nextRole) return;
    currentRole = nextRole;
    onRoleChange$1 == null ? void 0 : onRoleChange$1(nextRole);
  }
  function claimLock() {
    const candidate = {
      version: LOCK_VERSION,
      scope: LOCK_SCOPE,
      ownerTabId: tabId,
      ownerHost: window.location.hostname,
      heartbeatAt: Date.now()
    };
    safeWriteLock(candidate);
    const written = safeReadLock();
    return (written == null ? void 0 : written.ownerTabId) === tabId;
  }
  function refreshTick() {
    const lock = safeReadLock();
    if (!lock || lock.ownerTabId === tabId || isStale(lock)) {
      if (claimLock()) {
        setRole("owner");
        return;
      }
    }
    setRole("standby");
  }
  function releaseIfOwner() {
    const lock = safeReadLock();
    if (!lock || lock.ownerTabId !== tabId) return;
    try {
      localStorage.removeItem(INSTANCE_LOCK_KEY);
    } catch {
    }
  }
  function onStorageEvent(event) {
    if (event.key !== INSTANCE_LOCK_KEY) return;
    refreshTick();
  }
  function startInstanceLock(onChange) {
    if (started) return;
    started = true;
    onRoleChange$1 = onChange;
    window.addEventListener("storage", onStorageEvent);
    window.addEventListener("beforeunload", releaseIfOwner);
    refreshTick();
    tickTimer = setInterval(refreshTick, HEARTBEAT_INTERVAL);
  }
  function stopInstanceLock() {
    if (!started) return;
    started = false;
    if (tickTimer !== null) {
      clearInterval(tickTimer);
      tickTimer = null;
    }
    window.removeEventListener("storage", onStorageEvent);
    window.removeEventListener("beforeunload", releaseIfOwner);
    releaseIfOwner();
    onRoleChange$1 = null;
    currentRole = "standby";
  }
  let panelEl = null;
  function mountPanel() {
    panelEl = document.createElement("div");
    panelEl.id = "ycl-panel";
    panelEl.addEventListener("click", (event) => {
      if (state.minimized && event.target === event.currentTarget) {
        setMinimized(false);
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
    const isStandby = state.instanceRole !== "owner";
    const job = state.currentJob;
    const matched = job != null && urlMatches(window.location.href, job.url);
    panelEl.innerHTML = [
      renderHeader(),
      isStandby ? renderStandbyBanner() : "",
      renderDecision(isStandby),
      matched ? renderMatchBanner() : "",
      job ? renderJobDetail(job) : renderEmpty(isStandby),
      job ? renderActions(isStandby) : "",
      renderToggles(isStandby),
      renderHistory()
    ].join("");
    bindEvents();
  }
  function renderHeader() {
    const dot = state.connected ? "ycl-dot-on" : "ycl-dot-off";
    const roleLabel = state.instanceRole === "owner" ? "主实例" : "待机";
    const roleClass = state.instanceRole === "owner" ? "ycl-role-owner" : "ycl-role-standby";
    return `<div id="ycl-header">
    <span><span class="ycl-status-dot ${dot}"></span> Yanclaw Assistant <span class="ycl-role-badge ${roleClass}">${roleLabel}</span></span>
    <button id="ycl-min" title="最小化">─</button>
  </div>`;
  }
  function renderStandbyBanner() {
    return `<div class="ycl-standby-banner">当前 Tab 为待机实例，由其他 Tab 执行轮询与操作</div>`;
  }
  function renderMatchBanner() {
    return `<div class="ycl-match-banner">✅ 检测到目标页面 — 点击提交或等待自动提交</div>`;
  }
  function renderDecision(disabled) {
    var _a;
    const decision = state.pendingDecision;
    if (!decision) return "";
    const sample = ((_a = decision.sample_urls) == null ? void 0 : _a[0]) || "-";
    const disableAttr = disabled ? "disabled" : "";
    return `<div class="ycl-section" style="border-left:3px solid #f9e2af;">
    <div class="ycl-label">待决策</div>
    <div>院系: <b>${decision.org_unit_name || "-"}</b></div>
    <div>连续失败: ${decision.failure_count}</div>
    <div class="ycl-url" style="margin:4px 0">${truncUrl(sample, 60)}</div>
    <button class="ycl-btn ycl-btn-warn" id="ycl-decision-switch" ${disableAttr}>失败链接切人工</button>
  </div>`;
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
  function renderEmpty(isStandby) {
    const msg = isStandby ? "📡 待机中，等待主实例接管任务" : state.connected ? "⏳ 等待新任务..." : "🔴 未连接到后端";
    return `<div class="ycl-section" style="text-align:center;padding:16px 12px;">${msg}</div>`;
  }
  function renderActions(disabled) {
    const disableAttr = disabled ? "disabled" : "";
    return `<div class="ycl-btn-row">
    <button class="ycl-btn ycl-btn-primary" id="ycl-copy" ${disableAttr}>📋 复制URL</button>
    <button class="ycl-btn ycl-btn-primary" id="ycl-open" ${disableAttr}>🔗 打开URL</button>
    <button class="ycl-btn ycl-btn-success" id="ycl-submit" ${disableAttr}>✅ 提交当前页</button>
    <button class="ycl-btn ycl-btn-warn" id="ycl-skip" ${disableAttr}>⏭ 跳过</button>
    <button class="ycl-btn ycl-btn-muted" id="ycl-override" ${disableAttr}>✏️ 修改URL</button>
    <button class="ycl-btn ycl-btn-danger" id="ycl-fail" ${disableAttr}>❌ 失败</button>
  </div>`;
  }
  function renderToggles(disabled) {
    const disableAttr = disabled ? "disabled" : "";
    return `<div class="ycl-toggle">
    <input type="checkbox" id="ycl-auto" ${state.autoMode ? "checked" : ""} ${disableAttr}>
    <label for="ycl-auto">自动模式 (自动导航+提交)</label>
  </div>
  <div class="ycl-toggle">
    <input type="checkbox" id="ycl-pause" ${state.paused ? "checked" : ""} ${disableAttr}>
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
    var _a;
    const bind = (id, event, fn) => {
      var _a2;
      (_a2 = document.getElementById(id)) == null ? void 0 : _a2.addEventListener(event, fn);
    };
    const job = state.currentJob;
    (_a = document.getElementById("ycl-min")) == null ? void 0 : _a.addEventListener("click", (event) => {
      event.stopPropagation();
      setMinimized(true);
    });
    bind("ycl-copy", "click", () => {
      if (job) void navigator.clipboard.writeText(job.url);
    });
    bind("ycl-open", "click", () => {
      if (job) window.location.href = job.url;
    });
    bind("ycl-submit", "click", submitCurrent);
    bind("ycl-skip", "click", skipCurrent);
    bind("ycl-fail", "click", () => failCurrent());
    bind("ycl-override", "click", overrideUrl);
    bind("ycl-decision-switch", "click", () => {
      void switchPendingDecisionToHuman();
    });
    bind("ycl-auto", "change", () => {
      setAutoMode(!state.autoMode);
    });
    bind("ycl-pause", "change", () => {
      togglePaused();
    });
  }
  function isTopFrame() {
    try {
      return window.top === window.self;
    } catch {
      return false;
    }
  }
  async function waitForBody() {
    if (document.body) return;
    await new Promise((resolve) => {
      const onReady = () => resolve();
      document.addEventListener("DOMContentLoaded", onReady, { once: true });
    });
  }
  async function onRoleChange(role) {
    const previousRole = state.instanceRole;
    setInstanceRole(role);
    if (role === "owner") {
      await recoverState();
      startPolling();
      startAutoWatcher();
      if (previousRole !== "owner") {
        showToast("已接管为主实例");
      }
      return;
    }
    stopPolling();
    stopAutoWatcher();
  }
  async function bootstrap() {
    if (isAssistantBlockedHost() || !isTopFrame()) return;
    await waitForBody();
    await cleanupLegacyStorage();
    await hydratePrefs();
    mountToast();
    mountPanel();
    subscribe(renderPanel);
    renderPanel();
    startInstanceLock((role) => {
      void onRoleChange(role);
    });
    window.addEventListener("beforeunload", () => {
      stopInstanceLock();
    });
  }
  void bootstrap();

})();