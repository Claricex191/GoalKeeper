import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import TraceView from "./TraceView.jsx";

const newSession = () => crypto.randomUUID();

// One conversation. memory=false is the left-hand control panel: same agent, same
// data, but the gate gets no history, so differencing attacks succeed there.
export default function ChatPanel({ memory, project, me, broadcast }) {
  const [sessionId, setSessionId] = useState(newSession);
  const [sessionNo, setSessionNo] = useState(1);
  const [messages, setMessages] = useState([]); // {role, text, trace?, error?}
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const endRef = useRef(null);
  const lastBroadcast = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  const send = async (text) => {
    if (!text || busy) return;
    setMessages((m) => [...m, { role: "user", text }]);
    setBusy(true);
    try {
      const res = await api.chat({ project, session_id: sessionId, message: text, memory });
      setMessages((m) => [...m, { role: "assistant", text: res.reply, trace: res.trace }]);
    } catch (e) {
      setMessages((m) => [...m, { role: "assistant", text: e.message, error: true }]);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (broadcast && broadcast.id !== lastBroadcast.current) {
      lastBroadcast.current = broadcast.id;
      send(broadcast.text);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [broadcast]);

  const submit = (e) => {
    e.preventDefault();
    const text = input.trim();
    setInput("");
    send(text);
  };

  const startNewSession = () => {
    setSessionId(newSession());
    setSessionNo((n) => n + 1);
    setMessages([]);
  };

  return (
    <section className={`panel ${memory ? "panel-on" : "panel-off"}`}>
      <div className="panel-head">
        <div>
          <h2>{memory ? "With governed memory" : "Without memory"}</h2>
          <p className="panel-sub">
            {memory
              ? "The gate checks every answer against what your team was already told."
              : "Control: each answer is checked on its own, with no history."}
          </p>
        </div>
        <div className="panel-session">
          <span className="session-tag" title={sessionId}>Session {sessionNo}</span>
          <button className="btn btn-ghost" onClick={startNewSession} disabled={busy}>
            New session
          </button>
        </div>
      </div>

      <div className="messages" aria-live="polite">
        {messages.length === 0 && (
          <p className="empty">
            {me ? `${me.display_name} · new conversation` : "New conversation"}
          </p>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`msg msg-${m.role} ${m.error ? "msg-error" : ""}`}>
            <div className="msg-text">{m.text}</div>
            {m.trace && m.trace.length > 0 && <TraceView trace={m.trace} />}
          </div>
        ))}
        {busy && <div className="msg msg-assistant msg-busy">Thinking…</div>}
        <div ref={endRef} />
      </div>

      <form className="composer" onSubmit={submit}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask this panel only…"
          aria-label={`Message (${memory ? "with memory" : "without memory"})`}
          disabled={busy}
        />
        <button type="submit" className="btn" disabled={busy || !input.trim()}>
          Send
        </button>
      </form>
    </section>
  );
}
