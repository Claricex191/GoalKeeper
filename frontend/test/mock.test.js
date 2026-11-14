import assert from "node:assert/strict";
import test from "node:test";

let instance = 0;
async function fixture(user = "hr_1") {
  const { api } = await import(`../src/mock.js?test=${instance++}`);
  return { client: api(() => user), use: (next) => { user = next; } };
}
const question = (department, memory = true, project = "comp-review-2026") => ({
  message: `What is the average monthly income of ${department} managers?`, memory, project,
});
const raise = (client) => client.simulateUpdate("1408", "MonthlyIncome", "18500");

async function prepareThursday(client, use) {
  use("hr_1");
  await client.advanceClock(2);
  await raise(client);
  await client.advanceClock(1);
}

test("cold start seeds both teams and both scripted attacks block", async () => {
  const { client, use } = await fixture();
  await prepareThursday(client, use);
  use("mgr_1");
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
  await prepareThursday(client, use);
  await client.reset();
  assert.equal((await client.clock()).offset_days, 0);
  use("mgr_4");
  const baseline = await client.chat(question("HR", false));
  assert.equal(baseline.trace[0].cells[0].value, 18088.64);
  await prepareThursday(client, use);
  use("mgr_4");
  assert.equal((await client.chat(question("HR"))).trace[0].rule, "L4.pairwise");
});

test("memory off neither consumes nor adds release history", async () => {
  const { client, use } = await fixture("mgr_4");
  await client.chat(question("HR", false));
  await prepareThursday(client, use);
  use("mgr_4");
  const off = await client.chat(question("HR", false));
  assert.equal(off.trace[0].status, "allowed");
  assert.equal((await client.chat(question("HR"))).trace[0].rule, "L4.pairwise");
});

test("duplicate pre-update observations return only generic history evidence", async () => {
  const { client, use } = await fixture("mgr_4");
  await client.chat(question("HR"));
  await prepareThursday(client, use);
  use("mgr_4");
  const result = await client.chat(question("HR"));
  assert.equal(result.trace.length, 1);
  assert.equal(result.trace[0].rule, "L4");
  assert.deepEqual(result.trace[0].cells, []);
  assert.equal(result.reply, "This answer cannot be safely released alongside earlier answers.");
  assert.doesNotMatch(JSON.stringify(result), /timestamp|pre_update|1408/);
});

test("equal release and update time blocks before pairwise", async () => {
  const { client, use } = await fixture();
  await client.advanceClock(2);
  use("mgr_4");
  await client.chat(question("HR", true, "another-project"));
  use("hr_1");
  await raise(client);
  await client.advanceClock(1);
  use("mgr_4");
  assert.equal((await client.chat(question("HR", true, "another-project"))).trace[0].rule, "L4");
});

test("history is scoped to project; teammate evidence hides old details", async () => {
  const { client, use } = await fixture();
  await client.advanceClock(3);
  use("mgr_2");
  const teammate = await client.chat(question("Sales"));
  assert.match(teammate.trace[0].evidence_text, /an earlier answer given to your team/);
  assert.doesNotMatch(teammate.trace[0].evidence_text, /11\/09|Sales managers/);
  assert.equal((await client.chat(question("Sales", true, "another-project"))).trace[0].status, "allowed");
  use("mgr_3");
  assert.equal((await client.chat(question("Sales"))).trace[0].rule, "L1.scope");
});

test("memory off still enforces department scope and salary grants", async () => {
  const { client, use } = await fixture("mgr_3");
  for (const memory of [true, false]) {
    const outside = await client.chat(question("Sales", memory));
    assert.equal(outside.trace[0].rule, "L1.scope");
    assert.deepEqual(outside.trace[0].cells, []);
  }
  for (const user of ["ordinary_hr_1", "emp_1"]) {
    use(user);
    const denied = await client.chat(question("HR", false));
    assert.equal(denied.trace[0].rule, "L1.role");
    assert.deepEqual(denied.trace[0].cells, []);
  }
});

test("company salary grants do not inherit an assigned-department restriction", async () => {
  const { client, use } = await fixture();
  for (const user of ["hr_1", "ceo_1", "salary_1"]) {
    use(user);
    assert.equal((await client.chat(question("Sales", false))).trace[0].status, "allowed");
  }
  use("vp_1");
  assert.equal((await client.chat(question("HR", false))).trace[0].rule, "L1.scope");
});

test("invalid clock/update inputs cannot change state", async () => {
  const { client, use } = await fixture();
  for (const days of [0, -1, NaN, Infinity, "NaN", "Inf", "", true, [], 1e20, 1e-20]) {
    await assert.rejects(client.advanceClock(days), /^Error: 400:/);
    assert.equal((await client.clock()).offset_days, 0);
  }
  await assert.rejects(raise(client), /^Error: 409:/);
  await client.advanceClock(2);
  for (const args of [[473, "MonthlyIncome", 18500], [1408, "JobSatisfaction", 18500],
    [1408, "Attrition", "Yes"], [1408, "MonthlyIncome", "abc"],
    [1408, "MonthlyIncome", "NaN"], [1408, "MonthlyIncome", 18000]]) {
    await assert.rejects(client.simulateUpdate(...args), /^Error: 400:/);
  }
  use("mgr_4");
  const result = await client.chat(question("HR", false));
  assert.equal(result.trace[0].cells[0].value, 18088.64);
});

test("known users can read clock; only HR Head can change state or review", async () => {
  const { client, use } = await fixture("unknown");
  for (const action of [() => client.clock(), () => client.reset(),
    () => client.advanceClock(1), () => raise(client), () => client.chat(question("Sales"))]) {
    await assert.rejects(action(), /^Error: 401:/);
  }
  use("hr_1");
  await client.advanceClock(2);
  await raise(client);
  const before = await client.clock();
  for (const user of (await client.users()).filter((u) => u.role !== "hr_head")) {
    use(user.user);
    assert.deepEqual(await client.clock(), before);
    for (const action of [() => client.reset(), () => client.advanceClock(1),
      () => raise(client), () => client.pendingReviews(), () => client.decideReview("id", true)]) {
      await assert.rejects(action(), /^Error: 403:/);
    }
    assert.deepEqual(await client.clock(), before);
  }
  use("hr_1");
  assert.deepEqual(await client.pendingReviews(), []);
  assert.deepEqual(await client.decideReview("id", true), { ok: true });
  use("mgr_4");
  // The denied reset attempts must preserve the salary change and original history.
  const off = await client.chat(question("HR", false));
  assert.equal(off.trace[0].cells[0].value, 18243.27);
  assert.equal((await client.chat(question("HR"))).trace[0].rule, "L4.pairwise");
});
