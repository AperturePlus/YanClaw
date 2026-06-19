import { GM, GM_xmlhttpRequest } from '$';
import { isAssistantBlockedHost } from './hostPolicy';
import type { CompleteResponse, FetchJob, PaginationState, PendingDecision, StatusResponse } from './types';

const API_BASE = 'http://127.0.0.1:21520/api';
const TIMEOUT = 10_000;
type RequestMethod = 'GET' | 'POST';

function parseJson<T>(text: string): T | null {
  if (!text) return null;
  try {
    return JSON.parse(text) as T;
  } catch {
    return null;
  }
}

function requestWithLegacyApi<T>(method: RequestMethod, url: string, body?: string): Promise<T | null> {
  return new Promise((resolve, reject) => {
    GM_xmlhttpRequest({
      method,
      url,
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      data: body,
      timeout: TIMEOUT,
      onload(res) {
        if (res.status === 204) return resolve(null);
        resolve(parseJson<T>(res.responseText));
      },
      onerror: () => reject(new Error('network')),
      ontimeout: () => reject(new Error('timeout')),
    });
  });
}

async function requestWithModernApi<T>(method: RequestMethod, url: string, body?: string): Promise<T | null> {
  let timeoutHandle: ReturnType<typeof setTimeout> | null = null;
  const requestPromise = GM.xmlHttpRequest({
    method,
    url,
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
    data: body,
  });
  const timeoutPromise = new Promise<never>((_, reject) => {
    timeoutHandle = setTimeout(() => reject(new Error('timeout')), TIMEOUT);
  });
  const res = await Promise.race([requestPromise, timeoutPromise]);
  if (timeoutHandle !== null) {
    clearTimeout(timeoutHandle);
  }
  if (res.status === 204) return null;
  return parseJson<T>(res.responseText);
}

function request<T>(method: RequestMethod, path: string, data?: unknown): Promise<T | null> {
  if (isAssistantBlockedHost()) {
    return Promise.resolve(null);
  }
  const url = API_BASE + path;
  const body = data ? JSON.stringify(data) : undefined;
  if (typeof GM_xmlhttpRequest === 'function') {
    return requestWithLegacyApi<T>(method, url, body);
  }
  if (typeof GM?.xmlHttpRequest === 'function') {
    return requestWithModernApi<T>(method, url, body);
  }
  return Promise.reject(new Error('GM_xmlhttpRequest unavailable'));
}

export async function fetchNextJob(): Promise<FetchJob | null> {
  return request<FetchJob>('GET', '/jobs/next');
}

export async function completeJob(
  id: string,
  html: string,
  url: string,
  title: string,
  paginationStates?: PaginationState[],
): Promise<CompleteResponse | null> {
  return request<CompleteResponse>('POST', `/jobs/${id}/complete`, {
    html,
    url,
    title,
    pagination_states: paginationStates ?? [],
  });
}

export async function failJob(id: string, message: string): Promise<void> {
  await request('POST', `/jobs/${id}/fail`, { message });
}

export async function skipJob(id: string): Promise<void> {
  await request('POST', `/jobs/${id}/skip`);
}

export async function overrideJobUrl(id: string, newUrl: string): Promise<FetchJob | null> {
  return request<FetchJob>('POST', `/jobs/${id}/override`, { new_url: newUrl });
}

export async function fetchStatus(): Promise<StatusResponse | null> {
  return request<StatusResponse>('GET', '/status');
}

export async function fetchDecision(): Promise<PendingDecision | null> {
  return request<PendingDecision>('GET', '/decision');
}

export async function resolveDecision(id: string, action: string): Promise<void> {
  await request('POST', `/decision/${id}/resolve`, { action });
}
