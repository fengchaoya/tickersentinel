/* tickersentinel 网页的公共脚本（不需要构建）：
   - 写请求自动带上页面里的 token（X-Tsen-Token；htmx 和 tsen.fetch 都会带）
   - 涨跌配色（红涨 / 绿涨）、浅色 / 深色的切换，记在浏览器里（读写失败也能正常显示）
   - window.tsen：fetch / json / colors() / toast() / fmtPct() 供各页面脚本使用；配色变了会派发 "tsen:colors" 事件 */
(function () {
  "use strict";
  var html = document.documentElement;
  var meta = document.querySelector('meta[name="tsen-token"]');
  var token = meta ? meta.content : "";

  var store = {
    get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) { /* 隐私模式等 */ } }
  };

  function withToken(opts) {
    opts = opts || {};
    var headers = Object.assign({}, opts.headers || {});
    headers["X-Tsen-Token"] = token;
    return Object.assign({}, opts, { headers: headers, credentials: "same-origin" });
  }

  function tfetch(url, opts) { return fetch(url, withToken(opts)); }

  function json(url, opts) {
    return tfetch(url, opts).then(function (r) {
      if (!r.ok) {
        return r.text().then(function (t) { throw new Error(t || ("HTTP " + r.status)); });
      }
      return r.json();
    });
  }

  function cssVar(name) { return getComputedStyle(html).getPropertyValue(name).trim(); }

  function colors() {
    return {
      up: cssVar("--up"), down: cssVar("--down"), text: cssVar("--text"), muted: cssVar("--muted"),
      border: cssVar("--border"), panel: cssVar("--panel"), bg: cssVar("--bg"), accent: cssVar("--accent"),
      amber: cssVar("--amber"), purple: cssVar("--purple"), teal: cssVar("--teal"),
      series: [1, 2, 3, 4, 5, 6, 7, 8].map(function (i) { return cssVar("--series-" + i); })
    };
  }

  var toastEl = null, toastTimer = null;
  function toast(text) {
    if (!toastEl) {
      toastEl = document.createElement("div");
      toastEl.className = "toast";
      toastEl.setAttribute("role", "status");
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = text;
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.remove("show"); }, 2600);
  }

  function fmtPct(x, digits) {
    if (x === null || x === undefined || isNaN(x)) return "—";
    var d = digits === undefined ? 1 : digits;
    return (x > 0 ? "+" : "") + Number(x).toFixed(d) + "%";
  }

  function emitColors() { document.dispatchEvent(new CustomEvent("tsen:colors", { detail: colors() })); }

  // 涨跌配色
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-action]");
    if (!b) return;
    var act = b.getAttribute("data-action");
    if (act === "toggle-up") {
      var next = html.dataset.up === "red" ? "green" : "red";
      html.dataset.up = next;
      store.set("tsen.up", next === html.dataset.upDefault ? null : next);
      emitColors();
      toast(next === "red" ? "红涨绿跌" : "绿涨红跌");
    } else if (act === "toggle-theme") {
      var cur = html.dataset.theme || "";
      var nxt = cur === "" ? "light" : cur === "light" ? "dark" : "";
      if (nxt) html.dataset.theme = nxt; else delete html.dataset.theme;
      store.set("tsen.theme", nxt || null);
      emitColors();
      toast(nxt === "light" ? "浅色" : nxt === "dark" ? "深色" : "跟随系统");
    }
  });

  if (window.matchMedia) {
    var mq = window.matchMedia("(prefers-color-scheme: dark)");
    if (mq.addEventListener) mq.addEventListener("change", emitColors);
  }

  // htmx 的请求也带上 token
  document.addEventListener("htmx:configRequest", function (e) { e.detail.headers["X-Tsen-Token"] = token; });
  // 请求失败的统一提示：只显示状态码和简短的中文说明（返回的可能是整页 HTML）；
  // 自己处理错误的区域加 data-own-errors，这里就不再弹提示
  document.addEventListener("htmx:responseError", function (e) {
    var elt = e.detail.elt;
    if (elt && elt.closest && elt.closest("[data-own-errors]")) return;
    var xhr = e.detail.xhr;
    var status = xhr ? xhr.status : 0;
    var text = xhr && xhr.responseText ? xhr.responseText : "";
    var plain = /^\s*</.test(text) ? "" : text.slice(0, 60);
    var why = status === 403 ? "页面已过期，请刷新" : status === 404 ? "内容不存在" : status >= 500 ? "服务器出错" : "";
    toast("请求失败（" + (status || "网络") + "）" + (plain || why ? "：" + (plain || why) : ""));
  });
  document.addEventListener("htmx:sendError", function () { toast("连不上网页服务：tsen run / tsen web 还在运行吗？"); });

  window.tsen = { token: token, fetch: tfetch, json: json, colors: colors, toast: toast, fmtPct: fmtPct, store: store };
})();
