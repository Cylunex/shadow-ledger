import {
  $,
  $$,
  api,
  escapeHtml as esc,
  money,
  dateValue,
  localDateTime,
  openDialog,
  showError,
  busy,
  changed,
  sceneNames,
} from "./core.js";
import { showRecord } from "./records.js";

export async function loadConsumption() {
  const [scenes, merchants, items] = await Promise.all([
    api("/insights/scenes"),
    api("/insights/merchants"),
    api("/insights/items"),
  ]);
  $("#scene-cards").innerHTML =
    scenes.items
      .map(
        (item) =>
          `<div><strong>${item.count}</strong><p>${esc(sceneNames[item.scene] || item.scene)}</p></div>`,
      )
      .join("") || "<p>暂无已确认消费</p>";
  $("#merchant-list").innerHTML = merchants.items
    .map(
      (item) =>
        `<div class="list-item"><span>${esc(item.merchant_name_raw || "规范商家")}</span><span>${item.count} 次 ${item.merchant_id ? `<button class="quiet" data-memory="merchant" data-id="${item.merchant_id}">回看</button>` : ""}</span></div>`,
    )
    .join("");
  $("#item-list").innerHTML = items.items
    .map(
      (item) =>
        `<div class="list-item"><span>${esc(item.raw_name)}</span><span>${item.count} 次 ${item.item_identity_id ? `<button class="quiet" data-memory="item" data-id="${item.item_identity_id}">回看／使用</button>` : ""}</span></div>`,
    )
    .join("");
  $$("[data-memory]").forEach(
    (button) =>
      (button.onclick = () =>
        showMemory(button.dataset.memory, button.dataset.id).catch(showError)),
  );
}

export async function showMemory(kind, id, offset = 0) {
  const data = await api(`/identities/${kind}/${id}/memory?offset=${offset}`);
  openDialog(`<h2>${esc(data.identity.name)}</h2><p>${esc([data.identity.brand, data.identity.variant].filter(Boolean).join(" · "))}</p><p>${data.count} 次已确认消费。${esc(data.price_notice)}</p>
    ${kind === "item" ? '<button id="memory-use" class="quiet">明确开始使用</button>' : ""}
    <div class="list">${data.items.map((row) => `<button class="list-item" data-memory-record="${row.id}"><span>${esc(row.money_entry?.title || row.consumption?.merchant_name_raw || "消费")}<br>${dateValue(row.occurred_at).toLocaleString("zh-CN")}</span><span>${row.money_entry ? money(row.money_entry.amount, row.money_entry.currency) : "金额未知"}</span></button>`).join("")}</div>
    ${data.next_offset !== null ? '<button id="memory-more" class="quiet">下一页</button>' : ""}<h3>显式使用记录</h3>${data.use_cycles.map((row) => `<p>${esc(row.state)} · ${dateValue(row.started_at).toLocaleDateString("zh-CN")} → ${row.ended_at ? dateValue(row.ended_at).toLocaleDateString("zh-CN") : "未结束"}</p>`).join("") || "<p>没有使用记录，不代表没有使用过。</p>"}`);
  $$("[data-memory-record]").forEach(
    (button) =>
      (button.onclick = () => showRecord(button.dataset.memoryRecord)),
  );
  $("#memory-more")?.addEventListener("click", () =>
    showMemory(kind, id, data.next_offset).catch(showError),
  );
  $("#memory-use")?.addEventListener("click", () =>
    showStartCycle(data.identity.id, data.identity.name),
  );
}

export function showStartCycle(itemId, name) {
  openDialog(
    `<h2>明确开始使用</h2><p>${esc(name)}。购买不会自动视为开始使用。</p><label>开始时间<input id="cycle-start" type="datetime-local" value="${localDateTime(new Date().toISOString())}"></label><label>预计结束（可选）<input id="cycle-end" type="datetime-local"></label><label>备注<textarea id="cycle-note" maxlength="2000"></textarea></label><button id="cycle-save" class="primary">保存使用事实</button><p id="cycle-status" role="status"></p>`,
  );
  $("#cycle-save").onclick = (event) =>
    busy(
      event.target,
      async () => {
        await api("/use-cycles", {
          method: "POST",
          body: JSON.stringify({
            item_identity_id: itemId,
            label: name,
            started_at: new Date($("#cycle-start").value).toISOString(),
            expected_end_at: $("#cycle-end").value
              ? new Date($("#cycle-end").value).toISOString()
              : null,
            note: $("#cycle-note").value,
          }),
        });
        changed();
        $("#dialog").close();
      },
      $("#cycle-status"),
    );
}

