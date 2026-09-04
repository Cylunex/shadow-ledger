import { api, currentUser, key, changed, showError, $ } from "./core.js";

const storageKey = "ledger.text-drafts.v1";
const maxAge = 7 * 86400000;
function readQueue() {
  try {
    return JSON.parse(localStorage.getItem(storageKey) || "[]").filter(
      (row) => row.expires > Date.now() && row.owner === currentUser?.owner_id,
    );
  } catch {
    return [];
  }
}
function writeQueue(rows) {
  localStorage.setItem(storageKey, JSON.stringify(rows));
}
export function queueDraft(body) {
  if (!currentUser || !$("#offline-enable")?.checked)
    throw new Error("请先登录并主动开启本机文本草稿暂存");
  const rows = readQueue();
  if (rows.length >= 50) throw new Error("本机最多暂存 50 条，请先同步或清空");
  rows.push({
    id: key(),
    owner: currentUser.owner_id,
    expires: Date.now() + maxAge,
    body: { ...body, confirm: false },
  });
  writeQueue(rows);
  renderQueue();
}
function renderQueue() {
  if ($("#offline-count"))
    $("#offline-count").textContent =
      `本机待同步 ${readQueue().length} 条（7 天后清理，仅此用户可同步）`;
}
export function initOffline() {
  writeQueue(readQueue());
  renderQueue();
  $("#offline-clear")?.addEventListener("click", () => {
    if (confirm("清空本机尚未同步的文本草稿？此操作不能恢复。")) {
      writeQueue([]);
      renderQueue();
    }
  });
  $("#offline-sync")?.addEventListener("click", async (event) => {
    event.target.disabled = true;
    try {
      const fresh = await api("/me");
      if (fresh.owner_id !== currentUser.owner_id)
        throw new Error("当前登录用户已变化，请刷新；未同步任何草稿");
      for (const row of readQueue()) {
        await api("/records", {
          method: "POST",
          headers: { "Idempotency-Key": row.id },
          body: JSON.stringify(row.body),
        });
        writeQueue(readQueue().filter((item) => item.id !== row.id));
      }
      changed();
    } catch (error) {
      showError(error);
    } finally {
      event.target.disabled = false;
      renderQueue();
    }
  });
}
