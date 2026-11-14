# Frontend (Vite + React)

Create on the day: `npm create vite@latest . -- --template react`, then add the files below.

```
src/
  api.js                    fetch wrappers for /chat, /review, /admin/*; every call sends
                            header `X-Demo-User: <current user id>`
  App.jsx                   layout: two ChatPanels side by side + ReviewerPanel
  components/
    UserSwitcher.jsx        top-right dropdown filled from GET /demo/users; shows name,
                            role and team; switching user starts a new session
    ChatPanel.jsx           one conversation; prop `memory` (false = control panel);
                            "New session" button to prove cross-session memory
                            (session_id = crypto.randomUUID() per session)
    TraceView.jsx           per message: understood query → gate decision → rewrite
                            (green allowed / grey suppressed / red blocked with evidence)
    ReviewerPanel.jsx       pending approvals; approve a transform; overturn a precedent;
                            demo clock (+4 days) and reset buttons
```

Not Streamlit or Gradio. The ledger is shown in the Atlas UI, not here.

## Who is asking

The frontend never sends a role or team. It sends only the chosen user id in the
`X-Demo-User` header; the backend looks up role and team in `backend/app/users.py`
(unknown id → 401). Demo users: mgr_1, mgr_2 (Sales), mgr_3 (R&D), mgr_4 (HR dept.),
hr_1 (HR analyst, the only one who may approve reviews).
