import { failCurrent, overrideUrl, skipCurrent, submitCurrent, switchPendingDecisionToHuman } from '../actions';
import { setAutoMode, setMinimized, state, togglePaused } from '../state';
import type { FetchJob } from '../types';
import { statusIcon, timeAgo, truncUrl, urlMatches } from '../utils';
import { performFetchAction } from '../formPagination';

let panelEl: HTMLDivElement | null = null;

export function mountPanel(): void {
  panelEl = document.createElement('div');
  panelEl.id = 'ycl-panel';
  panelEl.addEventListener('click', (event) => {
    if (state.minimized && event.target === event.currentTarget) {
      setMinimized(false);
    }
  });
  document.body.appendChild(panelEl);
}

export function renderPanel(): void {
  if (!panelEl) return;

  if (state.minimized) {
    panelEl.className = 'ycl-minimized';
    panelEl.innerHTML = '';
    return;
  }
  panelEl.className = '';

  const isStandby = state.instanceRole !== 'owner';
  const job = state.currentJob;
  const matched = job != null && urlMatches(window.location.href, job.url);

  panelEl.innerHTML = [
    renderHeader(),
    isStandby ? renderStandbyBanner() : '',
    renderDecision(isStandby),
    matched ? renderMatchBanner() : '',
    job ? renderJobDetail(job) : renderEmpty(isStandby),
    job ? renderActions(isStandby) : '',
    renderToggles(isStandby),
    renderHistory(),
  ].join('');

  bindEvents();
}

function renderHeader(): string {
  const dot = state.connected ? 'ycl-dot-on' : 'ycl-dot-off';
  const roleLabel = state.instanceRole === 'owner' ? '主实例' : '待机';
  const roleClass = state.instanceRole === 'owner' ? 'ycl-role-owner' : 'ycl-role-standby';
  return `<div id="ycl-header">
    <span><span class="ycl-status-dot ${dot}"></span> Yanclaw Assistant <span class="ycl-role-badge ${roleClass}">${roleLabel}</span></span>
    <button id="ycl-min" title="最小化">─</button>
  </div>`;
}

function renderStandbyBanner(): string {
  return `<div class="ycl-standby-banner">当前 Tab 为待机实例，由其他 Tab 执行轮询与操作</div>`;
}

function renderMatchBanner(): string {
  return `<div class="ycl-match-banner">✅ 检测到目标页面 — 点击提交或等待自动提交</div>`;
}

function renderDecision(disabled: boolean): string {
  const decision = state.pendingDecision;
  if (!decision) return '';
  const sample = decision.sample_urls?.[0] || '-';
  const disableAttr = disabled ? 'disabled' : '';
  return `<div class="ycl-section" style="border-left:3px solid #f9e2af;">
    <div class="ycl-label">待决策</div>
    <div>院系: <b>${decision.org_unit_name || '-'}</b></div>
    <div>连续失败: ${decision.failure_count}</div>
    <div class="ycl-url" style="margin:4px 0">${truncUrl(sample, 60)}</div>
    <button class="ycl-btn ycl-btn-warn" id="ycl-decision-switch" ${disableAttr}>失败链接切人工</button>
  </div>`;
}

function renderJobDetail(job: FetchJob): string {
  const c = job.context;
  return `<div class="ycl-section">
    <div class="ycl-label">当前任务 #${job.id}</div>
    <div>大学: <b>${c.university_name || '-'}</b></div>
    <div>阶段: ${c.agent_state || '-'}</div>
    ${c.org_unit_name ? `<div>学院: ${c.org_unit_name}</div>` : ''}
    ${c.intent ? `<div class="ycl-intent">💡 ${c.intent}</div>` : ''}
    ${job.action?.label ? `<div class="ycl-hint">动作: ${job.action.label}</div>` : ''}
    <div class="ycl-label" style="margin-top:4px">目标 URL</div>
    <div class="ycl-url">${job.url}</div>
    ${c.parent_url ? `<div style="margin-top:2px"><span class="ycl-label">来源</span> <span class="ycl-url">${truncUrl(c.parent_url, 60)}</span></div>` : ''}
    ${c.depth != null ? `<div>深度: ${c.depth}</div>` : ''}
    ${c.hints?.length ? `<div class="ycl-hint">💡 ${c.hints.join(' | ')}</div>` : ''}
  </div>`;
}

