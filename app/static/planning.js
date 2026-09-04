import {
  $,
  $$,
  api,
  escapeHtml as esc,
  showError,
  busy,
  openDialog,
  localDateTime,
  money,
  localPath,
  changed,
  dateValue,
} from "./core.js";
import { showRecord } from "./records.js";

let current = "intents";
const states = {
  inbox: "待考虑",
  considering: "考虑中",
  planned: "已计划",
  due: "到期",
  completed: "已完成",
  skipped: "已跳过",
  cancelled: "已取消",
  active: "进行中",
  paused: "暂停",
  ended: "已结束",
  pending: "待处理",
  read: "已读",
  handled: "已处理",
  dismissed: "已关闭",
};
const pathFor = (kind) =>
  kind === "intents"
    ? "/intents"
    : kind === "commitments"
      ? "/recurring-commitments"
      : kind === "cycles"
        ? "/use-cycles"
        : "/reminders";
export async function loadPlanning(kind = current) {
  current = kind;
  const data = await api(pathFor(kind));
  $("#add-plan").hidden = kind === "reminders";
  $("#add-plan").textContent =
    kind === "commitments"
      ? "添加周期事项"
      : kind === "cycles"
        ? "去消费记忆开始使用"
        : "添加想要";
  $("#planning-list").innerHTML =
    data.items
      .map(
        (row) =>
          `<article class="list-item"><div><h3>${esc(row.title || row.label || row.payload?.title || "提醒")}</h3><p>${esc(states[row.state] || row.state)} · ${row.expected_amount ? money(row.expected_amount, row.currency) : "金额未知"}</p><span class="meta">${row.next_due_at ? dateValue(row.next_due_at).toLocaleString("zh-CN") : row.started_at ? dateValue(row.started_at).toLocaleDateString("zh-CN") : ""}</span></div><div class="actions">${["intents", "commitments"].includes(kind) ? `<button data-edit-plan="${row.id}">编辑</button>${!["completed", "cancelled", "ended", "paused", "skipped"].includes(row.state) ? `<button data-plan-draft="${row.id}">生成待确认草稿</button>` : ""}` : ""}${kind === "cycles" && row.state === "active" ? `<button data-end-cycle="${row.id}">明确结束使用</button>` : ""}${kind === "reminders" && ["pending", "read"].includes(row.state) ? `<button data-dismiss-reminder="${row.id}">关闭提醒</button>` : ""}</div></article>`,
      )
      .join("") || '<p class="muted">这里还没有内容。</p>';
  data.items.forEach((row, index) => {
    const article = $("#planning-list").querySelectorAll("article")[index];
    const actions = article.querySelector(".actions");
    if (["intents", "commitments"].includes(kind) && !row.expected_amount)
      article.querySelector("p").textContent =
        `${states[row.state] || row.state} · 未设预计金额`;
    if (kind === "reminders") {
      article.querySelector(".meta").textContent = dateValue(
        row.due_at,
      ).toLocaleString("zh-CN");
      article.querySelector("p").textContent =
        `${states[row.state] || row.state} · 提醒不代表已付款`;
    }
    if (kind === "intents" && row.state === "completed") {
      actions.querySelector("[data-edit-plan]")?.remove();
      if (row.completed_record_id) {
        const button = document.createElement("button");
        button.textContent = "查看关联记录";
        button.onclick = () =>
          showRecord(row.completed_record_id).catch(showError);
        actions.append(button);
      }
    }
    if (
      kind === "intents" &&
      !["completed", "cancelled", "skipped"].includes(row.state)
    ) {
      const button = document.createElement("button");
      button.textContent = "关联已确认记录完成";
      button.onclick = () => completePlan(row);
      actions.append(button);
    }
    if (
      kind === "reminders" &&
      row.source_type === "commitment" &&
      ["pending", "read"].includes(row.state)
    ) {
      const button = document.createElement("button");
      button.textContent = "为这次提醒生成草稿";
      button.onclick = () =>
        busy(button, async () => {
          const record = await api(
            `/recurring-commitments/${row.source_id}/draft?${new URLSearchParams({ occurrence: row.due_at })}`,
            { method: "POST" },
          );
          await loadPlanning("reminders");
          await showRecord(record.id);
        });
      actions.append(button);
    }
  });
  $$("[data-edit-plan]").forEach(
    (button) =>
      (button.onclick = () =>
        editPlan(
          kind,
          data.items.find((row) => row.id === button.dataset.editPlan),
        )),
  );
  $$("[data-plan-draft]").forEach(
    (button) =>
      (button.onclick = () =>
        busy(button, async () => {
          const row = await api(
            `${pathFor(kind)}/${button.dataset.planDraft}/draft`,
            { method: "POST" },
          );
          changed();
          await showRecord(row.id);
        })),
  );
  $$("[data-end-cycle]").forEach(
    (button) =>
      (button.onclick = () =>
        busy(button, async () => {
          const row = data.items.find(
            (row) => row.id === button.dataset.endCycle,
          );
          if (!confirm("确认已经结束使用？这与购买时间不同。")) return;
          await api(`/use-cycles/${row.id}/complete`, {
            method: "POST",
            headers: { "If-Match": String(row.revision) },
            body: JSON.stringify({ ended_at: null }),
          });
          await loadPlanning();
        })),
  );
  $$("[data-dismiss-reminder]").forEach(
    (button) =>
      (button.onclick = () =>
        busy(button, async () => {
          await api(`/reminders/${button.dataset.dismissReminder}/dismiss`, {
            method: "POST",
          });
          await loadPlanning();
        })),
  );
}
function completePlan(row) {
  openDialog(
    `<h2>完成计划：${esc(row.title)}</h2><p>选择已经确认的消费，不会自动录入新事实。</p><form id="complete-search"><label>搜索标题或商家<input id="complete-query" maxlength="200"></label><button>查找</button></form><form id="complete-plan"><label>已确认记录<select id="complete-record" required></select></label><p>最多显示 50 条；找不到时请缩小搜索范围。</p><button type="submit">关联并完成计划</button><p id="complete-status" role="status"></p></form>`,
  );
  const load = async () => {
    const result = await api(
      `/records?${new URLSearchParams({ state: "confirmed", limit: "50", query: $("#complete-query").value.trim() })}`,
    );
    $("#complete-record").innerHTML =
      '<option value="">请选择已确认记录</option>' +
      result.items
        .map(
          (record) =>
            `<option value="${record.id}">${esc(record.money_entry?.title || record.consumption?.merchant_name_raw || "消费记录")} · ${esc(dateValue(record.occurred_at).toLocaleString("zh-CN"))} · ${record.money_entry ? money(record.money_entry.amount, record.money_entry.currency) : "金额未知"}</option>`,
        )
        .join("");
  };
  $("#complete-search").onsubmit = (event) => {
    event.preventDefault();
    busy(event.submitter, load, $("#complete-status"));
  };
  $("#complete-plan").onsubmit = (event) => {
    event.preventDefault();
    busy(
      event.submitter,
      async () => {
        const id = $("#complete-record").value;
        if (!id) throw new Error("请选择已确认记录");
        await api(`/intents/${row.id}/complete?record_id=${id}`, {
          method: "POST",
          headers: { "If-Match": String(row.revision) },
        });
        $("#dialog").close();
        changed();
        await loadPlanning("intents");
      },
      $("#complete-status"),
    );
  };
  load().catch(showError);
}
function editPlan(kind, row = null) {
  const periodic = kind === "commitments";
  if (row?.state === "completed") {
    showError(new Error("已完成计划保留历史，不直接改回待处理。"));
    return;
  }
  const kinds = periodic
    ? {
        subscription: "订阅",
        membership: "会员",
        regular_service: "定期服务",
        regular_purchase: "定期购买",
        fixed_expense: "固定支出",
        regular_income: "固定收入",
      }
    : {
        buy: "想买",
        eat: "想吃",
        drink: "想喝",
        visit: "想去",
        subscribe: "考虑订阅",
        renew: "考虑续费",
        cancel: "考虑取消",
        replace: "考虑更换",
        other: "其他",
      };
  const allowed = periodic
    ? ["active", "paused", "ended"]
    : ["inbox", "considering", "planned", "due", "skipped", "cancelled"];
  openDialog(
    `<h2>${row ? "编辑" : "添加"}${periodic ? "周期事项" : "想要"}</h2><p>计划不代表已经消费，只能生成提醒或待确认草稿。</p><form id="plan-form"><label>内容<input id="plan-title" required maxlength="500"></label><label>类型<select id="plan-kind">${Object.entries(
      kinds,
    )
      .map(([value, label]) => `<option value="${value}">${label}</option>`)
      .join(
        "",
      )}</select></label><div class="details-grid"><label>预计金额（可选）<input id="plan-amount" inputmode="decimal"></label><label>币种<input id="plan-currency" maxlength="3" required value="CNY"></label></div>${periodic ? '<label>下一次时间<input id="plan-due" type="datetime-local" required></label><label>重复规则<input id="plan-rule" value="FREQ=MONTHLY" required maxlength="500"></label><p class="muted">常用：FREQ=MONTHLY（月）、FREQ=YEARLY（年）、FREQ=WEEKLY（周）。复杂规则保留原值。</p>' : ""}${row ? `<label>状态<select id="plan-state">${allowed.map((state) => `<option value="${state}">${states[state]}</option>`).join("")}</select></label>` : ""}<button class="primary" type="submit">保存计划</button><p id="plan-status" role="status"></p></form>`,
  );
  $("#plan-title").value = row?.title || "";
  $("#plan-amount").value = row?.expected_amount || "";
  $("#plan-currency").value = row?.currency || "CNY";
  $("#plan-kind").value =
    row?.[periodic ? "kind" : "intent_type"] ||
    (periodic ? "subscription" : "buy");
  if (row) $("#plan-state").value = row.state;
  if (periodic) {
    $("#plan-due").value = localDateTime(
      row?.next_due_at || new Date().toISOString(),
    );
    $("#plan-rule").value = row?.recurrence_rule || "FREQ=MONTHLY";
  }
  $("#plan-form").onsubmit = (event) => {
    event.preventDefault();
    busy(
      event.submitter,
      async () => {
        const fields = periodic
          ? [
              "kind",
              "title",
              "merchant_id",
              "item_identity_id",
              "expected_amount",
              "currency",
              "recurrence_rule",
              "timezone",
              "next_due_at",
              "auto_renew",
              "remind_before_seconds",
            ]
          : [
              "intent_type",
              "title",
              "merchant_id",
              "item_identity_id",
              "place_ref",
              "expected_amount",
              "currency",
              "desired_start",
              "desired_end",
              "priority",
              "state",
              "reason",
            ];
        const body = Object.fromEntries(
          fields
            .filter((field) => row && field in row)
            .map((field) => [field, row[field]]),
        );
        Object.assign(body, {
          title: $("#plan-title").value.trim(),
          expected_amount: $("#plan-amount").value.trim() || null,
          currency: $("#plan-currency").value.toUpperCase(),
          [periodic ? "kind" : "intent_type"]: $("#plan-kind").value,
        });
        if (periodic)
          Object.assign(body, {
            next_due_at: new Date($("#plan-due").value).toISOString(),
            recurrence_rule: $("#plan-rule").value.trim(),
            timezone:
              row?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone,
          });
        else body.state = row ? $("#plan-state").value : "inbox";
        const path =
          pathFor(kind) +
          (row ? "/" + row.id : "") +
          (row && periodic ? "?state=" + $("#plan-state").value : "");
        await api(path, {
          method: row ? "PATCH" : "POST",
          headers: row ? { "If-Match": String(row.revision) } : {},
          body: JSON.stringify(body),
        });
        $("#dialog").close();
        changed();
        await loadPlanning(kind);
      },
      $("#plan-status"),
    );
  };
}
export function initPlanning() {
  $$("[data-plan]").forEach(
    (button) =>
      (button.onclick = () => {
        $$("[data-plan]").forEach((other) =>
          other.classList.toggle("active", other === button),
        );
        loadPlanning(button.dataset.plan).catch(showError);
      }),
  );
  $("#add-plan").onclick = () =>
    current === "cycles"
      ? (location.href = localPath("/consumption"))
      : editPlan(current);
  loadPlanning().catch(showError);
}