export async function showIdentityEditor(record) {
  const [merchants, items] = await Promise.all([
    api("/merchants"),
    api("/items"),
  ]);
  const event = record.consumption;
  const options = (rows, current) =>
    '<option value="">保持当前关联／未知</option>' +
    rows
      .filter((row) => row.active)
      .map(
        (row) =>
          `<option value="${row.id}"${row.id === current ? " selected" : ""}>${esc(row.canonical_name)}${row.variant ? ` · ${esc(row.variant)}` : ""}</option>`,
      )
      .join("");
  openDialog(`<h2>整理身份，不改原文</h2><p>原始商家：${esc(event.merchant_name_raw || "未知")}</p><label>规范商家<select id="identity-merchant">${options(merchants.items, event.merchant_id)}</select></label>
    <button id="identity-new-merchant" class="quiet">新建商家</button>
    ${event.lines.map((line) => `<label>${esc(line.raw_name)}<br><span>${esc(line.note || "")}</span><select data-line-identity="${line.id}">${options(items.items, line.item_identity_id)}</select></label>`).join("")}
    <button id="identity-new-item" class="quiet">新建商品（含规格）</button><button id="identity-save" class="primary">保存规范关联</button><p id="identity-status" role="status"></p>`);
  $("#identity-new-merchant").onclick = () =>
    showCreateIdentity("merchant", () => showIdentityEditor(record));
  $("#identity-new-item").onclick = () =>
    showCreateIdentity("item", () => showIdentityEditor(record));
  $("#identity-save").onclick = (event) =>
    busy(
      event.target,
      async () => {
        const lines = Object.fromEntries(
          $$("[data-line-identity]")
            .filter((select) => select.value)
            .map((select) => [select.dataset.lineIdentity, select.value]),
        );
        await api(`/records/${record.id}/identities`, {
          method: "POST",
          body: JSON.stringify({
            revision: record.revision,
            merchant_id: $("#identity-merchant").value || null,
            lines,
          }),
        });
        changed();
        await showRecord(record.id);
      },
      $("#identity-status"),
    );
}

function showCreateIdentity(
  kind,
  onDone = () => loadCatalog(),
  existing = null,
) {
  openDialog(
    `<h2>新建${kind === "item" ? "商品身份" : "商家"}</h2><label>规范名称<input id="new-identity-name" maxlength="500"></label>${kind === "item" ? '<label>类型<select id="new-identity-kind"><option value="product">商品</option><option value="dish">菜品</option><option value="drink">饮品</option><option value="service">服务</option><option value="subscription">订阅</option><option value="other">其他</option></select></label><label>品牌<input id="new-identity-brand" maxlength="200"></label><label>规格／档位<input id="new-identity-variant" maxlength="200" placeholder="例如 250g、500ml、专业版"></label>' : ""}<button id="new-identity-save" class="primary">保存身份</button><p id="new-identity-status" role="status"></p>`,
  );
  if (existing) {
    $("#dialog-body h2").textContent = "编辑规范身份";
    $("#new-identity-name").value = existing.canonical_name;
    if (kind === "item") {
      $("#new-identity-kind").value = existing.kind;
      $("#new-identity-brand").value = existing.brand || "";
      $("#new-identity-variant").value = existing.variant || "";
    }
  }
  $("#new-identity-save").onclick = (event) =>
    busy(
      event.target,
      async () => {
        const fields =
          kind === "item"
            ? [
                "kind",
                "brand",
                "variant",
                "merchant_id",
                "barcode",
                "external_ids",
              ]
            : ["merchant_type", "place_ref"];
        const body = {
          ...Object.fromEntries(
            fields
              .filter((field) => existing && field in existing)
              .map((field) => [field, existing[field]]),
          ),
          canonical_name: $("#new-identity-name").value.trim(),
        };
        if (kind === "item")
          Object.assign(body, {
            kind: $("#new-identity-kind").value,
            brand: $("#new-identity-brand").value || null,
            variant: $("#new-identity-variant").value || null,
          });
        await api(
          (kind === "item" ? "/items" : "/merchants") +
            (existing ? "/" + existing.id : ""),
          {
            method: existing ? "PATCH" : "POST",
            body: JSON.stringify(body),
          },
        );
        $("#dialog").close();
        await onDone();
        changed();
      },
      $("#new-identity-status"),
    );
}

