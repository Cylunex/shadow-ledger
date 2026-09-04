import {
  $,
  $$,
  api,
  page,
  localPath,
  escapeHtml as esc,
  money,
  dateValue,
  openDialog,
  showError,
  busy,
  changed,
} from "./core.js";
import { showRecord, showEditRecord } from "./records.js";
import { showIdentityEditor, showIdentitySuggestion } from "./consumption.js";

const kindNames = {
  draft: "待确认",
  review: "导入复核",
  observation: "来源变化",
  failed: "采集失败",
  identity: "身份建议",
};
const reasonNames = {
  duplicate: "重复来源",
  refund_match_suggested: "退款关联建议",
  refund_match_missing: "未关联退款",
  amount_anomaly: "金额异常需核对",
  merchant_confirmation_needed: "商家可保持未知",
};
const matchNames = {
  same_platform_order: "同平台订单标识",
  same_canonical_merchant: "同规范商家",
  same_raw_merchant: "同原始商家",
  same_title: "同标题",
  same_amount: "金额相同",
  partial_refund_possible: "可能是部分退款",
};
let cursor = "";
let generation = 0;

export function showImportReviews() {
  location.href = localPath("/workbench");
}

export async function loadWorkbench(append = false) {
  const token = ++generation;
  const params = new URLSearchParams({ limit: "30" });
  if ($("#work-kind").value) params.set("kind", $("#work-kind").value);
  if ($("#work-query").value.trim())
    params.set("query", $("#work-query").value.trim());
  if (append && cursor) params.set("cursor", cursor);
  const data = await api(`/workbench?${params}`);
  if (token !== generation) return;
  $("#work-counts").textContent =
    `共 ${data.total} 项 · ` +
    Object.entries(kindNames)
      .map(([kind, label]) => `${label} ${data.counts[kind] || 0}`)
      .join(" · ");
  const html = data.items
    .map(
      (
        item,
      ) => `<button type="button" class="list-item work-item" data-kind="${item.kind}" data-id="${item.id}" data-target="${item.target_id}">
    <span><span class="badge">${kindNames[item.kind]}</span><strong>${esc(item.title)}</strong><br><span class="meta">${dateValue(item.created_at).toLocaleString("zh-CN")}</span></span><span>处理 →</span></button>`,
    )
    .join("");
  if (append) $("#work-list").insertAdjacentHTML("beforeend", html);
  else
    $("#work-list").innerHTML =
      html || '<p class="muted">没有符合条件的待处理项。</p>';
  cursor = data.next_cursor || "";
  $("#work-more").hidden = !cursor;
}

export function initWorkbench() {
  $("#work-filter").addEventListener("submit", (event) => {
    event.preventDefault();
    loadWorkbench().catch(showError);
  });
  $("#work-more").addEventListener("click", () =>
    loadWorkbench(true).catch(showError),
  );
  $("#work-list").addEventListener("click", async (event) => {
    const button = event.target.closest(".work-item");
    if (!button) return;
    const { kind, id, target } = button.dataset;
    try {
      if (kind === "draft") await showRecord(target);
      else if (kind === "review") await showReview(id);
      else if (kind === "observation") await showObservation(id);
      else if (kind === "identity") await showIdentitySuggestion(id);
      else await showFailure(id);
    } catch (error) {
      showError(error);
    }
  });
  document.addEventListener("ledger:changed", () =>
    loadWorkbench().catch(showError),
  );
  loadWorkbench().catch(showError);
}

