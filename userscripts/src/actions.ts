import * as api from './api';
import { addHistory, clearJob, notify, setJob, state } from './state';
import { showToast } from './ui/toast';
import { isErrorPage, sameHost, urlMatches } from './utils';

const POLL_INTERVAL = 2000;
const AUTO_CHECK_INTERVAL = 1000;
const AUTO_SUBMIT_DELAY = 2000;
const ERROR_RETRY_DELAY = 5000;
const MAX_ERROR_RETRIES = 3;

let pollTimer: ReturnType<typeof setInterval> | null = null;
let autoCheckTimer: ReturnType<typeof setInterval> | null = null;
let submitting = false;

/** Sync persisted state with backend on page load. */
export async function recoverState(): Promise<void> {
  try {
    const status = await api.fetchStatus();
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

export function startPolling(): void {
  pollTimer = setInterval(pollNext, POLL_INTERVAL);
}

export function stopPolling(): void {
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

/**
 * Start a persistent watcher that auto-submits when the current page
 * matches the job URL. Runs every second so it survives redirects,
 * late JS rendering, and page load timing issues.
 */
export function startAutoWatcher(): void {
  if (autoCheckTimer !== null) return;
  autoCheckTimer = setInterval(autoCheck, AUTO_CHECK_INTERVAL);
}

let matchedSince: number | null = null;
let errorRetries = 0;

function autoCheck(): void {
  const job = state.currentJob;
  if (!job || !state.autoMode || state.paused || submitting) {
    matchedSince = null;
    return;
  }

  // Detect error pages (502, 503, etc.) — auto-retry navigation.
  if (isErrorPage()) {
    matchedSince = null;
    if (errorRetries < MAX_ERROR_RETRIES) {
      errorRetries++;
      showToast(`错误页面，${ERROR_RETRY_DELAY / 1000}s 后重试 (${errorRetries}/${MAX_ERROR_RETRIES})`);
      setTimeout(() => { window.location.href = job.url; }, ERROR_RETRY_DELAY);
    } else {
      showToast('重试次数已用完，请手动处理');
    }
    return;
  }

  errorRetries = 0;

  if (sameHost(window.location.href, job.url)) {
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

async function pollNext(): Promise<void> {
  if (state.paused || state.currentJob) return;
  try {
    const job = await api.fetchNextJob();
    state.connected = true;
    if (job) assignJob(job);
  } catch {
    state.connected = false;
  }
  notify();
}

function assignJob(job: import('./types').FetchJob): void {
  errorRetries = 0;
  setJob(job);
  if (state.autoMode) {
    // Navigate — the auto watcher will handle submission after page loads.
    window.location.href = job.url;
  }
}

export async function submitCurrent(): Promise<void> {
  const job = state.currentJob;
  if (!job || submitting) return;
  if (isErrorPage()) {
    showToast('当前是错误页面，无法提交');
    return;
  }
  submitting = true;
  const html = document.documentElement.outerHTML;
  try {
    const res = await api.completeJob(job.id, html, window.location.href, document.title);
    addHistory(job, 'completed');
    clearJob();
    if (res?.next_job) {
      // Defer navigation so the current response is fully processed.
      setTimeout(() => assignJob(res.next_job!), 100);
    }
  } catch (e) {
    showToast(`提交失败: ${e instanceof Error ? e.message : e}`);
  }
  submitting = false;
  notify();
}

export async function skipCurrent(): Promise<void> {
  const job = state.currentJob;
  if (!job) return;
  try {
    await api.skipJob(job.id);
    addHistory(job, 'skipped');
  } catch { /* ignore */ }
  clearJob();
}

export async function failCurrent(msg?: string): Promise<void> {
  const job = state.currentJob;
  if (!job) return;
  try {
    await api.failJob(job.id, msg || '手动标记失败');
    addHistory(job, 'failed');
  } catch { /* ignore */ }
  clearJob();
}

export async function overrideUrl(): Promise<void> {
  const job = state.currentJob;
  if (!job) return;
  const url = prompt('输入正确的 URL:', job.url);
  if (!url) return;
  try {
    const updated = await api.overrideJobUrl(job.id, url);
    if (updated) setJob(updated);
  } catch { /* ignore */ }
}
