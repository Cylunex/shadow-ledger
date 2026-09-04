import { $, $$, api, escapeHtml, money, showError, busy } from "./core.js";
let latestRun = null;
const states = {
  active: "待考虑",
  snoozed: "稍后",
  handled: "已处理",
  dismissed: "已忽略",
};
function renderForecast() {
  const state = $("#forecast-state").value;
  const rows =
    latestRun?.items.filter((item) => !state || item.state === state) || [];
  $("#forecast-list").innerHTML =
    rows
      .map(
        (item) =>
          `<article class="forecast-card"><h4>${escapeHtml(item.explanation)}</h4><p>${new Date(item.predicted_at).toLocaleDateString("zh-CN")} · ${states[item.state]} · 启发式强度 ${item.confidence}（不是发生概率）</p><p>${item.expected_amount ? money(item.expected_amount, item.currency) : "金额未知，不用整单金额推断单品价格"}</p><details><summary>为什么出现这条建议</summary><p>依据仅来自已确认记录或明确的周期计划。处理建议不会生成已确认消费。</p><pre>${escapeHtml(JSON.stringify(item.evidence, null, 2))}</pre></details><div class="actions">${Object.entries(
            states,
          )
            .filter(([state]) => state !== item.state)
            .map(
              ([state, label]) =>
                `<button data-feedback="${state}" data-id="${item.id}" type="button">${state === "active" ? "恢复" : label}</button>`,
            )
            .join("")}</div></article>`,
      )
      .join("") ||
    '<p class="muted">当前没有此状态的建议。样本不足时不会编造预测。</p>';
  $$("[data-feedback]").forEach(
    (button) =>
      (button.onclick = () =>
        busy(button, async () => {
          const item = latestRun.items.find(
            (row) => row.id === button.dataset.id,
          );
          let until = null;
          if (button.dataset.feedback === "snoozed") {
            const days = prompt("几天后再提醒？（1–365）", "7");
            if (days === null) return;
            if (!/^\d+$/.test(days) || Number(days) < 1 || Number(days) > 365)
              throw new Error("请输入 1–365 天");
            until = new Date(
              Date.now() + Number(days) * 86400000,
            ).toISOString();
          }
          await api(`/forecast-items/${item.id}/feedback`, {
            method: "POST",
            body: JSON.stringify({
              revision: item.revision,
              feedback_revision: item.feedback_revision,
              state: button.dataset.feedback,
              snoozed_until: until,
            }),
          });
          await loadForecast();
        })),
  );
}
async function loadForecast(generate = false) {
  const data = generate
    ? await api("/forecasts/generate", {
        method: "POST",
        body: JSON.stringify({
          as_of: null,
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
          horizon_days: 90,
        }),
      })
    : await api("/forecasts/latest");
  latestRun = generate ? data : data.run;
  renderForecast();
}
export function initInsights() {
  $("#backtest-run").onclick = (event) =>
    busy(event.target, async () => {
      const day = $("#backtest-date").value;
      if (!day) throw new Error("请选择回看起始日期");
      const data = await api(
        "/forecast-evaluation?" +
          new URLSearchParams({
            as_of: day,
            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
            horizon_days: "90",
          }),
      );
      $("#backtest-result").innerHTML =
        `<p>${escapeHtml(data.notice)}</p><p>${data.predicted_count} 条复购建议，窗口内观察到 ${data.observed_count} 条后续购买。</p>` +
        data.items
          .map(
            (row) =>
              `<p>预测 ${new Date(row.predicted_at).toLocaleDateString("zh-CN")} · ${row.actual_at ? "后续购买 " + new Date(row.actual_at).toLocaleDateString("zh-CN") + "（相差 " + row.days_actual_minus_predicted + " 天）" : "窗口内未观察到"} · ${row.sample_count} 次历史样本</p>`,
          )
          .join("");
    });
  $("#forecast-generate").onclick = (event) =>
    busy(event.target, () => loadForecast(true));
  $("#forecast-state").onchange = renderForecast;
  loadInsights().catch(showError);
}
async function loadInsights() {
  const [summary, scenes, budgets, quality] = await Promise.all([
    api("/insights/summary"),
    api("/insights/scenes"),
    api(`/insights/budgets?month=${new Date().toISOString().slice(0, 7)}`),
    api("/insights/data-quality"),
  ]);
  $("#insight-summary").innerHTML = [
    ["支出", summary.expense],
    ["收入", summary.income],
    ["退款", summary.refund],
    ["净支出", summary.net_spending],
  ]
    .map(
      ([k, v]) =>
        `<div><span class="meta">${k}</span><h3>${money(v, summary.currency)}</h3></div>`,
    )
    .join("");
  const q = quality.data_completeness;
  const readiness = quality.prediction_readiness;
  $("#quality-summary").innerHTML =
    `<div class="list-item"><span>金额完整度</span><strong>${(Number(q.amount_coverage) * 100).toFixed(0)}%</strong></div><div class="list-item"><span>规范商家覆盖</span><strong>${(Number(q.canonical_merchant_coverage) * 100).toFixed(0)}%</strong></div><div class="list-item"><span>重复样本评估</span><strong>${readiness.status === "ready_for_evaluation" ? "样本可用" : "数据仍不足"}</strong></div>`;
  const max = Math.max(1, ...scenes.items.map((x) => x.count));
  $("#insight-scenes").innerHTML = scenes.items
    .map(
      (x) =>
        `<div><span>${escapeHtml(x.scene)} · ${x.count} 次</span><progress max="${max}" value="${x.count}" aria-label="消费次数"></progress></div>`,
    )
    .join("");
  $("#budget-list").innerHTML =
    budgets.items
      .map(
        (x) =>
          `<div class="list-item"><span>目标 ${money(x.target.monthly_amount, x.target.currency)}</span><strong>已用 ${money(x.net_spending, x.target.currency)}</strong></div>`,
      )
      .join("") || '<p class="muted">尚未设置本月消费目标</p>';
  await loadForecast(false);
}
