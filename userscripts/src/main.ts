import './style.css';
import { recoverState, startAutoWatcher, startPolling, stopAutoWatcher, stopPolling } from './actions';
import { startInstanceLock, stopInstanceLock } from './instanceLock';
import { isAssistantBlockedHost } from './hostPolicy';
import { cleanupLegacyStorage, hydratePrefs, setInstanceRole, state, subscribe } from './state';
import { mountPanel, renderPanel } from './ui/panel';
import { mountToast, showToast } from './ui/toast';

function isTopFrame(): boolean {
  try {
    return window.top === window.self;
  } catch {
    return false;
  }
}

async function waitForBody(): Promise<void> {
  if (document.body) return;
  await new Promise<void>((resolve) => {
    const onReady = () => resolve();
    document.addEventListener('DOMContentLoaded', onReady, { once: true });
  });
}

async function onRoleChange(role: 'owner' | 'standby'): Promise<void> {
  const previousRole = state.instanceRole;
  setInstanceRole(role);
  if (role === 'owner') {
    await recoverState();
    startPolling();
    startAutoWatcher();
    if (previousRole !== 'owner') {
      showToast('已接管为主实例');
    }
    return;
  }
  stopPolling();
  stopAutoWatcher();
}

async function bootstrap(): Promise<void> {
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
  window.addEventListener('beforeunload', () => {
    stopInstanceLock();
  });
}

void bootstrap();
