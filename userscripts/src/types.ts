/** Mirrors Python FetchJobStatus enum. */
export type JobStatus = 'pending' | 'assigned' | 'completed' | 'failed' | 'skipped';

export interface JobContext {
  university_name: string;
  agent_state: string;
  intent: string;
  parent_url: string;
  depth: number;
  org_unit_name: string;
  hints: string[];
}

export interface FetchJob {
  id: string;
  url: string;
  status: JobStatus;
  context: JobContext;
  created_at: string;
  timeout_seconds: number;
}

export interface CompleteResponse {
  status: string;
  next_job?: FetchJob;
}

export interface QueueStats {
  pending: number;
  assigned: number;
  completed: number;
  failed: number;
  skipped: number;
}

export interface StatusResponse {
  queue: QueueStats;
  current_job?: FetchJob;
  agent?: Record<string, unknown>;
  server_uptime_seconds: number;
}

export interface HistoryEntry {
  id: string;
  url: string;
  status: 'completed' | 'skipped' | 'failed';
  time: Date;
}