export async function showReview(id) {
  const item = await api(`/import-reviews/${id}`);
  const record = item.record;
  const [merchants, evidence] = await Promise.all([
    api("/merchants"),
    api(`/records/${record.id}/evidence`),
  ]);
  const duplicate = item.review_reasons.includes("duplicate");
  const refund = item.review_reasons.some((reason) =>
    reason.startsWith("refund_match"),
  );
  const merchant = item.review_reasons.includes("merchant_confirmation_needed");
  const anomaly = item.review_reasons.includes("amount_anomaly");
  openDialog(`<h2>复核并确认录入</h2><p>${item.review_reasons.map((reason) => `<span class="badge">${esc(reasonNames[reason] || reason)}</span>`).join("")}</p>
    <div class="review-columns"><section><h3>当前记录</h3><p>${esc(record.money_entry?.title || record.consumption?.merchant_name_raw || "消费记录")} · ${record.money_entry ? money(record.money_entry.amount, record.money_entry.currency) : "金额未知"}</p>
    <p class="meta">${dateValue(record.occurred_at).toLocaleString("zh-CN")} · ${esc(record.state)} · 版本 ${record.revision}</p>
    <form id="review-form">
    ${
      merchant && !duplicate
        ? `<label>规范商家（可选）<select id="review-merchant"><option value="">保持未知</option>${merchants.items
            .filter((row) => row.active)
            .map(
              (row) =>
                `<option value="${row.id}">${esc(row.canonical_name)}</option>`,
            )
            .join("")}</select></label>
      <label>或新建商家<input id="review-new-merchant" maxlength="500" placeholder="留空则不新建"></label><label class="check-label"><input type="checkbox" id="review-learn"> 为此原始商家建立可撤销精确规则</label>`
        : ""
    }
    ${refund && !duplicate ? '<div id="refund-picker"></div>' : ""}
    ${anomaly && !duplicate ? '<label class="check-label"><input type="checkbox" id="review-anomaly"> 我已对照证据核对异常金额</label>' : ""}
    ${duplicate ? '<p>此来源已导入，不会新建金额事实；关闭此复核不会删除原记录。</p><button class="primary" type="submit" value="dismiss">关闭重复项</button>' : `<div class="record-actions"><button type="submit" value="save" class="quiet">保存复核，暂不录入</button>${record.state === "draft" ? '<button type="submit" value="confirm" class="primary">复核并确认录入</button>' : ""}</div>`}
    <p id="review-status" role="status"></p></form><button id="review-record" class="quiet">查看／编辑记录</button></section>
    <section><h3>来源原文（只读）</h3><p>${esc(item.raw_merchant_name || "未提供商家")}</p><ul>${(item.raw_item_names || []).map((name) => `<li>${esc(name)}</li>`).join("")}</ul>
      ${evidence.sources.map((source) => `<details><summary>${esc(source.parser || source.type)}</summary><pre>${esc(source.raw_text || JSON.stringify(source.raw_payload, null, 2))}</pre></details>`).join("")}</section></div>`);
  $("#review-record").addEventListener("click", () => showRecord(record.id));
  if (refund && !duplicate) await refundPicker(record.id);
  $("#review-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const button = event.submitter;
    busy(
      button,
      async () => {
        let merchantId = $("#review-merchant")?.value || null;
        const newName = $("#review-new-merchant")?.value.trim();
        if (!merchantId && newName)
          merchantId = (
            await api("/merchants", {
              method: "POST",
              body: JSON.stringify({ canonical_name: newName }),
            })
          ).id;
        const data = {
          revision: item.revision,
          record_revision: record.revision,
          dismiss: button.value === "dismiss",
          confirm: button.value === "confirm",
          merchant_id: merchantId,
          learn_merchant_rule: Boolean(
            merchantId && $("#review-learn")?.checked,
          ),
          keep_merchant_unknown: merchant && !duplicate && !merchantId,
          refund_record_id: $("#refund-target")?.value || null,
          keep_refund_unlinked: Boolean($("#refund-unlinked")?.checked),
          accept_amount_anomaly: Boolean($("#review-anomaly")?.checked),
        };
        if (data.keep_refund_unlinked) data.refund_record_id = null;
        const result = await api(`/import-reviews/${id}/resolve`, {
          method: "POST",
          body: JSON.stringify(data),
        });
        changed();
        if (
          result.review_state === "pending" &&
          result.record_state !== "confirmed"
        )
          await showReview(id);
        else $("#dialog").close();
      },
      $("#review-status"),
      () => showReview(id),
    );
  });
}

async function refundPicker(recordId) {
  $("#refund-picker").innerHTML =
    '<h3>选择原支出</h3><label>搜索标题或商家<input id="refund-query" maxlength="200"></label><button id="refund-search" type="button" class="quiet">搜索</button><label>候选支出<select id="refund-target"><option value="">尚未选择</option></select></label><p id="refund-reasons" class="meta"></p><label class="check-label"><input type="checkbox" id="refund-unlinked"> 暂不关联原支出</label>';
  let candidates = [];
  const describe = () => {
    const item = candidates.find(
      (row) => row.record_id === $("#refund-target").value,
    );
    $("#refund-reasons").textContent = item
      ? `${item.reasons.map((reason) => matchNames[reason] || reason).join("、")}；已关联退款 ${item.already_linked_refunds}${item.exceeds_original ? "；加上本笔可能超过原支出，请核对" : ""}${item.strength === "weak" ? "。这是弱匹配，请勿仅凭金额确认" : ""}`
      : "可选择候选，也可明确保留未关联。不自动建立关系。";
  };
  const search = async () => {
    const data = await api(
      `/records/${recordId}/refund-candidates?query=${encodeURIComponent($("#refund-query").value)}`,
    );
    candidates = data.items;
    $("#refund-target").innerHTML =
      '<option value="">尚未选择</option>' +
      candidates
        .map(
          (item) =>
            `<option value="${item.record_id}">${esc(item.title || item.merchant || "支出")} · ${money(item.amount, item.currency)} · ${dateValue(item.occurred_at).toLocaleDateString("zh-CN")}</option>`,
        )
        .join("");
    describe();
  };
  $("#refund-search").addEventListener("click", () =>
    search().catch(showError),
  );
  $("#refund-target").addEventListener("change", () => {
    $("#refund-unlinked").checked = false;
    describe();
  });
  await search();
}

