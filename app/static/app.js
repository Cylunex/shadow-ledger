import {
  $,
  $$,
  page,
  localPath,
  loadUser,
  loadPaymentMethods,
  showError,
} from "./core.js";
import { categories, initRecords } from "./records.js";
import { initWorkbench } from "./workbench.js";
import { initConsumption } from "./consumption.js";
import { initPlanning } from "./planning.js";
import { initInsights } from "./insights.js";
import { initOffline } from "./offline.js";
import { initMobile } from "./mobile.js";

$$(".page").forEach(
  (section) => (section.hidden = section.id !== `${page}-page`),
);
$$(".bottom-nav a").forEach((link) => {
  if (link.dataset.page === page) link.setAttribute("aria-current", "page");
});
$(".dialog-close").onclick = () => $("#dialog").close();
if (page !== "records")
  $("#draft-button").onclick = () =>
    (location.href = localPath("/?state=draft"));
async function boot() {
  await loadUser();
  await Promise.all([loadPaymentMethods(), categories()]);
  const initializers = {
    records: initRecords,
    workbench: initWorkbench,
    consumption: initConsumption,
    planning: initPlanning,
    insights: initInsights,
  };
  initializers[page]?.();
  initMobile();
  try {
    initOffline();
  } catch {
    showError(
      new Error("浏览器禁止本机存储，离线暂存不可用，在线功能仍可使用。"),
    );
  }
}
boot().catch(showError);
