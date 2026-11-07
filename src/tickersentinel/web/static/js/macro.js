/* 宏观页的脚本（决策 21）：
   - 趋势小图：/api/macro/series，每个变量一张 Lightweight Charts 小图，纵轴是实际值（原油 104、VIX 19.4、收益率 4.93%），
     窗口起点画一条虚线；较起点的变化（bp 或 %）写在标题和十字线读数里。
     所有小图用同一组时间格（各变量的点取并集 + 按步长补齐的空白格），所以横轴严格对齐、按时间等比例；十字线联动。
     没有交易的时段（夜里、周末）断开不连线：图表库会把空白格两头直接连起来，所以把空档前最后一个点设成透明色
     （线段的颜色取自它起点那个点）。收益率夜里用国债期货折算的部分（fill）画虚线。
     只在交易时段有数据的变量（compress：VIX、KOSPI）不用共用的时间格，只喂自己的点：图表库的横轴按点的顺序排，
     前一段收盘直接接下一段开盘。这种图的横轴和其他图不对齐，所以每张图记下自己的横轴（o.axis：共用时间格或自己的点），
     十字线同步、拖选都按时间换算到各自的横轴上。
   - “谁动了”：/api/macro/movers，横条长度 ∝ |z|；在任一张图上按住鼠标拖一段，只算那一段。持仓标“持仓”。
   - 对比视图（2026-10-31 用户要求：原来的“叠在一起”把同单位的全叠上去太挤）：自己点选几个变量（最多 8 个）放进一张图，
     按较窗口起点的变化画——“% / bp”：价格类右轴 %、收益率左轴 bp（虚线）；“几倍日波动”：变化 ÷ 平常的日波动（sigma_day），
     一根纵轴。选了哪些记在浏览器里和网址的 ?cmp= 里；颜色跟着变量走（拿掉别的变量不会改它的颜色）；
     第一次打开（没选过）放“谁动了”的前 3 名。所有线用共用时间格。
   - 收益率曲线：/api/macro/curve?days=180（日线）。
   接口返回的图表时间已经加上本地时区偏移（按 UTC 显示即本地墙上时间）。
   只用 textContent / DOM 接口写文字（名称、代码都可能来自配置）。 */