function renderEmpty(isStandby: boolean): string {
  const msg = isStandby
    ? '📡 待机中，等待主实例接管任务'
    : state.connected
      ? '⏳ 等待新任务...'
      : '🔴 未连接到后端';
  return `<div class="ycl-section" style="text-align:center;padding:16px 12px;">${msg}</div>`;
}

function renderActions(disabled: boolean): string {
  const disableAttr = disabled ? 'disabled' : '';
  return `<div class="ycl-btn-row">
    <button class="ycl-btn ycl-btn-primary" id="ycl-copy" ${disableAttr}>📋 复制URL</button>
    <button class="ycl-btn ycl-btn-primary" id="ycl-open" ${disableAttr}>🔗 打开URL</button>
    <button class="ycl-btn ycl-btn-success" id="ycl-submit" ${disableAttr}>✅ 提交当前页</button>
    <button class="ycl-btn ycl-btn-warn" id="ycl-skip" ${disableAttr}>⏭ 跳过</button>
    <button class="ycl-btn ycl-btn-muted" id="ycl-override" ${disableAttr}>✏️ 修改URL</button>
    <button class="ycl-btn ycl-btn-danger" id="ycl-fail" ${disableAttr}>❌ 失败</button>
  </div>`;
}

function renderToggles(disabled: boolean): string {
  const disableAttr = disabled ? 'disabled' : '';
  return `<div class="ycl-toggle">
    <input type="checkbox" id="ycl-auto" ${state.autoMode ? 'checked' : ''} ${disableAttr}>
    <label for="ycl-auto">自动模式 (自动导航+提交)</label>
  </div>
  <div class="ycl-toggle">
    <input type="checkbox" id="ycl-pause" ${state.paused ? 'checked' : ''} ${disableAttr}>
    <label for="ycl-pause">暂停轮询</label>
  </div>`;
}

function renderHistory(): string {
  if (!state.history.length) return '';
  const items = state.history
    .map(
      (h) =>
        `<div class="ycl-history-item"><span>${statusIcon(h.status)} ${truncUrl(h.url, 35)}</span><span>${timeAgo(h.time)}</span></div>`,
    )
    .join('');
  return `<div class="ycl-section">
    <div class="ycl-label">历史 (${state.history.length})</div>
    <div class="ycl-history">${items}</div>
  </div>`;
}

function bindEvents(): void {
  const bind = (id: string, event: string, fn: () => void) => {
    document.getElementById(id)?.addEventListener(event, fn);
  };
  const job = state.currentJob;

  document.getElementById('ycl-min')?.addEventListener('click', (event) => {
    event.stopPropagation();
    setMinimized(true);
  });

  bind('ycl-copy', 'click', () => {
    if (job) void navigator.clipboard.writeText(job.url);
  });
  bind('ycl-open', 'click', () => {
    if (!job) return;
    if (urlMatches(window.location.href, job.url) && job.action && performFetchAction(job.action)) return;
    window.location.href = job.url;
  });
  bind('ycl-submit', 'click', submitCurrent);
  bind('ycl-skip', 'click', skipCurrent);
  bind('ycl-fail', 'click', () => failCurrent());
  bind('ycl-override', 'click', overrideUrl);
  bind('ycl-decision-switch', 'click', () => {
    void switchPendingDecisionToHuman();
  });
  bind('ycl-auto', 'change', () => {
    setAutoMode(!state.autoMode);
  });
  bind('ycl-pause', 'change', () => {
    togglePaused();
  });
}
