import { $, localPath, showError } from "./core.js";

export function initMobile() {
  let installPrompt = null;
  const button = $("#install-app");
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    installPrompt = event;
    button.textContent = "安装 Ledger";
  });
  button.onclick = async () => {
    if (installPrompt) {
      await installPrompt.prompt();
      installPrompt = null;
    } else
      showError(
        new Error(
          "可使用浏览器菜单“添加到主屏幕／安装应用”。是否支持取决于当前浏览器；未开启全站离线缓存。",
        ),
      );
  };
  const share = $("#share-app");
  share.hidden = !navigator.share;
  share.onclick = async () => {
    try {
      await navigator.share({
        title: "Shadow Ledger",
        url: new URL(localPath("/"), location.origin).href,
      });
    } catch (error) {
      if (error.name !== "AbortError") showError(error);
    }
  };
}