(function () {
  "use strict";

  var root = document.getElementById("macro-root");
  if (!root || root.getAttribute("data-store") !== "1") return;
  var LW = window.LightweightCharts;
  var T = window.tsen || {};
  var TZ = root.getAttribute("data-tz") || undefined;
  var QUERY = root.getAttribute("data-query") || "window=24h";
  var MINUS = "−";

  var getJSON = T.json || function (u, o) {
    return fetch(u, o).then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error(t || ("HTTP " + r.status)); });
      return r.json();
    });
  };

  var $ = function (id) { return document.getElementById(id); };
  var gridBox = $("tr-grid"), overlayBox = $("tr-compare"), moversBox = $("mv-list"), moversRange = $("mv-range");
  var resetBtn = $("mv-reset"), freshEl = $("mc-fresh"), emptyBox = $("mc-empty"), alertsBox = $("mc-alerts");

  var S = {
    series: null,     // /api/macro/series 的结果
    movers: null,
    curve: null,
    slots: [],        // 所有小图共用的时间格（升序，本地墙上时间的“UTC 秒”）
    sel: null,        // 拖选的时间段 {from, to}（时间格）
    layout: "grid",
    charts: [],       // 小图 {chart, series, box, item, values, times, marks, hover}
    overlay: [],      // 对比图 {chart, box, lines, axis}
    cmp: [],          // 对比图里的变量（key，按加入的顺序）
    cmpSlot: {},      // key → 颜色序号（0–7）：颜色跟着变量走
    cmpTouched: false,  // 读者自己选过（或网址 / 浏览器里有记录）：不再自动放“谁动了”的前几名
    norm: "raw",      // 对比图的纵轴：raw = % / bp，sigma = 几倍日波动
    curveCharts: [],
    active: null,     // 鼠标所在的小图（十字线从它同步到其他图）
    moversSeq: 0
  };

  // ───── 工具 ─────

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function errText(err) {
    var m = String((err && err.message) || err || "");
    try { var j = JSON.parse(m); if (j && j.detail) m = String(j.detail); } catch (e) { /* 不是 JSON */ }
    if (/^\s*</.test(m)) m = "";  // 整页 HTML
    return m.slice(0, 160);
  }

  function message(box, title, detail, isErr) {
    box.textContent = "";
    var m = el("div", "chart-msg" + (isErr ? " err" : ""));
    m.appendChild(el("strong", "", title));
    if (detail) m.appendChild(el("div", "", detail));
    box.appendChild(m);
  }

  /** 变化量的文字：bp 取整数，% 两位小数；负号用 “−”。 */
  function fmtChange(v, unit, digits) {
    if (v === null || v === undefined || !isFinite(v)) return "—";
    var bp = unit === "bp";
    var d = digits === undefined ? (bp ? 0 : 2) : digits;
    var r = Number(Number(v).toFixed(d));
    var sign = r > 0 ? "+" : r < 0 ? MINUS : "";
    return sign + Math.abs(r).toFixed(d) + (bp ? "bp" : "%");
  }

  function axisFormatter(unit) {
    return function (v) {
      if (unit === "bp") return fmtChange(v, "bp", Math.abs(v) < 10 ? 1 : 0);
      return fmtChange(v, "%", Math.abs(v) < 10 ? 2 : 1);
    };
  }

  /** 实际值的文字（和总览看板一致）：收益率 4.93%；≥ 1000 不带小数并加千分位；价格类（kind = price）前面加 $。 */
  function fmtValue(v, kind, digits) {
    if (v === null || v === undefined || !isFinite(v)) return "—";
    if (kind === "yield") return Number(v).toFixed(digits === undefined ? 2 : digits) + "%";
    var d = digits === undefined ? (Math.abs(v) >= 1000 ? 0 : Math.abs(v) >= 1 ? 2 : 4) : digits;
    var num = Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
    return (kind === "price" ? "$" : "") + num;
  }

  /** 纵轴刻度要几位小数：按这张图的数值范围，刻度大约五格、彼此分得开。 */
  function axisDigits(values, kind) {
    var lo = Infinity, hi = -Infinity;
    values.forEach(function (v) { if (isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); } });
    if (!isFinite(lo)) return 2;
    var range = Math.max(hi - lo, Math.abs(hi) * 1e-4, 1e-6);
    var d = Math.ceil(-Math.log10(range / 5));
    return Math.max(kind === "yield" ? 2 : 0, Math.min(4, d));
  }

  /** 较起点的变化（和服务端 macro_data.change 同一口径）：收益率 bp，其他 %。 */
  function changeOf(it, v) {
    var b = it.base;
    if (b === null || b === undefined || !isFinite(v) || !isFinite(b)) return null;
    if (it.kind === "yield") return (v - b) * 100;
    return b > 0 ? (v / b - 1) * 100 : null;
  }

  function ud(v, unit) {
    if (v === null || v === undefined || !isFinite(v)) return "flat";
    var eps = unit === "bp" ? 0.5 : 0.005;
    return v > eps ? "up" : v < -eps ? "down" : "flat";
  }

  function pad(n) { return n < 10 ? "0" + n : String(n); }

  /** 图表时间（本地墙上时间的“UTC 秒”）→ 文字。 */
  function wall(t, withDate) {
    var d = new Date(t * 1000);
    var hm = pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
    return withDate ? pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()) + " " + hm : hm;
  }

  function sameDay(a, b) { return Math.floor(a / 86400) === Math.floor(b / 86400); }

  /** 图表时间 → 不带时区的 ISO（接口按本地时区理解）。 */
  function naiveIso(t) { return new Date(t * 1000).toISOString().slice(0, 19); }

  /** 真正的 ISO 时间 → 本地时区的文字。 */
  function fmtReal(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    try {
      return new Intl.DateTimeFormat("zh-CN", { timeZone: TZ, month: "2-digit", day: "2-digit", hour: "2-digit",
                                                 minute: "2-digit", hour12: false }).format(d);
    } catch (e) {
      return d.toLocaleString();
    }
  }

  function colors() { return T.colors ? T.colors() : {}; }

  function allItems() {
    var out = [];
    if (S.series) S.series.groups.forEach(function (g) { g.items.forEach(function (it) { out.push(it); }); });
    return out;
  }

  /** 第一个 >= t 的下标（times 升序）。 */
  function lowerBound(times, t) {
    var lo = 0, hi = times.length;
    while (lo < hi) { var mid = (lo + hi) >> 1; if (times[mid] < t) lo = mid + 1; else hi = mid; }
    return lo;
  }

  function nearest(times, t) {
    if (!times.length) return null;
    var i = lowerBound(times, t);
    if (i <= 0) return times[0];
    if (i >= times.length) return times[times.length - 1];
    return (t - times[i - 1] <= times[i] - t) ? times[i - 1] : times[i];
  }

  /** t 时刻的点（或之前最近一个点）的时间；t 之前没有点、或最近的点隔了一个空档（那时没在交易）时 undefined。 */
  function pointAt(o, t) {
    if (o.values.hasOwnProperty(t)) return t;
    var i = lowerBound(o.times, t) - 1;
    if (i < 0) return undefined;
    var at = o.times[i];
    return t - at > gapSeconds() ? undefined : at;
  }

  // ───── 时间格：所有图共用，横轴才能对齐 ─────

  function buildSlots(d) {
    var step = Math.max(1, d.step_minutes || 1) * 60;
    var set = {}, first = null;
    allItems().forEach(function (it) {
      it.points.concat(it.fill || []).forEach(function (p) { set[p[0]] = 1; if (first === null || p[0] < first) first = p[0]; });
    });
    var phase = first === null ? 0 : ((first % step) + step) % step;
    var t0 = d.start_local - ((((d.start_local - phase) % step) + step) % step);
    for (var t = t0; t <= d.end_local; t += step) set[t] = 1;
    return Object.keys(set).map(Number).sort(function (a, b) { return a - b; });
  }

  // ───── 图表选项 ─────

  function chartOptions(c, unit, small) {
    return {
      autoSize: true,
      layout: { background: { type: "solid", color: c.panel || "transparent" }, textColor: c.muted,
                fontSize: small ? 10 : 12, fontFamily: getComputedStyle(document.body).fontFamily,
                attributionLogo: !small },
      grid: { vertLines: { visible: !small, color: c.border }, horzLines: { color: c.border } },
      rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.12, bottom: 0.1 } },
      timeScale: { borderColor: c.border, timeVisible: true, secondsVisible: false, fixLeftEdge: true, fixRightEdge: true,
                   lockVisibleTimeRangeOnResize: true },
      crosshair: { mode: LW.CrosshairMode ? LW.CrosshairMode.Normal : 0 },
      // 小图的横轴固定在整个窗口（各图对齐）；拖动留给“拖一段看谁动了”，滚轮留给页面滚动
      handleScroll: false,
      handleScale: false,
      localization: { priceFormatter: axisFormatter(unit) }
    };
  }

  /** 纵轴范围总是包含 ref（窗口起点：叠加视图里是 0，小图里是起点的实际值），起点那条虚线才看得见。 */
  function including(ref) {
    return function (orig) {
      var r = orig();
      if (r && r.priceRange && ref !== null && ref !== undefined && isFinite(ref)) {
        r.priceRange.minValue = Math.min(r.priceRange.minValue, ref);
        r.priceRange.maxValue = Math.max(r.priceRange.maxValue, ref);
      }
      return r;
    };
  }
  var withZero = including(0);

  var CLEAR = "rgba(0,0,0,0)";

  /** 相邻两个点隔多久算“没有交易”、断开不连线：3 个步长，至少 20 分钟（偶尔缺一两分钟的不算）。 */
  function gapSeconds() {
    var step = ((S.series && S.series.step_minutes) || 1) * 60;
    return Math.max(3 * step, 20 * 60);
  }

  /** slots = 共用时间格（空档留空、断线）；joined（拼接的图）传它自己的点，不留空、直接接上。 */
  function seriesData(values, slots, joined) {
    if (joined) {
      return slots.filter(function (t) { return values.hasOwnProperty(t); })
                  .map(function (t) { return { time: t, value: values[t] }; });
    }
    var gap = gapSeconds(), out = [], last = null;
    slots.forEach(function (t) {
      if (!values.hasOwnProperty(t)) { out.push({ time: t }); return; }  // 没有点的格子留空
      if (last !== null && t - last.time > gap) last.color = CLEAR;      // 空档：从上一个点出发的那段不画
      last = { time: t, value: values[t] };
      out.push(last);
    });
    return out;
  }

  /** 变量的点 {t: v}、升序时间；fill（期货折算）的点也并进来，implied 记下哪些时间是折算的。 */
  function pointsOf(it) {
    var values = {}, times = [], implied = {};
    it.points.forEach(function (p) { values[p[0]] = p[1]; });
    (it.fill || []).forEach(function (p) { if (!values.hasOwnProperty(p[0])) { values[p[0]] = p[1]; implied[p[0]] = 1; } });
    Object.keys(values).forEach(function (k) { times.push(Number(k)); });
    times.sort(function (a, b) { return a - b; });
    return { values: values, times: times, implied: implied };
  }

  /** 窗口里的异动画成箭头；only = 只画这个代码的（持仓的小图只标自己的异动）。 */
  function alertMarkers(times, c, withText, only, skip) {
    var out = [];
    ((S.series && S.series.alerts) || []).forEach(function (a) {
      if (only && a.symbol !== only) return;
      if (skip && skip[a.id]) return;
      var t = nearest(times, a.t);  // 标记要落在已有的数据点上
      if (t === null || Math.abs(t - a.t) > gapSeconds()) return;  // 那时这个变量没在交易：不把箭头挪到几小时外
      var up = a.direction > 0;
      out.push({ time: t, position: up ? "belowBar" : "aboveBar", color: up ? c.up : c.down,
                 shape: up ? "arrowUp" : "arrowDown", size: withText ? 1 : 0.8, id: "alert-" + a.id,
                 text: withText ? (a.name || a.symbol) : "", alertId: a.id });
    });
    out.sort(function (x, y) { return x.time - y.time; });
    return out;
  }

  function destroy(list) {
    list.forEach(function (o) { try { o.chart.remove(); } catch (e) { /* 已经移除 */ } });
    list.length = 0;
  }

  // ───── 趋势小图 ─────

  function renderSeries() {
    destroy(S.charts);
    destroy(S.overlay);
    S.active = null;
    var d = S.series;
    if (!d) return;
    var items = allItems();
    var any = items.some(function (it) { return it.points && it.points.length; });
    if (emptyBox) emptyBox.classList.toggle("hidden", any);
    S.slots = buildSlots(d);
    var stepEl = $("tr-step");
    if (stepEl) stepEl.textContent = (d.name || "") + " · 每 " + d.step_minutes + " 分钟一个点";
    renderAlerts(d.alerts || []);
    if (S.layout === "compare") renderCompare(); else renderGrid();
  }

  function renderGrid() {
    var d = S.series, c = colors();
    gridBox.textContent = "";
    d.groups.forEach(function (g) {
      var sec = el("section", "tr-group");
      sec.appendChild(el("h3", "tr-group-h", g.name));
      var grid = el("div", "tr-grid");
      sec.appendChild(grid);
      gridBox.appendChild(sec);
      g.items.forEach(function (it) { grid.appendChild(card(it, c)); });
    });
    refit(S.charts);
  }

  /** autoSize 要等卡片插进页面、量过尺寸才准：下一帧再对齐一次横轴、画选区。 */
  function refit(list) {
    list.forEach(function (o) { o.chart.timeScale().fitContent(); });
    paintBands();
    setTimeout(function () {
      list.forEach(function (o) { try { o.chart.timeScale().fitContent(); } catch (e) { /* 已经移除 */ } });
      paintBands();
    }, 80);
  }

  function card(it, c) {
    var box = el("div", "tr-card");
    box.setAttribute("data-key", it.key);
    var head = el("div", "tr-head");
    var name = el("span", "tr-name", it.name);
    name.title = it.name;
    head.appendChild(name);
    if (it.futures) {
      var tag = el("span", "tag", it.sub_tag || "期货");
      tag.title = it.sub_tag === "ETF"
        ? "窗口不全在美股盘中：换成盘前盘后也交易的 " + it.symbol + "（美东 04:00–20:00），数值是 ETF 的价格"
        : "窗口不全在美股盘中：股指换成 24 小时交易的期货";
      head.appendChild(tag);
    }
    if (it.compress) {
      var st = el("span", "tag", "交易时段");
      st.title = "这个变量只在交易时段有数据（工作日每天约 " + (it.hours_per_day || "?") + " 小时）：省掉夜里、周末的空档，" +
                 "前一段收盘直接接下一段开盘，所以横轴和其他图不对齐";
      head.appendChild(st);
    }
    var vals = el("span", "tr-vals");
    var cur = el("span", "tr-val", fmtValue(it.value, it.kind));
    cur.title = it.value_implied ? "最新值是按 " + it.proxy + " 期货折算的" : "窗口里最后一个值";
    vals.appendChild(cur);
    var chg = el("span", "tr-chg " + ud(it.change, it.unit), fmtChange(it.change, it.unit));
    chg.title = "较窗口起点（" + fmtValue(it.base, it.kind) + "）" +
                (it.proxy ? "；起点或终点落在收益率没有数据的时段，含 " + it.proxy + " 期货折算" : "");
    vals.appendChild(chg);
    head.appendChild(vals);
    box.appendChild(head);
    var sub = el("div", "tr-sub");
    sub.appendChild(el("span", "mono", it.symbol));
    var cb = el("button", "tr-cmp");
    cb.type = "button";
    cb.setAttribute("data-cmp-key", it.key);
    cmpButtonState(cb, it.key);
    cb.addEventListener("click", function () { toggleCmp(it.key); });
    sub.appendChild(cb);
    var hover = el("span", "tr-hover", "");
    sub.appendChild(hover);
    box.appendChild(sub);

    if (!(it.points && it.points.length) && !(it.fill && it.fill.length)) {
      box.appendChild(el("div", "tr-nodata", "这段时间没有数据"));
      return box;
    }
    var chartBox = el("div", "tr-chart");
    box.appendChild(chartBox);
    if (!LW) { message(chartBox, "图表库没有加载", "刷新页面再试", true); return box; }

    var pv = pointsOf(it), real = {};
    it.points.forEach(function (p) { real[p[0]] = p[1]; });
    var joined = !!it.compress;
    var o = { box: chartBox, item: it, values: pv.values, times: pv.times, implied: pv.implied, hover: hover, marks: [],
              joined: joined, axis: joined ? pv.times : S.slots };
    var lineColor = c[ud(it.change, it.unit) === "flat" ? "muted" : ud(it.change, it.unit)] || c.accent;
    var all = pv.times.map(function (t) { return pv.values[t]; });
    if (it.base !== null && it.base !== undefined) all.push(it.base);
    var digits = axisDigits(all, it.kind);
    var opts = chartOptions(c, it.unit, true);
    // 纵轴不带 $（窄），收益率带 %
    opts.localization = { priceFormatter: function (v) { return fmtValue(v, it.kind === "price" ? "level" : it.kind, digits); } };
    o.chart = LW.createChart(chartBox, opts);
    var fmt = { type: "price", precision: digits, minMove: Math.pow(10, -digits) };
    o.series = o.chart.addSeries(LW.LineSeries, {
      color: lineColor, lineWidth: 2, priceFormat: fmt,
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerRadius: 3, autoscaleInfoProvider: including(it.base)
    });
    // 拼接的图：折算的点（一般不会有）也并进同一条线
    o.series.setData(joined ? seriesData(pv.values, pv.times, true) : seriesData(real, S.slots));
    if (!joined && it.fill && it.fill.length) {
      var fv = {};
      it.fill.forEach(function (p) { fv[p[0]] = p[1]; });
      var fs = o.chart.addSeries(LW.LineSeries, {
        color: lineColor, lineWidth: 1, lineStyle: LW.LineStyle.Dashed, priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false, priceFormat: fmt, autoscaleInfoProvider: including(it.base)
      });
      fs.setData(seriesData(fv, S.slots));
      var note = el("div", "tr-proxy", "虚线：收益率没有数据的时段，按 " + it.proxy + " 期货折算" +
                    (it.beta_bp !== null && it.beta_bp !== undefined ? "（期货涨 1% ≈ " + fmtChange(it.beta_bp, "bp", 1) + "）" : ""));
      box.appendChild(note);
    }
    if (it.base !== null && it.base !== undefined) {
      o.series.createPriceLine({ price: it.base, color: c.muted, lineWidth: 1, lineStyle: LW.LineStyle.Dashed,
                                 axisLabelVisible: false, title: "" });  // 窗口起点
    }
    o.marks = alertMarkers(pv.times, c, false, it.holding ? it.symbol : null);
    if (o.marks.length && LW.createSeriesMarkers) LW.createSeriesMarkers(o.series, o.marks);
    wireCrosshair(o);
    wireSelect(o);
    S.charts.push(o);
    return box;
  }

  /** 十字线读数：时间、实际值、较起点的变化。t 落在这个变量没交易的时段：写“没有交易”。 */
  function showHover(o, t) {
    if (!o.hover) return;
    if (t === null || t === undefined) { o.hover.textContent = ""; return; }
    var at = pointAt(o, t);
    if (at === undefined) { o.hover.textContent = wall(t) + " 没有交易"; return; }
    var v = o.values[at];
    o.hover.textContent = wall(at) + " " + fmtValue(v, o.item.kind) + " " + fmtChange(changeOf(o.item, v), o.item.unit) +
                          (o.implied && o.implied[at] ? "（期货折算）" : "");
  }

  function wireCrosshair(o) {
    o.box.addEventListener("mouseenter", function () { S.active = o; });
    o.box.addEventListener("mouseleave", function () {
      if (S.active !== o) return;
      S.active = null;
      S.charts.forEach(function (x) {
        if (x !== o) x.chart.clearCrosshairPosition();
        showHover(x, null);
      });
    });
    o.chart.subscribeCrosshairMove(function (p) {
      if (S.active !== o) return;  // 只从鼠标所在的那张图往外同步（setCrosshairPosition 也会触发这个事件）
      var t = p && p.time;
      S.charts.forEach(function (x) {
        if (t === undefined || t === null) {
          if (x !== o) x.chart.clearCrosshairPosition();
          showHover(x, null);
          return;
        }
        if (x !== o) {
          // 共用时间格的图：十字线放在 t 那一格；拼接的图只有自己的点，放在 t 之前最近的那个点上
          var at = pointAt(x, t);
          if (at === undefined) x.chart.clearCrosshairPosition();
          else x.chart.setCrosshairPosition(x.values[at], x.joined ? at : t, x.series);
        }
        showHover(x, t);
      });
    });
  }

  // ───── 拖选一段：只算这一段谁动了 ─────

  /** 图上 x 坐标 → 时间。横轴是第几个点（逻辑下标），按这张图自己的横轴（共用时间格或拼接图自己的点）换算。 */
  function slotAt(o, x) {
    var axis = o.axis || S.slots;
    if (!axis.length) return null;
    var lg = o.chart.timeScale().coordinateToLogical(x);
    if (lg === null || lg === undefined) return null;
    var i = Math.max(0, Math.min(axis.length - 1, Math.round(lg)));
    return axis[i];
  }

  /** 选区在这张图横轴上的下标 [i1, i2]：起点取 >= from 的第一个点，终点取 <= to 的最后一个点。
      拼接的图在选区里一个点都没有（选区落在它没交易的时段）时 null，不画选区。 */
  function bandIndex(o, from, to) {
    var axis = o.axis || S.slots;
    if (!axis.length) return null;
    var i1 = lowerBound(axis, from), i2 = lowerBound(axis, to + 1) - 1;
    if (i1 > i2) return null;
    return [i1, i2];
  }

  function band(o, cls) {
    var b = o.box.querySelector("." + cls);
    if (!b) { b = el("div", "sel-band " + cls); o.box.appendChild(b); }
    return b;
  }

  function placeBand(b, x1, x2) {
    b.style.left = Math.min(x1, x2) + "px";
    b.style.width = Math.max(1, Math.abs(x2 - x1)) + "px";
    b.classList.remove("hidden");
  }

  function paintBands() {
    S.charts.concat(S.overlay).forEach(function (o) {
      var b = o.box.querySelector(".sel-keep");
      if (!S.sel) { if (b) b.classList.add("hidden"); return; }
      var ix = bandIndex(o, S.sel.from, S.sel.to);
      if (!ix) { if (b) b.classList.add("hidden"); return; }
      var ts = o.chart.timeScale();
      var x1 = ts.logicalToCoordinate(ix[0]), x2 = ts.logicalToCoordinate(ix[1]);
      if (x1 === null || x2 === null) return;
      placeBand(band(o, "sel-keep"), x1, x2);
    });
  }

  function wireSelect(o) {
    var start = null;
    function relX(e) {
      var r = o.box.getBoundingClientRect();
      var w = o.chart.timeScale().width();
      return Math.max(0, Math.min(w, e.clientX - r.left));
    }
    function move(e) {
      if (!start) return;
      var x = relX(e);
      if (Math.abs(x - start.x) < 4) return;
      placeBand(band(o, "sel-drag"), start.x, x);
    }
    function up(e) {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      if (!start) return;
      var s = start, x = relX(e);
      start = null;
      var b = o.box.querySelector(".sel-drag");
      if (b) b.classList.add("hidden");
      if (Math.abs(x - s.x) < 4) { clickAt(o, x); return; }
      var t2 = slotAt(o, x);
      if (s.t === null || t2 === null || t2 === s.t) return;
      select(Math.min(s.t, t2), Math.max(s.t, t2));
    }
    o.box.addEventListener("pointerdown", function (e) {
      if (e.button !== 0 || e.pointerType === "touch") return;  // 触屏上拖动留给页面滚动
      var x = relX(e);
      start = { x: x, t: slotAt(o, x) };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    });
  }

  /** 单击：离异动标记 8 像素以内就打开那条异动。 */
  function clickAt(o, x) {
    var best = null, bestD = 9;
    (o.marks || []).forEach(function (m) {
      var mx = o.chart.timeScale().timeToCoordinate(m.time);
      if (mx === null) return;
      var dd = Math.abs(mx - x);
      if (dd < bestD) { bestD = dd; best = m; }
    });
    if (best) window.location.href = "/alerts/" + encodeURIComponent(best.alertId);
  }

  function select(from, to) {
    S.sel = { from: from, to: to };
    paintBands();
    loadMovers();
  }

  if (resetBtn) resetBtn.addEventListener("click", function () { S.sel = null; paintBands(); loadMovers(); });

  // ───── 对比视图 ─────

  var MAX_CMP = 8;

  function itemByKey(key) {
    var hit = null;
    allItems().forEach(function (it) { if (it.key === key) hit = it; });
    return hit;
  }

  function hasData(it) { return !!((it.points && it.points.length) || (it.fill && it.fill.length)); }

  function cmpColor(c, key) {
    var pal = (c.series && c.series.length) ? c.series : [c.accent, c.amber, c.purple, c.teal];
    var slot = S.cmpSlot.hasOwnProperty(key) ? S.cmpSlot[key] : 0;
    return pal[slot % pal.length] || c.accent;
  }

  /** 换一组对比变量：去重、最多 MAX_CMP 个；还在的保留原来的颜色，新加的取最小的空闲颜色。 */
  function setCmp(keys) {
    var seen = {}, out = [];
    keys.forEach(function (k) { if (k && !seen[k] && out.length < MAX_CMP) { seen[k] = 1; out.push(k); } });
    var slots = {}, used = {};
    out.forEach(function (k) { if (S.cmpSlot.hasOwnProperty(k)) { slots[k] = S.cmpSlot[k]; used[S.cmpSlot[k]] = 1; } });
    out.forEach(function (k) {
      if (slots.hasOwnProperty(k)) return;
      var n = 0;
      while (used[n]) n++;
      slots[k] = n;
      used[n] = 1;
    });
    S.cmp = out;
    S.cmpSlot = slots;
  }

  function saveCmp() {
    if (T.store) {
      T.store.set("tsen.macro.cmp", S.cmp.length ? S.cmp.join(",") : "");
      T.store.set("tsen.macro.cmpslots", S.cmp.length ? JSON.stringify(S.cmpSlot) : null);
      T.store.set("tsen.macro.norm", S.norm === "sigma" ? "sigma" : null);
    }
    try {
      var params = new URLSearchParams(window.location.search);
      if (S.cmp.length) params.set("cmp", S.cmp.join(",")); else params.delete("cmp");
      if (S.norm === "sigma") params.set("norm", "sigma"); else params.delete("norm");
      var qs = params.toString();
      window.history.replaceState(null, "", window.location.pathname + (qs ? "?" + qs : "") + window.location.hash);
    } catch (e) { /* 老浏览器：不改网址 */ }
  }

  function loadCmp() {
    var params = new URLSearchParams(window.location.search);
    var raw = params.get("cmp");
    if (raw === null && T.store) raw = T.store.get("tsen.macro.cmp");
    if (raw !== null && raw !== undefined) {
      S.cmpTouched = true;
      // 上次的颜色序号：同一个变量刷新后还是同一个颜色（setCmp 保留已有的序号）
      try {
        var slots = JSON.parse((T.store && T.store.get("tsen.macro.cmpslots")) || "{}") || {};
        Object.keys(slots).forEach(function (k) {
          var n = Number(slots[k]);
          if (isFinite(n) && n >= 0 && n < MAX_CMP) S.cmpSlot[k] = n;
        });
      } catch (e) { /* 坏的记录：按顺序重新分配 */ }
      setCmp(String(raw).split(",").map(function (k) { return k.trim(); }).filter(Boolean));
    }
    var n = params.get("norm") || (T.store ? T.store.get("tsen.macro.norm") : null);
    S.norm = n === "sigma" ? "sigma" : "raw";
  }

  function cmpButtonState(btn, key) {
    var on = S.cmp.indexOf(key) >= 0;
    btn.textContent = on ? "✓ 对比" : "＋对比";
    btn.classList.toggle("on", on);
    btn.title = on ? "已在对比图里，点一下拿掉" : "加进对比图（最多 " + MAX_CMP + " 个）";
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  }

  /** 选择变了：更新按钮、计数、排行的标记；对比视图开着就重画。 */
  function cmpChanged() {
    saveCmp();
    document.querySelectorAll("[data-cmp-key]").forEach(function (b) { cmpButtonState(b, b.getAttribute("data-cmp-key")); });
    var cnt = $("cmp-count");
    if (cnt) cnt.textContent = S.cmp.length ? " · " + S.cmp.length : "";
    if (S.movers) renderMovers();
    if (S.layout === "compare" && S.series) renderCompare();
  }

  function toggleCmp(key) {
    var i = S.cmp.indexOf(key);
    if (i >= 0) {
      setCmp(S.cmp.slice(0, i).concat(S.cmp.slice(i + 1)));
    } else {
      if (S.cmp.length >= MAX_CMP) { if (T.toast) T.toast("最多对比 " + MAX_CMP + " 个，先拿掉一个"); return; }
      setCmp(S.cmp.concat([key]));
      var it = itemByKey(key);
      if (S.layout !== "compare" && T.toast && it) T.toast("已把“" + it.name + "”加进对比图（" + S.cmp.length + " 个）");
    }
    S.cmpTouched = true;
    cmpChanged();
  }

  /** 记下的变量里有已经不存在的（删了持仓、主题停用、手改的网址）：去掉，免得占着名额又没法单独拿掉。 */
  function pruneCmp() {
    var live = {};
    allItems().forEach(function (it) { live[it.key] = 1; });
    var kept = S.cmp.filter(function (k) { return live[k]; });
    if (kept.length === S.cmp.length) return;
    setCmp(kept);
    saveCmp();
    var cnt = $("cmp-count");
    if (cnt) cnt.textContent = S.cmp.length ? " · " + S.cmp.length : "";
  }

  /** 第一次打开（没选过）：放“谁动了”的前 3 名。 */
  function defaultCmp() {
    if (S.cmpTouched || S.cmp.length || !S.movers || S.sel) return;
    var keys = (S.movers.items || []).filter(function (m) { return m.z !== null && m.z !== undefined; })
      .slice(0, 3).map(function (m) { return m.key; });
    if (!keys.length) return;
    setCmp(keys);
    document.querySelectorAll("[data-cmp-key]").forEach(function (b) { cmpButtonState(b, b.getAttribute("data-cmp-key")); });
    var cnt = $("cmp-count");
    if (cnt) cnt.textContent = " · " + S.cmp.length;
    renderMovers();
    if (S.layout === "compare" && S.series) renderCompare();
  }

  /** 对比图的数值文字：% 两位小数、bp 整数、σ 一位小数。 */
  function fmtNorm(v, unitKey, axis) {
    if (unitKey === "sigma") {
      if (v === null || v === undefined || !isFinite(v)) return "—";
      var r = Number(Number(v).toFixed(axis ? 1 : 1));
      return (r > 0 ? "+" : r < 0 ? MINUS : "") + Math.abs(r).toFixed(1) + "σ";
    }
    return axis ? axisFormatter(unitKey)(v) : fmtChange(v, unitKey);
  }

  function tagOf(it) {
    if (it.holding) return "持仓";
    if (it.futures) return it.sub_tag || "期货";
    return null;
  }

  function renderPicks(c) {
    var box = $("cmp-picks");
    if (!box) return;
    box.textContent = "";
    S.series.groups.forEach(function (g) {
      var row = el("div", "cmp-group");
      row.appendChild(el("span", "cmp-gname", g.name));
      g.items.forEach(function (it) {
        var on = S.cmp.indexOf(it.key) >= 0, ok = hasData(it);
        var b = el("button", "cmp-chip" + (on ? " on" : ""));
        b.type = "button";
        b.disabled = !ok && !on;
        b.setAttribute("aria-pressed", on ? "true" : "false");
        b.title = ok ? (on ? "点一下从对比图里拿掉" : "加进对比图") : "这段时间没有数据";
        var dot = el("i", "cmp-dot");
        if (on) dot.style.background = cmpColor(c, it.key);
        b.appendChild(dot);
        b.appendChild(el("span", "", it.name));
        b.addEventListener("click", function () { toggleCmp(it.key); });
        row.appendChild(b);
      });
      box.appendChild(row);
    });
  }

  /** 两根纵轴都有线时，把两边的范围往外扩到 0 在同一高度（只扩不缩），免得两条 0 线错开、看着像两个基准。
      返回 {left: [lo, hi], right: [lo, hi]}；只有一根纵轴时 {}（交给图表库自己定，再加上 0）。 */
  function alignZero(span, both) {
    if (!both) return {};
    var frac = {}, keys = ["left", "right"];
    keys.forEach(function (k) {
      var lo = span[k][0], hi = span[k][1];
      if (hi - lo <= 0) { lo = -1; hi = 1; }
      span[k] = [lo, hi];
      frac[k] = hi / (hi - lo);  // 0 以上占整个范围的比例
    });
    var f = (frac.left + frac.right) / 2;
    f = Math.min(0.95, Math.max(0.05, f));
    var out = {};
    keys.forEach(function (k) {
      var lo = span[k][0], hi = span[k][1];
      // 让 hi / (hi − lo) = f：0 以上不够就加高 hi，否则加深 lo
      if (hi / (hi - lo) < f) hi = -lo * f / (1 - f); else lo = -hi * (1 - f) / f;
      out[k] = [lo, hi];
    });
    return out;
  }

  function renderCompare() {
    destroy(S.overlay);
    var c = colors(), box = $("cmp-chart"), legend = $("cmp-legend");
    renderPicks(c);
    box.textContent = "";
    legend.textContent = "";
    var sigma = S.norm === "sigma";
    document.querySelectorAll("[data-norm]").forEach(function (b) {
      b.classList.toggle("on", b.getAttribute("data-norm") === (sigma ? "sigma" : "raw"));
    });
    var items = S.cmp.map(itemByKey).filter(function (it) { return it && hasData(it); });
    if (!items.length) {
      message(box, S.cmp.length ? "选的变量这段时间都没有数据" : "还没有选变量",
              "点上面的标签，把想比较的变量加进来（最多 " + MAX_CMP + " 个）");
      return;
    }
    if (!LW) { message(box, "图表库没有加载", "刷新页面再试", true); return; }
    var drawn = items.filter(function (it) { return !sigma || it.sigma_day > 0; });
    var hasLeft = !sigma && drawn.some(function (it) { return it.unit === "bp"; });
    var hasRight = drawn.some(function (it) { return sigma || it.unit !== "bp"; });
    var opts = chartOptions(c, "%", false);
    delete opts.localization;  // 两根纵轴单位不同：各用各的 priceFormat
    opts.leftPriceScale = { visible: hasLeft, borderVisible: false, scaleMargins: { top: 0.12, bottom: 0.1 } };
    opts.rightPriceScale.visible = hasRight || !hasLeft;
    // 每次重画换一个新的子元素：wireSelect 挂的事件处理跟着旧元素一起丢掉，不会越点越多
    var host = el("div", "cmp-host");
    box.appendChild(host);
    var o = { box: host, lines: [], marks: [], axis: S.slots };
    o.chart = LW.createChart(host, opts);
    var zeroOn = {}, placed = {};
    // 每条线的数值（较起点的变化，或除以日波动）先算好：两根纵轴的范围要据此对齐 0
    var valuesOf = {}, span = { left: [0, 0], right: [0, 0] };
    items.forEach(function (it) {
      if (sigma && !(it.sigma_day > 0)) return;
      var pv = pointsOf(it), values = {}, sc = !sigma && it.unit === "bp" ? "left" : "right";
      pv.times.forEach(function (t) {
        var ch = changeOf(it, pv.values[t]);
        if (ch === null) return;
        var v = sigma ? ch / it.sigma_day : ch;
        values[t] = v;
        span[sc][0] = Math.min(span[sc][0], v);
        span[sc][1] = Math.max(span[sc][1], v);
      });
      valuesOf[it.key] = { values: values, times: pv.times };
    });
    var ranges = alignZero(span, hasLeft && hasRight);
    items.forEach(function (it) {
      var color = cmpColor(c, it.key);
      var bp = !sigma && it.unit === "bp";
      var unitKey = sigma ? "sigma" : it.unit;
      var li = el("span", "lg-item cmp-li");
      var sw = el("span", "lg-sw");
      sw.style.borderTopColor = color;
      sw.style.borderTopStyle = bp ? "dashed" : "solid";
      li.appendChild(sw);
      var tag = tagOf(it);
      li.appendChild(el("span", "", it.name + (tag && it.name.indexOf(tag) < 0 ? "（" + tag + "）" : "")));
      var val = el("span", "lg-val", "");
      li.appendChild(val);
      var x = el("button", "cmp-x", "×");
      x.type = "button";
      x.title = "从对比图里拿掉";
      x.setAttribute("aria-label", "拿掉 " + it.name);
      x.addEventListener("click", function () { toggleCmp(it.key); });
      li.appendChild(x);
      legend.appendChild(li);
      if (sigma && !(it.sigma_day > 0)) {
        val.textContent = "算不出平常的波动（历史太短），没画";
        return;
      }
      var pv = valuesOf[it.key], values = pv.values;
      var scaleId = bp ? "left" : "right";
      var rng = ranges[scaleId];
      var s = o.chart.addSeries(LW.LineSeries, {
        color: color, lineWidth: 2, lineStyle: bp ? LW.LineStyle.Dashed : LW.LineStyle.Solid, priceScaleId: scaleId,
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerRadius: 3,
        autoscaleInfoProvider: rng ? function () { return { priceRange: { minValue: rng[0], maxValue: rng[1] } }; } : withZero,
        priceFormat: { type: "custom", minMove: 0.01, formatter: function (v) { return fmtNorm(v, unitKey, true); } }
      });
      s.setData(seriesData(values, S.slots));
      if (!zeroOn[scaleId]) {
        zeroOn[scaleId] = 1;
        s.createPriceLine({ price: 0, color: c.muted, lineWidth: 1, lineStyle: LW.LineStyle.Dashed, axisLabelVisible: false, title: "" });
      }
      // 每条异动画在第一条那时有数据的线上（不是一律画在第一条线上：它可能那时没交易）
      var mk = alertMarkers(pv.times, c, true, null, placed);
      if (mk.length) {
        mk.forEach(function (m) { placed[m.alertId] = 1; });
        o.marks = o.marks.concat(mk);
        if (LW.createSeriesMarkers) LW.createSeriesMarkers(s, mk);
      }
      var last = it.change === null || it.change === undefined ? null : (sigma ? it.change / it.sigma_day : it.change);
      val.textContent = fmtNorm(last, unitKey);
      o.lines.push({ series: s, item: it, val: val, unitKey: unitKey, last: last });
    });
    o.chart.subscribeCrosshairMove(function (p) {
      o.lines.forEach(function (l) {
        var v = p && p.time && p.seriesData ? p.seriesData.get(l.series) : null;
        l.val.textContent = v && v.value !== undefined ? fmtNorm(v.value, l.unitKey) : fmtNorm(l.last, l.unitKey);
      });
    });
    wireSelect(o);
    S.overlay.push(o);
    refit(S.overlay);
  }

  document.querySelectorAll("[data-norm]").forEach(function (b) {
    b.addEventListener("click", function () {
      S.norm = b.getAttribute("data-norm") === "sigma" ? "sigma" : "raw";
      saveCmp();
      if (S.series) renderCompare();
    });
  });
  var clearBtn = $("cmp-clear");
  if (clearBtn) clearBtn.addEventListener("click", function () { setCmp([]); S.cmpTouched = true; cmpChanged(); });

  function setLayout(layout) {
    S.layout = layout === "compare" || layout === "overlay" ? "compare" : "grid";
    document.querySelectorAll("[data-layout]").forEach(function (b) {
      b.classList.toggle("on", b.getAttribute("data-layout") === S.layout);
    });
    gridBox.classList.toggle("hidden", S.layout !== "grid");
    overlayBox.classList.toggle("hidden", S.layout !== "compare");
    if (T.store) T.store.set("tsen.macro.layout", S.layout === "grid" ? null : S.layout);
    if (S.series) renderSeries();
  }

  document.querySelectorAll("[data-layout]").forEach(function (b) {
    b.addEventListener("click", function () { setLayout(b.getAttribute("data-layout")); });
  });

  // ───── 窗口里的异动 ─────

  function renderAlerts(list) {
    if (!alertsBox) return;
    alertsBox.textContent = "";
    if (!list.length) { alertsBox.appendChild(el("li", "muted small", "这段时间没有异动")); return; }
    list.forEach(function (a) {
      var li = el("li");
      var link = el("a");
      link.href = "/alerts/" + encodeURIComponent(a.id);
      var up = a.direction > 0;
      link.appendChild(el("span", up ? "up" : "down", up ? "▲" : "▼"));
      link.appendChild(el("strong", "", a.name || a.symbol));
      link.appendChild(el("span", "num muted", wall(a.t, true)));
      if (a.move_pct !== null && a.move_pct !== undefined) {
        link.appendChild(el("span", "num " + ud(a.move_pct, "%"), fmtChange(a.move_pct, "%")));
      }
      li.appendChild(link);
      alertsBox.appendChild(li);
    });
  }

  // ───── 谁动了 ─────

  function moversQuery() {
    if (!S.sel) return QUERY;
    // 时间格 t 上的点是 [t, t + 步长) 里最后一根：起点取格子结束（之前最后一根就是这个点），终点取格子里最后一分钟
    var step = ((S.series && S.series.step_minutes) || 1) * 60;
    var from = S.sel.from + step, to = S.sel.to + step - 60;
    if (to <= from) to = from + 60;
    return "start=" + encodeURIComponent(naiveIso(from)) + "&end=" + encodeURIComponent(naiveIso(to));
  }

  function loadMovers() {
    var seq = ++S.moversSeq;
    if (resetBtn) resetBtn.classList.toggle("hidden", !S.sel);
    moversRange.textContent = S.sel
      ? "所选：" + wall(S.sel.from, true) + " – " + wall(S.sel.to, !sameDay(S.sel.from, S.sel.to))
      : "";
    moversBox.classList.add("loading");
    getJSON("/api/macro/movers?" + moversQuery()).then(function (d) {
      if (seq !== S.moversSeq) return;
      S.movers = d;
      renderMovers();
      defaultCmp();
    }).catch(function (err) {
      if (seq !== S.moversSeq) return;
      moversBox.textContent = "";
      moversBox.appendChild(el("div", "mv-msg err", "取排行失败：" + (errText(err) || "网络错误")));
    }).then(function () { moversBox.classList.remove("loading"); });
  }

  function renderMovers() {
    var d = S.movers;
    moversBox.textContent = "";
    if (!S.sel) moversRange.textContent = (d.name || "") + "（整个窗口）";
    var items = d.items || [];
    if (!items.length) {
      moversBox.appendChild(el("div", "mv-msg", "这段时间里分钟线库没有数据，排不出来"));
      return;
    }
    var withZ = items.filter(function (i) { return i.z !== null && i.z !== undefined; });
    var noZ = items.filter(function (i) { return i.z === null || i.z === undefined; });
    var zmax = 3;
    withZ.forEach(function (i) { zmax = Math.max(zmax, Math.abs(i.z)); });
    withZ.forEach(function (i) { moversBox.appendChild(moverRow(i, zmax)); });
    if (noZ.length) {
      moversBox.appendChild(el("div", "mv-sep", "下面几个算不出 z（这段时间没有交易，或历史太短），只列变化："));
      noZ.forEach(function (i) { moversBox.appendChild(moverRow(i, zmax)); });
    }
  }

  function moverRow(it, zmax) {
    var hasZ = it.z !== null && it.z !== undefined;
    var az = hasZ ? Math.abs(it.z) : 0;
    var row = el("div", "mv-row" + (hasZ ? (az >= 2 ? " strong" : az < 1 ? " weak" : "") : " nz"));
    var tip = it.name + "（" + it.symbol + "）：" + fmtChange(it.change, it.unit);
    if (it.minutes) tip += "，有成交 " + it.minutes + " 分钟";
    tip += hasZ ? "，z = " + it.z.toFixed(2) : (it.minutes === 0 ? "，这段时间没有交易" : "，历史太短，算不出平常波动");
    if (it.proxy) tip += "；含 " + it.proxy + " 期货折算（收益率没有数据的时段）";
    row.title = tip;
    var name = el("div", "mv-name", it.name);
    var tg = tagOf(it);
    if (tg) name.appendChild(el("span", "tag" + (it.holding ? " tag-hold" : ""), tg));
    row.appendChild(name);
    var inCmp = S.cmp.indexOf(it.key) >= 0;
    if (inCmp) {
      row.classList.add("in-cmp");
      row.style.setProperty("--cmp-color", cmpColor(colors(), it.key));
    }
    row.setAttribute("data-key", it.key);
    var bar = el("div", "mv-bar");
    if (hasZ) {
      bar.appendChild(el("i", "mid"));
      var off = Math.min(2 / zmax, 1) * 50;
      var t1 = el("i", "thr"), t2 = el("i", "thr");
      t1.style.left = (50 - off) + "%";
      t2.style.left = (50 + off) + "%";
      bar.appendChild(t1);
      bar.appendChild(t2);
      var w = Math.min(az / zmax, 1) * 50;
      var fill = el("i", "fill " + (it.z > 0 ? "up" : it.z < 0 ? "down" : "flat"));
      fill.style.width = Math.max(w, 0.5) + "%";
      fill.style.left = (it.z >= 0 ? 50 : 50 - w) + "%";
      bar.appendChild(fill);
    }
    row.appendChild(bar);
    row.appendChild(el("div", "mv-chg " + ud(it.change, it.unit), fmtChange(it.change, it.unit)));
    var zs = hasZ ? (it.z > 0 ? "+" : it.z < 0 ? MINUS : "") + Math.abs(it.z).toFixed(1) : "—";
    row.appendChild(el("div", "mv-z", hasZ ? "z " + zs : zs));
    row.title = tip + (S.layout === "compare" ? "\n点一下：" + (inCmp ? "从对比图里拿掉" : "加进对比图")
                                            : "\n点一下：跳到这张小图（切到“对比”视图后点一下是加进对比图）");
    row.addEventListener("click", function () {
      if (S.layout === "compare") { toggleCmp(it.key); return; }
      var target = gridBox.querySelector('.tr-card[data-key="' + (window.CSS && CSS.escape ? CSS.escape(it.key) : it.key) + '"]');
      if (target) target.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    return row;
  }

  // ───── 数据新鲜度、补抓 ─────

  function renderFresh() {
    var d = S.series;
    var f = (d && d.freshness) || {};
    var parts = [];
    parts.push(d && d.data_until ? "数据截至 " + fmtReal(d.data_until) : "这段时间里没有数据");
    if (!f.running) parts.push("tsen run 没在运行，不会自动更新");
    else parts.push("tsen run 在运行，会定时补库（刷新页面看最新）");
    var bf = f.collector && f.collector.backfilling;
    if (bf > 0) parts.push("正在后台补 " + bf + " 个代码的历史，过几分钟再刷新");
    freshEl.textContent = parts.join(" · ");
  }

  var refreshBtn = $("mc-refresh"), refreshNote = $("mc-refresh-note");
  if (refreshBtn && root.getAttribute("data-can-refresh") === "1") {
    refreshBtn.addEventListener("click", function () {
      refreshBtn.disabled = true;
      refreshNote.textContent = "正在补抓…";
      getJSON("/api/bars/refresh", { method: "POST" }).then(function (r) {
        var msg = "补抓完成，写入 " + (r.rows || 0) + " 行";
        if (r.backfilling > 0) msg += "；正在后台补历史，过几分钟再刷新";
        refreshNote.textContent = msg;
        loadAll();
      }).catch(function (err) {
        refreshNote.textContent = "补抓失败：" + (errText(err) || "网络错误");
      }).then(function () { refreshBtn.disabled = false; });
    });
  }

  // ───── 收益率曲线 ─────

  function renderCurve() {
    destroy(S.curveCharts);
    var d = S.curve, c = colors();
    var lvBox = $("cv-levels"), spBox = $("cv-spread");
    if (!lvBox || !spBox || !d) return;
    lvBox.textContent = "";
    spBox.textContent = "";
    $("cv-legend").textContent = "";
    $("cv-legend-spread").textContent = "";
    if (!d.days || !d.days.length) {
      message(lvBox, "还没有收益率曲线数据", "每天由 tsen run 从财政部取一次");
      message(spBox, "还没有收益率曲线数据", "每天由 tsen run 从财政部取一次");
      return;
    }
    if (!LW) { message(lvBox, "图表库没有加载", "", true); message(spBox, "图表库没有加载", "", true); return; }
    var series = d.series || {};
    var pct = function (v) { return Number(v).toFixed(2) + "%"; };
    var bp = function (v) { return fmtChange(v, "bp", 1); };
    curveChart(lvBox, $("cv-legend"), [["2Y", c.amber], ["10Y", c.accent], ["30Y", c.purple]], series, pct, c, false);
    curveChart(spBox, $("cv-legend-spread"), [["2s10s", c.teal]], series, bp, c, true);
  }

  function curveChart(box, legend, defs, series, fmt, c, zero) {
    defs = defs.filter(function (x) { return series[x[0]] && series[x[0]].length; });
    if (!defs.length) { message(box, "没有这几个期限的数据", ""); return; }
    var opts = chartOptions(c, "%", false);
    opts.localization = { priceFormatter: fmt };
    opts.timeScale.timeVisible = false;
    var chart = LW.createChart(box, opts);
    var lines = [];
    defs.forEach(function (x, i) {
      var s = chart.addSeries(LW.LineSeries, { color: x[1], lineWidth: 2, priceLineVisible: false, lastValueVisible: true,
                                               crosshairMarkerRadius: 3,
                                               autoscaleInfoProvider: zero ? withZero : undefined });
      var pts = series[x[0]];
      s.setData(pts.map(function (p) { return p[1] === null ? { time: p[0] } : { time: p[0], value: p[1] }; }));
      if (zero && i === 0) s.createPriceLine({ price: 0, color: c.muted, lineWidth: 1, lineStyle: LW.LineStyle.Dashed, axisLabelVisible: false, title: "" });
      var last = null;
      for (var k = pts.length - 1; k >= 0; k--) { if (pts[k][1] !== null) { last = pts[k][1]; break; } }
      var item = el("span", "lg-item");
      var sw = el("span", "lg-sw");
      sw.style.borderTopColor = x[1];
      item.appendChild(sw);
      item.appendChild(el("span", "", x[0]));
      var val = el("span", "lg-val", last === null ? "—" : fmt(last));
      item.appendChild(val);
      legend.appendChild(item);
      lines.push({ series: s, val: val, last: last });
    });
    chart.subscribeCrosshairMove(function (p) {
      lines.forEach(function (l) {
        var v = p && p.time && p.seriesData ? p.seriesData.get(l.series) : null;
        l.val.textContent = v && v.value !== undefined ? fmt(v.value) : (l.last === null ? "—" : fmt(l.last));
      });
    });
    chart.timeScale().fitContent();
    S.curveCharts.push({ chart: chart });
  }

  // ───── 读取 ─────

  function loadAll() {
    getJSON("/api/macro/series?" + QUERY).then(function (d) {
      S.series = d;
      pruneCmp();
      renderFresh();
      try { renderSeries(); } catch (err) { message(gridBox, "画图出错", errText(err), true); }
    }).catch(function (err) {
      freshEl.textContent = "读取分钟线库失败";
      if (S.layout === "compare") setLayout("grid");
      message(gridBox, "取趋势数据失败", errText(err) || "网络错误", true);
      if (alertsBox) { alertsBox.textContent = ""; alertsBox.appendChild(el("li", "muted small", "—")); }
    });
    loadMovers();
  }

  function loadCurve() {
    getJSON("/api/macro/curve?days=180").then(function (d) {
      S.curve = d;
      try { renderCurve(); } catch (err) { message($("cv-levels"), "画图出错", errText(err), true); }
    }).catch(function (err) {
      message($("cv-levels"), "取收益率曲线失败", errText(err) || "网络错误", true);
      $("cv-spread").textContent = "";
    });
  }

  // 配色（浅色 / 深色、红涨 / 绿涨）变了：用缓存的数据重画
  document.addEventListener("tsen:colors", function () {
    if (S.series) renderSeries();
    if (S.curve) renderCurve();
  });

  var resizeTimer = null;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(paintBands, 120);
  });

  loadCmp();
  var cnt0 = $("cmp-count");
  if (cnt0) cnt0.textContent = S.cmp.length ? " · " + S.cmp.length : "";
  var saved = T.store ? T.store.get("tsen.macro.layout") : null;
  if (saved === "compare" || saved === "overlay") {
    S.layout = "compare";
    document.querySelectorAll("[data-layout]").forEach(function (b) {
      b.classList.toggle("on", b.getAttribute("data-layout") === "compare");
    });
    gridBox.classList.add("hidden");
    overlayBox.classList.remove("hidden");
  }
  loadAll();
  loadCurve();
})();
