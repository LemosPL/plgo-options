// Execution Bot page (/api/execution-bot): parent orders worked by the server
// every 5 seconds, crossing only when the book is cheap against the smoothing spline.
// Optimizer v2's "Send to execution" lands here through window.execbotReceive.
(function () {
  const ACTION = { cooldown: "resting", no_book: "no book", no_fill: "no fill", connector_down: "connector down" };
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
  let lastStatus = null;   // the last /status: caps, today's use, halts (for the confirm dialog)

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
    lastStatus = st;
    ["testnet", "live"].forEach((m) => document.querySelectorAll(`.xb-mode option[value=${m}]`).forEach((o) => {   // offered only when the server allows it
      o.disabled = !(st.modes && st.modes[m]);
      if (o.disabled && o.selected) o.parentElement.value = "paper";
    }));
    const halts = st.halts || {};
    $("xb-halts").innerHTML = Object.entries(halts).map(([m, h]) => `<div style="border:1px solid var(--red);border-left-width:4px;border-radius:6px;
      padding:.45rem .65rem;margin:.5rem 0;font-size:.8rem"><b style="color:var(--red)">${esc(m)} halted</b> since ${when(h.ts)}: ${esc(h.reason)}.
      Nothing is sent in ${esc(m)} until the account has been checked against the page.
      <button class="btn-secondary xb-clear-halt" data-mode="${esc(m)}" style="margin-left:.4rem">Checked: clear the halt</button></div>`).join("");
    const cp = st.caps || {}, td = st.today || {};
    const used = (m) => td[m] ? `${esc(m)} today: ${fmt(td[m].contracts)} of ${fmt(cp.day_contracts)} contracts, $${fmt(td[m].premium_usd)} of $${fmt(cp.day_premium_usd)}` : "";
    const card = (label, value, sub, style = "") => `<div class="collat-metric"><div class="collat-metric-label">${label}</div>
      <div class="collat-metric-value" style="${style}">${value}</div><div class="collat-metric-sub">${sub}</div></div>`;
    const c = st.counts || {};
    const lt = st.last_tick || {};
    $("xb-cards").innerHTML =
      card("Modes", ["Paper", st.modes && st.modes.testnet && "Testnet", st.modes && st.modes.live && "Live"].filter(Boolean).join(" · "),
        [st.modes && st.modes.testnet ? used("testnet") : "no testnet key on this server", st.modes && st.modes.live ? used("live") : esc(st.live_note || ""),
          `max ${fmt(cp.order_contracts)} contracts an order`].join(" · "), Object.keys(halts).length ? "color:var(--red)" : "")
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
        <td style="color:${stColor}">${esc(o.status)}${leftoverTag(o)}</td><td style="white-space:nowrap">${acts}</td></tr>`;
    }).join("");
  }

  // ── cost per leg ──────────────────────────────────────────────
  function costs(legs) {
    if (!legs.length) {
      $("xb-costs").innerHTML = `<tr><td colspan="11" style="text-align:center;color:var(--muted);padding:1rem">No fills yet.</td></tr>`;
      return;
    }
    const vp = (v) => v == null ? "—" : fmt(v, 2);
    const all = legs;
    // vega-weighted over the legs that have the reference
    const poolOf = (legs) => (name) => {
      const w = legs.reduce((a, g) => a + (g[name + "_vp"] != null ? g[name + "_vega"] : 0), 0);
      return w ? legs.reduce((a, g) => a + (g[name + "_vp"] != null ? g[name + "_vp"] * g[name + "_vega"] : 0), 0) / w : null;
    };
    const sumOf = (legs) => (k) => legs.some((g) => g[k] != null) ? legs.reduce((a, g) => a + (g[k] || 0), 0) : null;
    const allIn = (m, f) => m != null && f != null ? m + f : null;
    const partial = (g, name) => g[name + "_qty"] != null && g[name + "_qty"] < g.filled
      ? ` <span style="color:var(--muted)" title="covers ${fmt(g[name + "_qty"])} of ${fmt(g.filled)} contracts">*</span>` : "";
    const tag = (g) => (g.via === "rfq" ? ` <b style="color:var(--accent, #4f8cff)">RFQ</b>` : "") + (g.mode === "paper" ? "" : ` <b>${esc(g.mode)}</b>`);
    const row = (g) => `<tr data-id="${g.id}" style="cursor:pointer">
      <td>${g.id}</td><td style="text-align:left">${g.side === "buy" ? "Buy" : "Sell"} ${esc(g.instrument)}${tag(g)}</td>
      <td>${fmt(g.filled)} / ${fmt(g.order_qty)}${g.status === "working" ? "" : ` <span style="color:var(--muted)">${esc(g.status)}</span>`}</td>
      <td>${vp(g.mid_vp)}${partial(g, "mid")}</td><td>${vp(g.spline_vp)}</td><td>${vp(g.mark_vp)}${partial(g, "mark")}</td>
      <td>${vp(g.fee_vp)}</td><td>${vp(allIn(g.mark_vp, g.fee_vp))}</td>
      <td>${usd(g.spline_usd, 0)}</td><td>${usd(g.mark_usd, 0)}</td><td>${usd(g.fee_usd, 0)}</td></tr>`;
    // one total per route when both are there: on screen (the bot) and Block RFQ
    const groups = [["All", all]];
    const bot = all.filter((g) => g.via !== "rfq"), rfqs = all.filter((g) => g.via === "rfq");
    if (bot.length && rfqs.length) groups.push(["All on screen (bot)", bot], ["All Block RFQ", rfqs]);
    const total = ([name, legs], i) => {
      const pool = poolOf(legs), sum = sumOf(legs);
      return `<tr style="font-weight:600;${i === 0 ? "border-top:2px solid var(--border)" : ""}"><td></td><td style="text-align:left">${name}</td>
      <td>${fmt(legs.reduce((a, g) => a + g.filled, 0))}</td><td>${vp(pool("mid"))}</td><td>${vp(pool("spline"))}</td>
      <td>${vp(pool("mark"))}</td><td>${vp(pool("fee"))}</td><td>${vp(allIn(pool("mark"), pool("fee")))}</td>
      <td>${usd(sum("spline_usd"), 0)}</td><td>${usd(sum("mark_usd"), 0)}</td><td>${usd(sum("fee_usd"), 0)}</td></tr>`;
    };
    $("xb-costs").innerHTML = all.map(row).join("") + groups.map(total).join("") + allInRow(all);
  }

  // All-in, as the replay counts it: the fills against the spline, their fees, and what expired orders left
  // at the deadline crossed at the touch then (fee included, a lower bound); vol points over all their vega
  function allInRow(legs) {
    const left = legs.filter((g) => g.left_qty > 0);
    if (!left.length) return "";
    const priced = left.filter((g) => g.left_usd != null);
    const sum = (xs, f) => xs.reduce((a, g) => a + (f(g) || 0), 0);
    const fillsUsd = sum(legs, (g) => (g.spline_vp ?? 0) * g.spline_vega + (g.fee_vp ?? 0) * g.fee_vega);
    const leftUsd = sum(priced, (g) => g.left_usd), vega = sum(legs, (g) => g.spline_vega) + sum(priced, (g) => g.left_vega);
    const qLeft = sum(left, (g) => g.left_qty), qUnpriced = sum(left.filter((g) => g.left_usd == null), (g) => g.left_qty);
    const leftVp = sum(priced, (g) => g.left_vega) ? leftUsd / sum(priced, (g) => g.left_vega) : null;
    const usdAll = sum(legs, (g) => g.spline_usd) + sum(legs, (g) => g.fee_usd) + leftUsd;
    const tip = `fills vs the spline + fees + ${fmt(qLeft - qUnpriced)} contracts left at expired orders' deadlines, crossed at the touch`
      + ` on their last priced tick (${leftVp == null ? "—" : fmt(leftVp, 2) + " vp"}, fee included; a lower bound)`
      + (qUnpriced ? `; ${fmt(qUnpriced)} more left with no price on any tick, not counted` : "");
    return `<tr style="font-weight:600;border-top:2px solid var(--border)" title="${esc(tip)}"><td></td>
      <td style="text-align:left">All-in: fees and the remainder in</td>
      <td>${fmt(sum(legs, (g) => g.filled))} + ${fmt(qLeft)} left${qUnpriced ? ` <span style="color:var(--muted)">(${fmt(qUnpriced)} unpriced)</span>` : ""}</td>
      <td>—</td><td>${fmt((fillsUsd + leftUsd) / vega, 2)}</td><td>—</td><td>—</td><td>—</td>
      <td>${usd(usdAll, 0)}</td><td>—</td><td>—</td></tr>`;
  }

  // ── markouts ──────────────────────────────────────────────────
  async function markoutsLoad() {
    const q = new URLSearchParams({ days: $("xb-mo-days").value });
    if ($("xb-mo-mode").value) q.set("mode", $("xb-mo-mode").value);
    const d = await api("GET", "/api/execution-bot/markouts?" + q);
    const lbl = (h) => h < 60 ? `${h} s` : h < 3600 ? `${h / 60} min` : `${h / 3600} h`;
    $("xb-mo-head").innerHTML = `<tr><th style="text-align:left">Fills</th><th></th>${d.horizons.map((h) => `<th>${lbl(h)}</th>`).join("")}</tr>`;
    const color = (v) => v == null ? "" : v < -0.05 ? "color:var(--red)" : v > 0.05 ? "color:var(--green)" : "";
    const cell = (c, k) => c[k] == null ? `<td style="color:var(--muted)">—</td>`
      : `<td style="${color(c[k])}" title="${c.n} fill(s)">${fmt(c[k], 2)}</td>`;
    const rows2 = (name, cells, bold) => ["spline", "mid"].map((k, i) => `<tr${bold && i === 0 ? ' style="font-weight:600;border-top:2px solid var(--border)"' : ""}>
      <td style="text-align:left">${i === 0 ? name : ""}</td><td style="text-align:left;color:var(--muted)">${k === "spline" ? "Spline" : "vs mid"}</td>
      ${d.horizons.map((h) => cell(cells[h], k)).join("")}</tr>`).join("");
    if (!d.legs.length) {
      $("xb-mo").innerHTML = `<tr><td colspan="${d.horizons.length + 2}" style="text-align:center;color:var(--muted);padding:1rem">
        No markouts yet: they start with the next fills.</td></tr>`;
      return;
    }
    $("xb-mo").innerHTML = d.legs.map((g) => rows2(`#${g.id} ${g.side === "buy" ? "Buy" : "Sell"} ${esc(g.instrument)}`
        + `${g.via === "rfq" ? " <b>RFQ</b>" : ""}${g.mode === "paper" ? "" : ` <b>${esc(g.mode)}</b>`} <span style="color:var(--muted)">· ${fmt(g.fills)} fill(s), ${fmt(g.qty)}</span>`, g.cells)).join("")
      + rows2("All", d.all, true);
  }
  ["xb-mo-mode", "xb-mo-days"].forEach((id) => $(id)?.addEventListener("change", () => markoutsLoad().catch((e) => status(e.message, true))));

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
    ["xb-r-hours", "xb-n-hours"].forEach((id) => deadlineHint(id));
    try {
      const [st, os, cs] = await Promise.all([api("GET", "/api/execution-bot/status"), api("GET", "/api/execution-bot/orders"),
        api("GET", "/api/execution-bot/costs")]);
      header(st);
      rows(os.orders);
      restButton(os.orders);
      costs(cs.legs);
      await Promise.all([detail(), markoutsLoad()]);
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
    return { hours: n("xb-r-hours"), start_vp: n("xb-r-start"), end_vp: n("xb-r-end"), hard_vp: n("xb-r-hard"), cap: n("xb-r-cap"),
      mode: $("xb-r-mode").value, leftover_rfq: $("xb-r-left").checked };
  }

  // testnet and live orders are sent to Deribit: the server creates them only once confirmed
  function confirmVenue(orders, mode) {
    if (mode === "paper") return true;
    const st = lastStatus || {}, cp = st.caps || {}, td = (st.today || {})[mode] || {};
    const over = orders.filter((o) => o.qty > cp.order_contracts);
    const lines = orders.slice(0, 12).map((o) => `  ${o.side} ${o.qty} ${o.instrument}`).join("\n") + (orders.length > 12 ? `\n  ... and ${orders.length - 12} more` : "");
    const ok = confirm(`Send ${orders.length} order(s) to Deribit ${mode === "live" ? "PRODUCTION (real money)" : "testnet"}?\n\n${lines}\n\n`
      + `Today in ${mode}: ${fmt(td.contracts || 0)} of ${fmt(cp.day_contracts)} contracts, $${fmt(td.premium_usd || 0)} of $${fmt(cp.day_premium_usd)} premium.`
      + (over.length ? `\n${over.length} order(s) over the ${fmt(cp.order_contracts)}-contract cap will be refused.` : ""));
    if (ok) orders.forEach((o) => { o.confirmed = true; });
    return ok;
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
        ${s.screen_cost_usd != null && s.kind !== "box" ? `<div style="font-size:.78rem;margin-top:.45rem;padding:.35rem .55rem;border:1px solid var(--border);border-radius:4px">
          <b>Rule:</b> accept a quote at or better than <b>${pay(s.limit_usd)}</b> per unit (our limit) ·
          worse than <b>${pay(s.fair_usd + s.screen_cost_usd - s.rfq_fee_usd)}</b> the screen is cheaper: cancel</div>` : ""}
        <div style="margin-top:.5rem;display:flex;gap:.5rem;align-items:center;flex-wrap:wrap">
          <button class="btn-secondary xb-rfq-send" data-i="${i}" ${s.fit_warning ? "disabled title=\"the limit comes from a spline off its quotes: price again in a few minutes\""
            : s.hedge ? "disabled title=\"the hedge leg is not sent yet: untick Hedge\"" : ""}>Send as RFQ (testnet)</button>
          <span style="font-size:.72rem;color:var(--muted)">Makers quote for a few minutes; you accept or cancel here.</span></div>
        <div class="xb-rfq-live" data-i="${i}"></div></div>`;
    };
    const left = d.leftovers.length
      ? `<div style="font-size:.78rem;margin-top:.25rem"><b>To the bot on screen (${d.leftovers.length})</b>`
        + d.leftovers.map((x) => `<div>${side(x.side)} ${fmt(x.qty)} ${esc(x.instrument)} — <span style="color:var(--muted)">${esc(x.why)}</span></div>`).join("")
        + `<button class="btn-secondary" id="xb-rfq-left" style="margin-top:.35rem">Leftovers to the bot (with the settings and mode above)</button>`
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
          : `Not filled: ${esc(v.acceptance.reason)}`)
        + (v.acceptance.recorded ? `<div style="color:var(--muted)">In the cost table as order(s) ${v.acceptance.recorded.map((x) => "#" + x).join(", ")}.</div>` : "")
        + (v.acceptance.record_error ? `<div style="color:var(--red)">Not in the cost table: ${esc(v.acceptance.record_error)}</div>` : "")
        + `</div>` : "";
    const canAccept = open && v.quotes_visible && v.best && v.best.inside;
    slot.innerHTML = `<div style="border-top:1px dashed var(--border);margin-top:.5rem;padding-top:.45rem;font-size:.78rem">
      <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:.5rem">
        <b>RFQ #${v.id} · ${esc(v.network)} · ${esc(v.state)}${open ? ` · ${secs} s left` : ""}</b>
        <span style="color:var(--muted)">our limit ${pay(v.limit_usd)} per unit · ${pay(v.limit_total_usd)} in all ·
          Deribit writes it ${v.side === "buy" ? "as sent" : "reversed (we sell it)"} · amount ${fmt(v.amount)}</span></div>
      ${quotes}
      ${v.best ? `<div style="margin-top:.3rem">Best: ${pay(v.best.ours_usd)} per unit, ${pay(v.best.total_usd)} in all</div>` : ""}
      ${open ? rfqVerdict(review?.rfq?.structures[i], v) : ""}
      ${acc}
      <div class="xb-rfq-err" style="color:var(--red)"></div>
      ${open ? `<div style="margin-top:.4rem;display:flex;gap:.5rem">
        <button class="btn-primary xb-rfq-accept" ${canAccept ? "" : "disabled"}>Accept the best quote</button>
        <button class="btn-secondary xb-rfq-cancel">Cancel RFQ</button></div>` : ""}</div>`;
    slot.querySelector(".xb-rfq-accept")?.addEventListener("click", () => rfqAct(i, "accept", v));
    slot.querySelector(".xb-rfq-cancel")?.addEventListener("click", () => rfqAct(i, "cancel", v));
  }

  // One line that says what to do with the best quote: accept / beats the screen but above our limit / cancel.
  // All in $ for the whole structure, against the spline's fair value, fees included (the preview's figures).
  function rfqVerdict(s, v) {
    const line = (color, text) => `<div style="margin-top:.4rem;padding:.35rem .55rem;border-radius:4px;font-weight:600;`
      + `border:1px solid ${color};color:${color}">${text}</div>`;
    if (!v.quotes_visible || !v.best) return line("var(--muted)", "Waiting for quotes…");
    if (!s || s.screen_cost_usd == null) return v.best.inside ? line("var(--green)", "✅ Accept: the best quote is inside our limit")
      : line("var(--red)", "❌ Above our limit: cancel");
    const rfqCost = v.best.total_usd - s.fair_usd * s.amount + s.rfq_fee_usd * s.amount;   // over fair, fees in
    const screen = s.screen_cost_usd * s.amount;
    const save = screen - rfqCost;
    const vp = s.kind !== "box" && s.vega_usd > 0 ? ` (${fmt(rfqCost / s.amount / s.vega_usd, 2)} vp over fair, fees in)` : "";
    if (v.best.inside) return line("var(--green)", `✅ Accept: saves ${usd(save, 0)} vs the screen${vp}`);
    if (save > 0) return line("var(--orange, #d29922)", `🟡 Beats the screen by ${usd(save, 0)} but is above our limit${vp}: `
      + `raise the allowance and send again, or cancel`);
    return line("var(--red)", `❌ Cancel: the screen is ${usd(-save, 0)} cheaper${vp}`);
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
    const orders = review.rfq.leftovers.map((x) => ({ instrument: x.instrument, side: x.side, qty: x.qty, ...p,
      note: `${review.meta.source || "Optimizer"}: RFQ leftover (${x.why})`.slice(0, 500) }));
    if (!confirmVenue(orders, p.mode)) return;
    $("xb-rfq-left").disabled = true;
    try {
      const r = await api("POST", "/api/execution-bot/orders/batch", { orders });
      const refused = r.refused.map((x) => `${x.instrument}: ${x.reason}`).join("; ");
      status(`${r.created.length} leftover ${p.mode} order(s) created.` + (refused ? ` Refused: ${refused}` : ""), !!refused);
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
      return { instrument: l.instrument, side: l.side, qty: qtys[i], ...p,
        note: `${review.meta.source || "Optimizer"}: ${l.sources.join(" + ")}`.slice(0, 500) };
    });
    if (!confirmVenue(orders, p.mode)) return;
    $("xb-r-create").disabled = true;
    try {
      const r = await api("POST", "/api/execution-bot/orders/batch", { orders });
      const refused = r.refused.map((x) => `${x.instrument}: ${x.reason}`).join("; ");
      status(`${r.created.length} ${p.mode} order(s) created.` + (refused ? ` Refused: ${refused}` : ""), !!refused);
      review = null;
      $("xb-review").style.display = "none";
      await load();
    } catch (e) {
      status("Create failed: " + e.message, true);
      $("xb-r-create").disabled = false;
    }
  }

  // ── the rest at the deadline → Block RFQ ─────────────────────
  // What the bot left unfilled when its orders expired (the last 24 h) goes to the review card as one
  // solution: the RFQ preview groups it by expiry, and what is under Deribit's 250-contract minimum
  // stays a leftover (back to the bot, or left). Nothing is sent until someone clicks Send, then Accept.
  let rests = [];
  // the remainder's way to a Block RFQ (execution/leftovers.py), under the order's status
  function leftover(o) {
    try { return o.leftover_json ? JSON.parse(o.leftover_json) : null; } catch (e) { return null; }
  }
  function leftoverTag(o) {
    const l = leftover(o);
    if (!l) return "";
    const txt = { pending: "RFQ: pricing", sent: "RFQ: quotes", filled: "RFQ: filled", lapsed: "RFQ: lapsed", skipped: "RFQ: not sent" }[l.status] || l.status;
    const color = { filled: "var(--green)", lapsed: "var(--red)", skipped: "var(--muted)" }[l.status] || "var(--orange)";
    return `<div style="font-size:.7rem;color:${color}" title="${esc(l.detail || "")}">${esc(txt)}</div>`;
  }

  function restButton(orders) {
    const since = Date.now() / 1000 - 24 * 3600;
    const auto = (o) => ["pending", "sent", "filled"].includes(leftover(o)?.status);   // the bot has it, or did it
    rests = orders.filter((o) => o.status === "expired" && o.via !== "rfq" && o.qty - o.filled >= 1 && (o.finished_ts || 0) >= since && !auto(o));
    const b = $("xb-rest-rfq");
    if (!b) return;
    b.style.display = rests.length ? "" : "none";
    b.textContent = `Rest → Block RFQ (${rests.length})`;
  }
  function restToRfq() {
    if (!rests.length) return;
    const trades = rests.map((o) => {
      const [, , strike, opt] = o.instrument.split("-");
      return { counterparty: "Deribit", instrument: o.instrument, opt, strike: +strike, qty: Math.floor(o.qty - o.filled),
        side: o.side === "buy" ? "Buy" : "Sell" };
    });
    window.execbotReceive(trades, { source: `Bot rest at the deadline (#${rests.map((o) => o.id).join(", #")})`, asset: "ETH" });
    rfqPreview();
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

  // ── deadline hint: what the replay of 6-17 Nov 2026 found (scripts/execution_replay.py) ──
  // all-in cost (the 9 Nov order without its deep ITM 2200 put): ~0.95 vp at 2 h, ~0.91 at 6 h, ~0.71 at 24 h,
  // of which the fills ~0.6 vp and the rest left at the deadline ~1.1 vp; Saturday (UTC) windows filled under 10%
  function saturdayHours(from, hours) {
    let h = 0;
    for (let t = from; t < from + hours * 3600; t += 900) if (new Date(t * 1000).getUTCDay() === 6) h += 0.25;
    return h;
  }
  function deadlineHint(id) {
    const el = $(id + "-hint"), hours = parseFloat($(id)?.value);
    if (!el) return;
    if (!(hours > 0)) { el.innerHTML = ""; return; }
    const sat = saturdayHours(Date.now() / 1000, hours), parts = [];
    if (hours < 24) parts.push(`<span style="color:var(--muted)">All-in cost in the replay (6-17 Nov, liquid options): ~0.95 vp at 2 h,`
      + ` ~0.91 at 6 h, ~0.71 at 24 h; against crossing it all at the touch at once, 2 h saves nothing measurable, 6 h and`
      + ` 24 h ~0.08-0.09 vp. The fills cost ~0.6 vp, fee included; what is left at the deadline, crossed then, ~1.1 vp.</span>`);
    if (sat >= hours / 4) parts.push(`<span style="color:var(--red)">${fmt(sat, sat % 1 ? 1 : 0)} of these ${fmt(hours, hours % 1 ? 1 : 0)} h fall on Saturday (UTC):`
      + ` in the replay Saturday windows filled under 10%, the rest left for the deadline. Start after the weekend, or give it longer.</span>`);
    el.innerHTML = parts.join(" ");
  }
  ["xb-r-hours", "xb-n-hours"].forEach((id) => { $(id)?.addEventListener("input", () => deadlineHint(id)); deadlineHint(id); });

  // ── New order pickers: underlying, expiry, type, strike → the instrument name ──
  let chain = null;   // /instruments: { asset, index, expiries: [{ code, days, C: [strikes], P: [strikes] }] }
  const expOptions = () => chain.expiries.map((e) =>
    `<option value="${esc(e.code)}">${esc(e.code)} (${e.days < 1 ? fmt(e.days * 24) + " h" : fmt(e.days) + " d"})</option>`).join("");
  // the next quarterly (Deribit's last Friday of Mar, Jun, Sep, Dec) a week or more away, else the first expiry a week or more away
  function defaultExp() {
    const quarterly = (e) => /^(2[2-9]|3[01])(MAR|JUN|SEP|DEC)\d{2}$/.test(e.code);
    const e = chain.expiries.find((x) => quarterly(x) && x.days >= 7) || chain.expiries.find((x) => x.days >= 7) || chain.expiries[0];
    return e ? e.code : "";
  }
  const strikesOf = (code, cp) => (chain && chain.expiries.find((x) => x.code === code) || {})[cp] || [];
  const atmOf = (ks) => ks.length && chain.index ? ks.reduce((a, b) => Math.abs(b - chain.index) < Math.abs(a - chain.index) ? b : a) : null;
  const strikeOptions = (ks) => { const atm = atmOf(ks); return ks.map((k) => `<option value="${k}">${k}${k === atm ? " (ATM)" : ""}</option>`).join(""); };
  async function chainLoad() {
    if (chain) return chain;
    chain = await api("GET", "/api/execution-bot/instruments");
    return chain;
  }

  async function pickersLoad() {
    try {
      await chainLoad();
    } catch (e) {
      $("xb-n-exp").innerHTML = `<option value="">unavailable</option>`;
      status("Could not list Deribit's options: " + e.message + " (type the instrument instead)", true);
      return;
    }
    const cur = $("xb-n-exp").value;
    $("xb-n-exp").innerHTML = expOptions();
    // keep the expiry already picked; otherwise the default
    $("xb-n-exp").value = chain.expiries.some((e) => e.code === cur) ? cur : defaultExp();
    strikesFill();
  }
  function strikesFill(keep) {
    const ks = strikesOf($("xb-n-exp").value, $("xb-n-type").value), atm = atmOf(ks);
    $("xb-n-strike").innerHTML = strikeOptions(ks);
    const want = keep ?? parseInt($("xb-n-strike").dataset.last || "", 10);
    $("xb-n-strike").value = ks.includes(want) ? want : (atm ?? "");
    instFromPickers();
  }
  function instFromPickers() {
    const k = $("xb-n-strike").value, e = $("xb-n-exp").value;
    if (k) $("xb-n-strike").dataset.last = k;
    $("xb-n-inst").value = e && k ? `${$("xb-n-und").value}-${e}-${k}-${$("xb-n-type").value}` : "";
  }
  function pickersFromInst() {   // a pasted or typed name sets the pickers, when Deribit lists it
    const m = /^([A-Z]+)-(\d{1,2}[A-Z]{3}\d{2})-(\d+)-([CP])$/.exec($("xb-n-inst").value.trim().toUpperCase());
    if (!m || !chain) return;
    const e = chain.expiries.find((x) => x.code === m[2]);
    if (!e || !e[m[4]].includes(+m[3])) return;
    $("xb-n-exp").value = m[2];
    $("xb-n-type").value = m[4];
    strikesFill(+m[3]);
  }
  $("xb-n-exp")?.addEventListener("change", () => strikesFill());
  $("xb-n-type")?.addEventListener("change", () => strikesFill());
  $("xb-n-strike")?.addEventListener("change", instFromPickers);
  $("xb-n-inst")?.addEventListener("change", pickersFromInst);

  // ── New RFQ: legs picked by hand, previewed through the same Block RFQ preview as a solution ──
  function rfqLegRow(prev) {
    const tr = document.createElement("tr");
    tr.className = "xb-rl";
    const code = prev ? prev.querySelector(".xb-rl-exp").value : defaultExp();
    tr.innerHTML = `<td><select class="xb-rl-exp" style="width:8.5rem">${expOptions()}</select></td>
      <td><select class="xb-rl-type"><option value="C">Call</option><option value="P">Put</option></select></td>
      <td><select class="xb-rl-strike" style="width:7.5rem"></select></td>
      <td><select class="xb-rl-side"><option value="Buy">Buy</option><option value="Sell">Sell</option></select></td>
      <td><input class="xb-rl-qty" type="number" value="${prev ? prev.querySelector(".xb-rl-qty").value : 300}" min="1" step="1" style="width:6rem"></td>
      <td><button class="btn-secondary xb-rl-del" title="Remove the leg" style="width:auto;margin:0;padding:.15rem .55rem;font-size:.72rem">&times;</button></td>`;
    tr.querySelector(".xb-rl-exp").value = code;
    if (prev) tr.querySelector(".xb-rl-type").value = prev.querySelector(".xb-rl-type").value;
    const fill = () => {
      const ks = strikesOf(tr.querySelector(".xb-rl-exp").value, tr.querySelector(".xb-rl-type").value);
      const sel = tr.querySelector(".xb-rl-strike"), was = parseInt(sel.value, 10);
      sel.innerHTML = strikeOptions(ks);
      sel.value = ks.includes(was) ? was : (atmOf(ks) ?? "");
    };
    tr.querySelector(".xb-rl-exp").addEventListener("change", fill);
    tr.querySelector(".xb-rl-type").addEventListener("change", fill);
    tr.querySelector(".xb-rl-del").addEventListener("click", () => tr.remove());
    fill();
    if (prev) {   // a new leg starts one listed strike above the last one: the usual spread
      const sel = tr.querySelector(".xb-rl-strike"), ks = [...sel.options].map((o) => +o.value);
      const i = ks.indexOf(+prev.querySelector(".xb-rl-strike").value);
      if (i >= 0 && i + 1 < ks.length) sel.value = ks[i + 1];
      tr.querySelector(".xb-rl-side").value = prev.querySelector(".xb-rl-side").value === "Buy" ? "Sell" : "Buy";
    }
    $("xb-rl-body").appendChild(tr);
  }
  async function rfqNewOpen() {
    const box = $("xb-rfq-new");
    if (box.style.display !== "none") { box.style.display = "none"; return; }
    try { await chainLoad(); } catch (e) { status("Could not list Deribit's options: " + e.message, true); return; }
    box.style.display = "";
    if (!$("xb-rl-body").children.length) rfqLegRow();
  }
  function rfqNewPreview() {
    const trades = [...document.querySelectorAll("#xb-rl-body .xb-rl")].map((tr) => {
      const code = tr.querySelector(".xb-rl-exp").value, cp = tr.querySelector(".xb-rl-type").value, k = +tr.querySelector(".xb-rl-strike").value;
      return { counterparty: "Deribit", instrument: `ETH-${code}-${k}-${cp}`, opt: cp, strike: k,
        qty: parseFloat(tr.querySelector(".xb-rl-qty").value), side: tr.querySelector(".xb-rl-side").value };
    }).filter((t) => t.strike && t.qty > 0);
    if (!trades.length) { status("Add at least one leg with a quantity.", true); return; }
    window.execbotReceive(trades, { source: "New RFQ", asset: "ETH" });
    rfqPreview();
  }
  $("xb-rfq-new-btn")?.addEventListener("click", rfqNewOpen);
  $("xb-rl-add")?.addEventListener("click", () => { const rs = document.querySelectorAll("#xb-rl-body .xb-rl"); rfqLegRow(rs[rs.length - 1]); });
  $("xb-rl-go")?.addEventListener("click", rfqNewPreview);

  // ── manual order ──────────────────────────────────────────────
  async function createManual() {
    const v = (id) => $(id).value.trim();
    const n = (id) => parseFloat($(id).value);
    const body = {
      instrument: v("xb-n-inst").toUpperCase(), side: v("xb-n-side"), qty: n("xb-n-qty"), hours: n("xb-n-hours"),
      start_vp: n("xb-n-start"), end_vp: n("xb-n-end"), hard_vp: n("xb-n-hard"), cap: n("xb-n-cap"),
      mode: $("xb-n-mode").value, note: v("xb-n-note"), leftover_rfq: $("xb-n-left").checked };
    if (!confirmVenue([body], body.mode)) return;
    try {
      const r = await api("POST", "/api/execution-bot/orders", body);
      status(`${body.mode[0].toUpperCase() + body.mode.slice(1)} order #${r.id} created.`);
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
  $("xb-halts")?.addEventListener("click", async (ev) => {
    const b = ev.target.closest(".xb-clear-halt");
    if (!b || !confirm(`Clear the ${b.dataset.mode} halt? Do it only once the Deribit account matches the fills on this page.`)) return;
    try { await api("POST", `/api/execution-bot/halt/${b.dataset.mode}/clear`); await load(); } catch (e) { status(e.message, true); }
  });
  $("xb-rest-rfq")?.addEventListener("click", restToRfq);
  $("xb-new-btn")?.addEventListener("click", () => { deadlineHint("xb-n-hours"); if ($("xb-new").style.display === "none") pickersLoad(); $("xb-new").style.display = $("xb-new").style.display === "none" ? "" : "none"; });
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
