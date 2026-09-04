import {
  $,
  api,
  page,
  escapeHtml as esc,
  openDialog,
  showError,
  busy,
  changed,
  key,
} from "./core.js";

async function tool(skill, name, args) {
  const catalog = await api(`/agent/catalog?skill=${skill}`);
  return api("/agent/tools/call", {
    method: "POST",
    body: JSON.stringify({
      skill,
      catalog_hash: catalog.catalog_hash,
      tool: name,
      arguments: args,
    }),
  });
}

export async function showAgentReview(id) {
  const item = await api(`/agent/reviews/${id}`);
  const closed = item.problem || ["executed", "rejected"].includes(item.state);
  const entry = item.snapshot.money_entry;
  const summary = entry
    ? `${entry.title || "金额记录"} · ${entry.type} · ${entry.currency} ${entry.amount}`
    : "金额未知消费";
  openDialog(`<h2>Agent 精确审核</h2><p>Agent：${esc(item.agent_id)} · ${item.action === "confirm" ? "确认录入" : "删除草稿"} · 版本 ${item.revision}</p>
    <p>状态：${esc(item.state)}${item.problem ? ` · ${esc(item.problem)}，需要重新发起审核` : ""}</p>
    <p><strong>${esc(summary)}</strong><br>${esc(item.snapshot.occurred_at)} · ${esc(item.snapshot.timezone)}</p>
    <p>批准只对下面的冻结内容有效。内容、版本、证据或权限变化后必须重新审核；批准 10 分钟内有效。</p>
    <pre class="agent-snapshot">${esc(JSON.stringify(item.snapshot, null, 2))}</pre>
    <details><summary>策略与内容指纹</summary><pre>${esc(JSON.stringify(item.policy, null, 2))}</pre><code>${esc(item.display_hash)}</code></details>
    ${item.receipt ? `<h3>执行凭证</h3><pre class="agent-snapshot">${esc(JSON.stringify(item.receipt, null, 2))}</pre>` : ""}
    <p id="agent-decision-status" role="status"></p>
    ${closed ? "" : `<label><input type="checkbox" id="agent-ack">我已核对上面的完整内容与操作</label><div class="actions"><button id="agent-approve" type="button">${item.state === "approved" ? "执行已批准操作" : "批准并执行这次操作"}</button><button id="agent-deny" type="button" ${item.state === "approved" ? "disabled" : ""}>拒绝提案（保留草稿）</button></div>`}`);
  if (closed) return;
  $("#agent-approve").onclick = () =>
    busy($("#agent-approve"), async () => {
      if (!$("#agent-ack").checked)
        throw new Error("请先核对并勾选完整内容确认。");
      const grant = item.approval_grant_id
        ? { approval_grant_id: item.approval_grant_id }
        : await api(`/agent/reviews/${id}/decision`, {
            method: "POST",
            body: JSON.stringify({
              display_hash: item.display_hash,
              accept: true,
            }),
          });
      $("#agent-decision-status").textContent =
        "已批准，正在执行；网络失败可重新打开此卡片安全重试。";
      await api("/agent/execute", {
        method: "POST",
        body: JSON.stringify(
          grant.approval_grant_id
            ? { approval_grant_id: grant.approval_grant_id }
            : {},
        ),
      });
      changed();
      await showAgentReview(id);
    }).catch(showError);
  $("#agent-deny").onclick = () =>
    busy($("#agent-deny"), async () => {
      await api(`/agent/reviews/${id}/decision`, {
        method: "POST",
        body: JSON.stringify({
          display_hash: item.display_hash,
          accept: false,
        }),
      });
      changed();
      await showAgentReview(id);
    }).catch(showError);
}

let reviewOffset = null;
async function loadReviews(append = false) {
  const state = $("#agent-review-history").checked ? "all" : "pending";
  const result = await api(
    `/agent/reviews?state=${state}&offset=${append ? reviewOffset || 0 : 0}`,
  );
  const html = result.items
    .map(
      (item) =>
        `<button class="list-item" type="button" data-agent-review="${item.id}"><span>${esc(item.agent_id)} · ${item.action === "confirm" ? "确认录入" : "删除草稿"} · v${item.revision}</span><span>${esc(item.state)} →</span></button>`,
    )
    .join("");
  if (append) $("#agent-review-list").insertAdjacentHTML("beforeend", html);
  else
    $("#agent-review-list").innerHTML =
      html || '<p class="muted">没有待审核的 Agent 提案。</p>';
  reviewOffset = result.next_offset;
  $("#agent-review-more").hidden = reviewOffset === null;
}

