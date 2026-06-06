import { GM, GM_deleteValue, GM_getValue, GM_listValues, GM_setValue } from '$';
import { INSTANCE_LOCK_KEY, UI_PREFS_KEY, YCL_PREFIX } from './storageKeys';
import type { FetchJob, PendingDecision } from './types';

const LEGACY_LOCAL_FALLBACK_KEY = 'ycl_state_fallback';

export type InstanceRole = 'owner' | 'standby';

export interface AppState {
  currentJob: FetchJob | null;
  autoMode: boolean;
  paused: boolean;
  connected: boolean;
  minimized: boolean;
  pendingDecision: PendingDecision | null;
  instanceRole: InstanceRole;
}

interface UiPrefsPersisted {
  autoMode: boolean;
  minimized: boolean;
}

type Listener = () => void;

const listeners: Listener[] = [];
const prefsFallbackKey = `${UI_PREFS_KEY}_fallback`;
const cleanupWhitelist = new Set<string>([UI_PREFS_KEY, prefsFallbackKey, INSTANCE_LOCK_KEY]);
const PREFS_WRITE_DELAY = 150;
let prefsWriteTimer: ReturnType<typeof setTimeout> | null = null;
let prefsHydrated = false;
let hydratePrefsPromise: Promise<void> | null = null;
let lastPrefsRaw = '';

export const state: AppState = {
  currentJob: null,
  autoMode: false,
  paused: false,
  connected: false,
  minimized: false,
  pendingDecision: null,
  instanceRole: 'standby',
};

function isCleanupTarget(key: string): boolean {
  return key.startsWith(YCL_PREFIX) && !cleanupWhitelist.has(key);
}

function readLocal(key: string): string {
  try {
    return localStorage.getItem(key) ?? '';
  } catch {
    return '';
  }
}

function writeLocal(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // ignore
  }
}

function removeLocal(key: string): void {
  try {
    localStorage.removeItem(key);
  } catch {
    // ignore
  }
}

function parsePrefs(raw: string): UiPrefsPersisted {
  if (!raw) {
    return { autoMode: false, minimized: false };
  }
  try {
    const parsed = JSON.parse(raw) as Partial<UiPrefsPersisted>;
    return {
      autoMode: parsed.autoMode ?? false,
      minimized: parsed.minimized ?? false,
    };
  } catch {
    return { autoMode: false, minimized: false };
  }
}

function snapshotPrefsRaw(): string {
  const prefs: UiPrefsPersisted = {
    autoMode: state.autoMode,
    minimized: state.minimized,
  };
  return JSON.stringify(prefs);
}

function flushPrefs(): void {
  const raw = snapshotPrefsRaw();
  if (raw === lastPrefsRaw) return;
  lastPrefsRaw = raw;

  if (typeof GM_setValue === 'function') {
    GM_setValue(UI_PREFS_KEY, raw);
    return;
  }
  if (typeof GM?.setValue === 'function') {
    void GM.setValue(UI_PREFS_KEY, raw);
    return;
  }
  writeLocal(prefsFallbackKey, raw);
}

function schedulePrefsPersist(): void {
  if (prefsWriteTimer !== null) return;
  prefsWriteTimer = setTimeout(() => {
    prefsWriteTimer = null;
    flushPrefs();
  }, PREFS_WRITE_DELAY);
}

async function loadPrefsRaw(): Promise<string> {
  try {
    if (typeof GM_getValue === 'function') {
      return GM_getValue<string>(UI_PREFS_KEY, '');
    }
    if (typeof GM?.getValue === 'function') {
      return await GM.getValue<string>(UI_PREFS_KEY, '');
    }
    return readLocal(prefsFallbackKey);
  } catch {
    return '';
  }
}

async function listGMKeys(): Promise<string[]> {
  try {
    if (typeof GM_listValues === 'function') {
      return GM_listValues();
    }
    if (typeof GM?.listValues === 'function') {
      return await GM.listValues();
    }
  } catch {
    // ignore
  }
  return [];
}

async function deleteGMKey(key: string): Promise<void> {
  try {
    if (typeof GM_deleteValue === 'function') {
      GM_deleteValue(key);
      return;
    }
    if (typeof GM?.deleteValue === 'function') {
      await GM.deleteValue(key);
    }
  } catch {
    // ignore
  }
}

export async function cleanupLegacyStorage(): Promise<void> {
  try {
    const keysToDelete: string[] = [];
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i);
      if (!key) continue;
      if (isCleanupTarget(key)) {
        keysToDelete.push(key);
      }
    }
    // Historical fallback key is always safe to remove.
    keysToDelete.push(LEGACY_LOCAL_FALLBACK_KEY);
    for (const key of keysToDelete) {
      removeLocal(key);
    }
  } catch {
    // ignore
  }

  const gmKeys = await listGMKeys();
  for (const key of gmKeys) {
    if (isCleanupTarget(key)) {
      await deleteGMKey(key);
    }
  }
}

export async function hydratePrefs(): Promise<void> {
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

if (typeof window !== 'undefined') {
  window.addEventListener('beforeunload', () => {
    if (prefsWriteTimer !== null) {
      clearTimeout(prefsWriteTimer);
      prefsWriteTimer = null;
    }
    flushPrefs();
  });
}

export function subscribe(fn: Listener): void {
  listeners.push(fn);
}

export function notify(): void {
  for (const fn of listeners) fn();
}

export function setInstanceRole(role: InstanceRole): void {
  if (state.instanceRole === role) return;
  state.instanceRole = role;
  notify();
}

export function setAutoMode(value: boolean): void {
  if (state.autoMode === value) return;
  state.autoMode = value;
  schedulePrefsPersist();
  notify();
}

export function setMinimized(value: boolean): void {
  if (state.minimized === value) return;
  state.minimized = value;
  schedulePrefsPersist();
  notify();
}

export function togglePaused(): void {
  state.paused = !state.paused;
  notify();
}

export function setJob(job: FetchJob | null): void {
  state.currentJob = job;
  notify();
}

export function clearJob(): void {
  state.currentJob = null;
  notify();
}
