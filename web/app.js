/* quant-live 仪表盘前端逻辑 v2 */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const state = { news: null, backtest: null, factors: null, options: null, status: null, optCfg: null, review: null };

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
  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
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
    const [news, backtest, factors, options, status, optCfg, review] = await Promise.all([
      fetchJSON("data/news.json"), fetchJSON("data/backtest.json"), fetchJSON("data/factors.json"),
      fetchJSON("data/options_hits.json"), fetchJSON("data/status.json"), fetchJSON("config/options.json"),
      fetchJSON("data/review.json"),
    ]);
    Object.assign(state, { news, backtest, factors, options, status, optCfg, review });
    renderNews(); renderBacktest(); renderFactors(); renderOptions(); renderReview(); renderStatus(); renderBadge();
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
    const wc = (state.news.items || []).filter((i) => i.world_class).length;
    meta.textContent = `更新于 ${state.news.updated_at} · 共收录 ${state.news.count || state.news.items.length} 条 · AI 解读 ${state.news.ai_enabled ? "已启用" : "未启用"} · 🌍世界级热点 ${wc} 条`;
    const items = state.news.items.slice(0, 40);
    list.innerHTML = items.map((it) => {
      const sent = it.sentiment === "利好" ? "bull" : it.sentiment === "利空" ? "bear" : "flat";
      const stars = "★".repeat(Math.min(5, it.impact || 0)) + "☆".repeat(Math.max(0, 5 - (it.impact || 0)));
      const sectors = (it.sectors || []).map((s) => `<span class="tag sector">${esc(s)}</span>`).join("");
      const kws = (it.keywords || []).slice(0, 4).map((k) => `<span class="tag">${esc(k)}</span>`).join("");
      let verify = "";
      if (it.world_class) verify = `<span class="tag" style="background:rgba(255,201,77,.2);color:#ffc94d">🌍 世界级热点 · 波动 ${num(it.atr_ratio, 1)}×</span>`;
      else if (it.impact_confirmed === true) verify = `<span class="tag" style="background:rgba(47,212,143,.15);color:#2fd48f">✓ 波动验证通过 ${num(it.atr_ratio, 1)}×</span>`;
      else if (it.impact_confirmed === false) verify = `<span class="tag" style="background:rgba(255,93,108,.12);color:#ff5d6c">✗ 未超均波动 ${num(it.atr_ratio, 1)}×</span>`;
      return `<div class="news-card">
        <div class="news-score"><div class="s">${it.score ?? 0}</div><div class="l">热度</div></div>
        <div class="news-body">
          <div class="news-title"><a href="${esc(it.link)}" target="_blank" rel="noopener">${esc(it.title_cn || it.title)}</a>
            ${it.title_cn && it.title_cn !== it.title ? `<span class="meta" style="font-size:11px">（${esc(it.title.slice(0, 80))}…）</span>` : ""}
          </div>
          ${it.summary ? `<div class="news-summary">${esc(it.summary)}</div>` : ""}
          <div class="news-tags"><span class="sent ${sent}">${esc(it.sentiment || "中性")}</span>
            <span class="stars" title="影响程度">${stars}</span>${sectors}${kws}${verify}
            <span class="tag">${esc(it.source || "")}</span>
            <span class="tag">${esc(it.first_seen || "")}</span>
          </div>
        </div>
      </div>`;
    }).join("");
  }

  /* ---------- 回测（样本内/外 + 过拟合） ---------- */
  let btChart = null;
  function renderBacktest() {
    const bt = state.backtest;
    if (!bt || !bt.results || !Object.keys(bt.results).length) {
      $("btMetrics").innerHTML = ""; $("btRankTable").innerHTML = ""; $("btTargets").textContent = "";
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

    const t = bt.targets || {};
    const sum = bt.summary || {};
    $("btTargets").innerHTML =
      `目标：胜率 ≥ <b>${pct(t.min_win_rate)}</b> · 盈亏比 &gt; <b>${t.min_pl_ratio}</b> · 夏普 &gt; <b>${t.min_sharpe}</b>　|　` +
      `样本外达标 <b class="${sum.pass_oos ? "pos" : "neg"}">${sum.pass_oos ?? "—"}</b> / ${sum.combos} 组合　|　` +
      `疑似过拟合 <b class="${sum.overfit_count ? "neg" : "pos"}">${sum.overfit_count ?? "—"}</b> 个　|　样本外比例 ${pct(bt.oos_ratio)}`;

    const full = (bt.results[curT] || {})[curS];
    const b = (bt.benchmark || {})[curT] || {};
    if (!full) {
      $("btMetrics").innerHTML = '<p class="empty">该组合暂无数据</p>';
      return;
    }
    const is = full.is, oos = full.oos, pass = full.pass_targets || {};
    const cards = [
      ["样本外·年化", pct(oos.ann_return), oos.ann_return >= 0 ? "pos" : "neg"],
      ["样本外·夏普", num(oos.sharpe), pass.oos && pass.oos.sharpe ? "pos" : (oos.sharpe >= 0 ? "pos" : "neg")],
      ["样本外·胜率", pct(oos.win_rate), pass.oos && pass.oos.win_rate ? "pos" : (oos.win_rate >= 0.5 ? "pos" : "neg")],
      ["样本外·盈亏比", num(oos.pl_ratio), pass.oos && pass.oos.pl_ratio ? "pos" : (oos.pl_ratio >= 1 ? "pos" : "neg")],
      ["样本外·回撤", pct(oos.maxdd), "neg"],
      ["样本内·夏普", num(is.sharpe), is.sharpe >= 0 ? "pos" : "neg"],
      ["样本内·胜率", pct(is.win_rate), is.win_rate >= 0.5 ? "pos" : "neg"],
      ["基准年化(B&H)", pct((b.combined || {}).ann_return), (b.combined || {}).ann_return >= 0 ? "pos" : "neg"],
      ["样本外达标", pass.oos && pass.oos.all ? "✅ 全部达标" : "❌ 未达标", pass.oos && pass.oos.all ? "pos" : "neg"],
      ["过拟合检测", full.overfit && full.overfit.flag ? "⚠️ 疑似过拟合" : "✅ 样本内外一致", full.overfit && full.overfit.flag ? "neg" : "pos"],
      ["参数(网格优选)", JSON.stringify(full.params || {}), ""],
    ];
    $("btMetrics").innerHTML = cards.map(([k, v, cls]) =>
      `<div class="card"><div class="k">${k}</div><div class="v ${cls}" style="font-size:14px">${v}</div></div>`).join("");

    if (btChart) btChart.destroy();
    const isCurve = is.equity, oosCurve = oos.equity;
    btChart = new Chart($("btChart"), {
      type: "line",
      data: {
        labels: (isCurve || []).concat(oosCurve || []).map((p) => p[0]),
        datasets: [
          { label: "样本内(历史)净值", data: (isCurve || []).map((p) => p[1]), borderColor: "#8fa3c4", backgroundColor: "transparent", tension: .15, pointRadius: 0 },
          { label: "样本外(近端)净值", data: (oosCurve || []).map((p) => p[1]), borderColor: "#3fa7ff", backgroundColor: "rgba(63,167,255,.12)", fill: true, tension: .15, pointRadius: 0 },
        ],
      },
      options: baseOptions(),
    });

    const rows = (bt.ranking || []).map((r) =>
      `<tr><td><b>${r.ticker}</b></td><td>${esc(r.strategy)}</td>
      <td class="${r.oos_sharpe >= 0 ? "pos" : "neg"}">${num(r.oos_sharpe)}</td>
      <td class="${r.oos_ann >= 0 ? "pos" : "neg"}">${pct(r.oos_ann)}</td>
      <td>${pct(r.oos_win)}</td><td>${num(r.oos_pl)}</td><td class="neg">${pct(r.oos_dd)}</td>
      <td>${num(r.is_sharpe)}</td>
      <td>${r.pass_oos ? '<b class="pos">✅</b>' : '<span class="neg">✗</span>'}</td>
      <td>${r.overfit ? '<span class="neg">⚠️疑似</span>' : '<span class="pos">正常</span>'}</td></tr>`).join("");
    $("btRankTable").innerHTML =
      `<tr><th>代码</th><th>策略</th><th>样本外夏普</th><th>样本外年化</th><th>胜率</th><th>盈亏比</th><th>回撤</th><th>样本内夏普</th><th>达标</th><th>过拟合</th></tr>${rows}`;
  }

  /* ---------- 因子（含综合榜） ---------- */
  let facChart = null;
  function renderFactors() {
    const f = state.factors;
    if (!f || !f.horizons) {
      $("facHorizon").innerHTML = ""; $("facHeat").innerHTML = ""; $("facCompTable").innerHTML = "";
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

    const comp = f.composite || {};
    if (comp.ranking && comp.ranking.length) {
      const rows = comp.ranking.map((r, i) => `<tr>
        <td>${i + 1}</td><td><b>${r.ticker}</b></td>
        <td class="${r.score >= 0 ? "pos" : "neg"}"><b>${num(r.score, 3)}</b></td>
        <td class="meta">${(r.top_factors || []).map((x) => `${esc(x.factor)} ${x.contribution >= 0 ? "+" : ""}${num(x.contribution, 2)}`).join(" · ")}</td></tr>`).join("");
      $("facCompTable").innerHTML =
        `<tr><th>#</th><th>标的</th><th>综合得分</th><th>主要贡献因子（ICIR加权）</th></tr>${rows}
        <tr><td colspan="4" class="meta">截止 ${comp.as_of} · 权重：${Object.entries(comp.weights || {}).map(([k, v]) => `${k} ${num(v, 3)}`).join("，")}</td></tr>`;
    } else {
      $("facCompTable").innerHTML = '<tr><td class="empty">暂无数据</td></tr>';
    }

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

  /* ---------- 期权（五维共振） ---------- */
  function renderOptions() {
    const o = state.options;
    const cfg = state.optCfg;
    if (cfg && cfg.conditions) {
      const c = cfg.conditions;
      const hasOv = c.overrides && Object.keys(c.overrides).length;
      const res = cfg.resonance || {};
      $("optCond").innerHTML =
        `默认条件：胜率 ≥ <b>${pct(c.min_win_rate)}</b> · 盈亏比 ≥ <b>${c.min_pl_ratio}</b> · 年化期望 ≥ <b>${pct(c.min_ann_return)}</b>` +
        (hasOv ? ` · <b>各形态单独阈值</b>` : "") +
        (res.enabled ? ` · 邮件仅推 <b>共振 ≥ ${res.min_resonance} 分</b>且方向对齐` : ` · 共振过滤已关闭`) +
        ` · 到期 ${cfg.dte_min}–${cfg.dte_max} 天 · 更新于 ${o ? o.updated_at : "—"}`;
    }
    if (!o || !o.items) {
      $("optStats").innerHTML = ""; $("optTable").innerHTML = ""; $("optHistTable").innerHTML = ""; $("optResTable").innerHTML = "";
      return;
    }
    const run = o.run || {};
    $("optStats").innerHTML = [
      ["本次扫描合约数", run.scanned ?? "—", ""],
      ["命中条件数", run.hits ?? "—", "pos"],
      ["新命中", run.new ?? "—", "pos"],
      ["其中共振达标(已邮件)", run.resonance_hits ?? "—", "pos"],
    ].map(([k, v, cls]) => `<div class="card"><div class="k">${k}</div><div class="v ${cls}" style="font-size:15px">${v}</div></div>`).join("");

    const resMap = (o.resonance && o.resonance.map) || {};
    const resRows = Object.entries(resMap).map(([sym, r]) => `<tr>
      <td><b>${sym}</b></td>
      <td class="${r.score >= 60 ? "pos" : "neg"}"><b>${num(r.score, 1)}</b></td>
      <td>${r.bias > 0 ? "🟢 偏多" : r.bias < 0 ? "🔴 偏空" : "⚪ 中性"}</td>
      <td>新闻 ${num(r.news * 100, 0)}</td><td>周期 ${num(r.cycle * 100, 0)}</td>
      <td>技术 ${num(r.technical * 100, 0)}</td><td>资金 ${num(r.capital * 100, 0)}</td>
      <td>${r.news_hits} 条热点</td></tr>`).join("");
    $("optResTable").innerHTML =
      `<tr><th>标的</th><th>共振分</th><th>方向</th><th>热点</th><th>周期</th><th>技术</th><th>资金</th><th>热点条数</th></tr>${resRows}` ||
      '<tr><td class="empty">暂无共振数据</td></tr>';

    const emailKeys = new Set((o.new_items || []).map((h) => h.key));
    const newKeys = new Set((o.new_items_all || []).map((h) => h.key));
    const rowFn = (h) => {
      const isEmail = emailKeys.has(h.key), isNew = newKeys.has(h.key);
      return `<tr class="${isNew ? "new-hit" : ""}">
      <td><b>${h.ticker}</b></td><td>${esc(h.strategy_name)}</td><td>${h.expiry}</td>
      <td>${h.strike}${h.strike2 ? " / " + h.strike2 : ""}</td>
      <td>${h.bid.toFixed(2)}</td>
      <td class="pos"><b>${pct(h.win_rate)}</b></td>
      <td class="pos">${num(h.pl_ratio)}</td>
      <td class="${h.ann_return >= 0 ? "pos" : "neg"}">${pct(h.ann_return)}</td>
      <td class="${h.resonance >= 60 ? "pos" : "neg"}"><b>${num(h.resonance, 1)}</b></td>
      <td>${h.res_bias > 0 ? "🟢" : h.res_bias < 0 ? "🔴" : "⚪"}</td>
      <td>${pct(h.iv)}</td><td>${num(h.delta)}</td><td>${h.dte}</td>
      <td>${isEmail ? "<span class='pos'>🆕📧</span>" : isNew ? "<span>🆕</span>" : ""}</td></tr>`;
    };
    $("optTable").innerHTML =
      `<tr><th>代码</th><th>形态</th><th>到期</th><th>行权价</th><th>权利金(买价)</th><th>预估胜率</th><th>盈亏比</th><th>年化期望</th><th>共振分</th><th>方向</th><th>IV</th><th>Δ</th><th>剩余天</th><th></th></tr>` +
      ((o.items || []).map(rowFn).join("") || '<tr><td colspan="14" class="empty">本次扫描无合约满足条件</td></tr>');

    const hist = (o.history || []).slice().reverse().slice(0, 50);
    $("optHistTable").innerHTML =
      `<tr><th>发现时间</th><th>代码</th><th>形态</th><th>到期</th><th>行权价</th><th>胜率</th><th>盈亏比</th><th>年化期望</th><th>共振</th><th>结算</th></tr>` +
      ((hist || []).map((h) => `<tr><td>${esc(h.first_seen || "—")}</td><td><b>${h.ticker}</b></td><td>${esc(h.strategy_name)}</td><td>${h.expiry}</td><td>${h.strike}${h.strike2 ? " / " + h.strike2 : ""}</td><td class="pos">${pct(h.win_rate)}</td><td>${num(h.pl_ratio)}</td><td class="${h.ann_return >= 0 ? "pos" : "neg"}">${pct(h.ann_return)}</td><td>${num(h.resonance, 1)}</td><td>${h.settled ? (h.won ? '<span class="pos">✅ 盈利</span>' : '<span class="neg">❌ 亏损</span>') : "进行中"}</td></tr>`).join("") ||
        '<tr><td colspan="10" class="empty">暂无历史命中</td></tr>');
  }

  /* ---------- 复盘 ---------- */
  function renderReview() {
    const rv = state.review;
    $("reviewMeta").textContent = rv ? `更新于 ${rv.updated_at} · 耗时 ${rv.sec}s` : "";
    const news = (rv && rv.news) || {};
    const opts = (rv && rv.options) || {};
    const tune = (rv && rv.tune) || {};
    $("reviewNewsCards").innerHTML = [
      ["已核验热点条数", news.checked ?? "—", ""],
      ["实际波动超均波动条数", news.confirmed ?? "—", "pos"],
      ["波动确认率", pct(news.confirmed_rate), news.confirmed_rate >= 0.5 ? "pos" : "neg"],
      ["AI 高影响(≥4)确认率", pct(news.ai_accuracy && news.ai_accuracy.high_impact && news.ai_accuracy.high_impact.confirmed_rate), "pos"],
      ["世界级热点数", (news.world_class || []).length, "pos"],
    ].map(([k, v, cls]) => `<div class="card"><div class="k">${k}</div><div class="v ${cls}" style="font-size:15px">${v}</div></div>`).join("") || '<p class="empty">等待每日 23:00 复盘</p>';
    $("reviewWcTable").innerHTML =
      `<tr><th>标题</th><th>标的</th><th>波动/均波动</th><th>热度</th><th>时间</th></tr>` +
      ((news.world_class || []).map((it) => `<tr><td><a href="${esc(it.link)}" target="_blank" rel="noopener">${esc((it.title || "").slice(0, 90))}</a></td><td>${(it.tickers || []).join(" ")}</td><td class="pos"><b>${num(it.atr_ratio, 1)}×</b></td><td>${it.score}</td><td>${esc(it.first_seen || "")}</td></tr>`).join("") ||
        '<tr><td colspan="5" class="empty">暂无世界级热点</td></tr>');
    const st = opts.stats || {};
    $("reviewOptCards").innerHTML = [
      ["累计已结算笔数", st.settled_total ?? "—", ""],
      ["实际胜率", pct(st.actual_win_rate), st.actual_win_rate >= 0.5 ? "pos" : "neg"],
      ["共振≥60 命中实际胜率", pct(st.resonance_win_rate), (st.resonance_win_rate ?? 0) >= 0.5 ? "pos" : "neg"],
      ["平均单笔实际收益", pct(st.avg_actual_return), st.avg_actual_return >= 0 ? "pos" : "neg"],
    ].map(([k, v, cls]) => `<div class="card"><div class="k">${k}</div><div class="v ${cls}" style="font-size:15px">${v}</div></div>`).join("") || '<p class="empty">暂无已到期结算</p>';
    $("reviewSettleTable").innerHTML =
      `<tr><th>标的</th><th>形态</th><th>预估胜率</th><th>共振</th><th>实际结果</th><th>实际收益率</th></tr>` +
      ((opts.recent || []).map((h) => `<tr><td><b>${h.ticker}</b></td><td>${esc(h.strategy_name || "")}</td><td>${pct(h.win_rate_pred)}</td><td>${num(h.resonance, 1)}</td><td>${h.won ? '<span class="pos">✅ 盈利</span>' : '<span class="neg">❌ 亏损</span>'}</td><td class="${h.actual_return >= 0 ? "pos" : "neg"}">${pct(h.actual_return)}</td></tr>`).join("") ||
        '<tr><td colspan="6" class="empty">暂无结算记录（期权到期后自动结算）</td></tr>');
    $("reviewTune").innerHTML = (tune.actions && tune.actions.length)
      ? "<ul>" + tune.actions.map((a) => `<li>⚙️ ${esc(a)}</li>`).join("") + "</ul>"
      : '<p class="empty">无调整（样本不足或表现达标）。自动调参在到期结算样本 ≥8 时按实际胜率微调各形态门槛（±0.15 边界）。</p>';
  }

  /* ---------- 状态 ---------- */
  function renderStatus() {
    const st = state.status;
    if (!st) { $("statusMeta").textContent = ""; $("statusTable").innerHTML = '<tr><td class="empty">等待首次运行</td></tr>'; return; }
    $("statusMeta").textContent = `全局更新于 ${st.updated_at}`;
    const names = { news: "🔥 热点新闻", scan: "🎯 期权扫描", quant: "📊 回测+因子", digest: "📧 每日摘要", review: "🔁 每日复盘" };
    const runs = Object.entries(st.last_runs || {}).map(([k, v]) =>
      `<tr><td><b>${names[k] || k}</b></td><td class="${v.ok ? "pos" : "neg"}">${v.ok ? "✅ 成功" : "❌ 失败"}</td><td>${v.at}</td><td class="meta">${JSON.stringify(v.detail || {}).slice(0, 300)}</td></tr>`).join("");
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
