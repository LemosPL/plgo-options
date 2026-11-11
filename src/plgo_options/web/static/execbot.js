// Execution Bot page (/api/execution-bot): parent orders worked by the server
// every 5 seconds, crossing only when the book is cheap against the smoothing spline.
// Optimizer v2's "Send to execution" lands here through window.execbotReceive.
(function () {
  const ACTION = { cooldown: "resting", no_book: "no book" };
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const fmt = (v, dp = 0) => (v == null || !isFinite(v)) ? "—"
    : Number(v).toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
  const usd = (v, dp = 2) => (v == null || !isFinite(v)) ? "—" : (v < 0 ? "-$" : "$") + fmt(Math.abs(v), dp);
  const when = (ts) => ts ? new Date(ts * 1000).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";
  const whenS = (ts) => ts ? new Date(ts * 1000).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";
  const left = (ts) => {
    const s = ts - Date.now() / 1000;
    if (s <= 0) return "past";
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h ? `${h} h ${String(m).padStart(2, "0")}` : `${m} min`;
  };
  const active = () => $("page-execbot")?.classList.contains("active");

  let selected = null;     // order id shown in the detail card
  let review = null;       // { trades, meta, data, rfq } from Optimizer v2
  const live = {};         // RFQ structure index -> { id, timer } while it is watched

  async function api(method, url, body) {
    const r = await fetch(url, {
      method, credentials: "same-origin",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!r.ok) {
      let msg = r.status + " " + r.statusText;
      try {
        const j = await r.json();
        if (j.detail) msg = typeof j.detail === "string" ? j.detail : j.detail.map((d) => d.msg).join("; ");
      } catch (_) { /* not JSON */ }
      throw new Error(msg);
    }
    return r.json();
  }

  function status(msg, isErr) {
    const el = $("xb-status");
    el.textContent = msg || "";
    el.style.color = isErr ? "var(--red)" : "";
  }

  // ── header ────────────────────────────────────────────────────
  function header(st) {
    const card = (label, value, sub, style = "") => `<div class="collat-metric"><div class="collat-metric-label">${label}</div>
      <div class="collat-metric-value" style="${style}">${value}</div><div class="collat-metric-sub">${sub}</div></div>`;
    const c = st.counts || {};
    const lt = st.last_tick || {};
    $("xb-cards").innerHTML =
      card("Mode", "Paper", esc(st.live_note || ""))
      + card("Kill switch", st.kill_switch ? "ON" : "off", st.kill_switch ? "nothing is sent while it is on" : "Agents page controls it",
        st.kill_switch ? "color:var(--red)" : "")
      + card("Last run", lt.ts ? when(lt.ts) : "—", lt.ts ? `${fmt(lt.n)} order(s) · ${fmt(lt.passes || 1)} pass(es) · ${fmt(lt.takes || 0)} take(s)${lt.quotes ? ` · quotes ${esc(lt.quotes)}` : ""}${lt.halted ? " · halted" : ""}${lt.failed ? ` · ${fmt(lt.failed)} failed (${esc(lt.error || "")})` : ""}` : "no run yet: is the scheduler job deployed?")
      + card("Working", fmt(c.working || 0), `${fmt(c.paused || 0)} paused`)
      + card("Finished", fmt((c.filled || 0) + (c.expired || 0) + (c.cancelled || 0)),
        `${fmt(c.filled || 0)} filled · ${fmt(c.expired || 0)} expired · ${fmt(c.cancelled || 0)} cancelled`);
  }

  // ── orders table ──────────────────────────────────────────────
  function rows(orders) {
    if (!orders.length) {
      $("xb-orders").innerHTML = `<tr><td colspan="10" style="text-align:center;color:var(--muted);padding:1rem">
        No orders yet. Send a solution from Optimizer v2, or use “+ New order”.</td></tr>`;
      return;
    }
    $("xb-orders").innerHTML = orders.map((o) => {
      const pct = o.qty ? Math.min(100, 100 * o.filled / o.qty) : 0;
      const avg = o.filled ? o.cost_usd / o.filled : null;
      const open = o.status === "working" || o.status === "paused";
      const now = o.last_best_cost != null && o.last_theta_usd != null
        ? `${usd(o.last_best_cost)} <span style="color:var(--muted)">vs</span> ${usd(o.last_theta_usd)}` : "—";
      const bs = "width:auto;margin:0;padding:.15rem .55rem;font-size:.72rem";
      const acts = !open ? "" : (o.status === "working"
        ? `<button class="btn-secondary" data-act="pause" data-id="${o.id}" style="${bs}">Pause</button>`
        : `<button class="btn-secondary" data-act="resume" data-id="${o.id}" style="${bs}">Resume</button>`)
        + ` <button class="btn-secondary" data-act="cancel" data-id="${o.id}" style="${bs};color:var(--red)">Cancel</button>`;
      const stColor = { working: "var(--green)", paused: "var(--orange)", expired: "var(--red)" }[o.status] || "var(--muted)";
      return `<tr data-id="${o.id}" style="cursor:pointer${o.id === selected ? ";background:var(--hover-bg)" : ""}">
        <td>${o.id}</td><td style="text-align:left">${esc(o.instrument)}</td>
        <td style="text-align:left">${o.side === "buy" ? "Buy" : "Sell"}</td>
        <td>${fmt(o.filled)} / ${fmt(o.qty)}
          <div style="height:4px;background:var(--border);border-radius:2px;margin-top:3px">
          <div style="height:4px;width:${pct}%;background:var(--accent);border-radius:2px"></div></div></td>
        <td>${usd(avg)}</td><td title="best cost at the touch vs the threshold, last tick">${now}</td>
        <td style="text-align:left" title="${esc(o.last_detail || "")}">${esc(ACTION[o.last_action] || o.last_action || "—")}</td>
        <td>${open ? left(o.deadline_ts) : when(o.finished_ts)}</td>
        <td style="color:${stColor}">${esc(o.status)}</td><td style="white-space:nowrap">${acts}</td></tr>`;
    }).join("");
  }

  // ── cost per leg ──────────────────────────────────────────────
  function costs(legs) {
    if (!legs.length) {
      $("xb-costs").innerHTML = `<tr><td colspan="11" style="text-align:center;color:var(--muted);padding:1rem">No fills yet.</td></tr>`;
      return;
    }
    const vp = (v) => v == null ? "—" : fmt(v, 2);
    // vega-weighted over the legs that have the reference
    const pool = (name) => {
      const w = legs.reduce((a, g) => a + (g[name + "_vp"] != null ? g[name + "_vega"] : 0), 0);
      return w ? legs.reduce((a, g) => a + (g[name + "_vp"] != null ? g[name + "_vp"] * g[name + "_vega"] : 0), 0) / w : null;
    };
    const sum = (k) => legs.some((g) => g[k] != null) ? legs.reduce((a, g) => a + (g[k] || 0), 0) : null;
    const allIn = (m, f) => m != null && f != null ? m + f : null;
    const partial = (g, name) => g[name + "_qty"] != null && g[name + "_qty"] < g.filled
      ? ` <span style="color:var(--muted)" title="covers ${fmt(g[name + "_qty"])} of ${fmt(g.filled)} contracts">*</span>` : "";
    const row = (g) => `<tr data-id="${g.id}" style="cursor:pointer">
      <td>${g.id}</td><td style="text-align:left">${g.side === "buy" ? "Buy" : "Sell"} ${esc(g.instrument)}${g.mode === "paper" ? "" : ` <b>${esc(g.mode)}</b>`}</td>
      <td>${fmt(g.filled)} / ${fmt(g.order_qty)}${g.status === "working" ? "" : ` <span style="color:var(--muted)">${esc(g.status)}</span>`}</td>
      <td>${vp(g.mid_vp)}${partial(g, "mid")}</td><td>${vp(g.spline_vp)}</td><td>${vp(g.mark_vp)}${partial(g, "mark")}</td>
      <td>${vp(g.fee_vp)}</td><td>${vp(allIn(g.mark_vp, g.fee_vp))}</td>
      <td>${usd(g.spline_usd, 0)}</td><td>${usd(g.mark_usd, 0)}</td><td>${usd(g.fee_usd, 0)}</td></tr>`;
    $("xb-costs").innerHTML = legs.map(row).join("")
      + `<tr style="font-weight:600;border-top:2px solid var(--border)"><td></td><td style="text-align:left">All</td>
      <td>${fmt(legs.reduce((a, g) => a + g.filled, 0))}</td><td>${vp(pool("mid"))}</td><td>${vp(pool("spline"))}</td>
      <td>${vp(pool("mark"))}</td><td>${vp(pool("fee"))}</td><td>${vp(allIn(pool("mark"), pool("fee")))}</td>
      <td>${usd(sum("spline_usd"), 0)}</td><td>${usd(sum("mark_usd"), 0)}</td><td>${usd(sum("fee_usd"), 0)}</td></tr>`;
  }

  // ── detail ────────────────────────────────────────────────────
  async function detail() {
    if (selected == null) { $("xb-detail").style.display = "none"; return; }
    const d = await api("GET", `/api/execution-bot/orders/${selected}`);
    const o = d.order, t = d.ticks, f = d.fills;
    $("xb-detail").style.display = "";
    $("xb-detail-title").textContent = `#${o.id} · ${o.side === "buy" ? "Buy" : "Sell"} ${fmt(o.qty)} ${o.instrument}`;
    const vegaAt = (ts) => { let v = null; for (const x of t) { if (x.ts <= ts && x.vega) v = x.vega; } return v; };
    const avg = o.filled ? o.cost_usd / o.filled : null;
    const vegaAvg = f.length ? f.reduce((s, x) => s + x.qty * (vegaAt(x.ts) || 0), 0) / o.filled : null;
    const fair0 = t.find((x) => x.fair_usd != null)?.fair_usd;
    const px = o.filled ? f.reduce((s, x) => s + x.qty * x.price_usd, 0) / o.filled : null;
    const vsStart = px != null && fair0 != null ? (o.side === "buy" ? px - fair0 : fair0 - px) : null;
    // Cost against Deribit's mark at the fill, fee included like the spline's; fills from before 9 Nov have no mark
    const vsMark = (r) => r.mark_usd == null ? null : (o.side === "buy" ? r.price_usd - r.mark_usd : r.mark_usd - r.price_usd) + r.fee_usd / r.qty;
    const fm = f.filter((r) => r.mark_usd != null), qm = fm.reduce((s, x) => s + x.qty, 0);
    const avgMark = qm ? fm.reduce((s, x) => s + x.qty * vsMark(x), 0) / qm : null;
    const vegaMark = qm ? fm.reduce((s, x) => s + x.qty * (vegaAt(x.ts) || 0), 0) / qm : null;
    const card = (label, value, sub) => `<div class="collat-metric"><div class="collat-metric-label">${label}</div>
      <div class="collat-metric-value">${value}</div><div class="collat-metric-sub">${sub}</div></div>`;
    $("xb-detail-cards").innerHTML =
      card("Filled", `${fmt(o.filled)} / ${fmt(o.qty)}`, `${f.length} fill(s)`)
      + card("Avg cost vs spline", usd(avg), vegaAvg ? `${fmt(avg / vegaAvg, 2)} vol pts, fee included` : "fee included")
      + card("Avg cost vs Deribit mark", usd(avgMark), avgMark == null ? "no fill with a mark yet"
          : `${vegaMark ? fmt(avgMark / vegaMark, 2) + " vol pts, " : ""}fee included${qm < o.filled ? ` · ${fmt(qm)} of ${fmt(o.filled)} filled` : ""}`)
      + card("Total cost", usd(o.cost_usd, 0), `of which fees ${usd(o.fees_usd, 0)}`)
      + card("Avg price vs fair at start", usd(vsStart), fair0 != null ? `fair at start ${usd(fair0)}; includes the market's move` : "—")
      + card("Threshold", `${fmt(o.start_vp, 2)} → ${fmt(o.end_vp, 2)} vp`, `then ${fmt(o.hard_vp, 2)} vp in the final hour · cap ${fmt(o.cap)}/take`);

    const cc = typeof chartColors === "function" ? chartColors() : { paper: "#161b22", plot: "#0d1117", text: "#e6edf3", muted: "#8b949e", grid: "#21262d" };
    const x = (ts) => new Date(ts * 1000);
    const priced = t.filter((r) => r.best_cost != null);
    const traces = [
      { x: priced.map((r) => x(r.ts)), y: priced.map((r) => r.best_cost), name: "Best cost at the touch", mode: "lines",
        line: { color: "#58a6ff", width: 2 }, hovertemplate: "%{x|%d %b %H:%M}<br>best $%{y:.2f}<extra></extra>" },
      { x: priced.map((r) => x(r.ts)), y: priced.map((r) => r.theta_usd), name: "Threshold", mode: "lines",
        line: { color: cc.muted, width: 2, dash: "dash", shape: "hv" }, hovertemplate: "threshold $%{y:.2f}<extra></extra>" },
      { x: f.map((r) => x(r.ts)), y: f.map((r) => r.cost_usd), name: "Fills", mode: "markers",
        marker: { color: "#3fb950", size: f.map((r) => 8 + 10 * Math.sqrt(r.qty / Math.max(...f.map((z) => z.qty)))),
          line: { color: cc.paper, width: 2 } },
        text: f.map((r) => `${fmt(r.qty)} @ ${usd(r.price_usd)}`), hovertemplate: "%{text}<br>cost $%{y:.2f}<extra></extra>" },
    ];
    Plotly.react("xb-chart", traces, {
      paper_bgcolor: cc.paper, plot_bgcolor: cc.plot, font: { color: cc.text, size: 11 },
      margin: { l: 55, r: 15, t: 10, b: 40 }, height: 300, hovermode: "x unified",
      legend: { orientation: "h", y: 1.12, x: 0 },
      xaxis: { color: cc.muted, gridcolor: cc.grid },
      yaxis: { title: "USD per contract, fee included", color: cc.muted, gridcolor: cc.grid, zerolinecolor: cc.grid },
    }, { responsive: true, displayModeBar: false });

    $("xb-fills").innerHTML = f.length ? f.slice().reverse().map((r) => `<tr><td style="text-align:left">${whenS(r.ts)}</td>
      <td>${fmt(r.qty)}</td><td>${fmt(r.price_eth, 4)}</td><td>${usd(r.price_usd)}</td><td>${usd(r.fair_usd)}</td>
      <td>${usd(r.mark_usd)}</td><td>${usd(r.fee_usd / r.qty)}</td><td>${usd(r.cost_usd)}</td><td>${usd(vsMark(r))}</td></tr>`).join("")
      : `<tr><td colspan="9" style="text-align:center;color:var(--muted)">No fills yet</td></tr>`;
    const last = t.slice(-12).reverse();
    $("xb-ticks").innerHTML = last.map((r) => `<tr><td style="text-align:left">${whenS(r.ts)}</td><td style="text-align:left">${esc(ACTION[r.action] || r.action)}</td>
      <td>${usd(r.best_cost)}</td><td>${usd(r.theta_usd)}</td><td>${usd(r.fair_usd)}</td>
      <td>${r.iv != null ? fmt(r.iv * 100, 1) + "%" : "—"}</td><td>${fmt(r.qty)}</td>
      <td style="text-align:left;color:var(--muted)">${esc(r.detail)}</td></tr>`).join("");
  }

  async function load() {
    try {
      const [st, os, cs] = await Promise.all([api("GET", "/api/execution-bot/status"), api("GET", "/api/execution-bot/orders"),
        api("GET", "/api/execution-bot/costs")]);
      header(st);
      rows(os.orders);
      costs(cs.legs);
      await detail();
    } catch (e) {
      status("Load failed: " + e.message, true);
    }
  }

  // An expiry whose spline is off its quotes (thin books): its prices are not to be trusted
  const warnBox = (ws) => (ws || []).map((w) => `<div style="border:1px solid var(--red);border-left-width:4px;border-radius:6px;
      padding:.45rem .65rem;margin-bottom:.6rem;font-size:.78rem"><b style="color:var(--red)">Spline off its quotes</b> — ${esc(w.message)}</div>`).join("");

  // ── review: an optimizer solution → orders ────────────────────
  function params() {
    const n = (id) => parseFloat($(id).value);
    return { hours: n("xb-r-hours"), start_vp: n("xb-r-start"), end_vp: n("xb-r-end"), hard_vp: n("xb-r-hard"), cap: n("xb-r-cap") };
  }

  async function reprice() {
    if (!review) return;
    $("xb-review").style.display = "";
    $("xb-review-body").innerHTML = `<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:1rem">
      Reading the Deribit books and fitting the spline…</td></tr>`;
    $("xb-review-skipped").innerHTML = "";
    $("xb-review-warn").innerHTML = "";
    $("xb-r-create").disabled = true;
    const p = params();
    try {
      review.data = await api("POST", "/api/execution-bot/preview",
        { trades: review.trades, asset: review.meta.asset || "ETH", start_vp: p.start_vp, hours: p.hours });
    } catch (e) {
      $("xb-review-body").innerHTML = `<tr><td colspan="9" style="color:var(--red)">${esc(e.message)}</td></tr>`;
      return;
    }
    const { legs, skipped } = review.data;
    $("xb-review-warn").innerHTML = warnBox(review.data.warnings);
    $("xb-review-sub").textContent = `${review.meta.source || "Optimizer"} · ${review.trades.length} trade(s) in the solution · `
      + `${legs.length} Deribit order(s) · priced ${when(review.data.asof)}`;
    $("xb-review-body").innerHTML = legs.length ? legs.map((l, i) => `<tr>
        <td><input type="checkbox" class="xb-r-pick" data-i="${i}" ${l.note && l.note.startsWith("outside") ? "" : "checked"}></td>
        <td style="text-align:left" title="${esc(l.sources.join("\n"))}">${esc(l.instrument)}</td>
        <td style="text-align:left">${l.side === "buy" ? "Buy" : "Sell"}</td>
        <td><input type="number" class="xb-r-qty" data-i="${i}" value="${l.qty}" min="1" step="1" style="width:6rem"></td>
        <td>${usd(l.fair_usd)}</td><td>${l.iv != null ? fmt(l.iv, 1) + "%" : "—"}</td>
        <td>${usd(l.best_cost_usd)} <span style="color:var(--muted)">(${fmt(l.best_cost_vp, 2)} vp)</span></td>
        <td>${l.now_qty ? fmt(l.now_qty) : "—"}</td>
        <td style="text-align:left;color:var(--muted)">${esc(l.note || "")}</td></tr>`).join("")
      : `<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:1rem">No trade in this solution can go to Deribit.</td></tr>`;
    $("xb-review-skipped").innerHTML = skipped.length
      ? `<div style="font-weight:600;margin-bottom:.25rem">Not sent (${skipped.length})</div>`
        + skipped.map((s) => `<div>${esc(s.trade)} — <span style="color:var(--muted)">${esc(s.reason)}</span></div>`).join("")
      : "";
    $("xb-r-create").disabled = !legs.length;
  }

  // ── review as Block RFQ (preview only) ────────────────────────
  async function rfqPreview() {
    if (!review) return;
    Object.values(live).forEach((x) => clearTimeout(x.timer));   // the cards are redrawn: stop watching old ones
    Object.keys(live).forEach((k) => delete live[k]);
    const n = (id) => parseFloat($(id).value);
    $("xb-rfq").innerHTML = `<div style="color:var(--muted);font-size:.78rem">Reading the Deribit books and fitting the spline…</div>`;
    let d;
    try {
      d = await api("POST", "/api/execution-bot/rfq-preview", { trades: review.trades, asset: review.meta.asset || "ETH",
        unit: Math.max(1, Math.round(n("xb-q-unit"))), allowance_vp: n("xb-q-vp"), box_bps: n("xb-q-bps"),
        hedge: $("xb-q-hedge").checked });
    } catch (e) {
      $("xb-rfq").innerHTML = `<div style="color:var(--red);font-size:.78rem">${esc(e.message)}</div>`;
      return;
    }
    review.rfq = d;
    const side = (s) => s === "buy" ? "Buy" : "Sell";
    const pay = (v) => v == null ? "—" : v >= 0 ? `pay ${usd(v)}` : `receive ${usd(-v)}`;
    const card = (s, i) => {
      const t = s.total;
      const title = s.kind === "box" ? `Box · ${s.expiry}` : `${s.expiry} legs`;
      const delta = s.hedge
        ? `hedge: ${side(s.hedge.direction)} $${fmt(s.hedge.amount)} ${esc(s.hedge.instrument_name)} (${fmt(s.hedge.delta_eth, 1)} ETH delta)`
        : `delta ${fmt(s.delta_total_eth, 1)} ETH (${usd(s.delta_total_usd, 0)}), not hedged`;
      return `<div style="border:1px solid var(--border);border-radius:6px;padding:.6rem .75rem;margin-bottom:.75rem">
        <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:.5rem;font-size:.82rem">
          <b>${esc(title)} · amount ${fmt(s.amount)} × ratios</b>
          <span style="color:var(--muted)">${esc(delta)}</span></div>
        ${s.fit_warning ? `<div style="color:var(--red);font-size:.75rem;margin-top:.3rem">Spline off its quotes: fair, limit and screen cost below are not reliable. Wait before sending.</div>` : ""}
        <div class="chain-wrapper" style="margin-top:.4rem"><table class="pf-table">
          <thead><tr><th style="text-align:left">Leg</th><th>Ratio</th><th>Contracts</th><th>Fair ($)</th><th>Bid ($)</th><th>Ask ($)</th>
            <th>Vol</th><th>Vega</th><th>Delta</th><th>Screen ($/contract)</th></tr></thead>
          <tbody>${s.legs.map((l) => `<tr><td style="text-align:left">${side(l.side)} ${esc(l.instrument)}</td><td>${l.ratio}</td>
            <td>${fmt(l.qty)}</td><td>${usd(l.fair_usd)}</td><td>${usd(l.bid_usd)}</td><td>${usd(l.ask_usd)}</td>
            <td>${fmt(l.iv, 1)}%</td><td>${fmt(l.vega, 2)}</td><td>${fmt(l.delta, 2)}</td><td>${usd(l.cross_usd)}</td></tr>`).join("")}</tbody>
        </table></div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(11rem,1fr));gap:.4rem .9rem;font-size:.78rem;margin-top:.5rem">
          <div><span style="color:var(--muted)">Fair, per unit</span><br>${pay(s.fair_usd)} <span style="color:var(--muted)">(${fmt(s.fair_eth, 4)} ETH)</span></div>
          <div><span style="color:var(--muted)">Limit, per unit</span><br>${pay(s.limit_usd)} <span style="color:var(--muted)">(${fmt(s.limit_eth, 4)} ETH)</span></div>
          <div><span style="color:var(--muted)">Allowance</span><br>${usd(t.allowance_usd, 0)} <span style="color:var(--muted)">· ${esc(s.allowance_basis)}</span></div>
          <div><span style="color:var(--muted)">Screen, all legs</span><br>${usd(t.screen_cost_usd, 0)} <span style="color:var(--muted)">fees ${usd(t.screen_fee_usd, 0)} included</span></div>
          <div><span style="color:var(--muted)">RFQ fees (est.)</span><br>${usd(t.rfq_fee_usd, 0)}</div>
          <div><span style="color:var(--muted)">Total at fair</span><br>${pay(t.fair_usd)}</div>
        </div>
        <div style="margin-top:.5rem;display:flex;gap:.5rem;align-items:center;flex-wrap:wrap">
          <button class="btn-secondary xb-rfq-send" data-i="${i}" ${s.fit_warning ? "disabled title=\"the limit comes from a spline off its quotes: price again in a few minutes\""
            : s.hedge ? "disabled title=\"the hedge leg is not sent yet: untick Hedge\"" : ""}>Send as RFQ (testnet)</button>
          <span style="font-size:.72rem;color:var(--muted)">Makers quote for a few minutes; you accept or cancel here.</span></div>
        <div class="xb-rfq-live" data-i="${i}"></div></div>`;
    };
    const left = d.leftovers.length
      ? `<div style="font-size:.78rem;margin-top:.25rem"><b>To the bot on screen (${d.leftovers.length})</b>`
        + d.leftovers.map((x) => `<div>${side(x.side)} ${fmt(x.qty)} ${esc(x.instrument)} — <span style="color:var(--muted)">${esc(x.why)}</span></div>`).join("")
        + `<button class="btn-secondary" id="xb-rfq-left" style="margin-top:.35rem">Leftovers to the bot (paper, with the settings above)</button>`
        + `</div>` : "";
    const sk = d.skipped.length
      ? `<div style="font-size:.78rem;margin-top:.5rem"><b>Not in an RFQ (${d.skipped.length})</b>`
        + d.skipped.map((s) => `<div>${esc(s.trade)} — <span style="color:var(--muted)">${esc(s.reason)}</span></div>`).join("") + `</div>` : "";
    $("xb-rfq").innerHTML = warnBox(d.warnings) + (d.structures.length ? d.structures.map(card).join("")
      : `<div style="color:var(--muted);font-size:.78rem">No Deribit structure in this solution.</div>`)
      + left + sk + `<div style="font-size:.72rem;color:var(--muted);margin-top:.4rem">Priced ${when(d.asof)}.</div>`;
    document.querySelectorAll(".xb-rfq-send").forEach((b) => b.addEventListener("click", () => rfqSend(+b.dataset.i, b)));
    $("xb-rfq-left")?.addEventListener("click", leftoversToBot);
  }

  // ── Block RFQ desk (testnet): send, watch the quotes, accept or cancel ──

  async function rfqSend(i, btn) {
    const s = review.rfq.structures[i];
    btn.disabled = true;
    const slot = document.querySelector(`.xb-rfq-live[data-i="${i}"]`);
    slot.innerHTML = `<div style="font-size:.78rem;color:var(--muted);margin-top:.4rem">Sending to Deribit testnet…</div>`;
    try {
      const v = await api("POST", "/api/execution-bot/rfq/send", { kind: s.kind, amount: s.amount, limit_eth: s.limit_eth, spot: s.S,
        legs: s.legs.map((l) => ({ instrument: l.instrument, side: l.side, qty: l.qty })) });
      live[i] = { id: v.id };
      rfqRender(i, v);
      rfqPoll(i);
    } catch (e) {
      slot.innerHTML = `<div style="font-size:.78rem;color:var(--red);margin-top:.4rem">${esc(e.message)}</div>`;
      btn.disabled = false;
    }
  }

  function rfqPoll(i) {
    clearTimeout(live[i]?.timer);
    live[i].timer = setTimeout(async () => {
      if (!live[i] || !review) return;
      try {
        const v = await api("GET", `/api/execution-bot/rfq/${live[i].id}`);
        rfqRender(i, v);
        if (v.state === "open" || v.state === "created") rfqPoll(i);
      } catch (e) {
        const slot = document.querySelector(`.xb-rfq-live[data-i="${i}"] .xb-rfq-err`);
        if (slot) slot.textContent = e.message;
        rfqPoll(i);
      }
    }, 2000);
  }

  function rfqRender(i, v) {
    const slot = document.querySelector(`.xb-rfq-live[data-i="${i}"]`);
    if (!slot) return;
    const pay = (x) => x == null ? "—" : x >= 0 ? `pay ${usd(x)}` : `receive ${usd(-x)}`;
    const open = v.state === "open" || v.state === "created";
    const secs = Math.round(v.seconds_left);
    const quotes = !v.quotes_visible ? `<div style="color:var(--muted)">Quotes show after Deribit's 5-second grace…</div>`
      : v.quotes.length ? `<table class="pf-table" style="margin-top:.3rem"><thead><tr><th style="text-align:left">Quote, per unit</th>
          <th>Total</th><th>Amount</th><th>Kind</th><th style="text-align:left">Makers</th><th>At our limit?</th></tr></thead><tbody>`
          + v.quotes.map((q) => `<tr><td style="text-align:left">${pay(q.ours_usd)} <span style="color:var(--muted)">(${fmt(q.ours_eth, 5)} ETH)</span></td>
            <td>${pay(q.ours_usd * v.amount)}</td><td>${fmt(q.amount)}</td><td>${q.all_or_none ? "all or none" : "any part"}</td>
            <td style="text-align:left">${esc(q.makers.join(", "))}</td><td style="color:${q.inside ? "var(--green)" : "var(--red)"}">${q.inside ? "yes" : "no"}</td></tr>`).join("")
          + `</tbody></table>`
        : `<div style="color:var(--muted)">No quote yet.</div>`;
    const acc = v.acceptance ? `<div style="margin-top:.3rem;color:${v.acceptance.filled ? "var(--green)" : "var(--red)"}">`
        + (v.acceptance.filled ? `Filled: ${v.acceptance.trades.map((t) => `${t.direction} ${fmt(t.amount)} ${esc(t.instrument_name)} @ ${t.price}`).join(" · ")}`
          : `Not filled: ${esc(v.acceptance.reason)}`) + `</div>` : "";
    const canAccept = open && v.quotes_visible && v.best && v.best.inside;
    slot.innerHTML = `<div style="border-top:1px dashed var(--border);margin-top:.5rem;padding-top:.45rem;font-size:.78rem">
      <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:.5rem">
        <b>RFQ #${v.id} · ${esc(v.network)} · ${esc(v.state)}${open ? ` · ${secs} s left` : ""}</b>
        <span style="color:var(--muted)">our limit ${pay(v.limit_usd)} per unit · ${pay(v.limit_total_usd)} in all ·
          Deribit writes it ${v.side === "buy" ? "as sent" : "reversed (we sell it)"} · amount ${fmt(v.amount)}</span></div>
      ${quotes}
      ${v.best ? `<div style="margin-top:.3rem">Best: ${pay(v.best.ours_usd)} per unit, ${pay(v.best.total_usd)} in all</div>` : ""}
      ${acc}
      <div class="xb-rfq-err" style="color:var(--red)"></div>
      ${open ? `<div style="margin-top:.4rem;display:flex;gap:.5rem">
        <button class="btn-primary xb-rfq-accept" ${canAccept ? "" : "disabled"}>Accept the best quote</button>
        <button class="btn-secondary xb-rfq-cancel">Cancel RFQ</button></div>` : ""}</div>`;
    slot.querySelector(".xb-rfq-accept")?.addEventListener("click", () => rfqAct(i, "accept", v));
    slot.querySelector(".xb-rfq-cancel")?.addEventListener("click", () => rfqAct(i, "cancel", v));
  }

  async function rfqAct(i, action, v) {
    if (action === "accept" && !confirm(`Accept RFQ #${v.id} on ${v.network}: ${v.best.total_usd >= 0 ? "pay" : "receive"} `
        + `${usd(Math.abs(v.best.total_usd), 0)} for the whole structure (our limit ${usd(Math.abs(v.limit_total_usd), 0)})?`)) return;
    try {
      rfqRender(i, await api("POST", `/api/execution-bot/rfq/${v.id}/${action}`));
    } catch (e) {
      const el = document.querySelector(`.xb-rfq-live[data-i="${i}"] .xb-rfq-err`);
      if (el) el.textContent = e.message;
    }
  }

  async function leftoversToBot() {
    const p = params();
    const orders = review.rfq.leftovers.map((x) => ({ instrument: x.instrument, side: x.side, qty: x.qty, ...p, mode: "paper",
      note: `${review.meta.source || "Optimizer"}: RFQ leftover (${x.why})`.slice(0, 500) }));
    $("xb-rfq-left").disabled = true;
    try {
      const r = await api("POST", "/api/execution-bot/orders/batch", { orders });
      const refused = r.refused.map((x) => `${x.instrument}: ${x.reason}`).join("; ");
      status(`${r.created.length} leftover paper order(s) created.` + (refused ? ` Refused: ${refused}` : ""), !!refused);
      await load();
    } catch (e) {
      status("Create failed: " + e.message, true);
      $("xb-rfq-left").disabled = false;
    }
  }

  async function createFromReview() {
    const p = params();
    const qtys = {};
    document.querySelectorAll(".xb-r-qty").forEach((el) => { qtys[el.dataset.i] = parseFloat(el.value); });
    const picked = [...document.querySelectorAll(".xb-r-pick")].filter((el) => el.checked).map((el) => el.dataset.i);
    if (!picked.length) { status("Tick at least one order.", true); return; }
    const orders = picked.map((i) => {
      const l = review.data.legs[i];
      return { instrument: l.instrument, side: l.side, qty: qtys[i], ...p, mode: "paper",
        note: `${review.meta.source || "Optimizer"}: ${l.sources.join(" + ")}`.slice(0, 500) };
    });
    $("xb-r-create").disabled = true;
    try {
      const r = await api("POST", "/api/execution-bot/orders/batch", { orders });
      const refused = r.refused.map((x) => `${x.instrument}: ${x.reason}`).join("; ");
      status(`${r.created.length} paper order(s) created.` + (refused ? ` Refused: ${refused}` : ""), !!refused);
      review = null;
      $("xb-review").style.display = "none";
      await load();
    } catch (e) {
      status("Create failed: " + e.message, true);
      $("xb-r-create").disabled = false;
    }
  }

  window.execbotReceive = function (trades, meta) {
    review = { trades, meta: meta || {}, data: null };
    Object.values(live).forEach((x) => clearTimeout(x.timer));
    Object.keys(live).forEach((k) => delete live[k]);
    $("xb-rfq").innerHTML = "";
    document.querySelector('.nav-item[data-page="execbot"]')?.click();
    reprice();
    $("xb-review").scrollIntoView({ behavior: "smooth", block: "start" });
  };

  // ── manual order ──────────────────────────────────────────────
  async function createManual() {
    const v = (id) => $(id).value.trim();
    const n = (id) => parseFloat($(id).value);
    try {
      const r = await api("POST", "/api/execution-bot/orders", {
        instrument: v("xb-n-inst").toUpperCase(), side: v("xb-n-side"), qty: n("xb-n-qty"), hours: n("xb-n-hours"),
        start_vp: n("xb-n-start"), end_vp: n("xb-n-end"), hard_vp: n("xb-n-hard"), cap: n("xb-n-cap"),
        mode: "paper", note: v("xb-n-note") });
      status(`Paper order #${r.id} created.`);
      $("xb-new").style.display = "none";
      selected = r.id;
      await load();
    } catch (e) {
      status("Create failed: " + e.message, true);
    }
  }

  // ── wiring ────────────────────────────────────────────────────
  document.querySelector('.nav-item[data-page="execbot"]')?.addEventListener("click", load);
  $("xb-refresh")?.addEventListener("click", load);
  $("xb-new-btn")?.addEventListener("click", () => { $("xb-new").style.display = $("xb-new").style.display === "none" ? "" : "none"; });
  $("xb-n-submit")?.addEventListener("click", createManual);
  $("xb-r-reprice")?.addEventListener("click", reprice);
  $("xb-r-create")?.addEventListener("click", createFromReview);
  $("xb-r-discard")?.addEventListener("click", () => { review = null; $("xb-review").style.display = "none"; });
  $("xb-q-go")?.addEventListener("click", rfqPreview);
  $("xb-orders")?.addEventListener("click", async (ev) => {
    const b = ev.target.closest("button");
    if (b) {
      ev.stopPropagation();
      const id = parseInt(b.dataset.id, 10);
      if (b.dataset.act === "cancel" && !confirm(`Cancel order #${id}? What is unfilled stays unfilled.`)) return;
      try {
        await api("POST", `/api/execution-bot/orders/${id}/${b.dataset.act}`);
        status(`Order #${id} ${{ pause: "paused", resume: "resumed", cancel: "cancelled" }[b.dataset.act]}.`);
      } catch (e) { status(e.message, true); }
      await load();
      return;
    }
    const tr = ev.target.closest("tr[data-id]");
    if (tr) { selected = parseInt(tr.dataset.id, 10); await load(); }
  });
  $("xb-costs")?.addEventListener("click", async (ev) => {
    const tr = ev.target.closest("tr[data-id]");
    if (tr) { selected = parseInt(tr.dataset.id, 10); await load(); }
  });
  setInterval(() => { if (active() && !document.hidden) load(); }, 30000);
})();
