import { INSTANCE_LOCK_KEY } from './storageKeys';
import type { InstanceRole } from './state';

const HEARTBEAT_INTERVAL = 2000;
const STALE_TIMEOUT = 7000;
const LOCK_VERSION = 1;
const LOCK_SCOPE = 'global';

interface InstanceLockRecord {
  version: number;
  scope: string;
  ownerTabId: string;
  ownerHost: string;
  heartbeatAt: number;
}

const tabId = (() => {
  try {
    if (typeof crypto?.randomUUID === 'function') {
      return crypto.randomUUID();
    }
  } catch {
    // ignore
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
})();

let tickTimer: ReturnType<typeof setInterval> | null = null;
let currentRole: InstanceRole = 'standby';
let onRoleChange: ((role: InstanceRole) => void) | null = null;
let started = false;

function safeReadLock(): InstanceLockRecord | null {
  try {
    const raw = localStorage.getItem(INSTANCE_LOCK_KEY);
    if (!raw) return null;
    const lock = JSON.parse(raw) as InstanceLockRecord;
    if (!lock?.ownerTabId || typeof lock.heartbeatAt !== 'number') return null;
    return lock;
  } catch {
    return null;
  }
}

function safeWriteLock(record: InstanceLockRecord): void {
  try {
    localStorage.setItem(INSTANCE_LOCK_KEY, JSON.stringify(record));
  } catch {
    // ignore
  }
}

function isStale(lock: InstanceLockRecord): boolean {
  return Date.now() - lock.heartbeatAt > STALE_TIMEOUT;
}

function setRole(nextRole: InstanceRole): void {
  if (currentRole === nextRole) return;
  currentRole = nextRole;
  onRoleChange?.(nextRole);
}

function claimLock(): boolean {
  const candidate: InstanceLockRecord = {
    version: LOCK_VERSION,
    scope: LOCK_SCOPE,
    ownerTabId: tabId,
    ownerHost: window.location.hostname,
    heartbeatAt: Date.now(),
  };
  safeWriteLock(candidate);
  const written = safeReadLock();
  return written?.ownerTabId === tabId;
}

function refreshTick(): void {
  const lock = safeReadLock();
  if (!lock || lock.ownerTabId === tabId || isStale(lock)) {
    if (claimLock()) {
      setRole('owner');
      return;
    }
  }
  setRole('standby');
}

function releaseIfOwner(): void {
  const lock = safeReadLock();
  if (!lock || lock.ownerTabId !== tabId) return;
  try {
    localStorage.removeItem(INSTANCE_LOCK_KEY);
  } catch {
    // ignore
  }
}

function onStorageEvent(event: StorageEvent): void {
  if (event.key !== INSTANCE_LOCK_KEY) return;
  refreshTick();
}

export function isOwner(): boolean {
  return currentRole === 'owner';
}

export function startInstanceLock(onChange: (role: InstanceRole) => void): void {
  if (started) return;
  started = true;
  onRoleChange = onChange;
  window.addEventListener('storage', onStorageEvent);
  window.addEventListener('beforeunload', releaseIfOwner);
  refreshTick();
  tickTimer = setInterval(refreshTick, HEARTBEAT_INTERVAL);
}

export function stopInstanceLock(): void {
  if (!started) return;
  started = false;
  if (tickTimer !== null) {
    clearInterval(tickTimer);
    tickTimer = null;
  }
  window.removeEventListener('storage', onStorageEvent);
  window.removeEventListener('beforeunload', releaseIfOwner);
  releaseIfOwner();
  onRoleChange = null;
  currentRole = 'standby';
}