export async function enrichRecord(record) {
  const area = document.createElement("section");
  area.className = "record-enrichment";
  area.innerHTML =
    '<h3>消费记忆与证据</h3><div class="record-actions"><button class="quiet" data-evidence>来源／退款／修正历史</button><button class="quiet" data-link>关联其他来源</button></div>' +
    (record.consumption && record.state !== "voided"
      ? '<button class="quiet" data-identity>整理商家和商品身份</button>'
      : "");
  $("#dialog-body").append(area);
  area
    .querySelector("[data-evidence]")
    .addEventListener("click", () => showEvidence(record).catch(showError));
  area
    .querySelector("[data-link]")
    .addEventListener("click", () => showEvidenceLink(record).catch(showError));
  area
    .querySelector("[data-identity]")
    ?.addEventListener("click", () =>
      showIdentityEditor(record).catch(showError),
    );
}

async function showEvidence(record) {
  const data = await api(`/records/${record.id}/evidence`);
  openDialog(`<h2>来源与修正历史</h2><button id="evidence-back" class="quiet">返回记录</button><h3>原始来源</h3>
    ${data.sources.map((source) => `<details><summary>${esc(source.parser || source.type)}</summary><pre>${esc(source.raw_text || JSON.stringify(source.raw_payload, null, 2))}</pre><button class="quiet" data-observations="${source.id}">查看观察版本</button></details>`).join("") || "<p>没有采集来源</p>"}
    ${data.sources_truncated ? "<p>来源超过 100 项，此处仅展示前 100 项。</p>" : ""}<h3>关联退款</h3>${data.refunds.map((row) => `<button class="list-item" data-refund-record="${row.record_id}">${money(row.amount, row.currency)} · ${esc(row.state)}</button>`).join("") || "<p>没有关联退款</p>"}
    <h3>最近修正</h3>${data.audit.map((row) => `<details><summary>${esc(row.action)} · ${dateValue(row.at).toLocaleString("zh-CN")}</summary><pre>${esc(JSON.stringify(row.details, null, 2))}</pre></details>`).join("") || "<p>没有修正历史</p>"}`);
  $("#evidence-back").onclick = () => showRecord(record.id);
  $$("[data-refund-record]").forEach(
    (button) =>
      (button.onclick = () => showRecord(button.dataset.refundRecord)),
  );
  $$("[data-observations]").forEach(
    (button) =>
      (button.onclick = () =>
        showObservations(button.dataset.observations).catch(showError)),
  );
}

async function showObservations(sourceId, offset = 0) {
  const data = await api(
    `/capture-sources/${sourceId}/observations?offset=${offset}`,
  );
  openDialog(
    `<h2>来源观察版本</h2>${data.items.map((row) => `<button class="list-item observation-item" data-id="${row.id}"><span>${esc(row.external_revision)}<br>${esc(row.parser)} ${esc(row.parser_version)}</span><span>${esc(row.state)}</span></button>`).join("") || "<p>历史来源尚无观察版本；原文仍保留在来源详情。</p>"}${data.next_offset !== null ? '<button id="observations-more" class="quiet">下一页</button>' : ""}`,
  );
  $$(".observation-item").forEach(
    (button) =>
      (button.onclick = () =>
        showObservation(button.dataset.id).catch(showError)),
  );
  $("#observations-more")?.addEventListener("click", () =>
    showObservations(sourceId, data.next_offset).catch(showError),
  );
}

