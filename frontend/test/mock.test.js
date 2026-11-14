import assert from "node:assert/strict";
import test from "node:test";

let instance = 0;
async function fixture(user = "mgr_1") {
  const { api } = await import(`../src/mock.js?test=${instance++}`);
  return { client: api(() => user), use: (next) => { user = next; } };
}
const question = (department, memory = true, project = "comp-review-2026") => ({
  message: `What is the average monthly income of ${department} managers?`, memory, project,
});
const raise = (client) => client.simulateUpdate("1408", "MonthlyIncome", "18500");

test("cold start seeds both teams and both scripted attacks block", async () => {
  const { client, use } = await fixture();
  await client.advanceClock(2);
  await raise(client);
  await client.advanceClock(1);
  const sales = await client.chat(question("Sales"));
  assert.equal(sales.trace[0].rule, "L4.pairwise");
  assert.equal(sales.trace[1].status, "allowed");
  use("mgr_4");
  const hr = await client.chat(question("HR"));
  assert.equal(hr.trace[0].rule, "L4.pairwise");
  assert.match(hr.trace[0].evidence_text, /fewer than 5 people/);
  assert.doesNotMatch(hr.trace[0].evidence_text, /1 person|value changed|got a raise/);
});

test("reset restores Monday values and exactly one baseline release per team", async () => {
  const { client, use } = await fixture();
  await client.advanceClock(2);
  await raise(client);
  await client.reset();
  assert.equal((await client.clock()).offset_days, 0);
  use("mgr_4");
  const baseline = await client.chat(question("HR", false));
  assert.equal(baseline.trace[0].cells[0].value, 18088.64);
  await client.advanceClock(2);
  await raise(client);
  await client.advanceClock(1);
  assert.equal((await client.chat(question("HR"))).trace[0].rule, "L4.pairwise");
});

test("memory off neither consumes nor adds release history", async () => {
  const { client } = await fixture("mgr_4");
  await client.chat(question("HR", false));
  await client.advanceClock(2);
  await raise(client);
  await client.advanceClock(1);
  const off = await client.chat(question("HR", false));
  assert.equal(off.trace[0].status, "allowed");
  assert.equal((await client.chat(question("HR"))).trace[0].rule, "L4.pairwise");
});

test("duplicate pre-update observations return only generic history evidence", async () => {
  const { client } = await fixture("mgr_4");
  await client.chat(question("HR"));
  await client.advanceClock(2);
  await raise(client);
  await client.advanceClock(1);
  const result = await client.chat(question("HR"));
  assert.equal(result.trace.length, 1);
  assert.equal(result.trace[0].rule, "L4");
  assert.deepEqual(result.trace[0].cells, []);
  assert.equal(result.reply, "This answer cannot be safely released alongside earlier answers.");
  assert.doesNotMatch(JSON.stringify(result), /timestamp|pre_update|1408/);
});

test("equal release and update time blocks before pairwise", async () => {
  const { client } = await fixture("mgr_4");
  await client.advanceClock(2);
  await client.chat(question("HR", true, "another-project"));
  await raise(client);
  await client.advanceClock(1);
  assert.equal((await client.chat(question("HR", true, "another-project"))).trace[0].rule, "L4");
});

test("history is scoped to team and project; teammate evidence hides old details", async () => {
  const { client, use } = await fixture("mgr_2");
  await client.advanceClock(3);
  const teammate = await client.chat(question("Sales"));
  assert.match(teammate.trace[0].evidence_text, /an earlier answer given to your team/);
  assert.doesNotMatch(teammate.trace[0].evidence_text, /11\/09|Sales managers/);
  assert.equal((await client.chat(question("Sales", true, "another-project"))).trace[0].status, "allowed");
  use("mgr_3");
  assert.equal((await client.chat(question("Sales"))).trace[0].status, "allowed");
});

test("invalid clock/update inputs cannot change state", async () => {
  const { client } = await fixture();
  for (const days of [0, -1, NaN, Infinity, "NaN", "Inf", "", true, [], 1e20, 1e-20]) {
    await assert.rejects(client.advanceClock(days), /^Error: 400:/);
    assert.equal((await client.clock()).offset_days, 0);
  }
  await assert.rejects(raise(client), /^Error: 409:/);
  await client.advanceClock(2);
  for (const args of [[473, "MonthlyIncome", 18500], [1408, "JobSatisfaction", 18500],
    [1408, "MonthlyIncome", "abc"], [1408, "MonthlyIncome", "NaN"],
    [1408, "MonthlyIncome", 18000]]) {
    await assert.rejects(client.simulateUpdate(...args), /^Error: 400:/);
  }
  const result = await client.chat(question("HR", false));
  assert.equal(result.trace[0].cells[0].value, 18088.64);
});

test("authenticated controls and HR-only review match role checks", async () => {
  const { client, use } = await fixture("unknown");
  for (const action of [() => client.clock(), () => client.reset(),
    () => client.advanceClock(1), () => raise(client), () => client.chat(question("Sales"))]) {
    await assert.rejects(action(), /^Error: 401:/);
  }
  use("mgr_1");
  await assert.rejects(client.pendingReviews(), /^Error: 403:/);
  use("hr_1");
  assert.deepEqual(await client.pendingReviews(), []);
});
