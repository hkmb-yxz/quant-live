/* quant-live 仪表盘前端逻辑 */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const state = { news: null, backtest: null, factors: null, options: null, status: null, optCfg: null };

  const REFRESH_SEC = 60;
  let charts = {};

  function pct(x) {
    if (x === null || x === undefined || isNaN(x)) return "—";
    return (x * 100).toFixed(1) + "%";
  }
  function num(x, d = 2) {
    if (x === null || x === undefined || isNaN(x)) return "—";
    return Number(x).toFixed(d);
  }

  async function fetchJSON(path) {
    try {
      const r = await fetch(path + "?t=" + Date.now(), { cache: "no-store" });
      if (!r.ok) return null;
      return await r.json();
    } catch (e) {
      return null;
    }
  }

  async function loadAll() {
    const [news, backtest, factors, options, status, optCfg] = await Promise.all([
      fetchJSON("data/news.json"),
      fetchJSON("data/backtest.json"),
      fetchJSON("data/factors.json"),
      fetchJSON("data/options_hits.json"),
      fetchJSON("data/status.json"),
      fetchJSON("config/options.json"),
    ]);
    Object.assign(state, { news, backtest, factors, options, status, optCfg });
    renderNews(); renderBacktest(); renderFactors(); renderOptions(); renderStatus(); renderBadge();
  }

  /* ---------- 顶部状态 ---------- */
  function renderBadge() {
    const badge = $("runBadge");
    if (!state.status || !state.status.updated_at) {
      badge.className = "badge badge-warn"; badge.textContent = "等待首次运行";
      return;
    }
    const at = new Date(state.status.updated_at.replace(" ", "T") + ":00+08:00");
    const ageMin = (Date.now() - at.getTime()) / 60000;
    const recent = state.status.last_runs || {};
    const anyOk = Object.values(recent).some((r) => r && r.ok);
    if (ageMin < 40 && anyOk) {
      badge.className = "badge badge-ok";
      badge.textContent = "● 运行正常 · " + state.status.updated_at;
    } else if (ageMin < 90) {
      badge.className = "badge badge-warn";
      badge.textContent = "◐ 数据稍旧 · " + state.status.updated_at;
    } else {
      badge.className = "badge badge-err";
      badge.textContent = "○ 数据过期 · " + state.status.updated_at;
    }
  }

  /* ---------- 新闻 ---------- */
  function renderNews() {
    const list = $("newsList");
    const meta = $("newsMeta");
    if (!state.news || !state.news.items || !state.news.items.length) {
      list.innerHTML = '<p class="empty">等待首次运行（每 15 分钟更新）…</p>';
      meta.textContent = "";
      return;
    }
    meta.textContent = `更新于 ${state.news.updated_at} · 共收录 ${state.news.count || state.news.items.length} 条 · AI 解读 ${state.news.ai_enabled ? "已启用" : "未启用（规则模式）"}`;
    const items = state.news.items.slice(0, 40);
    list.innerHTML = items.map((it) => {
      const sent = it.sentiment === "利好" ? "bull" : it.sentiment === "利空" ? "bear" : "flat";
      const stars = "★".repeat(Math.min(5, it.impact || 0)) + "☆".repeat(Math.max(0, 5 - (it.impact || 0)));
      const sectors = (it.sectors || []).map((s) => `<span class="tag sector">${esc(s)}</span>`).join("");
      const kws = (it.keywords || []).slice(0, 4).map((k) => `<span class="tag">${esc(k)}</span>`).join("");
      return `<div class="news-card">
        <div class="news-score"><div class="s">${it.score ?? 0}</div><div class="l">热度</div></div>
        <div class="news-body">
          <div class="news-title"><a href="${esc(it.link)}" target="_blank" rel="noopener">${esc(it.title_cn || it.title)}</a>
            ${it.title_cn && it.title_cn !== it.title ? `<span class="meta" style="font-size:11px">（${esc(it.title.slice(0, 80))}…）</span>` : ""}
          </div>
          ${it.summary ? `<div class="news-summary">${esc(it.summary)}</div>` : ""}
          <div class="news-tags"><span class="sent ${sent}">${esc(it.sentiment || "中性")}</span>
            <span class="stars" title="影响程度">${stars}</span>${sectors}${kws}
            <span class="tag">${esc(it.source || "")}</span>
            <span class="tag">${esc(it.first_seen || "")}</span>
          </div>
        </div>
      </div>`;
    }).join("");
  }

  /* ---------- 回测 ---------- */
  let btChart = null;
  function renderBacktest() {
    const bt = state.backtest;
    if (!bt || !bt.results || !Object.keys(bt.results).length) {
      $("btMetrics").innerHTML = ""; $("btRankTable").innerHTML = "";
      $("btTicker").innerHTML = ""; $("btStrategy").innerHTML = "";
      if (btChart) { btChart.destroy(); btChart = null; }
      $("btChart").style.display = "none"; $("btEmpty").hidden = false;
      return;
    }
    $("btChart").style.display = ""; $("btEmpty").hidden = true;
    const tickers = Object.keys(bt.results);
    const strategies = bt.strategies || [];
    const selT = $("btTicker"), selS = $("btStrategy");
    const curT = selT.value || tickers[0];
    const curS = selS.value || (strategies[0] && strategies[0].id);
    selT.innerHTML = tickers.map((t) => `<option ${t === curT ? "selected" : ""}>${t}</option>`).join("");
    selS.innerHTML = strategies.map((s) => `<option value="${s.id}" ${s.id === curS ? "selected" : ""}>${s.name}</option>`).join("");

    const m = (bt.results[curT] || {})[curS];
    const b = (bt.benchmark || {})[curT] || {};
    if (!m) {
      $("btMetrics").innerHTML = '<p class="empty">该组合暂无数据</p>';
      return;
    }
    const cards = [
      ["年化收益", pct(m.ann_return), m.ann_return >= 0 ? "pos" : "neg"],
      ["总收益", pct(m.total_return), m.total_return >= 0 ? "pos" : "neg"],
      ["夏普比率", num(m.sharpe), m.sharpe >= 0 ? "pos" : "neg"],
      ["最大回撤", pct(m.maxdd), "neg"],
      ["胜率", pct(m.win_rate), m.win_rate >= 0.5 ? "pos" : "neg"],
      ["盈亏比", num(m.pl_ratio), m.pl_ratio >= 1 ? "pos" : "neg"],
      ["交易次数", m.n_trades, ""],
      ["基准年化(B&H)", pct(b.ann_return), b.ann_return >= 0 ? "pos" : "neg"],
    ];
    $("btMetrics").innerHTML = cards.map(([k, v, cls]) =>
      `<div class="card"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`).join("");

    const labels = m.equity.map((p) => p[0]);
    const data = m.equity.map((p) => p[1]);
    if (btChart) btChart.destroy();
    btChart = new Chart($("btChart"), {
      type: "line",
      data: { labels, datasets: [{ label: `${curT} · ${(strategies.find(s => s.id === curS) || {}).name || curS} 净值`, data, borderColor: "#3fa7ff", backgroundColor: "rgba(63,167,255,.12)", fill: true, tension: .15, pointRadius: 0 }] },
      options: baseOptions(),
    });

    const rows = (bt.ranking || []).map((r) =>
      `<tr><td><b>${r.ticker}</b></td><td>${esc(r.strategy)}</td>
      <td class="${r.ann_return >= 0 ? "pos" : "neg"}">${pct(r.ann_return)}</td>
      <td class="${r.sharpe >= 0 ? "pos" : "neg"}">${num(r.sharpe)}</td>
      <td class="neg">${pct(r.maxdd)}</td>
      <td>${pct(r.win_rate)}</td><td>${num(r.pl_ratio)}</td>
      <td class="${r.total_return >= 0 ? "pos" : "neg"}">${pct(r.total_return)}</td></tr>`).join("");
    $("btRankTable").innerHTML =
      `<tr><th>代码</th><th>策略</th><th>年化</th><th>夏普</th><th>最大回撤</th><th>胜率</th><th>盈亏比</th><th>总收益</th></tr>${rows}`;
  }

  /* ---------- 因子 ---------- */
  let facChart = null;
  function renderFactors() {
    const f = state.factors;
    if (!f || !f.horizons) {
      $("facHorizon").innerHTML = "";
      $("facHeat").innerHTML = "";
      $("facAi").innerHTML = '<p class="empty">暂无 AI 解读</p>';
      return;
    }
    const horizons = Object.keys(f.horizons);
    const selH = $("facHorizon");
    const curH = selH.value || horizons[0];
    selH.innerHTML = horizons.map((h) => `<option value="${h}" ${h === curH ? "selected" : ""}>未来 ${h} 日</option>`).join("");
    const stats = (f.horizons[curH] || []).filter((s) => s.mean_ic !== undefined);

    if (facChart) facChart.destroy();
    const top10 = stats.slice(0, 10).reverse();
    facChart = new Chart($("facChart"), {
      type: "bar",
      data: {
        labels: top10.map((s) => s.name),
        datasets: [{
          label: "ICIR（信息比率，越高越有效）",
          data: top10.map((s) => s.icir),
          backgroundColor: top10.map((s) => s.icir >= 0 ? "rgba(47,212,143,.7)" : "rgba(255,93,108,.7)"),
        }],
      },
      options: Object.assign(baseOptions(), { indexAxis: "y", plugins: { legend: { labels: { color: "#dbe4f5" } } } }),
    });

    const ai = f.ai_comment || {};
    $("facAi").innerHTML = ai.comment ? `
      <h4>🤖 AI 因子解读（${ai.updated_at || ""}）</h4>
      <p>${esc(ai.comment)}</p>
      ${(ai.ideas || []).length ? "<h4>💡 组合建议</h4><ul>" + ai.ideas.map((i) => `<li>${esc(i)}</li>`).join("") + "</ul>" : ""}
      ${ai.risk_note ? `<h4>⚠️ 风险提示</h4><p>${esc(ai.risk_note)}</p>` : ""}
    ` : '<p class="empty">暂无 AI 解读（配置 DeepSeek API Key 后每小时生成）</p>';

    const hm = f.heatmap || {};
    if (hm.values && hm.values.length) {
      let rows = `<tr><th>代码</th>${hm.factors.map((x) => `<th>${esc(x)}</th>`).join("")}</tr>`;
      hm.values.forEach((row, i) => {
        rows += `<tr><td><b>${hm.tickers[i]}</b></td>` +
          row.map((v) => {
            const c = v >= 0 ? `rgba(47,212,143,${Math.min(1, Math.abs(v) / 2 + .1)})` : `rgba(255,93,108,${Math.min(1, Math.abs(v) / 2 + .1)})`;
            return `<td style="background:${c}">${num(v, 2)}</td>`;
          }).join("") + "</tr>";
      });
      $("facHeat").innerHTML = `<table class="heat">${rows}</table>
        <p class="meta" style="margin-top:8px">截止 ${hm.as_of} · 横截面 z 值：绿=因子值偏高，红=偏低</p>`;
    } else {
      $("facHeat").innerHTML = '<p class="empty">暂无数据</p>';
    }
  }

  /* ---------- 期权 ---------- */
  let optChart = null;
  function renderOptions() {
    const o = state.options;
    const cfg = state.optCfg;
    if (cfg && cfg.conditions) {
      const c = cfg.conditions;
      const hasOv = c.overrides && Object.keys(c.overrides).length;
      $("optCond").innerHTML =
        `默认条件：胜率 ≥ <b>${pct(c.min_win_rate)}</b> · 盈亏比 ≥ <b>${c.min_pl_ratio}</b> · 年化期望 ≥ <b>${pct(c.min_ann_return)}</b>` +
        (hasOv ? ` · <b>各形态单独阈值</b>（见 config/options.json → conditions.overrides）` : "") +
        ` · 到期 ${cfg.dte_min}–${cfg.dte_max} 天 · 更新于 ${o ? o.updated_at : "—"}`;
    }
    if (!o || !o.items) {
      $("optStats").innerHTML = ""; $("optTable").innerHTML = ""; $("optHistTable").innerHTML = "";
      if (optChart) { optChart.destroy(); optChart = null; }
      $("optChart").style.display = "none"; $("optEmpty").hidden = false;
      return;
    }
    $("optChart").style.display = ""; $("optEmpty").hidden = true;
    const run = o.run || {};
    $("optStats").innerHTML = [
      ["本次扫描合约数", run.scanned ?? "—", ""],
      ["命中条件数", run.hits ?? "—", "pos"],
      ["新命中（已邮件提醒）", run.new ?? "—", "pos"],
      ["扫描标的", (cfg && cfg.tickers ? cfg.tickers.join(" ") : "—"), ""],
    ].map(([k, v, cls]) => `<div class="card"><div class="k">${k}</div><div class="v ${cls}" style="font-size:15px">${v}</div></div>`).join("");

    if (optChart) optChart.destroy();
    const items = o.items || [];
    if (items.length) {
      optChart = new Chart($("optChart"), {
        type: "scatter",
        data: {
          datasets: [{
            label: "命中形态（胜率 × 盈亏比）",
            data: items.map((h) => ({ x: h.win_rate * 100, y: h.pl_ratio })),
            backgroundColor: "rgba(47,212,143,.75)", pointRadius: 5,
          }],
        },
        options: Object.assign(baseOptions(), {
          scales: {
            x: { title: { display: true, text: "预估胜率 %", color: "#8fa3c4" }, min: 40, grid: { color: "#22304a" }, ticks: { color: "#8fa3c4" } },
            y: { title: { display: true, text: "盈亏比", color: "#8fa3c4" }, grid: { color: "#22304a" }, ticks: { color: "#8fa3c4" } },
          },
        }),
      });
    }

    const newKeys = new Set((o.new_items || []).map((h) => h.key));
    const rowFn = (h, isNew) => `<tr class="${isNew ? "new-hit" : ""}">
      <td><b>${h.ticker}</b></td><td>${esc(h.strategy_name)}</td><td>${h.expiry}</td>
      <td>${h.strike}${h.strike2 ? " / " + h.strike2 : ""}</td>
      <td>${h.bid.toFixed(2)}</td>
      <td class="pos"><b>${pct(h.win_rate)}</b></td>
      <td class="pos">${num(h.pl_ratio)}</td>
      <td class="${h.ann_return >= 0 ? "pos" : "neg"}">${pct(h.ann_return)}</td>
      <td>${pct(h.iv)}</td><td>${pct(h.iv_pct)}</td><td>${num(h.delta)}</td>
      <td>${h.dte}</td><td>${h.oi}</td>
      ${isNew ? "<td class='pos'>🆕 新</td>" : "<td></td>"}</tr>`;
    $("optTable").innerHTML =
      `<tr><th>代码</th><th>形态</th><th>到期</th><th>行权价</th><th>权利金(买价)</th><th>预估胜率</th><th>盈亏比</th><th>年化期望</th><th>IV</th><th>IV分位</th><th>Δ</th><th>剩余天</th><th>OI</th><th></th></tr>` +
      items.map((h) => rowFn(h, newKeys.has(h.key))).join("") ||
      '<tr><td colspan="14" class="empty">本次扫描无合约满足条件</td></tr>';

    const hist = (o.history || []).slice().reverse().slice(0, 50);
    $("optHistTable").innerHTML =
      `<tr><th>发现时间</th><th>代码</th><th>形态</th><th>到期</th><th>行权价</th><th>胜率</th><th>盈亏比</th><th>年化期望</th></tr>` +
      (hist.map((h) => `<tr><td>${esc(h.first_seen || "—")}</td><td><b>${h.ticker}</b></td><td>${esc(h.strategy_name)}</td><td>${h.expiry}</td><td>${h.strike}${h.strike2 ? " / " + h.strike2 : ""}</td><td class="pos">${pct(h.win_rate)}</td><td>${num(h.pl_ratio)}</td><td class="${h.ann_return >= 0 ? "pos" : "neg"}">${pct(h.ann_return)}</td></tr>`).join("") ||
        '<tr><td colspan="8" class="empty">暂无历史命中</td></tr>');
  }

  /* ---------- 状态 ---------- */
  function renderStatus() {
    const st = state.status;
    if (!st) { $("statusMeta").textContent = ""; $("statusTable").innerHTML = '<tr><td class="empty">等待首次运行</td></tr>'; return; }
    $("statusMeta").textContent = `全局更新于 ${st.updated_at}`;
    const names = { news: "🔥 热点新闻", scan: "🎯 期权扫描", quant: "📊 回测+因子", digest: "📧 每日摘要" };
    const runs = Object.entries(st.last_runs || {}).map(([k, v]) =>
      `<tr><td><b>${names[k] || k}</b></td><td class="${v.ok ? "pos" : "neg"}">${v.ok ? "✅ 成功" : "❌ 失败"}</td><td>${v.at}</td><td class="meta">${JSON.stringify(v.detail || {})}</td></tr>`).join("");
    $("statusTable").innerHTML =
      `<tr><th>任务</th><th>状态</th><th>时间</th><th>详情</th></tr>${runs}`;
    const errs = (st.errors || []).map((e) => `<div>❌ [${e.at}] ${e.mode}: ${esc(e.msg)}</div>`).join("");
    $("errList").innerHTML = errs || '<p class="empty">无错误</p>';
  }

  /* ---------- 通用 ---------- */
  function baseOptions() {
    return {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { labels: { color: "#8fa3c4" } } },
      scales: {
        x: { grid: { color: "#1b2942" }, ticks: { color: "#8fa3c4", maxTicksLimit: 8 } },
        y: { grid: { color: "#1b2942" }, ticks: { color: "#8fa3c4" } },
      },
    };
  }
  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  /* ---------- 交互 ---------- */
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      $("tab-" + btn.dataset.tab).classList.add("active");
    });
  });
  $("refreshBtn").addEventListener("click", () => { loadAll(); resetCountdown(); });
  $("btTicker").addEventListener("change", renderBacktest);
  $("btStrategy").addEventListener("change", renderBacktest);
  $("facHorizon").addEventListener("change", renderFactors);

  let remain = REFRESH_SEC;
  function resetCountdown() { remain = REFRESH_SEC; }
  setInterval(() => {
    remain--;
    if (remain <= 0) { loadAll(); remain = REFRESH_SEC; }
    $("countdown").textContent = remain + "s 后自动刷新";
  }, 1000);

  loadAll();
})();
