import { GM_xmlhttpRequest } from '$';
import type { CompleteResponse, FetchJob, PendingDecision, StatusResponse } from './types';

const API_BASE = 'http://127.0.0.1:21520/api';
const TIMEOUT = 10_000;

function request<T>(method: string, path: string, data?: unknown): Promise<T | null> {
  return new Promise((resolve, reject) => {
    GM_xmlhttpRequest({
      method: method as 'GET' | 'POST',
      url: API_BASE + path,
      headers: { 'Content-Type': 'application/json' },
      data: data ? JSON.stringify(data) : undefined,
      timeout: TIMEOUT,
      onload(res) {
        if (res.status === 204) return resolve(null);
        try {
          resolve(JSON.parse(res.responseText) as T);
        } catch {
          resolve(null);
        }
      },
      onerror: () => reject(new Error('network')),
      ontimeout: () => reject(new Error('timeout')),
    });
  });
}

export async function fetchNextJob(): Promise<FetchJob | null> {
  return request<FetchJob>('GET', '/jobs/next');
}

export async function completeJob(
  id: string,
  html: string,
  url: string,
  title: string,
): Promise<CompleteResponse | null> {
  return request<CompleteResponse>('POST', `/jobs/${id}/complete`, { html, url, title });
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
