/* 异动页的脚本：
   - 列表：筛选下拉框改了就提交；点整行打开详情（Ctrl / ⌘ 点击在新标签页打开）
   - 详情：从 /api/alerts/{id}/bars 取数据，用 Lightweight Charts 画这只资产、基准和同业较各自参照价的涨跌（%），
     标出异动时刻、这一波的起点、检测窗口起点和全天累计的档位；配色跟着浅色 / 深色、红涨 / 绿涨切换。
   只用 textContent / DOM 接口写不可信的文字。 */
(function () {
  "use strict";

  // ───── 列表 ─────
  document.querySelectorAll("form[data-autosubmit] select").forEach(function (sel) {
    sel.addEventListener("change", function () { sel.form.submit(); });
  });

  document.addEventListener("click", function (e) {
    var tr = e.target.closest("tr.clickable[data-href]");
    if (!tr || e.target.closest("a, button, input, select, textarea, label")) return;
    var href = tr.getAttribute("data-href");
    if (!href || href.charAt(0) !== "/") return;
    if (e.metaKey || e.ctrlKey) window.open(href, "_blank");
    else window.location.href = href;
  });

  // ───── 详情：走势图 ─────
  var box = document.getElementById("alert-chart");
  if (!box) return;
  var legend = document.getElementById("chart-legend");
  var note = document.getElementById("chart-note");
  var id = box.getAttribute("data-id");
  var LW = window.LightweightCharts;
  var T = window.tsen || {};

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function message(title, detail) {
    box.textContent = "";
    var m = el("div", "chart-msg");
    m.appendChild(el("strong", "", title));
    if (detail) m.appendChild(el("div", "", detail));
    box.appendChild(m);
    if (legend) legend.textContent = "";
  }

  function fmt(x) {
    if (x === null || x === undefined || isNaN(x)) return "—";
    return (x > 0 ? "+" : "") + Number(x).toFixed(2) + "%";
  }

  if (!LW) { message("图表库没有加载", "刷新页面再试"); return; }
  if (box.getAttribute("data-has-prices") === "0") {
    message("画不了走势", "没有行情数据源（预览或配置里没有启用）");
    return;
  }

  var chart = null, data = null, lines = [], markersApi = null;

  function palette(c) {
    // 这只资产用强调色、基准琥珀色，同业依次紫 / 青 / 灰
    return { symbol: c.accent, benchmark: c.amber, peers: [c.purple, c.teal, c.muted] };
  }

  function colorFor(s, i, c) {
    var p = palette(c);
    if (s.role === "symbol") return p.symbol;
    if (s.role === "benchmark") return p.benchmark;
    return p.peers[i % p.peers.length];
  }

  function chartOptions(c) {
    return {
      autoSize: true,
      layout: { background: { type: "solid", color: c.panel || "transparent" }, textColor: c.muted,
                fontFamily: getComputedStyle(document.body).fontFamily, attributionLogo: true },
      grid: { vertLines: { color: c.border }, horzLines: { color: c.border } },
      rightPriceScale: { borderColor: c.border, scaleMargins: { top: 0.12, bottom: 0.12 } },
      timeScale: { borderColor: c.border, timeVisible: true, secondsVisible: false },
      crosshair: { mode: LW.CrosshairMode ? LW.CrosshairMode.Normal : 0 },
      // 滚轮留给页面滚动；拖动平移、双指 / 拖坐标轴缩放照常
      handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
      handleScale: { mouseWheel: false, pinch: true, axisPressedMouseMove: true },
      localization: { priceFormatter: fmt }
    };
  }

  function markerList(c) {
    var out = [];
    if (!data) return out;
    // 起点、窗口起点只画小记号（不写字，说明在图下方），免得和异动箭头的文字挤在一起
    if (data.window_start) {
      out.push({ time: data.window_start, position: "inBar", color: c.muted, shape: "square", size: 0.5 });
    }
    if (data.anchor_time) {
      out.push({ time: data.anchor_time, position: "inBar", color: c.text, shape: "circle", size: 0.6 });
    }
    if (data.marker && data.marker.time) {
      var up = data.marker.direction > 0;
      out.push({ time: data.marker.time, position: up ? "belowBar" : "aboveBar", color: up ? c.up : c.down,
                 shape: up ? "arrowUp" : "arrowDown", text: data.marker.text || "异动" });
    }
    out.sort(function (a, b) { return a.time - b.time; });
    return out;
  }

  function applyColors() {
    if (!chart || !data) return;
    var c = T.colors ? T.colors() : {};
    chart.applyOptions(chartOptions(c));
    var peerIdx = 0;
    lines.forEach(function (l) {
      var col = colorFor(l.meta, l.meta.role === "peer" ? peerIdx++ : 0, c);
      l.series.applyOptions({ color: col });
      if (l.sw) l.sw.style.background = col;
    });
    var sym = lines[0];
    if (sym) {
      sym.priceLines.forEach(function (pl) {
        var o = pl.__opts;
        pl.applyOptions({ color: o.kind === "zero" ? c.muted : (data.marker && data.marker.direction > 0 ? c.up : c.down) });
      });
      if (markersApi) markersApi.setMarkers(markerList(c));
    }
  }

  function legendItem(l, fixed) {
    var item = el("label", "lg-item" + (fixed ? " fixed" : ""));
    if (!fixed) {
      var cb = el("input");
      cb.type = "checkbox";
      cb.checked = true;
      cb.addEventListener("change", function () { l.series.applyOptions({ visible: cb.checked }); });
      item.appendChild(cb);
    }
    l.sw = el("span", "lg-sw");
    item.appendChild(l.sw);
    var name = l.meta.label + (l.meta.role === "benchmark" ? "（基准）" : "");
    var txt = el("span", "", name);
    if (l.meta.name) item.title = l.meta.name;
    item.appendChild(txt);
    l.val = el("span", "lg-val", "");
    item.appendChild(l.val);
    return item;
  }

  function showLast() {
    lines.forEach(function (l) {
      var pts = l.meta.points;
      if (l.val) l.val.textContent = pts.length ? fmt(pts[pts.length - 1].value) : "—";
    });
  }

  function draw(d) {
    data = d;
    box.textContent = "";
    var c = T.colors ? T.colors() : {};
    chart = LW.createChart(box, chartOptions(c));
    var peerIdx = 0;
    lines = [];
    d.series.forEach(function (s) {
      if (!s.points || !s.points.length) return;
      var col = colorFor(s, s.role === "peer" ? peerIdx++ : 0, c);
      var series = chart.addSeries(LW.LineSeries, {
        color: col, lineWidth: s.role === "symbol" ? 2 : (s.role === "benchmark" ? 2 : 1),
        lineStyle: s.role === "peer" && LW.LineStyle ? LW.LineStyle.Solid : 0,
        priceLineVisible: false, lastValueVisible: s.role === "symbol", title: "",
        crosshairMarkerRadius: s.role === "symbol" ? 4 : 3
      });
      series.setData(s.points);
      lines.push({ meta: s, series: series, priceLines: [] });
    });
    var sym = lines[0];
    if (!sym) { message("没有可画的数据", ""); return; }
    // 参照价（0%）与全天累计的档位
    var zero = sym.series.createPriceLine({ price: 0, color: c.muted, lineWidth: 1, lineStyle: LW.LineStyle.Dotted,
                                            axisLabelVisible: false, title: d.ref_label || "参照" });
    zero.__opts = { kind: "zero" };
    sym.priceLines.push(zero);
    (d.levels || []).forEach(function (lv) {
      var pl = sym.series.createPriceLine({ price: lv.value, color: d.marker && d.marker.direction > 0 ? c.up : c.down,
                                            lineWidth: 1, lineStyle: LW.LineStyle.Dashed, axisLabelVisible: true,
                                            title: lv.title + (lv.hit ? "" : "（未到）") });
      pl.__opts = { kind: "level" };
      sym.priceLines.push(pl);
    });
    if (LW.createSeriesMarkers) markersApi = LW.createSeriesMarkers(sym.series, markerList(c));

    if (legend) {
      legend.textContent = "";
      lines.forEach(function (l, i) { legend.appendChild(legendItem(l, i === 0)); });
    }
    applyColors();
    showLast();
    chart.subscribeCrosshairMove(function (p) {
      if (!p || !p.time || !p.seriesData) { showLast(); return; }
      lines.forEach(function (l) {
        var v = p.seriesData.get(l.series);
        if (l.val) l.val.textContent = v && v.value !== undefined ? fmt(v.value) : "—";
      });
    });
    chart.timeScale().fitContent();

    if (note) {
      var parts = ["纵轴是较各自参照价的涨跌（%）：" + (sym.meta.label || "") + " " + (d.ref_label || "") + "，基准与同业按同一口径",
                   "时间为 " + (d.tz_label || "本地时间")];
      if (d.anchor_time) parts.push("圆点 = 这一波的起点");
      if (d.window_start) parts.push("方块 = " + (d.window_minutes ? d.window_minutes + " 分钟" : "") + "检测窗口的起点");
      if (d.ref_note) parts.push(d.ref_note);
      if (d.source) parts.push("分钟线来自" + d.source);
      note.textContent = parts.join("；") + "。";
    }
  }

  function load() {
    var fetcher = T.json ? T.json : function (u) { return fetch(u).then(function (r) { return r.json(); }); };
    fetcher("/api/alerts/" + encodeURIComponent(id) + "/bars").then(function (d) {
      if (!d || !d.available) {
        message("画不了走势", (d && d.reason) || "没有数据");
        return;
      }
      try { draw(d); } catch (err) { message("画图出错", String(err && err.message || err).slice(0, 120)); }
    }).catch(function (err) {
      message("取走势数据失败", String(err && err.message || err).slice(0, 120));
    });
  }

  document.addEventListener("tsen:colors", applyColors);
  load();
})();