async function loadCatalog() {
  const kind = $("#identity-kind").value;
  const data = await api(
    `${kind === "item" ? "/items" : "/merchants"}?query=${encodeURIComponent($("#identity-query").value)}`,
  );
  $("#identity-catalog").innerHTML =
    data.items
      .map(
        (row) =>
          `<div class="list-item"><span>${esc(row.canonical_name)} ${esc(row.variant || "")}<br><span class="meta">${row.active ? "有效身份" : "已合并／停用"}</span></span><span><button class="quiet" data-catalog-memory="${row.id}">回看</button>${row.active ? `<button class="quiet" data-edit-identity="${row.id}">编辑</button><button class="quiet" data-merge="${row.id}">合并</button>` : ""}</span></div>`,
      )
      .join("") || "<p>没有匹配身份，可新建或缩小搜索范围。</p>";
  $$("[data-catalog-memory]").forEach(
    (button) =>
      (button.onclick = () =>
        showMemory(kind, button.dataset.catalogMemory).catch(showError)),
  );
  $$("[data-edit-identity]").forEach(
    (button) =>
      (button.onclick = () =>
        busy(button, async () => {
          const row = await api(
            `/${kind === "item" ? "items" : "merchants"}/${button.dataset.editIdentity}`,
          );
          showCreateIdentity(kind, () => loadCatalog(), row);
        })),
  );
  $$("[data-merge]").forEach(
    (button) =>
      (button.onclick = () =>
        showMerge(kind, button.dataset.merge, data.items).catch(showError)),
  );
}

async function showMerge(kind, id, rows) {
  const source = rows.find((row) => row.id === id);
  openDialog(
    `<h2>合并身份</h2><p>来源：${esc(source.canonical_name)} ${esc(source.variant || "")}。旧 ID 和原始消费文本保留。</p><label>目标身份<select id="merge-target"><option value="">请选择</option>${rows
      .filter((row) => row.active && row.id !== id)
      .map(
        (row) =>
          `<option value="${row.id}">${esc(row.canonical_name)} ${esc(row.variant || "")}</option>`,
      )
      .join(
        "",
      )}</select></label><p>请核对品牌与规格；名称相似不代表同一商品。此操作没有无条件的一键撤销。</p><label>合并原因<input id="merge-reason" maxlength="1000"></label><button id="merge-submit" class="primary">确认合并</button><p id="merge-status" role="status"></p>`,
  );
  $("#merge-submit").onclick = (event) =>
    busy(
      event.target,
      async () => {
        if (!$("#merge-target").value || !$("#merge-reason").value.trim())
          throw new Error("请选择目标并填写原因");
        if (!confirm("确认来源与目标是同一身份，继续合并？")) return;
        await api(`/${kind === "item" ? "items" : "merchants"}/${id}/merge`, {
          method: "POST",
          body: JSON.stringify({
            target_id: $("#merge-target").value,
            reason: $("#merge-reason").value.trim(),
          }),
        });
        $("#dialog").close();
        await loadCatalog();
        changed();
      },
      $("#merge-status"),
    );
}

export async function showIdentitySuggestion(id) {
  const row = await api(`/identity-suggestions/${id}`);
  openDialog(
    `<h2>身份建议</h2><p>${esc(row.source)} → ${esc(row.target)}</p><p>${esc(row.reason)}</p><p>请先核对规格和实际含义。接受将保留旧 ID 的重定向。</p><button class="quiet" data-identity-decision="reject">不是同一身份</button><button class="primary" data-identity-decision="accept">确认相同并合并</button><p id="suggestion-status" role="status"></p>`,
  );
  $$("[data-identity-decision]").forEach(
    (button) =>
      (button.onclick = () =>
        busy(
          button,
          async () => {
            await api(
              `/identity-suggestions/${id}/${button.dataset.identityDecision}`,
              { method: "POST" },
            );
            changed();
            $("#dialog").close();
          },
          $("#suggestion-status"),
        )),
  );
}

export function initConsumption() {
  $("#identity-filter").onsubmit = (event) => {
    event.preventDefault();
    loadCatalog().catch(showError);
  };
  $("#identity-create").onclick = () =>
    showCreateIdentity($("#identity-kind").value);
  $("#identity-propose").onclick = (event) =>
    busy(event.target, async () => {
      await api("/identities/propose", { method: "POST" });
      changed();
      showError(
        new Error("已检查同名候选；请到待处理查看。最多扫描每类 500 个身份。"),
      );
    });
  loadConsumption().catch(showError);
  loadCatalog().catch(showError);
}
