import {
  $,
  $$,
  api,
  page,
  key,
  localPath,
  platformNames,
  typeNames,
  sceneNames,
  money,
  dateValue,
  paymentLabels,
  paymentOptions,
  escapeHtml,
  localDateTime,
  loadPaymentMethods,
  showError,
  sumAmounts,
  changed,
  openDialog,
  recoverConflict,
} from "./core.js";
import { showImportReviews, enrichRecord } from "./workbench.js";
import { queueDraft } from "./offline.js";
export function recordCard(r) {
  const title =
    r.money_entry?.title ||
    r.consumption?.merchant_name_raw ||
    r.note ||
    "未命名记录";
  const amount = r.money_entry
    ? money(r.money_entry.amount, r.money_entry.currency)
    : "金额未知";
  const stateName =
    r.state === "draft" ? "草稿" : r.state === "voided" ? "已撤销" : null;
  const tags = [
    stateName,
    paymentLabels[r.money_entry?.payment_method],
    r.consumption?.scene
      ? sceneNames[r.consumption.scene] || r.consumption.scene
      : null,
    r.money_entry?.category_key,
  ].filter(Boolean);
  const action =
    r.state === "draft"
      ? "点开：编辑 · 确认 · 删除"
      : r.state === "confirmed"
        ? "点开：查看 · 撤销"
        : "点开查看详情";
  return `<article class="record" data-id="${r.id}" role="button" tabindex="0" aria-label="查看记录详情"><div><h3>${escapeHtml(title)}</h3><div>${tags.map((t) => `<span class="badge">${escapeHtml(t)}</span>`).join("")}</div><span class="meta">${dateValue(r.occurred_at).toLocaleString("zh-CN")}</span></div><div class="record-side"><span class="amount">${amount}</span><span class="meta record-action">${action}</span></div></article>`;
}
let timelineState = "confirmed";
let timelineCursor = "";
let timelineGeneration = 0;
let activeFilters = {};
const loadedRecords = new Map();
const selectedRecords = new Map();

function persistFilters() {
  const params = timelineParams();
  params.delete("limit");
  history.replaceState(null, "", `${localPath("/")}?${params}`);
}
function bindSelection() {
  $$("#timeline .record").forEach((card) => {
    const record = loadedRecords.get(card.dataset.id);
    if (record?.state !== "draft" || card.querySelector("[data-select-record]"))
      return;
    const label = document.createElement("label");
    label.className = "record-selector";
    label.innerHTML = `<input type="checkbox" data-select-record="${record.id}"> 选择`;
    label.addEventListener("click", (event) => event.stopPropagation());
    label.addEventListener("keydown", (event) => event.stopPropagation());
    label.querySelector("input").checked = selectedRecords.has(record.id);
    label.querySelector("input").addEventListener("change", (event) => {
      if (event.target.checked) selectedRecords.set(record.id, record);
      else selectedRecords.delete(record.id);
      $("#batch-confirm").textContent = `确认选中（${selectedRecords.size}）`;
    });
    card.append(label);
  });
  $("#batch-confirm").textContent = `确认选中（${selectedRecords.size}）`;
  $("#select-visible").hidden = timelineState !== "draft";
}
async function showBatchConfirm() {
  const rows = [...selectedRecords.values()];
  if (!rows.length) {
    showError(new Error("请先勾选要确认的草稿，或选择当前已加载项"));
    return;
  }
  const totals = {};
  rows.forEach((row) => {
    const entry = row.money_entry;
    if (!entry) return;
    const group = `${entry.currency}:${entry.type}`;
    (totals[group] ||= []).push(entry.amount);
  });
  openDialog(`<h2>确认选中的 ${rows.length} 条草稿</h2><p>仅确认已勾选项，不包括未加载的分页。</p>
    <ul>${Object.entries(totals)
      .map(([group, values]) => {
        const [currency, type] = group.split(":");
        return `<li>${typeNames[type]}：${money(sumAmounts(values), currency)}</li>`;
      })
      .join("")}</ul>
    <p>金额未知：${rows.filter((row) => !row.money_entry).length} 条。任一版本冲突或阻塞复核未处理时，整批不会录入。</p>
    <button id="batch-submit" type="button" class="primary">确认录入这 ${rows.length} 条</button><p id="batch-status" role="status"></p>`);
  $("#batch-submit").addEventListener("click", async (event) => {
    event.target.disabled = true;
    try {
      await api("/records/batch-confirm", {
        method: "POST",
        body: JSON.stringify({
          records: rows.map((row) => ({ id: row.id, revision: row.revision })),
        }),
      });
      $("#dialog").close();
      changed();
      await Promise.all([loadTimeline("draft"), loadSummary()]);
    } catch (error) {
      $("#batch-status").textContent = error.message;
      event.target.disabled = false;
    }
  });
}

