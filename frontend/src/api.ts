import { CONFIG } from "./config";
import { getToken } from "./auth";

export interface ModelRow {
  region: string;
  model_id: string;
  display_name: string;
  input_tokens: number;
  output_tokens: number;
  cache_write_tokens: number;
  cache_read_tokens: number;
  total_tokens: number;
  invocations: number;
  estimated_cost: number;
  needs_pricing: boolean;
}

export interface RegionRow {
  region: string;
  input_tokens: number;
  output_tokens: number;
  cache_write_tokens: number;
  cache_read_tokens: number;
  total_tokens: number;
  invocations: number;
  estimated_cost: number;
  billed_cost: number;
}

export interface UserRow {
  principal: string;
  identity_arn: string;
  regions: string[];
  input_tokens: number;
  output_tokens: number;
  cache_write_tokens: number;
  cache_read_tokens: number;
  total_tokens: number;
  invocations: number;
  estimated_cost: number;
}

export interface SeriesPoint {
  t: string;
  input_tokens: number;
  output_tokens: number;
  invocations: number;
}

export interface UsagePayload {
  range: { start: string; end: string; period_seconds: number; label: string };
  regions: string[];
  totals: {
    input_tokens: number;
    output_tokens: number;
    cache_write_tokens: number;
    cache_read_tokens: number;
    total_tokens: number;
    invocations: number;
    estimated_cost: number;
    billed_cost: number;
  };
  by_region: RegionRow[];
  by_model: ModelRow[];
  by_user: UserRow[];
  series: SeriesPoint[];
  warnings: string[];
  pricing_region: string;
  generated_at: string;
  stale?: boolean;
}

export class AuthError extends Error {}

async function apiError(resp: Response): Promise<Error> {
  try {
    const data = await resp.json();
    if (typeof data?.error === "string" && data.error.trim()) {
      return new Error(data.error);
    }
  } catch {
    // Non-JSON error body; fall back to status text below.
  }
  return new Error(`API error ${resp.status}`);
}

// Demo mode: no backend configured → serve bundled real data for offline preview.
export const DEMO_MODE = !CONFIG.apiUrl;

export async function fetchUsage(range: string): Promise<UsagePayload> {
  if (DEMO_MODE) {
    const resp = await fetch(`${import.meta.env.BASE_URL}demo.json`);
    if (!resp.ok) throw new Error("demo data unavailable");
    const data: UsagePayload = await resp.json();
    data.range.label = range;
    return data;
  }
  const token = getToken();
  if (!token) throw new AuthError("not signed in");
  const resp = await fetch(`${CONFIG.apiUrl}/usage?range=${encodeURIComponent(range)}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (resp.status === 401 || resp.status === 403) throw new AuthError("session expired");
  if (!resp.ok) throw await apiError(resp);
  return resp.json();
}

export interface ChatMessage {
  role: "user" | "assistant";
  text: string;
}

interface AskJob {
  job_id: string;
  status: "queued" | "running" | "complete" | "failed";
  answer?: string;
  error?: string;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function fetchAskJob(jobId: string): Promise<AskJob> {
  const token = getToken();
  if (!token) throw new AuthError("not signed in");
  const resp = await fetch(`${CONFIG.apiUrl}/ask/${encodeURIComponent(jobId)}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (resp.status === 401 || resp.status === 403) throw new AuthError("session expired");
  if (!resp.ok) throw await apiError(resp);
  return resp.json();
}

async function waitForAskJob(jobId: string): Promise<string> {
  const started = Date.now();
  let delay = 1000;
  while (Date.now() - started < 120_000) {
    await sleep(delay);
    const job = await fetchAskJob(jobId);
    if (job.status === "complete") {
      if (typeof job.answer === "string" && job.answer.trim()) return job.answer;
      throw new Error("Ask returned no text answer. Try again or narrow the time range.");
    }
    if (job.status === "failed") throw new Error(job.error || "Ask is temporarily unavailable.");
    delay = Math.min(2500, Math.round(delay * 1.2));
  }
  throw new Error("Ask is still running. Try again in a moment.");
}

// Multi-turn ask: the browser carries the conversation and sends the recent
// turns with every request (the backend caps how many it uses).
export async function askChat(messages: ChatMessage[], range: string): Promise<string> {
  if (DEMO_MODE) {
    return "Demo mode — the Ask agent runs against the live backend once deployed. It answers questions about your Bedrock usage and cost (and politely declines anything else, enforced by a Bedrock Guardrail).";
  }
  const token = getToken();
  if (!token) throw new AuthError("not signed in");
  const resp = await fetch(`${CONFIG.apiUrl}/ask`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ messages, range }),
  });
  if (resp.status === 401 || resp.status === 403) throw new AuthError("session expired");
  if (!resp.ok) throw await apiError(resp);
  const data = await resp.json();
  if (resp.status === 202 && data.job_id) return waitForAskJob(data.job_id);
  if (typeof data.answer === "string" && data.answer.trim()) return data.answer;
  throw new Error("Ask returned no text answer. Try again or narrow the time range.");
}
