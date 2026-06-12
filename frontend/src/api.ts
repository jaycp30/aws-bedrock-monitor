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
  series: SeriesPoint[];
  warnings: string[];
  pricing_region: string;
  generated_at: string;
}

export class AuthError extends Error {}

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
  if (!resp.ok) throw new Error(`API error ${resp.status}`);
  return resp.json();
}

export interface ChatMessage {
  role: "user" | "assistant";
  text: string;
}

// Multi-turn ask: the browser carries the conversation and sends the recent
// turns with every request (the backend caps how many it uses).
export async function askChat(messages: ChatMessage[]): Promise<string> {
  if (DEMO_MODE) {
    return "Demo mode — the Ask agent runs against the live backend once deployed. It answers questions about your Bedrock usage and cost (and politely declines anything else, enforced by a Bedrock Guardrail).";
  }
  const token = getToken();
  if (!token) throw new AuthError("not signed in");
  const resp = await fetch(`${CONFIG.apiUrl}/ask`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ messages }),
  });
  if (resp.status === 401 || resp.status === 403) throw new AuthError("session expired");
  if (!resp.ok) throw new Error(`API error ${resp.status}`);
  const data = await resp.json();
  return data.answer ?? "(no answer)";
}
