export const $ = (selector) => document.querySelector(selector);
export const $$ = (selector) => [...document.querySelectorAll(selector)];
export const prefix = document.body.dataset.prefix || "";
export const localPath = (path) => `${prefix}${path}`;
export const page = document.body.dataset.page;
export const key = () => crypto.randomUUID();
export const platformNames = {
  jd: "京东",
  taobao: "淘宝",
  meituan: "美团",
  eleme: "饿了么",
  alipay: "支付宝",
  wechat: "微信",
};
export const typeNames = { expense: "支出", income: "收入", refund: "退款" };
export const sceneNames = {
  online_purchase: "线上购物",
  offline_purchase: "线下购物",
  delivery: "外卖",
  dine_in: "堂食",
  drink: "饮品",
  service: "服务",
  subscription: "订阅",
  transport: "交通",
  entertainment: "娱乐",
  travel: "旅行",
  other: "其他",
};
export let paymentLabels = {};
export let currentUser = null;
const pendingKeys = new Map();
const inFlight = new Map();
const cookie = (name) =>
  document.cookie
    .split("; ")
    .find((v) => v.startsWith(`${name}=`))
    ?.split("=")
    .slice(1)
    .join("=");

export async function api(path, options = {}) {
  const method = options.method || "GET";
  const mutation = !["GET", "HEAD"].includes(method);
  const signature = `${method}:${path}:${options.headers?.["If-Match"] || ""}:${options.headers?.["Idempotency-Key"] || ""}:${options.body || ""}`;
  if (mutation && inFlight.has(signature)) return inFlight.get(signature);
  const run = async () => {
    const headers = { Accept: "application/json", ...options.headers };
    if (options.body && !headers["Content-Type"])
      headers["Content-Type"] = "application/json";
    if (mutation) {
      headers["X-CSRF-Token"] = decodeURIComponent(
        cookie("__Host-ledger-csrf") || "",
      );
      if (mutation) {
        if (!pendingKeys.has(signature))
          pendingKeys.set(signature, headers["Idempotency-Key"] || key());
        headers["Idempotency-Key"] = pendingKeys.get(signature);
      }
    }
    const response = await fetch(localPath(`/api/v1${path}`), {
      ...options,
      headers,
      credentials: "same-origin",
    });
    if (response.status === 401) {
      const error = new Error(
        "登录已失效，请重新登录后重试；当前表单尚未清空。",
      );
      error.status = 401;
      throw error;
    }
    if (!response.ok) {
      let data = {};
      try {
        data = await response.json();
      } catch {}
      if (response.status < 500) pendingKeys.delete(signature);
      const error = new Error(
        data.error?.message ||
          (data.detail
            ? "输入格式有误，请检查必填项和时间。"
            : `请求失败（${response.status}）`),
      );
      error.status = response.status;
      error.code = data.error?.code;
      throw error;
    }
    // A truncated success body is still an uncertain write: preserve its retry key.
    const result = response.status === 204 ? null : await response.json();
    pendingKeys.delete(signature);
    return result;
  };
  const promise = run();
  if (mutation) inFlight.set(signature, promise);
  try {
    return await promise;
  } finally {
    inFlight.delete(signature);
  }
}

export function escapeHtml(value) {
  const element = document.createElement("div");
  element.textContent = String(value ?? "");
  return element.innerHTML;
}

export function money(value, currency = "CNY") {
  const match = String(value ?? "0").match(/^(-?)(\d+)(?:\.(\d{1,4}))?$/);
  if (!match) return "金额格式错误";
  const decimals = (match[3] || "")
    .padEnd(2, "0")
    .replace(/0+$/, "")
    .padEnd(2, "0");
  return `${escapeHtml(currency)} ${match[1]}${BigInt(match[2]).toLocaleString("zh-CN")}.${decimals}`;
}

export function sumAmounts(values) {
  const total = values.reduce((sum, value) => {
    const match = String(value).match(/^(-?)(\d+)(?:\.(\d{1,4}))?$/);
    if (!match) throw new Error("金额格式错误");
    const units =
      BigInt(match[2]) * 10000n + BigInt((match[3] || "").padEnd(4, "0"));
    return sum + (match[1] ? -units : units);
  }, 0n);
  const absolute = total < 0n ? -total : total;
  return `${total < 0n ? "-" : ""}${absolute / 10000n}.${String(absolute % 10000n).padStart(4, "0")}`;
}

export const dateValue = (value) =>
  new Date(/(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`);
export function monthInTimezone(timezone, value = new Date()) {
  const parts = new Intl.DateTimeFormat("en", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
  }).formatToParts(value);
  return `${parts.find((p) => p.type === "year").value}-${parts.find((p) => p.type === "month").value}`;
}
export function localDateTime(value) {
  const date = dateValue(value);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 16);
}
export function paymentOptions(value = "", empty = "未提供") {
  return (
    `<option value="">${escapeHtml(empty)}</option>` +
    Object.entries(paymentLabels)
      .map(
        ([key, label]) =>
          `<option value="${key}"${key === value ? " selected" : ""}>${escapeHtml(label)}</option>`,
      )
      .join("")
  );
}
export async function loadPaymentMethods() {
  const data = await api("/payment-methods");
  paymentLabels = Object.fromEntries(
    data.items.map((item) => [item.key, item.label]),
  );
  $("#payment-method").innerHTML = paymentOptions();
  $("#filter-payment").innerHTML = paymentOptions("", "全部支付方式");
}
export async function loadUser() {
  currentUser = await api("/me");
  return currentUser;
}
export function showError(error) {
  const target = $("#global-status");
  target.replaceChildren(document.createTextNode(error.message));
  if (error.status === 401) {
    const link = document.createElement("a");
    link.href = localPath(
      `/login?return_to=${encodeURIComponent(location.pathname.slice(prefix.length) || "/")}`,
    );
    link.textContent = "重新登录";
    target.append(" ", link);
  }
}
export function openDialog(html) {
  $("#dialog-body").innerHTML = html;
  if (!$("#dialog").open) $("#dialog").showModal();
}
export function changed() {
  document.dispatchEvent(new Event("ledger:changed"));
}
export function recoverConflict(status, error, reload) {
  if (error.code !== "revision_conflict") return;
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "重新读取最新版本";
  button.onclick = () => {
    if (
      confirm(
        "当前输入尚未保存。放弃本次输入并读取最新版本，以免覆盖其他修改？",
      )
    )
      Promise.resolve(reload()).catch(showError);
  };
  status.append(" ", button);
}
export async function busy(button, action, status, reload) {
  button.disabled = true;
  try {
    return await action();
  } catch (error) {
    if (status) {
      status.textContent = error.message;
      if (reload) recoverConflict(status, error, reload);
    } else showError(error);
  } finally {
    if (button.isConnected) button.disabled = false;
  }
}
