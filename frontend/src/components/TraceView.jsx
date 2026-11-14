// Per assistant message: understood query → gate decision → rewrite.
// Green = allowed, grey = suppressed cells, red = blocked (with evidence).

const OP_TEXT = { "=": "=", "!=": "≠", "<": "<", "<=": "≤", ">": ">", ">=": "≥", in: "in" };
const METRIC_TEXT = { avg: "average", sum: "total", count: "count" };
const TRANSFORM_TEXT = {
  T1_merge_groups: "merged small groups",
  T2_drop_filter: "dropped a filter",
  T3_widen_scope: "widened the scope",
};
const STATUS_TEXT = {
  allowed: "Allowed",
  partial: "Partly suppressed",
  blocked: "Blocked",
  error: "Query error",
  pending: "Sent for review",
};

const fmtVal = (v) => (Array.isArray(v) ? `[${v.join(", ")}]` : String(v));
const fmtGroup = (g) =>
  Object.keys(g || {}).length ? Object.entries(g).map(([k, v]) => `${k}=${v}`).join(", ") : "all";
const fmtNum = (v) =>
  v == null ? "—" : Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 });

function QueryChips({ q }) {
  return (
    <div className="chips">
      <span className="chip chip-metric">
        {METRIC_TEXT[q.metric] ?? q.metric}
        {q.target ? ` ${q.target}` : ""}
      </span>
      {(q.filters || []).map((f, i) => (
        <span className="chip" key={i}>
          {f.col} {OP_TEXT[f.op] ?? f.op} {fmtVal(f.val)}
        </span>
      ))}
      {(q.group_by || []).length > 0 && (
        <span className="chip chip-group">by {q.group_by.join(", ")}</span>
      )}
    </div>
  );
}

function Step({ entry, index }) {
  if (entry.tool === "request_review") {
    return (
      <li className="step step-pending">
        <div className="step-head">
          <span className="status status-pending">{STATUS_TEXT.pending}</span>
          <span className="step-rule">approval {entry.approval_id}</span>
        </div>
      </li>
    );
  }
  const q = entry.query || {};
  const status = entry.status || "error";
  return (
    <li className={`step step-${status}`}>
      <div className="step-head">
        <span className="step-no">{index + 1}</span>
        {q.transform && (
          <span className="rewrite">↻ rewrite: {TRANSFORM_TEXT[q.transform] ?? q.transform}</span>
        )}
        <span className={`status status-${status}`}>{STATUS_TEXT[status] ?? status}</span>
        {entry.rule && <span className="step-rule">{entry.rule}</span>}
      </div>

      <QueryChips q={q} />

      {entry.evidence_text && <p className="evidence">{entry.evidence_text}</p>}

      {(entry.cells?.length > 0 || entry.suppressed?.length > 0) && (
        <table className="cells">
          <thead>
            <tr><th>Group</th><th>People</th><th>Value</th></tr>
          </thead>
          <tbody>
            {(entry.cells || []).map((c, i) => (
              <tr key={`c${i}`}>
                <td>{fmtGroup(c.group)}</td>
                <td>{c.n}</td>
                <td>{fmtNum(c.value)}</td>
              </tr>
            ))}
            {(entry.suppressed || []).map((s, i) => (
              <tr key={`s${i}`} className="row-suppressed">
                <td>{fmtGroup(s.group)}</td>
                <td colSpan={2}>suppressed: {s.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {entry.hints?.length > 0 && (
        <ul className="hints">
          {entry.hints.map((h, i) => <li key={i}>{h}</li>)}
        </ul>
      )}
    </li>
  );
}

export default function TraceView({ trace }) {
  return (
    <details className="trace" open>
      <summary>Gate trace · {trace.length} {trace.length === 1 ? "step" : "steps"}</summary>
      <ol className="steps">
        {trace.map((t, i) => <Step key={i} entry={t} index={i} />)}
      </ol>
    </details>
  );
}
