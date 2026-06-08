import type { ModelRow, RegionRow } from "../api";
import { fmtInt, fmtUsd, regionLabel } from "../format";

export function RegionTable({ rows }: { rows: RegionRow[] }) {
  return (
    <table className="tbl">
      <thead>
        <tr>
          <th>Region</th>
          <th className="num">Invocations</th>
          <th className="num">Input</th>
          <th className="num">Output</th>
          <th className="num">Cache R/W</th>
          <th className="num">Est. cost</th>
          <th className="num">Billed</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.region}>
            <td>{regionLabel(r.region)}</td>
            <td className="num">{fmtInt(r.invocations)}</td>
            <td className="num">{fmtInt(r.input_tokens)}</td>
            <td className="num">{fmtInt(r.output_tokens)}</td>
            <td className="num muted">{fmtInt(r.cache_read_tokens)} / {fmtInt(r.cache_write_tokens)}</td>
            <td className="num">{fmtUsd(r.estimated_cost)}</td>
            <td className="num muted">{fmtUsd(r.billed_cost)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function ModelTable({ rows }: { rows: ModelRow[] }) {
  const visible = rows.filter((r) => r.invocations > 0);
  return (
    <table className="tbl">
      <thead>
        <tr>
          <th>Model</th>
          <th>Region</th>
          <th className="num">Invocations</th>
          <th className="num">Input</th>
          <th className="num">Output</th>
          <th className="num">Cache R/W</th>
          <th className="num">Est. cost</th>
        </tr>
      </thead>
      <tbody>
        {visible.map((r) => (
          <tr key={`${r.region}/${r.model_id}`}>
            <td>
              {r.display_name}
              {r.needs_pricing && <span className="tag" title="No live price found">no price</span>}
            </td>
            <td className="muted">{r.region}</td>
            <td className="num">{fmtInt(r.invocations)}</td>
            <td className="num">{fmtInt(r.input_tokens)}</td>
            <td className="num">{fmtInt(r.output_tokens)}</td>
            <td className="num muted">{fmtInt(r.cache_read_tokens)} / {fmtInt(r.cache_write_tokens)}</td>
            <td className="num">{fmtUsd(r.estimated_cost)}</td>
          </tr>
        ))}
        {visible.length === 0 && (
          <tr>
            <td colSpan={7} className="empty">No model activity in this range.</td>
          </tr>
        )}
      </tbody>
    </table>
  );
}
