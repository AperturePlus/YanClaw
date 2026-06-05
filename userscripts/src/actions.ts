import * as api from './api';
import { addHistory, clearJob, notify, setJob, state } from './state';
import type { PendingDecision } from './types';
import { showToast } from './ui/toast';
import { isErrorPage, urlMatches } from './utils';

const POLL_INTERVAL = 1500;
const FAST_POLL_INTERVAL = 500;
const FAST_POLL_ROUNDS = 4;
const AUTO_CHECK_INTERVAL = 1000;
const AUTO_SUBMIT_DELAY = 1000;
const CAPTURE_STABLE_INTERVAL = 200;
const CAPTURE_STABLE_ROUNDS = 3;
const CAPTURE_MAX_WAIT = 8000;
const DECISION_POLL_INTERVAL = 5000;
const ERROR_RETRY_DELAY = 5000;
const MAX_ERROR_RETRIES = 3;
const DEFAULT_DECISION_ACTION = 'switch_failed_to_human';

let pollTimer: ReturnType<typeof setInterval> | null = null;
let autoCheckTimer: ReturnType<typeof setInterval> | null = null;
let submitting = false;
let polling = false;
let decisionPromptedId: string | null = null;
let resolvingDecision = false;
let lastDecisionCheckAt = 0;

/** Sync persisted state with backend on page load. */
export async function recoverState(): Promise<void> {
  let changed = false;
  try {
    const status = await api.fetchStatus();
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
    const pendingId = status.pending_decision?.id ?? null;
    if ((state.pendingDecision?.id ?? null) !== pendingId) {
      changed = true;
    }
    state.pendingDecision = status.pending_decision ?? null;
    if (status.current_job) {
      if ((state.currentJob?.id ?? null) !== status.current_job.id) {
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

export function startPolling(): void {
  if (pollTimer !== null) return;
  void pollNext();
  pollTimer = setInterval(() => {
    void pollNext();
  }, POLL_INTERVAL);
}

export function stopPolling(): void {
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

/**
 * Start a persistent watcher that auto-submits when the current page
 * matches the job URL. Runs periodically so it survives redirects,
 * late JS rendering, and page load timing issues.
 */
export function startAutoWatcher(): void {
  if (autoCheckTimer !== null) return;
  autoCheckTimer = setInterval(autoCheck, AUTO_CHECK_INTERVAL);
}

export function stopAutoWatcher(): void {
  if (autoCheckTimer !== null) {
    clearInterval(autoCheckTimer);
    autoCheckTimer = null;
  }
}

let matchedSince: number | null = null;
let errorRetries = 0;

function autoCheck(): void {
  if (state.instanceRole !== 'owner') return;
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

async function pollNext(): Promise<void> {
  if (state.instanceRole !== 'owner') return;
  const now = Date.now();
  if (now - lastDecisionCheckAt >= DECISION_POLL_INTERVAL) {
    lastDecisionCheckAt = now;
    await checkPendingDecision();
  }

  if (document.visibilityState === 'hidden' && !state.currentJob) return;
  if (state.paused || state.currentJob || polling) return;

  const connectedBefore = state.connected;
  let jobAssigned = false;
  polling = true;
  try {
    const job = await api.fetchNextJob();
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

function assignJob(job: import('./types').FetchJob): void {
  errorRetries = 0;
  setJob(job);
  if (state.autoMode) {
    // Navigate — the auto watcher will handle submission after page loads.
    window.location.href = job.url;
  }
}

function triggerFastPollBurst(): void {
  if (state.paused || state.currentJob) return;
  for (let i = 0; i < FAST_POLL_ROUNDS; i += 1) {
    setTimeout(() => {
      void pollNext();
    }, i * FAST_POLL_INTERVAL);
  }
}

async function checkPendingDecision(): Promise<void> {
  if (state.instanceRole !== 'owner') return;
  if (resolvingDecision) return;
  const previousId = state.pendingDecision?.id ?? null;
  try {
    const decision = await api.fetchDecision();
    state.pendingDecision = decision;
    const nextId = decision?.id ?? null;
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
        `院系：${decision.org_unit_name || 'Unknown'}`,
        `后台详情抓取连续失败：${decision.failure_count}`,
        '点击“确定”将失败链接切人工处理。',
      ].join('\n'),
    );
    if (accepted) {
      await resolvePendingDecision(decision, DEFAULT_DECISION_ACTION);
    }
  } catch {
    // ignore
  }
}

async function resolvePendingDecision(decision: PendingDecision, action: string): Promise<void> {
  if (resolvingDecision) return;
  resolvingDecision = true;
  try {
    await api.resolveDecision(decision.id, action);
    state.pendingDecision = null;
    decisionPromptedId = null;
    showToast('已切换为人工处理详情失败链接');
  } catch (e) {
    showToast(`决策提交失败: ${e instanceof Error ? e.message : e}`);
  }
  resolvingDecision = false;
  notify();
}

export async function switchPendingDecisionToHuman(): Promise<void> {
  if (state.instanceRole !== 'owner') return;
  const decision = state.pendingDecision;
  if (!decision) return;
  await resolvePendingDecision(decision, DEFAULT_DECISION_ACTION);
}

export async function submitCurrent(): Promise<void> {
  if (state.instanceRole !== 'owner') return;
  const job = state.currentJob;
  if (!job || submitting) return;
  if (isErrorPage()) {
    showToast('当前是错误页面，无法提交');
    return;
  }
  submitting = true;
  try {
    const html = await captureCurrentHtml();
    const res = await api.completeJob(job.id, html, window.location.href, document.title);
    addHistory(job, 'completed');
    clearJob();
    if (res?.next_job) {
      // Defer navigation so the current response is fully processed.
      setTimeout(() => assignJob(res.next_job!), 100);
    } else {
      triggerFastPollBurst();
    }
  } catch (e) {
    showToast(`提交失败: ${e instanceof Error ? e.message : e}`);
  }
  submitting = false;
  notify();
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function captureCurrentHtml(): Promise<string> {
  await waitForCaptureReady();
  return serializePageWithoutOverlay();
}

async function waitForCaptureReady(): Promise<void> {
  const started = Date.now();
  let lastSignature = '';
  let stableRounds = 0;
  let scrolled = false;

  while (Date.now() - started < CAPTURE_MAX_WAIT) {
    if (document.readyState === 'complete') {
      if (!scrolled && Date.now() - started >= Math.floor(AUTO_SUBMIT_DELAY / 2)) {
        scrolled = true;
        window.scrollTo({ top: document.body?.scrollHeight ?? 0, behavior: 'auto' });
      }

      const signature = captureSignature();
      if (signature === lastSignature) {
        stableRounds += 1;
      } else {
        lastSignature = signature;
        stableRounds = 0;
      }

      if (Date.now() - started >= AUTO_SUBMIT_DELAY && stableRounds >= CAPTURE_STABLE_ROUNDS) {
        return;
      }
    }
    await sleep(CAPTURE_STABLE_INTERVAL);
  }
}

function captureSignature(): string {
  const textLength = document.body?.innerText?.length ?? 0;
  const nodeCount = document.getElementsByTagName('*').length;
  const imageCount = document.images.length;
  return `${textLength}:${nodeCount}:${imageCount}`;
}

function serializePageWithoutOverlay(): string {
  const clone = document.documentElement.cloneNode(true) as HTMLElement;
  clone.querySelectorAll('#ycl-panel,#ycl-toast,[data-yanclaw-overlay]').forEach((node) => node.remove());
  return clone.outerHTML;
}

export async function skipCurrent(): Promise<void> {
  if (state.instanceRole !== 'owner') return;
  const job = state.currentJob;
  if (!job) return;
  try {
    await api.skipJob(job.id);
    addHistory(job, 'skipped');
  } catch { /* ignore */ }
  clearJob();
  triggerFastPollBurst();
}

export async function failCurrent(msg?: string): Promise<void> {
  if (state.instanceRole !== 'owner') return;
  const job = state.currentJob;
  if (!job) return;
  try {
    await api.failJob(job.id, msg || '手动标记失败');
    addHistory(job, 'failed');
  } catch { /* ignore */ }
  clearJob();
  triggerFastPollBurst();
}

export async function overrideUrl(): Promise<void> {
  if (state.instanceRole !== 'owner') return;
  const job = state.currentJob;
  if (!job) return;
  const url = prompt('输入正确的 URL:', job.url);
  if (!url) return;
  try {
    const updated = await api.overrideJobUrl(job.id, url);
    if (updated) setJob(updated);
  } catch { /* ignore */ }
}