function initResearch() {
  const container = $("#insights-page");
  const now = new Date();
  const month = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
  container.insertAdjacentHTML(
    "afterbegin",
    `<details class="panel"><summary>问账 · 有据可查</summary><p>直接调用确定性工具，不连接云模型。支持月度收支、预算和待处理建议；不自行猜测复杂问题。</p>
    <form id="agent-question"><label>问题<input id="agent-question-text" placeholder="本月净支出多少？" maxlength="200"></label><label>月份<input id="agent-month" type="month" value="${month}" required></label><label>币种<input id="agent-currency" value="CNY" pattern="[A-Z]{3}" maxlength="3" required></label><button>查询</button></form><div id="agent-answer" role="status"></div></details>`,
  );
  $("#agent-question").onsubmit = (event) => {
    event.preventDefault();
    busy($("#agent-question button"), async () => {
      const question = $("#agent-question-text").value.trim();
      if (
        question &&
        !/^(本月|这个月|所选月)?(净支出|支出|收入|退款|收支|预算|待处理|预测|周期)(多少|有哪些|怎么样|情况)?[？?]?$/.test(
          question,
        )
      )
        throw new Error(
          "当前内置问账只支持月度收支、预算、待处理、预测、周期。更复杂的问题请由已授权 Agent 选择结构化工具。",
        );
      const kind = question.includes("周期")
        ? "recurring"
        : question.includes("待处理")
          ? "draft"
          : "forecast";
      const attention = /周期|待处理|预测/.test(question);
      const result = await tool(
        "research",
        attention ? "ledger_attention" : "ledger_overview",
        attention
          ? { kind }
          : {
              month: $("#agent-month").value,
              currency: $("#agent-currency").value,
              timezone: "Asia/Shanghai",
            },
      );
      $("#agent-answer").innerHTML =
        `<p>${esc(result.facts_text)}</p><details><summary>结构化数字、口径与查询凭证</summary><pre class="agent-snapshot">${esc(JSON.stringify(result, null, 2))}</pre></details>`;
    }).catch(showError);
  };
}

function initCapture() {
  $("#workbench-page").insertAdjacentHTML(
    "beforeend",
    `<details class="panel"><summary>对话文本捕获 · 只存草稿</summary><p>例如“咖啡 32 元 微信”。原文只作数据，多个金额或时间不明确时需核对。</p><form id="agent-capture"><label>原文<textarea id="agent-capture-text" maxlength="4000" required></textarea></label><label>发生时间<input type="datetime-local" id="agent-capture-time" required></label><button>解析预览</button></form><div id="agent-capture-result"></div></details>`,
  );
  $("#agent-capture").onsubmit = (event) => {
    event.preventDefault();
    busy($("#agent-capture button"), async () => {
      const text = $("#agent-capture-text").value;
      const occurred_at = new Date(
        $("#agent-capture-time").value,
      ).toISOString();
      const result = await tool("capture", "ledger_parse_capture", {
        text,
        occurred_at,
      });
      $("#agent-capture-result").innerHTML =
        `<pre class="agent-snapshot">${esc(JSON.stringify(result, null, 2))}</pre>${result.ready_for_draft ? '<button id="agent-save-capture" type="button">核对无误，保存草稿</button>' : "<p>信息存在缺失或歧义，请修正原文后重新解析，或使用普通录入。</p>"}`;
      if (!result.ready_for_draft) return;
      const idempotency_key = key();
      $("#agent-save-capture").onclick = () =>
        busy($("#agent-save-capture"), async () => {
          const saved = await tool("capture", "ledger_create_draft", {
            ...result.candidate,
            idempotency_key,
            source_text: text,
          });
          $("#agent-capture-result").textContent =
            `已保存草稿，尚未入账：${saved.record_ref}`;
          changed();
        }).catch(showError);
    }).catch(showError);
  };
}

export function initAgent() {
  if (page === "insights") initResearch();
  if (page !== "workbench") return;
  $("#workbench-page").insertAdjacentHTML(
    "afterbegin",
    '<details class="panel" open><summary>Agent 审核与执行凭证</summary><label><input id="agent-review-history" type="checkbox">包含历史、过期和已执行</label><div id="agent-review-list" class="list"></div><button id="agent-review-more" type="button" hidden>更多审核</button></details>',
  );
  $("#agent-review-list").onclick = (event) => {
    const button = event.target.closest("[data-agent-review]");
    if (button) showAgentReview(button.dataset.agentReview).catch(showError);
  };
  $("#agent-review-history").onchange = () => loadReviews().catch(showError);
  $("#agent-review-more").onclick = () => loadReviews(true).catch(showError);
  document.addEventListener("ledger:changed", () =>
    loadReviews().catch(showError),
  );
  loadReviews().catch(showError);
  const intent = new URLSearchParams(location.search).get("agent_intent");
  if (intent && /^[a-f0-9-]{36}$/i.test(intent))
    showAgentReview(intent).catch(showError);
  initCapture();
}
