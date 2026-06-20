import { useEffect, useRef, useState } from "react";
import { AuthError, DEMO_MODE, fetchUsage, type UsagePayload } from "./api";
import { handleRedirect, login, logout } from "./auth";
import { Kpi } from "./components/Kpi";
import { UsageChart } from "./components/UsageChart";
import { ModelTable, RegionTable, UserTable } from "./components/Tables";
import { AskBox } from "./components/AskBox";
import { ProfileMenu } from "./components/ProfileMenu";
import { LoadingState } from "./components/LoadingState";
import { fmtInt, fmtTokens, fmtUsd } from "./format";

const RANGES = [
  { id: "today", label: "Today" },
  { id: "7d", label: "7 days" },
  { id: "30d", label: "30 days" },
  { id: "90d", label: "90 days" },
];

export default function App() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [range, setRange] = useState("7d");
  const [data, setData] = useState<UsagePayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const usageCacheRef = useRef<Record<string, UsagePayload>>({});
  const requestSeqRef = useRef(0);

  useEffect(() => {
    if (DEMO_MODE) {
      setAuthed(true);
      return;
    }
    handleRedirect().then(setAuthed).catch(() => setAuthed(false));
  }, []);

  useEffect(() => {
    if (!authed) return;
    const cached = usageCacheRef.current[range];
    if (cached) setData(cached);
    else setData(null);
    const requestSeq = requestSeqRef.current + 1;
    requestSeqRef.current = requestSeq;
    setLoading(true);
    setError(null);
    fetchUsage(range)
      .then((payload) => {
        usageCacheRef.current[range] = payload;
        if (requestSeqRef.current === requestSeq) setData(payload);
      })
      .catch((e) => {
        if (requestSeqRef.current !== requestSeq) return;
        if (e instanceof AuthError) setAuthed(false);
        else setError(e.message);
      })
      .finally(() => {
        if (requestSeqRef.current === requestSeq) setLoading(false);
      });
  }, [authed, range]);

  if (authed === null) {
    return (
      <div className="center">
        <LoadingState />
      </div>
    );
  }

  if (!authed) {
    return (
      <div className="center">
        <div className="login-card">
          <div className="brand-mark" />
          <h1>Bedrock Usage Monitor</h1>
          <p>Sign in to view model usage and cost across regions.</p>
          <button className="btn btn--primary" onClick={() => login()}>Sign in</button>
        </div>
      </div>
    );
  }

  const t = data?.totals;
  const cacheTotal = (t?.cache_read_tokens ?? 0) + (t?.cache_write_tokens ?? 0);

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar__brand">
          <div className="brand-mark" />
          <span>Bedrock Usage Monitor</span>
          {DEMO_MODE && <span className="pill">Demo data</span>}
        </div>
        <div className="topbar__actions">
          <div className="seg">
            {RANGES.map((r) => (
              <button
                key={r.id}
                className={`seg__btn${range === r.id ? " is-active" : ""}`}
                onClick={() => setRange(r.id)}
              >
                {r.label}
              </button>
            ))}
          </div>
          {!DEMO_MODE && <ProfileMenu />}
          {!DEMO_MODE && (
            <button className="btn btn--ghost" onClick={() => logout()}>Sign out</button>
          )}
        </div>
      </header>

      <main className="content">
        {error && <div className="banner banner--err">{error}</div>}
        {data?.warnings.map((w, i) => (
          <div key={i} className="banner banner--warn">{w}</div>
        ))}

        <section className="kpis">
          <Kpi label="Estimated cost" value={fmtUsd(t?.estimated_cost ?? 0)} sub={`Billed ${fmtUsd(t?.billed_cost ?? 0)}`} accent />
          <Kpi label="Total tokens" value={fmtTokens(t?.total_tokens ?? 0)} sub={`${fmtInt(t?.input_tokens ?? 0)} in · ${fmtInt(t?.output_tokens ?? 0)} out`} />
          <Kpi label="Invocations" value={fmtInt(t?.invocations ?? 0)} sub={cacheTotal ? `${fmtTokens(cacheTotal)} cached tokens` : undefined} />
          <Kpi label="Regions" value={String(data?.regions.length ?? 0)} sub={data?.regions.join(", ")} />
        </section>

        <section className="card">
          <h2>Token usage over time</h2>
          {data && data.series.length > 0 ? (
            <UsageChart series={data.series} periodSeconds={data.range.period_seconds} />
          ) : loading ? (
            <LoadingState label="Loading usage" />
          ) : (
            <div className="empty">No usage in this range.</div>
          )}
        </section>

        <AskBox range={range} />

        <section className="card">
          <h2>By region</h2>
          {data && <RegionTable rows={data.by_region} />}
        </section>

        <section className="card">
          <h2>By model</h2>
          {data && <ModelTable rows={data.by_model} />}
        </section>

        <section className="card">
          <h2>By user</h2>
          {data && <UserTable rows={data.by_user ?? []} />}
        </section>

        {data && (
          <footer className="meta">
            Updated {new Date(data.generated_at).toLocaleString()} · tokens from CloudWatch
            invocation logs · est. cost from live AWS prices ({data.pricing_region})
          </footer>
        )}
      </main>
    </div>
  );
}
