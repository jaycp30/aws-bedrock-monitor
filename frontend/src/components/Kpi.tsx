interface KpiProps {
  label: string;
  value: string;
  sub?: string;
  accent?: boolean;
}

export function Kpi({ label, value, sub, accent }: KpiProps) {
  return (
    <div className={`kpi${accent ? " kpi--accent" : ""}`}>
      <div className="kpi__label">{label}</div>
      <div className="kpi__value">{value}</div>
      {sub && <div className="kpi__sub">{sub}</div>}
    </div>
  );
}
