/* 事件日历页：点一条事件 → 弹出对话框，用 htmx 取 /partials/calendar/event 的详情；“+N 更多”展开当天的全部事件。
   详情片段由我们自己的模板渲染（已转义）；出错时只用 textContent 写提示。 */
(function () {
  "use strict";
  var dialog = document.getElementById("cal-dialog");
  var body = document.getElementById("cal-dialog-body");
  if (!dialog || !body) return;

  function loading() {
    body.replaceChildren();
    [["60%", "18px"], ["90%", "14px"], ["75%", "14px"], ["82%", "14px"]].forEach(function (s) {
      var d = document.createElement("div");
      d.className = "skeleton";
      d.style.width = s[0];
      d.style.minHeight = s[1];
      body.appendChild(d);
    });
  }

  function showError(text) {
    body.replaceChildren();
    var d = document.createElement("div");
    d.className = "cal-dialog-err";
    d.textContent = text;
    body.appendChild(d);
  }

  function openDialog() {
    if (dialog.open) return;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function closeDialog() {
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  var lastTrigger = null;

  function openEvent(key, trigger) {
    lastTrigger = trigger || null;
    loading();
    openDialog();
    var url = "/partials/calendar/event?key=" + encodeURIComponent(key);
    if (window.htmx && typeof window.htmx.ajax === "function") {
      window.htmx.ajax("GET", url, { source: body, target: body, swap: "innerHTML" }).catch(function () {
        showError("加载失败，请稍后再试。");
      });
    } else {
      // 没有 htmx 时退回 fetch（片段来自我们自己的模板）
      window.tsen.fetch(url).then(function (r) {
        if (!r.ok && r.status !== 404) throw new Error("HTTP " + r.status);
        return r.text();
      }).then(function (html) { body.innerHTML = html; }).catch(function (e) {
        showError("加载失败：" + (e && e.message ? e.message : "网络错误"));
      });
    }
  }

  // 不认识的 key 返回 404 片段：照样显示（htmx 默认不替换 4xx 的内容）
  document.addEventListener("htmx:beforeSwap", function (e) {
    if (e.detail.target !== body) return;
    var st = e.detail.xhr ? e.detail.xhr.status : 0;
    if (st === 404) {
      e.detail.shouldSwap = true;
      e.detail.isError = false;
    } else if (st >= 400) {
      e.detail.shouldSwap = false;
      e.detail.isError = false;  // 对话框里已经写了提示，不再弹全局的“请求失败”
      showError("加载失败（HTTP " + st + "），请稍后再试。");
    }
  });
  document.addEventListener("htmx:sendError", function (e) {
    if (e.detail && e.detail.target === body) showError("连不上 tickersentinel：网页服务可能已经停了。");
  });

  document.addEventListener("click", function (e) {
    var chip = e.target.closest("[data-ev-key]");
    if (chip) {
      e.preventDefault();
      openEvent(chip.getAttribute("data-ev-key"), chip);
      return;
    }
    var more = e.target.closest("[data-cal-more]");
    if (more) {
      var cell = more.closest(".cal-cell");
      if (!cell) return;
      var on = cell.classList.toggle("expanded");
      more.textContent = on ? "收起" : more.getAttribute("data-more-text");
      more.setAttribute("aria-expanded", on ? "true" : "false");
      return;
    }
    if (e.target.closest("[data-cal-close]")) closeDialog();
  });

  // 点对话框外面（背景）关闭；Esc 由浏览器处理
  dialog.addEventListener("click", function (e) {
    if (e.target !== dialog) return;
    var r = dialog.getBoundingClientRect();
    var inside = e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
    if (!inside) closeDialog();
  });
  dialog.addEventListener("close", function () {
    if (lastTrigger && typeof lastTrigger.focus === "function") lastTrigger.focus();
    lastTrigger = null;
  });
})();
