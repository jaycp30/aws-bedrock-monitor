import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { SeriesPoint } from "../api";
import { fmtTokens } from "../format";

function tick(t: string, periodSeconds: number): string {
  const d = new Date(t);
  if (periodSeconds <= 3600)
    return d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric" });
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export function UsageChart({
  series,
  periodSeconds,
}: {
  series: SeriesPoint[];
  periodSeconds: number;
}) {
  const data = series.map((p) => ({
    t: tick(p.t, periodSeconds),
    Input: p.input_tokens,
    Output: p.output_tokens,
  }));

  return (
    <ResponsiveContainer width="100%" height={300}>
      <AreaChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
        <defs>
          <linearGradient id="gIn" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--series-2)" stopOpacity={0.4} />
            <stop offset="100%" stopColor="var(--series-2)" stopOpacity={0.02} />
          </linearGradient>
          <linearGradient id="gOut" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--brand-orange)" stopOpacity={0.4} />
            <stop offset="100%" stopColor="var(--brand-orange)" stopOpacity={0.02} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
        <XAxis dataKey="t" tick={{ fontSize: 11, fill: "var(--muted)" }} tickLine={false} axisLine={{ stroke: "var(--border)" }} minTickGap={24} />
        <YAxis tickFormatter={fmtTokens} tick={{ fontSize: 11, fill: "var(--muted)" }} tickLine={false} axisLine={false} width={48} />
        <Tooltip
          formatter={(v: number) => fmtTokens(v) + " tokens"}
          contentStyle={{
            background: "var(--surface)",
            border: "1px solid var(--border)",
            borderRadius: 8,
            fontSize: 12,
          }}
        />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        <Area type="monotone" dataKey="Input" stroke="var(--series-2)" fill="url(#gIn)" strokeWidth={2} />
        <Area type="monotone" dataKey="Output" stroke="var(--brand-orange)" fill="url(#gOut)" strokeWidth={2} />
      </AreaChart>
    </ResponsiveContainer>
  );
}
