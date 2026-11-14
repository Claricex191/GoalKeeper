import { useCallback, useEffect, useState } from "react";
import { api, isMock, setUser } from "./api.js";
import ChatPanel from "./components/ChatPanel.jsx";
import ReviewerPanel from "./components/ReviewerPanel.jsx";
import UserSwitcher from "./components/UserSwitcher.jsx";

const PROJECT = "comp-review-2026";

export default function App() {
  const [users, setUsers] = useState([]);
  const [userId, setUserId] = useState("mgr_1");
  const [clock, setClock] = useState(null);
  const [error, setError] = useState(null);
  const [broadcast, setBroadcast] = useState(null); // {id, text}: "Ask both panels"
  const [bothText, setBothText] = useState("");
  const [resetCount, setResetCount] = useState(0);  // bump → panels start new sessions

  const refreshClock = useCallback(() => {
    api.clock().then(setClock).catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    api.users().then(setUsers).catch((e) => setError(e.message));
    refreshClock();
  }, [refreshClock]);

  const switchUser = (id) => {
    setUser(id);
    setUserId(id);
  };

  const askBoth = (e) => {
    e.preventDefault();
    const text = bothText.trim();
    if (!text) return;
    setBroadcast({ id: crypto.randomUUID(), text });
    setBothText("");
  };

  const me = users.find((u) => u.user === userId);

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo" aria-hidden>◆</span>
          <div>
            <h1>GoalKeeper</h1>
            <p className="subtitle">HR analytics with a memory-governed privacy gate</p>
          </div>
          {isMock && <span className="badge badge-mock">MOCK BACKEND</span>}
        </div>
        <div className="topbar-right">
          {clock && (
            <div className="clock" title="Simulated demo clock">
              <span className="clock-label">Demo time</span>
              <span className="clock-value">
                {new Date(clock.now).toLocaleString("en-US", {
                  weekday: "short", month: "short", day: "numeric",
                  hour: "2-digit", minute: "2-digit", timeZone: "UTC" })}
              </span>
            </div>
          )}
          <UserSwitcher users={users} value={userId} onChange={switchUser} />
        </div>
      </header>

      {error && (
        <div className="banner banner-error" role="alert">
          Request failed: {error}{" "}
          <button className="link" onClick={() => setError(null)}>dismiss</button>
        </div>
      )}

      <form className="askboth" onSubmit={askBoth}>
        <input
          value={bothText}
          onChange={(e) => setBothText(e.target.value)}
          placeholder="Ask both panels the same question, e.g. “What is the average monthly income of Sales managers?”"
          aria-label="Ask both panels"
        />
        <button type="submit" className="btn btn-primary">Ask both</button>
      </form>

      <main className="panels">
        <ChatPanel
          key={`off-${userId}-${resetCount}`}
          memory={false}
          project={PROJECT}
          me={me}
          broadcast={broadcast}
        />
        <ChatPanel
          key={`on-${userId}-${resetCount}`}
          memory={true}
          project={PROJECT}
          me={me}
          broadcast={broadcast}
        />
      </main>

      <ReviewerPanel
        me={me}
        onClockChange={setClock}
        onReset={() => {
          setResetCount((n) => n + 1);
          refreshClock();
        }}
        onError={setError}
      />
    </div>
  );
}