export function initRecords() {
  let type = "expense";
  $$(".type-switch button").forEach((button) =>
    button.addEventListener("click", () => {
      $$(".type-switch button").forEach((other) =>
        other.classList.remove("active"),
      );
      button.classList.add("active");
      type = button.dataset.type;
    }),
  );
  $("#occurred-at").value = localDateTime(new Date().toISOString());
  const params = new URLSearchParams(location.search);
  if (["draft", "confirmed", "voided"].includes(params.get("state")))
    timelineState = params.get("state");
  const filterIds = {
    query: "query",
    money_type: "type",
    payment_method: "payment",
    category_key: "category",
    scene: "scene",
    amount_min: "min",
    amount_max: "max",
  };
  Object.entries(filterIds).forEach(([name, id]) => {
    if (params.has(name)) {
      activeFilters[name] = params.get(name);
      $("#filter-" + id).value = params.get(name);
    }
  });
  for (const [name, id] of [
    ["occurred_from", "from"],
    ["occurred_to", "to"],
  ]) {
    if (params.has(name) && !Number.isNaN(Date.parse(params.get(name)))) {
      activeFilters[name] = params.get(name);
      const date = dateValue(params.get(name));
      if (id === "to") date.setDate(date.getDate() - 1);
      $("#filter-" + id).value = localDateTime(date.toISOString()).slice(0, 10);
    }
  }
  $("#record-filters").open = Object.keys(activeFilters).length > 0;
  $("#quick-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.submitter;
    const status = $("#form-status");
    button.disabled = true;
    try {
      const body = {
        occurred_at: new Date($("#occurred-at").value).toISOString(),
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        note: "",
        money_entry: {
          type,
          amount: $("#amount").value,
          currency: "CNY",
          category_key: $("#category").value || null,
          title: $("#description").value,
          payment_method: $("#payment-method").value || null,
        },
        consumption: null,
        confirm: button.dataset.mode !== "draft",
      };
      if (button.dataset.mode === "offline") {
        queueDraft(body);
        $("#amount").value = "";
        $("#description").value = "";
        status.textContent = "已暂存本机，尚未录入统计";
        return;
      }
      await api("/records", { method: "POST", body: JSON.stringify(body) });
      status.textContent = body.confirm ? "已确认录入" : "已保存草稿";
      $("#amount").value = "";
      $("#description").value = "";
      await Promise.all([
        loadTimeline(body.confirm ? "confirmed" : "draft"),
        loadSummary(),
      ]);
      changed();
    } catch (error) {
      status.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
  $("#record-filter-form").addEventListener("submit", (event) => {
    event.preventDefault();
    activeFilters = readFilters();
    loadTimeline($("#filter-state").value).catch(showError);
  });
  $("#filter-reset").addEventListener("click", () => {
    $("#record-filter-form").reset();
    activeFilters = {};
    loadTimeline("confirmed").catch(showError);
  });
  $("#refresh").addEventListener("click", () =>
    loadTimeline().catch(showError),
  );
  $("#load-more").addEventListener("click", () =>
    loadTimeline(timelineState, { append: true, cursor: timelineCursor }).catch(
      showError,
    ),
  );
  $("#draft-button").addEventListener("click", () =>
    loadTimeline(timelineState === "draft" ? "confirmed" : "draft").catch(
      showError,
    ),
  );
  $("#timeline").addEventListener("click", (event) => {
    const card = event.target.closest(".record");
    if (card) showRecord(card.dataset.id);
  });
  $("#timeline").addEventListener("keydown", (event) => {
    if (["Enter", " "].includes(event.key)) {
      const card = event.target.closest(".record");
      if (card) {
        event.preventDefault();
        showRecord(card.dataset.id);
      }
    }
  });
  $("#select-visible").addEventListener("click", () => {
    $$("#timeline [data-select-record]").forEach((box) => {
      box.checked = true;
      box.dispatchEvent(new Event("change"));
    });
  });
  $("#batch-confirm").addEventListener("click", () =>
    showBatchConfirm().catch(showError),
  );
  $("#text-capture").addEventListener("click", showTextCapture);
  $("#asset-capture").addEventListener("click", showAssetCapture);
  $("#bill-import").addEventListener("click", showBillImport);
  $("#import-review").addEventListener("click", showImportReviews);
  $("#copy-last").addEventListener("click", async () => {
    try {
      const data = await api("/records?limit=1");
      const entry = data.items[0]?.money_entry;
      if (entry) {
        $("#amount").value = entry.amount;
        $("#description").value = entry.title;
        $("#payment-method").value = entry.payment_method || "";
      }
    } catch (error) {
      showError(error);
    }
  });
  const shared = new URLSearchParams(location.search);
  if (shared.get("text") || shared.get("title") || shared.get("url"))
    $("#description").value = [
      shared.get("title"),
      shared.get("text"),
      shared.get("url"),
    ]
      .filter(Boolean)
      .join(" ")
      .slice(0, 500);
  loadTimeline().catch(showError);
  loadSummary().catch(showError);
}
function dateBoundary(value, nextDay = false) {
  if (!value) return "";
  const date = new Date(`${value}T00:00:00`);
  if (nextDay) date.setDate(date.getDate() + 1);
  return date.toISOString();
}
function readFilters() {
  const filters = {};
  const values = {
    query: $("#filter-query").value.trim(),
    money_type: $("#filter-type").value,
    payment_method: $("#filter-payment").value,
    category_key: $("#filter-category").value,
    scene: $("#filter-scene").value,
    occurred_from: dateBoundary($("#filter-from").value),
    occurred_to: dateBoundary($("#filter-to").value, true),
    amount_min: $("#filter-min").value,
    amount_max: $("#filter-max").value,
  };
  Object.entries(values).forEach(([name, value]) => {
    if (value !== "" && value !== null) filters[name] = value;
  });
  return filters;
}
function timelineParams(state = timelineState, cursor = "", limit = 30) {
  const params = new URLSearchParams({ state, limit: String(limit) });
  Object.entries(activeFilters).forEach(([name, value]) =>
    params.set(name, value),
  );
  if (cursor) params.set("cursor", cursor);
  return params;
}
function updateFilterUi() {
  const count =
    Object.keys(activeFilters).length + (timelineState === "confirmed" ? 0 : 1);
  $("#filter-count").hidden = count === 0;
  $("#filter-count").textContent = count ? `${count} 项已启用` : "";
  $("#filter-state").value = timelineState;
  $("#draft-button").textContent =
    timelineState === "draft" ? "返回已入账" : "草稿箱";
}
export async function loadTimeline(
  state = timelineState,
  { append = false, cursor = "" } = {},
) {
  const generation = ++timelineGeneration;
  timelineState = state;
  updateFilterUi();
  persistFilters();
  const button = $("#load-more");
  button.disabled = true;
  button.textContent = "正在加载…";
  try {
    const data = await api(`/records?${timelineParams(state, cursor)}`);
    if (generation !== timelineGeneration) return;
    if (!append) {
      loadedRecords.clear();
      selectedRecords.clear();
    }
    data.items.forEach((r) => loadedRecords.set(r.id, r));
    const titles = {
      confirmed: "最近记录",
      draft: "待确认草稿",
      voided: "已撤销记录",
    };
    $("#timeline-title").textContent = titles[state] || "记录";
    if (append) {
      $("#timeline").insertAdjacentHTML(
        "beforeend",
        data.items.map(recordCard).join(""),
      );
    } else {
      $("#timeline").innerHTML = data.items.length
        ? data.items.map(recordCard).join("")
        : `<p class="muted">${state === "draft" ? "没有符合条件的草稿。" : state === "voided" ? "没有符合条件的已撤销记录。" : "没有符合条件的记录。"}</p>`;
    }
    timelineCursor = data.next_cursor || "";
    button.hidden = !timelineCursor;
    $("#batch-confirm").hidden = state !== "draft" || !$("#timeline .record");
  } finally {
    if (generation === timelineGeneration) {
      button.disabled = false;
      button.textContent = "加载更多";
      bindSelection();
    }
  }
}
export async function loadSummary() {
  const d = await api("/insights/summary");
  $("#expense").textContent = money(d.expense, d.currency);
  $("#income").textContent = money(d.income, d.currency);
  $("#refund").textContent = money(d.refund, d.currency);
  $("#net").textContent = money(d.net_spending, d.currency);
}
export async function categories() {
  const d = await api("/categories");
  const options = d.items
    .filter((x) => x.active)
    .map((x) => `<option value="${x.key}">${escapeHtml(x.name)}</option>`)
    .join("");
  $("#category").insertAdjacentHTML("beforeend", options);
  $("#filter-category").insertAdjacentHTML("beforeend", options);
}
export function consumptionInput(value) {
  if (!value) return null;
  return {
    scene: value.scene,
    merchant_id: value.merchant_id,
    merchant_name_raw: value.merchant_name_raw,
    channel_key: value.channel_key,
    channel_name_raw: value.channel_name_raw,
    place_ref: value.place_ref,
    rating: value.rating,
    would_repeat: value.would_repeat,
    note: value.note || "",
    lines: (value.lines || []).map((line) => ({
      item_identity_id: line.item_identity_id,
      raw_name: line.raw_name,
      quantity: line.quantity,
      unit: line.unit,
      amount: line.amount,
      content_category: line.content_category,
      note: line.note || "",
      sort_order: line.sort_order,
    })),
  };
}
export function showEditRecord(r) {
  const dialog = $("#dialog");
  const entry = r.money_entry;
  const consumption = r.consumption;
  const categoryOptions = $("#category").innerHTML;
  const sceneOptions = $("#filter-scene").innerHTML.replace(
    '<option value="">全部场景</option>',
    "",
  );
  $("#dialog-body").innerHTML =
    `<h2>${r.state === "confirmed" ? "修正记录" : "编辑草稿"}</h2><form id="record-edit-form"><label>日期时间<input id="edit-occurred-at" type="datetime-local" required value="${localDateTime(r.occurred_at)}"></label><label>记录备注<textarea id="edit-note" maxlength="2000" rows="3"></textarea></label>${entry ? `<div class="details-grid"><label>收支类型<select id="edit-type"><option value="expense">支出</option><option value="income">收入</option><option value="refund">退款</option></select></label><label>金额<input id="edit-amount" type="number" min="0.0001" step="0.0001" inputmode="decimal" required value="${entry.amount}"></label><label>标题<input id="edit-title" maxlength="500"></label><label>分类<select id="edit-category">${categoryOptions}</select></label><label>支付方式<select id="edit-payment">${paymentOptions(entry.payment_method)}</select></label></div>` : ""}${consumption ? `<label>消费场景<select id="edit-scene">${sceneOptions}</select></label>` : ""}<div class="record-actions"><button class="primary" type="submit">保存修改</button><button id="edit-cancel" class="quiet" type="button">取消</button></div><p id="edit-status" role="status"></p></form>`;
  $("#edit-note").value = r.note || "";
  if (entry) {
    $("#edit-type").value = entry.type;
    $("#edit-title").value = entry.title || "";
    $("#edit-category").value = entry.category_key || "";
  }
  if (consumption) $("#edit-scene").value = consumption.scene;
  $("#edit-cancel").addEventListener("click", () => showRecord(r.id));
  $("#record-edit-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit =
      event.submitter || $('#record-edit-form button[type="submit"]');
    submit.disabled = true;
    submit.textContent = "正在保存…";
    const body = {
      occurred_at: new Date($("#edit-occurred-at").value).toISOString(),
      timezone: r.timezone,
      note: $("#edit-note").value,
    };
    if (entry)
      body.money_entry = {
        type: $("#edit-type").value,
        amount: $("#edit-amount").value,
        currency: entry.currency,
        category_key: $("#edit-category").value || null,
        title: $("#edit-title").value,
        related_entry_id: entry.related_entry_id,
        payment_method: $("#edit-payment").value || null,
      };
    if (r.state === "confirmed") body.correction_reason = "用户修正记录";
    if (consumption) {
      body.consumption = consumptionInput(consumption);
      body.consumption.scene = $("#edit-scene").value;
    }
    try {
      await api(`/records/${r.id}`, {
        method: "PATCH",
        headers: { "If-Match": `"${r.revision}"` },
        body: JSON.stringify(body),
      });
      dialog.close();
      changed();
      if (page === "records")
        await Promise.all([loadTimeline(timelineState), loadSummary()]);
    } catch (e) {
      submit.disabled = false;
      submit.textContent = "保存修改";
      $("#edit-status").textContent = e.message;
      recoverConflict($("#edit-status"), e, async () =>
        showEditRecord(await api(`/records/${r.id}`)),
      );
    }
  });
}
export async function showRecord(id) {
  const dialog = $("#dialog");
  try {
    const r = await api(`/records/${id}`);
    const title =
      r.money_entry?.title ||
      r.consumption?.merchant_name_raw ||
      r.note ||
      "未命名记录";
    const entry = r.money_entry;
    const consumption = r.consumption;
    const lines = consumption?.lines?.length
      ? `<h3>消费明细</h3><ul>${consumption.lines.map((line) => `<li>${escapeHtml(line.raw_name)}${line.quantity ? ` × ${escapeHtml(line.quantity)}${escapeHtml(line.unit || "")}` : ""}${line.amount ? ` · ${money(line.amount, entry?.currency || "CNY")}` : ""}${line.note ? `<br><span class="meta">${escapeHtml(line.note)}</span>` : ""}</li>`).join("")}</ul>`
      : "";
    const actions =
      r.state === "draft"
        ? '<div class="record-actions"><button id="record-confirm" type="button" class="primary">确认入账</button><button id="record-edit" type="button" class="quiet">编辑草稿</button><button id="record-delete" type="button" class="quiet danger">删除草稿</button></div>'
        : r.state === "confirmed"
          ? '<div class="record-actions"><button id="record-edit" type="button" class="quiet">修正记录</button><button id="record-void" type="button" class="quiet danger">撤销记录</button></div>'
          : "";
    $("#dialog-body").innerHTML =
      `<h2>${escapeHtml(title)}</h2><p><span class="badge">${r.state === "draft" ? "草稿" : r.state === "confirmed" ? "已入账" : "已撤销"}</span></p><p><strong>${entry ? money(entry.amount, entry.currency) : "金额未知"}</strong> · ${escapeHtml(entry ? typeNames[entry.type] || entry.type : "仅消费记录")}</p><p class="meta">${dateValue(r.occurred_at).toLocaleString("zh-CN")} · revision ${r.revision}</p>${entry ? `<p>支付方式：${escapeHtml(paymentLabels[entry.payment_method] || "未提供")}</p>` : ""}${r.note ? `<p>${escapeHtml(r.note)}</p>` : ""}${consumption ? `<p>场景：${escapeHtml(sceneNames[consumption.scene] || consumption.scene)}${consumption.merchant_name_raw ? `<br>商家：${escapeHtml(consumption.merchant_name_raw)}` : ""}${consumption.channel_name_raw ? `<br>渠道：${escapeHtml(consumption.channel_name_raw)}` : ""}</p>` : ""}${consumption?.note ? `<p>${escapeHtml(consumption.note)}</p>` : ""}${lines}${actions}<p id="record-status" role="status"></p>`;
    if (!dialog.open) dialog.showModal();
    enrichRecord(r).catch(showError);
    if (r.state === "draft") {
      $("#record-edit").addEventListener("click", () => showEditRecord(r));
      $("#record-confirm").addEventListener("click", async () => {
        const button = $("#record-confirm");
        button.disabled = true;
        button.textContent = "正在确认…";
        try {
          await api(`/records/${r.id}/confirm`, {
            method: "POST",
            headers: { "If-Match": `"${r.revision}"` },
          });
          dialog.close();
          changed();
          if (page === "records")
            await Promise.all([loadTimeline("draft"), loadSummary()]);
        } catch (e) {
          button.disabled = false;
          button.textContent = "确认入账";
          $("#record-status").textContent = e.message;
        }
      });
      $("#record-delete").addEventListener("click", async () => {
        if (!window.confirm("确定删除这条草稿吗？删除后无法恢复。")) return;
        try {
          await api(`/records/${r.id}`, {
            method: "DELETE",
            headers: { "If-Match": `"${r.revision}"` },
          });
          dialog.close();
          changed();
          if (page === "records") await loadTimeline("draft");
        } catch (e) {
          $("#record-status").textContent = e.message;
        }
      });
    }
    if (r.state === "confirmed") {
      $("#record-edit").addEventListener("click", () => showEditRecord(r));
      $("#record-void").addEventListener("click", async () => {
        if (
          !window.confirm(
            "已入账记录不能直接删除。确定撤销这条记录吗？撤销后将不再计入统计。",
          )
        )
          return;
        const button = $("#record-void");
        button.disabled = true;
        button.textContent = "正在撤销…";
        try {
          await api(`/records/${r.id}/void`, {
            method: "POST",
            headers: { "If-Match": `"${r.revision}"` },
          });
          dialog.close();
          changed();
          if (page === "records")
            await Promise.all([loadTimeline("confirmed"), loadSummary()]);
        } catch (e) {
          button.disabled = false;
          button.textContent = "撤销记录";
          $("#record-status").textContent = e.message;
        }
      });
    }
  } catch (e) {
    showError(e);
  }
}
function showTextCapture() {
  const dialog = $("#dialog");
  $("#dialog-body").innerHTML =
    '<h2>说一句</h2><label>描述<input id="capture-text" placeholder="昨天外卖 27.5，味道一般"></label><button id="capture-submit" type="button" class="primary">生成草稿</button><p id="capture-status" role="status"></p>';
  if (!dialog.open) dialog.showModal();
  $("#capture-submit").addEventListener("click", async () => {
    try {
      await api("/capture/text", {
        method: "POST",
        headers: { "Idempotency-Key": key() },
        body: JSON.stringify({ text: $("#capture-text").value }),
      });
      dialog.close();
      await loadTimeline("draft");
    } catch (e) {
      $("#capture-status").textContent = e.message;
    }
  });
}
function showAssetCapture() {
  const dialog = $("#dialog");
  $("#dialog-body").innerHTML =
    '<h2>扫截图</h2><label>小票或订单截图<input id="asset-file" type="file" accept="image/jpeg,image/png,image/webp,application/pdf"></label><button id="asset-submit" type="button" class="primary">上传并生成草稿</button><p id="asset-status" role="status">文件将直传 Shadow Asset，Ledger 不保存文件字节。</p>';
  if (!dialog.open) dialog.showModal();
  $("#asset-submit").addEventListener("click", async () => {
    const f = $("#asset-file").files[0];
    if (!f) return;
    const status = $("#asset-status");
    try {
      status.textContent = "正在准备安全上传…";
      const d = await api("/capture/assets/init", {
        method: "POST",
        headers: { "Idempotency-Key": key() },
        body: JSON.stringify({
          filename: f.name,
          mime_type: f.type,
          size: f.size,
        }),
      });
      const targets = [d.canonical_target, ...(d.alternate_targets || [])];
      let uploaded = false;
      for (const target of targets) {
        try {
          const r = await fetch(target.url, {
            method: target.method || "PUT",
            headers: target.headers || {},
            body: f,
          });
          if (r.ok) {
            uploaded = true;
            break;
          }
        } catch {}
      }
      if (!uploaded) throw new Error("文件上传失败");
      status.textContent = "正在登记并解析…";
      await api("/capture/assets/complete", {
        method: "POST",
        headers: { "Idempotency-Key": key() },
        body: JSON.stringify({ upload_id: d.upload_id, usage: "evidence" }),
      });
      dialog.close();
      await loadTimeline("draft");
    } catch (e) {
      status.textContent = e.message;
    }
  });
}
function showBillImport() {
  const dialog = $("#dialog");
  let content = "";
  let format = "markdown";
  $("#dialog-body").innerHTML =
    '<h2>导入消费账单</h2><p class="muted">支持现有四平台 Markdown，以及已识别表头的支付宝／微信 CSV、Markdown。仅生成草稿；非消费条目跳过。</p><label>文本账单<input id="bill-file" type="file" accept=".md,.csv,text/markdown,text/csv,text/plain"></label><button id="bill-preview" type="button" class="primary">预览</button><div id="bill-result" role="status"></div>';
  if (!dialog.open) dialog.showModal();
  $("#bill-preview").addEventListener("click", async () => {
    const file = $("#bill-file").files[0];
    const result = $("#bill-result");
    if (!file) {
      result.textContent = "请先选择 Markdown 或 CSV 文件";
      return;
    }
    if (file.size > 1000000) {
      result.textContent = "单个文件不能超过 1 MB";
      return;
    }
    try {
      result.textContent = "正在解析…";
      format = /\.csv$/i.test(file.name) ? "csv" : "markdown";
      const bytes = await file.arrayBuffer();
      try {
        content = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
      } catch {
        content = new TextDecoder("gb18030", { fatal: true }).decode(bytes);
      }
      const preview = await api("/imports/preview", {
        method: "POST",
        body: JSON.stringify({ format, content }),
      });
      const warnings = preview.warnings.length
        ? `<ul>${preview.warnings.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>`
        : "";
      result.innerHTML = `<p><strong>${escapeHtml(platformNames[preview.platform] || preview.platform)}</strong>：${preview.record_count} 条草稿，跳过 ${preview.skipped_count} 条</p>${warnings}<button id="bill-commit" type="button" class="primary">导入到草稿箱</button>`;
      $("#bill-commit").addEventListener("click", async () => {
        const button = $("#bill-commit");
        button.disabled = true;
        button.textContent = "正在导入…";
        try {
          const committed = await api("/imports/commit", {
            method: "POST",
            headers: { "Idempotency-Key": key() },
            body: JSON.stringify({ source: { format, content } }),
          });
          result.innerHTML = `<p>已新建 ${committed.created_count} 条草稿，保留 ${committed.duplicate_count} 条已有记录。来源内容若有变化，请到“待处理”复核，不会覆盖金额。</p>`;
          await loadTimeline("draft");
        } catch (e) {
          button.disabled = false;
          button.textContent = "重试导入";
          result.insertAdjacentHTML(
            "beforeend",
            `<p>${escapeHtml(e.message)}</p>`,
          );
        }
      });
    } catch (e) {
      result.textContent = e.message;
    }
  });
}
