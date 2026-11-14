// Scripted in-browser backend for `npm run dev:mock`: lets the UI be built and
// rehearsed without a live backend. Numbers are the real IBM HR values for
// Act 1 (new hire) and Act 2 (raise); everything else returns a generic answer.
// Response shapes follow ../API.md; this is a scripted rehearsal, not the real gate.

const USERS = [
  { user: "mgr_1", role: "department_manager", team: "sales", departments: ["Sales"], display_name: "Manager 1 (Sales)" },
  { user: "mgr_2", role: "department_manager", team: "sales", departments: ["Sales"], display_name: "Manager 2 (Sales)" },
  { user: "mgr_3", role: "department_manager", team: "rnd", departments: ["Research & Development"], display_name: "Manager 3 (R&D)" },
  { user: "mgr_4", role: "department_manager", team: "hr", departments: ["Human Resources"], display_name: "Manager 4 (HR dept.)" },
  { user: "hr_1", role: "hr_head", team: "hr", departments: ["Human Resources"], display_name: "HR Head" },
  { user: "ceo_1", role: "ceo", team: "executive", departments: [], display_name: "CEO" },
  { user: "vp_1", role: "vp", team: "sales", departments: ["Sales"], display_name: "VP (Sales)" },
  { user: "salary_1", role: "hr_salary_manager", team: "hr", departments: ["Human Resources"], display_name: "HR Salary Manager" },
  { user: "dept_hr_1", role: "department_hr_manager", team: "sales", departments: ["Sales"], display_name: "Department HR Manager (Sales)" },
  { user: "ordinary_hr_1", role: "ordinary_hr", team: "hr", departments: ["Human Resources"], display_name: "Ordinary HR" },
  { user: "emp_1", role: "employee", team: "sales", departments: ["Sales"], display_name: "Employee (Sales)" },
];
// Grants for the two scripted salary aggregates, matching policy/defaults.py.
const COMPANY_SCOPE_ROLES = new Set(["ceo", "hr_head", "hr_salary_manager"]);
const SALARY_AGGREGATE_ROLES = new Set([
  ...COMPANY_SCOPE_ROLES, "vp", "department_hr_manager", "department_manager",
]);
const DEMO_START = Date.UTC(2026, 10, 9, 9, 0); // Mon 2026-11-09 09:00 UTC
const DAY = 86400000;
const PROJECT = "comp-review-2026";
// Same conservative upper bound as backend/app/clock.py.
const MAX_OFFSET_DAYS = Math.floor((Date.UTC(9999, 11, 31, 23, 59, 59, 999) - DEMO_START) / DAY) - 1;

const state = { offset: 0, raisedAt: null, releases: [] };

