import { GM_getValue, GM_setValue } from '$';
import type { FetchJob, HistoryEntry, PendingDecision } from './types';

const MAX_HISTORY = 20;
const STORAGE_KEY = 'ycl_state';

export interface AppState {
  currentJob: FetchJob | null;
  autoMode: boolean;
  paused: boolean;
  connected: boolean;
  minimized: boolean;
  pendingDecision: PendingDecision | null;
  history: HistoryEntry[];
}

interface PersistedState {
  currentJob: FetchJob | null;
  autoMode: boolean;
  paused: boolean;
  minimized: boolean;
  history: Array<{ id: string; url: string; status: string; time: string }>;
}

type Listener = () => void;

const listeners: Listener[] = [];

function loadPersisted(): Partial<AppState> {
  try {
    const raw = GM_getValue<string>(STORAGE_KEY, '');
    if (!raw) return {};
    const p: PersistedState = JSON.parse(raw);
    return {
      currentJob: p.currentJob ?? null,
      autoMode: p.autoMode ?? false,
      paused: p.paused ?? false,
      minimized: p.minimized ?? false,
      history: (p.history ?? []).map((h) => ({
        ...h,
        status: h.status as HistoryEntry['status'],
        time: new Date(h.time),
      })),
    };
  } catch {
    return {};
  }
}

function savePersisted(): void {
  const p: PersistedState = {
    currentJob: state.currentJob,
    autoMode: state.autoMode,
    paused: state.paused,
    minimized: state.minimized,
    history: state.history.map((h) => ({
      id: h.id,
      url: h.url,
      status: h.status,
      time: h.time.toISOString(),
    })),
  };
  GM_setValue(STORAGE_KEY, JSON.stringify(p));
}

const persisted = loadPersisted();

export const state: AppState = {
  currentJob: persisted.currentJob ?? null,
  autoMode: persisted.autoMode ?? false,
  paused: persisted.paused ?? false,
  connected: false,
  minimized: persisted.minimized ?? false,
  pendingDecision: null,
  history: persisted.history ?? [],
};

export function subscribe(fn: Listener): void {
  listeners.push(fn);
}

export function notify(): void {
  savePersisted();
  for (const fn of listeners) fn();
}

export function setJob(job: FetchJob | null): void {
  state.currentJob = job;
  notify();
}

export function clearJob(): void {
  state.currentJob = null;
  notify();
}

export function addHistory(job: FetchJob, status: HistoryEntry['status']): void {
  state.history.unshift({ id: job.id, url: job.url, status, time: new Date() });
  if (state.history.length > MAX_HISTORY) state.history.pop();
}

export function toggle<K extends 'autoMode' | 'paused' | 'minimized'>(key: K): void {
  state[key] = !state[key];
  notify();
}
