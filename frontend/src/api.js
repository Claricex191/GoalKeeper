// Fetch wrappers. Every call sends X-Demo-User; role and team are resolved server-side.
// `npm run dev:mock` swaps in a scripted in-browser backend (mock.js) for UI work
// before the real /chat exists. See ../API.md for the contract.
import * as mock from "./mock.js";

const MOCK = import.meta.env.VITE_MOCK === "1";
export const isMock = MOCK;

let currentUser = "mgr_1";
export function setUser(id) {
  currentUser = id;
}

async function call(method, path, { body, query } = {}) {
  const qs = query ? "?" + new URLSearchParams(query).toString() : "";
  const res = await fetch(`/api${path}${qs}`, {
    method,
    headers: {
      "X-Demo-User": currentUser,
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(`${res.status}: ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
  }
  return res.json();
}

export const api = MOCK
  ? mock.api(() => currentUser)
  : {
      users: () => call("GET", "/demo/users"),
      chat: (payload) => call("POST", "/chat", { body: payload }),
      clock: () => call("GET", "/admin/clock"),
      advanceClock: (days) => call("POST", "/admin/clock", { query: { days } }),
      simulateUpdate: (employee_number, field, new_value) =>
        call("POST", "/admin/simulate-update", { query: { employee_number, field, new_value } }),
      reset: () => call("POST", "/admin/reset"),
      pendingReviews: () => call("GET", "/review/pending"),
      decideReview: (id, approve, transform) =>
        call("POST", `/review/${id}`, { query: { approve, ...(transform ? { transform } : {}) } }),
    };