async function showObservation(id) {
  const observation = await api(`/source-observations/${id}`);
  openDialog(`<h2>来源变化复核</h2><p>原始来源未覆盖；采用候选会修正同一记录，不改变其确认状态。</p>
    <div class="review-columns"><section><h3>现有记录</h3><select id="observation-record">${observation.records.map((row) => `<option value="${row.id}">${esc(row.money_entry?.title || row.note || "消费记录")} · ${row.money_entry ? money(row.money_entry.amount, row.money_entry.currency) : "金额未知"} · ${esc(row.state)}</option>`).join("")}</select><pre id="observation-old"></pre></section>
    <section><h3>新候选</h3><pre>${esc(JSON.stringify(observation.candidate, null, 2))}</pre></section></div>
    <details><summary>查看新来源原文与字段定位</summary><pre>${esc(JSON.stringify(observation.payload, null, 2))}</pre><pre>${esc(JSON.stringify(observation.field_evidence, null, 2))}</pre></details>
    ${observation.state === "pending" ? '<label>处理原因<input id="observation-reason" maxlength="500" placeholder="说明为何保留原事实或采用新候选"></label><div class="record-actions"><button class="quiet" data-observation-action="keep">保留原事实</button><button class="primary" data-observation-action="apply">采用候选修正</button></div>' : `<p>已处理：${esc(observation.state)}</p>`}<p id="observation-status" role="status"></p>`);
  const old = () => {
    $("#observation-old").textContent = JSON.stringify(
      observation.records.find(
        (row) => row.id === $("#observation-record").value,
      ),
      null,
      2,
    );
  };
  old();
  $("#observation-record").onchange = old;
  $$("[data-observation-action]").forEach(
    (button) =>
      (button.onclick = () =>
        busy(
          button,
          async () => {
            const record = observation.records.find(
              (row) => row.id === $("#observation-record").value,
            );
            if (!$("#observation-reason").value.trim())
              throw new Error("请填写处理原因");
            if (
              button.dataset.observationAction === "apply" &&
              !confirm("采用候选修正所选记录？此操作会记录修正原因。")
            )
              return;
            await api(`/source-observations/${id}/decide`, {
              method: "POST",
              body: JSON.stringify({
                revision: observation.revision,
                action: button.dataset.observationAction,
                record_id: record?.id || null,
                record_revision: record?.revision || null,
                reason: $("#observation-reason").value.trim(),
              }),
            });
            changed();
            $("#dialog").close();
          },
          $("#observation-status"),
        )),
  );
}

async function showFailure(id) {
  const source = await api(`/capture-sources/${id}`);
  openDialog(
    `<h2>采集失败</h2><p>${esc(source.error_code || "解析失败")} · ${esc(source.parser || "")}</p><p>来源和附件保留，重试不会自动确认消费。</p><button id="capture-retry" class="primary">重试解析</button><p id="retry-status" role="status"></p>`,
  );
  $("#capture-retry").onclick = (event) =>
    busy(
      event.target,
      async () => {
        await api(`/capture-sources/${id}/retry`, { method: "POST" });
        $("#dialog").close();
        changed();
      },
      $("#retry-status"),
    );
}

async function showEvidenceLink(record) {
  if (!record.sources.length) throw new Error("此记录没有可关联的采集来源");
  openDialog(`<h2>把本条来源关联到已有消费</h2><p>请确认两者是同一次消费。不会更改目标金额。</p><label>查找已有消费<input id="link-query" maxlength="200"></label><button id="link-search" class="quiet">搜索已确认记录</button><label>目标消费<select id="link-target"></select></label><label>来源<select id="link-source">${record.sources.map((source) => `<option value="${source.source_id}">${esc(source.role)} · ${source.source_id.slice(0, 8)}</option>`).join("")}</select></label>
    ${record.state === "draft" ? '<label class="check-label"><input type="checkbox" id="link-discard"> 同时删除本条重复草稿（保留来源证据；有其他引用时拒绝删除）</label>' : ""}
    <label>关联原因<input id="link-reason" maxlength="500"></label><button id="link-submit" class="primary">确认关联证据</button><p id="link-status" role="status"></p>`);
  let targets = [];
  const search = async () => {
    const data = await api(
      `/records?state=confirmed&limit=30&query=${encodeURIComponent($("#link-query").value)}`,
    );
    targets = data.items.filter((row) => row.id !== record.id);
    $("#link-target").innerHTML =
      '<option value="">请选择</option>' +
      targets
        .map(
          (row) =>
            `<option value="${row.id}">${esc(row.money_entry?.title || row.note || "消费")} · ${row.money_entry ? money(row.money_entry.amount, row.money_entry.currency) : "金额未知"} · ${dateValue(row.occurred_at).toLocaleDateString("zh-CN")}</option>`,
        )
        .join("");
  };
  $("#link-search").onclick = () => search().catch(showError);
  await search();
  $("#link-submit").onclick = (event) =>
    busy(
      event.target,
      async () => {
        const target = targets.find(
          (row) => row.id === $("#link-target").value,
        );
        if (!target) throw new Error("请选择目标记录");
        if (!$("#link-reason").value.trim()) throw new Error("请填写关联原因");
        const discard = Boolean($("#link-discard")?.checked);
        if (
          discard &&
          !confirm(
            "删除本条重复草稿并将来源并入目标记录？草稿删除不可恢复，来源证据保留。",
          )
        )
          return;
        await api(`/records/${target.id}/link-evidence`, {
          method: "POST",
          body: JSON.stringify({
            source_id: $("#link-source").value,
            record_revision: target.revision,
            reason: $("#link-reason").value.trim(),
            redundant_draft_id: discard ? record.id : null,
            redundant_revision: discard ? record.revision : null,
          }),
        });
        changed();
        await showRecord(target.id);
      },
      $("#link-status"),
    );
}
