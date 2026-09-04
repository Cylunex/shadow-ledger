import assert from "node:assert/strict";
import { test } from "node:test";

const nodes = new Map();
globalThis.document = {
  body: { dataset: { prefix: "/ledger", page: "records" } },
  cookie: "__Host-ledger-csrf=test-csrf",
  querySelector: (selector) => nodes.get(selector),
  createElement: () => ({
    textContent: "",
    get innerHTML() {
      return this.textContent;
    },
  }),
  dispatchEvent: () => {},
};
const core = await import("../../app/static/core.js");

test("calendar month uses the configured zone at UTC boundaries", () => {
  const instant = new Date("2026-08-31T18:00:00Z");
  assert.equal(core.monthInTimezone("Asia/Shanghai", instant), "2026-09");
  assert.equal(core.monthInTimezone("UTC", instant), "2026-08");
});

test("money and selected totals use exact decimal arithmetic", () => {
  assert.equal(core.sumAmounts(["0.1000", "0.2000"]), "0.3000");
  assert.equal(
    core.sumAmounts(["9999999999999.9999", "0.0001"]),
    "10000000000000.0000",
  );
  assert.equal(core.money("12.3400", "CNY"), "CNY 12.34");
  assert.equal(core.sumAmounts(["-1.2500", "0.1000"]), "-1.1500");
  assert.equal(core.sumAmounts(["-0.2500", "0.1000"]), "-0.1500");
});

test("separate explicit intents never share an in-flight write", async () => {
  const keys = [];
  globalThis.fetch = async (_path, options) => {
    keys.push(options.headers["Idempotency-Key"]);
    return { ok: true, status: 201, json: async () => ({}) };
  };
  await Promise.all(
    ["intent-a", "intent-b"].map((id) =>
      core.api("/records", {
        method: "POST",
        headers: { "Idempotency-Key": id },
        body: '{"note":"identical"}',
      }),
    ),
  );
  assert.deepEqual(keys, ["intent-a", "intent-b"]);
});

test("unreadable success response preserves the original write key", async () => {
  const keys = [];
  let broken = true;
  globalThis.fetch = async (_path, options) => {
    keys.push(options.headers["Idempotency-Key"]);
    return {
      ok: true,
      status: 201,
      json: async () => {
        if (broken) throw new Error("truncated");
        return {};
      },
    };
  };
  const options = { method: "POST", body: '{"note":"retry-success-body"}' };
  await assert.rejects(core.api("/records", options));
  broken = false;
  await core.api("/records", options);
  assert.equal(keys[0], keys[1]);
});

test("question answer selects requested income and budget rather than net spending", async () => {
  const { queryAnswer } = await import("../../app/static/agent.js");
  const result = {
    metrics: [{ metric_id: "income", value: "100.0000", currency: "CNY" }],
    facts_text: "净支出 5 CNY",
    budgets: [],
  };
  assert.match(queryAnswer("本月收入多少？", result), /^收入 100.0000 CNY/);
  assert.match(queryAnswer("预算", result), /没有可读取的消费目标/);
});

test("network retry keeps key; concurrent same intent shares one request", async () => {
  const calls = [];
  let fail = true;
  globalThis.fetch = async (path, options) => {
    calls.push({ path, ...options });
    if (fail) throw new Error("network");
    return { ok: true, status: 201, json: async () => ({ id: "once" }) };
  };
  const options = { method: "POST", body: JSON.stringify({ amount: "1" }) };
  await assert.rejects(core.api("/records", options));
  fail = false;
  const results = await Promise.all([
    core.api("/records", options),
    core.api("/records", options),
  ]);
  assert.equal(calls.length, 2);
  assert.equal(
    calls[0].headers["Idempotency-Key"],
    calls[1].headers["Idempotency-Key"],
  );
  assert.equal(calls[1].path, "/ledger/api/v1/records");
  assert.equal(calls[1].headers["X-CSRF-Token"], "test-csrf");
  assert.deepEqual(results, [{ id: "once" }, { id: "once" }]);
  await core.api("/records", options);
  assert.notEqual(
    calls[2].headers["Idempotency-Key"],
    calls[1].headers["Idempotency-Key"],
  );
});

test("versioned commands do not share an in-flight request across revisions", async () => {
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    return { ok: true, status: 200, json: async () => ({}) };
  };
  await Promise.all(
    [1, 2].map((revision) =>
      core.api("/records/test", {
        method: "PATCH",
        headers: { "If-Match": String(revision) },
        body: '{"note":"x"}',
      }),
    ),
  );
  assert.equal(calls, 2);
});

test("offline queue is opt-in, user-bound and only syncs drafts", async () => {
  const store = new Map();
  globalThis.localStorage = {
    getItem: (key) => store.get(key) || null,
    setItem: (key, value) => store.set(key, value),
  };
  const events = {};
  for (const selector of ["#offline-clear", "#offline-sync"]) {
    nodes.set(selector, {
      disabled: false,
      addEventListener: (name, handler) => (events[selector] = handler),
    });
  }
  nodes.set("#offline-count", { textContent: "" });
  nodes.set("#offline-enable", { checked: false });
  const sent = [];
  globalThis.fetch = async (path, options) => {
    if (path.endsWith("/me"))
      return {
        ok: true,
        status: 200,
        json: async () => ({ owner_id: "alice" }),
      };
    sent.push(options);
    return { ok: true, status: 201, json: async () => ({ id: "draft" }) };
  };
  await core.loadUser();
  const offline = await import("../../app/static/offline.js");
  offline.initOffline();
  assert.throws(() => offline.queueDraft({ confirm: true }), /开启/);
  nodes.get("#offline-enable").checked = true;
  offline.queueDraft({ confirm: true, note: "text" });
  const saved = JSON.parse(store.get("ledger.text-drafts.v1"));
  assert.equal(saved[0].body.confirm, false);
  assert.equal(saved[0].owner, "alice");
  await events["#offline-sync"]({ target: nodes.get("#offline-sync") });
  assert.equal(JSON.parse(sent[0].body).confirm, false);
  assert.equal(sent[0].headers["Idempotency-Key"], saved[0].id);
  assert.equal(JSON.parse(store.get("ledger.text-drafts.v1")).length, 0);
});
