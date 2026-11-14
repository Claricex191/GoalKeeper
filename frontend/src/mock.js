// Scripted in-browser backend for `npm run dev:mock`: lets the UI be built and
// rehearsed before the real /chat exists. Numbers are the real IBM HR values for
// Act 1 (new hire) and Act 2 (raise); everything else returns a generic answer.
// Shapes follow ../API.md exactly, so switching to the real backend changes nothing.

const USERS = [
  { user: "mgr_1", role: "department_manager", team: "sales", departments: ["Sales"], display_name: "Manager 1 (Sales)" },
  { user: "mgr_2", role: "department_manager", team: "sales", departments: ["Sales"], display_name: "Manager 2 (Sales)" },
  { user: "mgr_3", role: "department_manager", team: "rnd", departments: ["Research & Development"], display_name: "Manager 3 (R&D)" },
  { user: "mgr_4", role: "department_manager", team: "hr", departments: ["Human Resources"], display_name: "Manager 4 (HR dept.)" },
  { user: "hr_1", role: "hr_head", team: "hr", departments: ["Human Resources"], display_name: "HR Head / reviewer" },
];
const DEMO_START = Date.UTC(2026, 10, 9, 9, 0); // Mon 2026-11-09 09:00 UTC
const DAY = 86400000;

const state = { offset: 0, raisedAt: null, releases: [] }; // releases: {team, scenario, day}

const eq = (col, val) => ({ col, op: "=", val });
const SCENARIOS = {
  sales_mgr: {
    match: (m) => /sales/i.test(m) && /manager/i.test(m),
    query: { filters: [eq("Department", "Sales"), eq("JobRole", "Manager")], group_by: [],
             metric: "avg", target: "MonthlyIncome", transform: null },
    label: "Sales managers, average monthly income",
    result: (day) => (day >= 3 ? { n: 37, value: 16986.97 } : { n: 36, value: 17111.5 }),
    // what changed between an earlier release (on `then`) and now
    changes: (then, now) => ({ joined: then < 3 && now >= 3 ? 1 : 0, valueChanged: 0 }),
    widened: {
      query: { filters: [eq("Department", "Sales")], group_by: [], metric: "avg",
               target: "MonthlyIncome", transform: "T3_widen_scope" },
      result: (day) => (day >= 3 ? { n: 446, value: 6959.17 } : { n: 445, value: 6946.71 }),
      label: "the whole Sales department",
    },
  },
  hr_mgr: {
    match: (m) => /(hr|human resources)/i.test(m) && /manager/i.test(m),
    query: { filters: [eq("Department", "Human Resources"), eq("JobRole", "Manager")],
             group_by: [], metric: "avg", target: "MonthlyIncome", transform: null },
    label: "HR managers, average monthly income",
    result: () => ({ n: 11, value: state.raisedAt !== null ? 18243.27 : 18088.64 }),
    changes: (then) => ({ joined: 0,
                          valueChanged: state.raisedAt !== null && state.raisedAt > then ? 1 : 0 }),
    widened: {
      query: { filters: [eq("Department", "Human Resources")], group_by: [], metric: "avg",
               target: "MonthlyIncome", transform: "T3_widen_scope" },
      result: () => ({ n: 63, value: state.raisedAt !== null ? 6681.51 : 6654.51 }),
      label: "the whole HR department",
    },
  },
};

const day = () => state.offset;
const nowIso = () => new Date(DEMO_START + state.offset * DAY).toISOString();
const dayName = (d) =>
  new Date(DEMO_START + d * DAY).toLocaleDateString("en-US", {
    weekday: "short", month: "2-digit", day: "2-digit", timeZone: "UTC" });
const money = (v) => v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

function evidenceText(prev, ch) {
  const parts = [];
  if (ch.joined) parts.push(`${ch.joined} person joined or left this group`);
  if (ch.valueChanged) parts.push(`${ch.valueChanged} person's data in this group changed`);
  return `Since your team's answer on ${dayName(prev.day)} ('${prev.label}'), only ` +
    `${parts.join(" and ")}; answering now would reveal that person's value.`;
}

async function chat(userId, { message, memory }) {
  await wait(500);
  const who = USERS.find((u) => u.user === userId);
  const key = Object.keys(SCENARIOS).find((k) => SCENARIOS[k].match(message));
  if (!key) {
    return {
      reply: "(mock) Try: “What is the average monthly income of Sales managers?” or " +
             "“… of HR managers?”. The mock backend only scripts Act 1 and Act 2.",
      trace: [],
    };
  }
  const s = SCENARIOS[key];
  const base = { tool: "query_hr", memory_on: memory, suppressed: [], hints: [] };
  const department = s.query.filters.find((f) => f.col === "Department").val;
  if (!who || (who.role !== "hr_head" && !who.departments.includes(department))) {
    const reason = "This request is outside your authorized employee scope.";
    return { reply: reason, trace: [{ ...base, query: s.query, status: "blocked",
      rule: "L1.scope", evidence_text: reason, cells: [], allowed_transforms: [] }] };
  }
  const r = s.result(day());

  if (memory) {
    const prev = state.releases
      .filter((x) => x.team === who.team && x.scenario === key)
      .map((x) => ({ ...x, ch: s.changes(x.day, day()) }))
      .find((x) => { const c = x.ch.joined + x.ch.valueChanged; return c >= 1 && c <= 4; });
    if (prev) {
      const blocked = { ...base, query: s.query, status: "blocked", rule: "L4.pairwise",
        evidence_text: evidenceText(prev, prev.ch), cells: [],
        allowed_transforms: ["T3_widen_scope"],
        hints: ["Reviewer precedent: for single-role pay questions, widen to the department"] };
      const w = s.widened.result(day());
      const rewrite = { ...base, query: s.widened.query, status: "allowed", rule: null,
        evidence_text: null, cells: [{ group: {}, n: w.n, value: w.value }],
        allowed_transforms: [] };
      return {
        reply: `I can't give the ${s.label.toLowerCase()} right now: ${blocked.evidence_text} ` +
               `Instead, for ${s.widened.label} (${w.n} people) the average monthly income is ` +
               `$${money(w.value)}.`,
        trace: [blocked, rewrite],
      };
    }
    state.releases.push({ team: who.team, scenario: key, day: day(), label: s.label });
  }
  return {
    reply: `The average monthly income of ${s.label.split(",")[0].toLowerCase()} is ` +
           `$${money(r.value)} (${r.n} people).`,
    trace: [{ ...base, query: s.query, status: "allowed", rule: null, evidence_text: null,
              cells: [{ group: {}, n: r.n, value: r.value }], allowed_transforms: [] }],
  };
}

export function api(getUser) {
  return {
    users: async () => USERS,
    chat: (payload) => chat(getUser(), payload),
    clock: async () => ({ now: nowIso(), offset_days: state.offset }),
    advanceClock: async (days) => {
      state.offset += Number(days);
      return { now: nowIso(), offset_days: state.offset };
    },
    simulateUpdate: async () => {
      state.raisedAt = day();
      return { ok: true };
    },
    reset: async () => {
      Object.assign(state, { offset: 0, raisedAt: null, releases: [] });
      // warmup: "last week" questions already asked by the Sales and HR teams on Monday
      state.releases.push({ team: "sales", scenario: "sales_mgr", day: 0, label: SCENARIOS.sales_mgr.label });
      state.releases.push({ team: "hr", scenario: "hr_mgr", day: 0, label: SCENARIOS.hr_mgr.label });
      return { ok: true, now: nowIso() };
    },
    pendingReviews: async () => [],
    decideReview: async () => ({ ok: true }),
  };
}