const eq = (col, val) => ({ col, op: "=", val });
const SCENARIOS = {
  sales_mgr: {
    match: (m) => /sales/i.test(m) && /manager/i.test(m),
    query: { mode: "aggre_stats", filters: [eq("Department", "Sales"), eq("JobRole", "Manager")], group_by: [],
             metric: "avg", target: "MonthlyIncome", transform: null },
    label: "Sales managers, average monthly income",
    result: (day) => (day >= 3 ? { n: 37, value: 16986.97 } : { n: 36, value: 17111.5 }),
    // what changed between an earlier release (on `then`) and now
    changes: (then, now) => ({ joined: then < 3 && now >= 3 ? 1 : 0, valueChanged: 0 }),
    widened: {
      query: { mode: "aggre_stats", filters: [eq("Department", "Sales")], group_by: [], metric: "avg",
               target: "MonthlyIncome", transform: "T3_widen_scope" },
      result: (day) => (day >= 3 ? { n: 446, value: 6959.17 } : { n: 445, value: 6946.71 }),
      label: "the whole Sales department",
    },
  },
  hr_mgr: {
    match: (m) => /(hr|human resources)/i.test(m) && /manager/i.test(m),
    query: { mode: "aggre_stats", filters: [eq("Department", "Human Resources"), eq("JobRole", "Manager")],
             group_by: [], metric: "avg", target: "MonthlyIncome", transform: null },
    label: "HR managers, average monthly income",
    result: () => ({ n: 11, value: state.raisedAt !== null ? 18243.27 : 18088.64 }),
    changes: (then) => ({ joined: 0,
                          valueChanged: state.raisedAt !== null && state.raisedAt > then ? 1 : 0 }),
    widened: {
      query: { mode: "aggre_stats", filters: [eq("Department", "Human Resources")], group_by: [], metric: "avg",
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

function resetState() {
  Object.assign(state, { offset: 0, raisedAt: null, releases: [
    { user: "mgr_1", team: "sales", project: PROJECT, scenario: "sales_mgr", day: 0,
      label: SCENARIOS.sales_mgr.label },
    { user: "mgr_4", team: "hr", project: PROJECT, scenario: "hr_mgr", day: 0,
      label: SCENARIOS.hr_mgr.label },
  ] });
}

// Cold start and Reset prepare the same two Monday disclosures.
resetState();

function identity(userId) {
  const who = USERS.find((u) => u.user === userId);
  if (!who) throw new Error("401: unknown user (send X-Demo-User)");
  return who;
}

function numeric(value) {
  if (!["string", "number"].includes(typeof value) || String(value).trim() === "") return NaN;
  return Number(value);
}

function evidenceText(prev, viewer) {
  const source = prev.user === viewer
    ? `your earlier question on ${dayName(prev.day)} ('${prev.label}')`
    : "an earlier answer given to your team";
  return `Since ${source}, fewer than 5 people in this group joined, left or had their ` +
    "data changed. Answering now would reveal their values.";
}

async function chat(userId, { message, memory = true, project = PROJECT }) {
  const who = identity(userId);
  await wait(500);
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
  if (!SALARY_AGGREGATE_ROLES.has(who.role)) {
    const reason = `Your role (${who.role}) cannot ask for avg of MonthlyIncome.`;
    return { reply: reason, trace: [{ ...base, query: s.query, status: "blocked",
      rule: "L1.role", evidence_text: reason, cells: [], allowed_transforms: [] }] };
  }
  if (!COMPANY_SCOPE_ROLES.has(who.role) && !who.departments.includes(department)) {
    const reason = "This request is outside your authorized employee scope.";
    return { reply: reason, trace: [{ ...base, query: s.query, status: "blocked",
      rule: "L1.scope", evidence_text: reason, cells: [], allowed_transforms: [] }] };
  }
  const r = s.result(day());

  if (memory) {
    const history = state.releases.filter((x) =>
      x.team === who.team && x.project === project && x.scenario === key);
    // Every scripted HR population contains the updated manager. The real gate
    // checks ambiguity before pairwise, including prior widened answers.
    const ambiguous = key === "hr_mgr" && state.raisedAt !== null && (
      history.some((x) => x.day === state.raisedAt) ||
      history.filter((x) => x.day < state.raisedAt).length > 1
    );
    if (ambiguous) {
      const explanation = "This answer cannot be safely released alongside earlier answers.";
      return {
        reply: explanation,
        trace: [{ ...base, query: s.query, status: "blocked", rule: "L4",
          evidence_text: explanation, cells: [],
          allowed_transforms: ["T1_merge_groups", "T2_drop_filter", "T3_widen_scope"] }],
      };
    }
    const prev = history.filter((x) => !x.widened)
      .map((x) => ({ ...x, ch: s.changes(x.day, day()) }))
      .find((x) => { const c = x.ch.joined + x.ch.valueChanged; return c >= 1 && c <= 4; });
    if (prev) {
      const blocked = { ...base, query: s.query, status: "blocked", rule: "L4.pairwise",
        evidence_text: evidenceText(prev, who.user), cells: [],
        allowed_transforms: ["T3_widen_scope"],
        hints: ["Reviewer precedent: for single-role pay questions, widen to the department"] };
      const w = s.widened.result(day());
      const rewrite = { ...base, query: s.widened.query, status: "allowed", rule: null,
        evidence_text: null, cells: [{ group: {}, n: w.n, value: w.value }],
        allowed_transforms: [] };
      state.releases.push({ user: who.user, team: who.team, project, scenario: key,
        day: day(), label: s.widened.label, widened: true });
      return {
        reply: `I can't give the ${s.label.toLowerCase()} right now: ${blocked.evidence_text} ` +
               `Instead, for ${s.widened.label} (${w.n} people) the average monthly income is ` +
               `$${money(w.value)}.`,
        trace: [blocked, rewrite],
      };
    }
    state.releases.push({ user: who.user, team: who.team, project, scenario: key,
      day: day(), label: s.label });
  }
  return {
    reply: `The average monthly income of ${s.label.split(",")[0].toLowerCase()} is ` +
           `$${money(r.value)} (${r.n} people).`,
    trace: [{ ...base, query: s.query, status: "allowed", rule: null, evidence_text: null,
              cells: [{ group: {}, n: r.n, value: r.value }], allowed_transforms: [] }],
  };
}

export function api(getUser) {
  const current = () => identity(getUser());
  const requireHrHead = () => {
    const who = current();
    if (who.role !== "hr_head") throw new Error("403: HR Head role required");
    return who;
  };
  return {
    users: async () => USERS,
    chat: (payload) => chat(getUser(), payload),
    clock: async () => {
      current();
      return { now: nowIso(), offset_days: state.offset };
    },
    advanceClock: async (days) => {
      requireHrHead();
      const delta = numeric(days);
      const next = state.offset + delta;
      const timestamp = DEMO_START + next * DAY;
      if (!Number.isFinite(delta) || delta < 1 / DAY || next <= state.offset ||
          !Number.isFinite(timestamp) || !Number.isFinite(new Date(timestamp).getTime()) ||
          Math.trunc(timestamp) <= Math.trunc(DEMO_START + state.offset * DAY) ||
          next > MAX_OFFSET_DAYS) {
        throw new Error("400: days must be finite, positive and within the demo clock range");
      }
      state.offset = next;
      return { now: nowIso(), offset_days: state.offset };
    },
    simulateUpdate: async (employee_number, field, new_value) => {
      requireHrHead();
      if (numeric(employee_number) !== 1408 || field !== "MonthlyIncome" ||
          numeric(new_value) !== 18500) {
        throw new Error("400: mock supports only employee 1408, MonthlyIncome = 18500");
      }
      if (day() <= 0) throw new Error("409: advance the demo clock before updating employee data");
      state.raisedAt = day();
      return { ok: true };
    },
    reset: async () => {
      requireHrHead();
      resetState();
      return { ok: true, now: nowIso() };
    },
    pendingReviews: async () => {
      requireHrHead();
      return [];
    },
    decideReview: async () => {
      requireHrHead();
      return { ok: true };
    },
  };
}
