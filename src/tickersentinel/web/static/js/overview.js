/* 总览页：消息流的实时推送（SSE /api/stream）、标题旁的时钟。
   新消息的 HTML 由服务器用同一个模板渲染（我们自己的模板，正文已转义），这里只负责放到合适的位置：
   归因 / 跟帖挂到页面上对应的那条异动下面，其余的放在最上面。 */
(function () {
  "use strict";
  var feed = document.getElementById("ov-feed");
  var live = document.getElementById("ov-live");
  var clock = document.getElementById("ov-clock");

  // ── 时钟：每 20 秒按设置的时区刷新一次 ──
  if (clock && window.Intl && Intl.DateTimeFormat) {
    var tz = clock.getAttribute("data-tz") || undefined;
    var fmt = null;
    try {
      fmt = new Intl.DateTimeFormat("zh-CN", {
        timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit", weekday: "short",
        hour: "2-digit", minute: "2-digit", hourCycle: "h23"
      });
    } catch (e) { fmt = null; }
    // 时区缩写沿用服务器给的（CDT / CST）：浏览器给的可能是 “GMT-5”
    var tzAbbr = (clock.textContent.trim().split(/\s+/).pop() || "");
    if (!/^[A-Z]{2,5}$/.test(tzAbbr)) tzAbbr = "";
    var tick = function () {
      if (!fmt || !fmt.formatToParts) return;
      var p = {};
      fmt.formatToParts(new Date()).forEach(function (x) { p[x.type] = x.value; });
      clock.textContent = p.year + "-" + p.month + "-" + p.day + " " + (p.weekday || "") + " " +
        p.hour + ":" + p.minute + (tzAbbr ? " " + tzAbbr : "");
    };
    setInterval(tick, 20000);
  }

  // ── 选项卡：对话（默认）| 消息流。记住上次停在哪个；在对话里时新推送记成未读，显示在“消息流”上 ──
  var T = window.tsen || {};
  var panes = { chat: document.getElementById("tab-chat"), feed: document.getElementById("tab-feed") };
  var unreadEl = document.getElementById("ov-unread");
  var unread = 0;
  function feedVisible() { return panes.feed && !panes.feed.hidden; }
  function setUnread(n, alert) {
    unread = n;
    if (!unreadEl) return;
    unreadEl.hidden = n <= 0;
    unreadEl.textContent = n > 99 ? "99+" : String(n);
    if (n <= 0) unreadEl.classList.remove("alert");
    else if (alert) unreadEl.classList.add("alert");  // 有异动：醒目的颜色
  }
  function showTab(name, save) {
    if (!panes[name]) return;
    Object.keys(panes).forEach(function (k) { if (panes[k]) panes[k].hidden = k !== name; });
    document.querySelectorAll(".ov-tab").forEach(function (b) {
      var on = b.getAttribute("data-tab") === name;
      b.classList.toggle("on", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    if (name === "feed") setUnread(0);
    if (save && T.store) T.store.set("tsen.ov.tab", name);
    if (name === "chat") {
      var log = document.getElementById("chat-log");
      if (log) log.scrollTop = log.scrollHeight;
    }
  }
  document.querySelectorAll(".ov-tab").forEach(function (b) {
    b.addEventListener("click", function () { showTab(b.getAttribute("data-tab"), true); });
  });
  if (T.store && T.store.get("tsen.ov.tab") === "feed") showTab("feed", false);

  // ── 问一下：消息流里某条推送 → 切到对话，带着这条推送提问（像在飞书里回复那条消息） ──
  var askRef = null;  // {title, text}
  var ctxBox = document.getElementById("chat-ctx");
  function setAskRef(ref) {
    askRef = ref;
    if (!ctxBox) return;
    ctxBox.hidden = !ref;
    var t = document.getElementById("chat-ctx-t");
    if (t) t.textContent = ref ? ref.title : "";
  }
  var ctxX = document.getElementById("chat-ctx-x");
  if (ctxX) ctxX.addEventListener("click", function () { setAskRef(null); });

  // ── 网页提问（对话选项卡）：POST /api/chat（和飞书里同一套对话；本地模型约 20–50 秒，先显示“思考中”和已等的秒数） ──
  var chatForm = document.getElementById("chat-form");
  var chatInput = document.getElementById("chat-input");
  var chatLog = document.getElementById("chat-log");
  var chatSend = document.getElementById("chat-send");
  if (chatForm && chatInput && chatLog && window.tsen) {
    var busy = false;
    var bubble = function (role, text) {
      var d = document.createElement("div");
      d.className = "chat-msg chat-" + role;
      d.textContent = text;
      chatLog.appendChild(d);
      chatLog.scrollTop = chatLog.scrollHeight;
      return d;
    };
    var grow = function () {
      chatInput.style.height = "auto";
      chatInput.style.height = Math.min(chatInput.scrollHeight, 160) + "px";
    };
    chatInput.addEventListener("input", grow);
    chatInput.addEventListener("keydown", function (e) {
      // 输入法选字时的 Enter 不发送（Safari 选字确认那一下 isComposing 是 false，只能靠 keyCode 229 认出来）
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
        e.preventDefault();
        chatForm.requestSubmit ? chatForm.requestSubmit() : chatForm.dispatchEvent(new Event("submit", { cancelable: true }));
      }
    });
    chatForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var text = chatInput.value.trim();
      if (!text || busy) return;
      busy = true;
      chatSend.disabled = true;
      chatInput.value = "";
      grow();
      var ref = askRef;
      setAskRef(null);
      var ub = bubble("user", text);
      if (ref) {
        var q = document.createElement("div");
        q.className = "chat-quote";
        q.textContent = "关于：" + ref.title;
        ub.insertBefore(q, ub.firstChild);
      }
      var wait = bubble("assistant", "思考中…");
      wait.classList.add("chat-wait");
      var t0 = Date.now();
      var timer = setInterval(function () {
        wait.textContent = "思考中… " + Math.round((Date.now() - t0) / 1000) + " 秒（本地模型一般 20–50 秒）";
      }, 1000);
      window.tsen.json("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text, reply_to_text: ref ? ref.text : null })
      }).then(function (r) {
        wait.classList.remove("chat-wait");
        if (r.ok) { wait.innerHTML = r.answer_html; }  // 服务器转好的 HTML（正文已转义，链接只认 http(s)）
        else { wait.classList.add("chat-err"); wait.textContent = r.error || "出错了"; }
      }).catch(function (err) {
        wait.classList.remove("chat-wait");
        wait.classList.add("chat-err");
        var msg = String((err && err.message) || err);
        try { msg = JSON.parse(msg).error || msg; } catch (x) { /* 不是 JSON */ }
        wait.textContent = msg;
      }).then(function () {
        clearInterval(timer);
        busy = false;
        chatSend.disabled = false;
        chatLog.scrollTop = chatLog.scrollHeight;
        chatInput.focus();
      });
    });
    // htmx 填好历史（打开页面、每分钟刷新一次以带上飞书里的问答）后：原来就在最底下（或第一次）才滚到最新，往上翻着看时不打扰
    var atBottom = true;
    chatLog.addEventListener("htmx:beforeSwap", function () {
      atBottom = chatLog.scrollHeight - chatLog.scrollTop - chatLog.clientHeight < 40;
    });
    chatLog.addEventListener("htmx:afterSwap", function () { if (atBottom) chatLog.scrollTop = chatLog.scrollHeight; });
  }

  if (!feed) return;

  function setLive(state) {
    if (!live) return;
    live.classList.toggle("on", state === "on");
    live.classList.toggle("off", state === "off");
    live.title = state === "on" ? "已连接：新消息会实时出现" : state === "off" ? "连接断开，正在重连…" : "正在连接实时推送";
  }

  function fromHTML(html) {
    var t = document.createElement("template");
    t.innerHTML = (html || "").trim();
    return t.content.firstElementChild;
  }

  function cssEscape(s) {
    return window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/["\\]/g, "\\$&");
  }

  function seen(id) {
    return id !== null && id !== undefined && feed.querySelector('[data-id="' + cssEscape(String(id)) + '"]') !== null;
  }

  // 回复要挂到哪张卡片：先按 reply_to 找消息，再（归因）按异动 id 找异动卡片
  function parentCard(msg) {
    var el = null;
    if (msg.reply_to) {
      el = feed.querySelector('[data-mid="' + cssEscape(msg.reply_to) + '"]');
    }
    if (!el && msg.kind === "attribution" && msg.alert_ids && msg.alert_ids.length) {
      var cards = feed.querySelectorAll('article.fc-alert[data-alert-ids]');
      for (var i = 0; i < cards.length && !el; i++) {
        var ids = (cards[i].getAttribute("data-alert-ids") || "").split(",");
        for (var j = 0; j < msg.alert_ids.length; j++) {
          if (ids.indexOf(String(msg.alert_ids[j])) >= 0) { el = cards[i]; break; }
        }
      }
    }
    return el ? el.closest("article.fc") : null;
  }

  function flash(el) {
    if (!el) return;
    el.classList.add("fc-new");
    setTimeout(function () { el.classList.remove("fc-new"); }, 2600);
  }

  function place(msg) {
    if (!msg || seen(msg.id)) return;
    var parent = (msg.kind === "attribution" || msg.reply_to) ? parentCard(msg) : null;
    if (parent && msg.thread_html) {
      var thread = parent.querySelector(".fc-thread");
      if (!thread) {
        thread = document.createElement("div");
        thread.className = "fc-thread";
        parent.querySelector(".fc-main").appendChild(thread);
      }
      var reply = fromHTML(msg.thread_html);
      if (!reply) return;
      thread.appendChild(reply);
      // 正文里“正在检索可能原因…”那一行：归因到了就去掉
      if (msg.kind === "attribution") {
        parent.querySelectorAll(".fc-main > .fc-body li, .fc-main > .fc-body p").forEach(function (n) {
          if (/^\s*⏳/.test(n.textContent)) n.remove();
        });
      }
      flash(reply);
      flash(parent);
    } else {
      var card = fromHTML(msg.html);
      if (!card) return;
      var empty = feed.querySelector(".feed-empty");
      if (empty) empty.remove();
      feed.insertBefore(card, feed.firstElementChild);
      flash(card);
    }
    if (msg.kind === "alert") {
      document.body.dispatchEvent(new CustomEvent("tsen:alert"));  // 让“今日异动”刷新
    }
    if (!feedVisible()) setUnread(unread + 1, msg.kind === "alert" || msg.kind === "error");
  }

  // “问一下”：取这条推送的标题和正文（含挂在下面的归因），切到对话
  feed.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("[data-ask]") : null;
    if (!btn) return;
    e.preventDefault();
    var card = btn.closest("article.fc");
    if (!card) return;
    var titleEl = card.querySelector(".fc-title");
    var main = card.querySelector(".fc-main");
    var title = (titleEl ? titleEl.innerText : "").trim() || (card.querySelector(".fc-kind") || {}).innerText || "这条推送";
    // textContent：折起来的简报正文、宏观快照也要带上（innerText 不含没展开的部分）
    var text = (main || card).textContent.replace(/问一下/g, "").replace(/[ \t]+/g, " ").replace(/\s*\n\s*/g, "\n").trim();
    setAskRef({ title: title.slice(0, 80), text: text.slice(0, 6000) });
    showTab("chat", true);
    var input = document.getElementById("chat-input");
    if (input) input.focus();
  });

  if (!window.EventSource) { setLive("off"); return; }
  // 已经收到的最大消息 id：从后退缓存恢复页面时从这里接着收
  var lastId = parseInt(feed.getAttribute("data-after") || "0", 10) || 0;
  var es = null;

  function connect() {
    setLive("");
    try {
      es = new EventSource("/api/stream?after=" + encodeURIComponent(String(lastId)));
    } catch (err) { es = null; setLive("off"); return; }
    es.onopen = function () { setLive("on"); };
    es.onerror = function () { setLive("off"); };  // EventSource 会自己重连（带上 Last-Event-ID）
    es.addEventListener("message", function (e) {
      setLive("on");
      var msg = null;
      try { msg = JSON.parse(e.data); } catch (err) { return; }
      var n = parseInt(e.lastEventId || (msg && msg.id) || "0", 10);
      if (n > lastId) lastId = n;
      try { place(msg); } catch (err) { /* 一条出错不影响后面的 */ }
    });
  }

  function disconnect() {
    if (es) { try { es.close(); } catch (e) { /* 忽略 */ } }
    es = null;
  }

  connect();
  window.addEventListener("pagehide", disconnect);
  // 从浏览器的后退 / 前进缓存恢复：连接在 pagehide 时关掉了，重新连上
  window.addEventListener("pageshow", function (e) {
    if (e.persisted) { disconnect(); connect(); }
  });
})();
