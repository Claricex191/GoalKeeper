import { useEffect, useState } from "react";
import { api } from "../api.js";

// Demo controls (clock, simulated data change, reset) and, for the HR Head role,
// pending review requests.
export default function ReviewerPanel({ me, onClockChange, onReset, onError }) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [pending, setPending] = useState([]);
  const [emp, setEmp] = useState("1408");
  const [field, setField] = useState("MonthlyIncome");
  const [value, setValue] = useState("18500");
  const isReviewer = me?.role === "hr_head";

  const run = async (label, fn) => {
    setBusy(true);
    setNote("");
    try {
      await fn();
      setNote(label);
    } catch (e) {
      onError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const loadPending = () =>
    api.pendingReviews().then(setPending).catch(() => setPending([]));

  useEffect(() => {
    if (isReviewer) loadPending();
  }, [isReviewer]);

  return (
    <section className="reviewer">
      <div className="reviewer-group">
        <h3>Demo clock</h3>
        <div className="row">
          <button className="btn" disabled={busy}
            onClick={() => run("Clock +1 day", async () => onClockChange(await api.advanceClock(1)))}>
            +1 day
          </button>
          <button className="btn" disabled={busy}
            onClick={() => run("Clock +3 days (Thursday)", async () => onClockChange(await api.advanceClock(3)))}>
            +3 days
          </button>
        </div>
      </div>

      <div className="reviewer-group">
        <h3>Simulate a data change</h3>
        <div className="row">
          <input className="small" value={emp} onChange={(e) => setEmp(e.target.value)}
            aria-label="Employee number" />
          <select value={field} onChange={(e) => setField(e.target.value)} aria-label="Field">
            <option>MonthlyIncome</option>
            <option>JobSatisfaction</option>
            <option>PerformanceRating</option>
          </select>
          <input className="small" value={value} onChange={(e) => setValue(e.target.value)}
            aria-label="New value" />
          <button className="btn" disabled={busy}
            onClick={() => run(`Employee ${emp}: ${field} updated`,
              () => api.simulateUpdate(emp, field, value))}>
            Apply
          </button>
        </div>
      </div>

      <div className="reviewer-group">
        <h3>Reset</h3>
        <div className="row">
          <button className="btn btn-danger" disabled={busy}
            onClick={() => run("Reset: Monday, warmup replayed", async () => {
              await api.reset();
              onReset();
            })}>
            Reset demo
          </button>
        </div>
      </div>

      {isReviewer && (
        <div className="reviewer-group reviewer-reviews">
          <h3>Pending reviews <button className="link" onClick={loadPending}>refresh</button></h3>
          {pending.length === 0 ? (
            <p className="muted">No pending requests.</p>
          ) : (
            <ul className="reviews">
              {pending.map((r) => (
                <li key={r.id}>
                  <span><b>{r.user}</b>: {r.reason}</span>
                  <button className="btn" disabled={busy}
                    onClick={() => run("Approved", async () => {
                      await api.decideReview(r.id, true, "T3_widen_scope");
                      loadPending();
                    })}>
                    Approve (widen scope)
                  </button>
                  <button className="btn btn-ghost" disabled={busy}
                    onClick={() => run("Rejected", async () => {
                      await api.decideReview(r.id, false);
                      loadPending();
                    })}>
                    Reject
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {note && <p className="note" role="status">✓ {note}</p>}
    </section>
  );
}
