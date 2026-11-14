// Forwards Monitor (/api/forwards/monitor, /levels, /history): every forward-
// like position of BOTH assets (booked forwards + synthetic forwards built from
// options), one card each with its stop / entry / spot / target ladder, levels
// edited in the card, and the selected position on the spot chart. Levels are
// spot prices; each refresh re-checks them against live spot.
(function () {
  "use strict";

  const ASSETS = ["ETH", "FIL"];
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const fmt = (v, dp = 0) => (v == null || !isFinite(v)) ? "—"
    : Number(v).toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
  const usd = (v) => (v == null || !isFinite(v)) ? "—" : (v < 0 ? "−$" : "$") + fmt(Math.abs(v));
  const usdK = (v) => {
    if (v == null || !isFinite(v)) return "—";
    const a = Math.abs(v), s = v < 0 ? "−$" : "$";
    return a >= 1e6 ? s + fmt(a / 1e6, 2) + "M" : a >= 1e4 ? s + fmt(a / 1e3, 0) + "k" : s + fmt(a);
  };
  const pct = (v) => (v == null || !isFinite(v)) ? "—" : (v >= 0 ? "+" : "") + fmt(v, 1) + "%";
  const sign = (v) => (v < 0 ? "color:var(--red)" : v > 0 ? "color:var(--green)" : "");
  const dp = (a) => (a === "FIL" ? 4 : 2);
  const px = (a, v) => fmt(v, dp(a));
  const active = () => $("page-fwmonitor")?.classList.contains("active");
  const REFRESH_MS = 60_000;
  const HIST_MS = 10 * 60_000;

  let books = {};          // asset -> /monitor response (or {error})
  let positions = [];      // all positions, each with .asset and .spot
  const hist = {};         // asset -> {data, at}
  let selected = null;     // position key ("ETH|fwd:3") shown on the chart
  let editing = null;      // position key whose level form is open
  let timer = null;
  let lastStatus = {};
  let closedAll = [];      // round trips, all assets (backend match_round_trips)
  let settledAll = [];     // forwards settled at maturity
  let legsAll = [];        // every open booked forward leg (pairing form)
  let tab = "open";
  let pairPrefill = null;  // {asset, a, b} from a card's "mark closed"
  const baseTitle = document.title;

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
    const el = $("fm-status");
    if (!el) return;
    el.textContent = msg || "";
    el.style.color = isErr ? "var(--red)" : "";
  }

  const STATUS = {
    stop_hit: ["STOP HIT", "fw-lv-hit-stop"], target_hit: ["TARGET HIT", "fw-lv-hit-target"],
    near_stop: ["near stop", "fw-lv-near-stop"], near_target: ["near target", "fw-lv-near-target"],
    watching: ["watching", "fw-lv-watch"], none: ["no levels", "fw-lv-watch"],
  };
  const RANK = { stop_hit: 0, target_hit: 1, near_stop: 2, near_target: 3, watching: 4, none: 5 };
  const pill = (s) => { const [t, c] = STATUS[s] || STATUS.none; return `<span class="fw-lv ${c}">${t}</span>`; };
  const pk = (p) => `${p.asset}|${p.key}`;
  const short = (p) => p.side === "sell";
  const label = (p) => `${p.asset} ${p.counterparty} ${short(p) ? "short" : "long"} ${fmt(p.qty)} @ ${px(p.asset, p.forward_price)}`;
  const maturityText = (d) => { const t = new Date(d + "T00:00:00Z"); return isNaN(t) ? d : t.toLocaleDateString("en-GB", { day: "2-digit", month: "short", timeZone: "UTC" }); };

  // ── summary strip ─────────────────────────────────────────────
  function summary() {
    const card = (lab, val, sub, style = "") => `<div class="collat-metric"><div class="collat-metric-label">${lab}</div>
      <div class="collat-metric-value" style="${style}">${val}</div><div class="collat-metric-sub">${sub}</div></div>`;
    let html = "";
    for (const a of ASSETS) {
      const b = books[a];
      if (!b) continue;
      if (b.error) { html += card(a, "—", `<span style="color:var(--red)">${esc(b.error)}</span>`); continue; }
      const s = b.summary;
      html += card(`${a} spot`, "$" + px(a, b.spot),
        s.count ? `net <span style="${sign(s.net_qty)}">${s.net_qty > 0 ? "+" : ""}${fmt(s.net_qty)}</span> ${a} · MTM <span style="${sign(s.mtm)}">${usdK(s.mtm)}</span>` : "no open forwards");
    }
    const tot = positions.reduce((t, p) => t + p.mtm, 0);
    const n = (k) => positions.filter((p) => p.levels.status === k).length;
    const hitS = n("stop_hit"), hitT = n("target_hit"), nearS = n("near_stop"), nearT = n("near_target");
    const next = positions.map((p) => p.maturity_date).sort()[0];
    const nextDays = positions.filter((p) => p.maturity_date === next).map((p) => p.days_to_maturity)[0];
    html += card("Forwards MTM", usdK(tot), `${positions.length} position${positions.length === 1 ? "" : "s"} · all assets`, sign(tot))
      + card("Stops", hitS ? `${hitS} hit` : nearS ? `${nearS} near` : "clear",
        `${positions.filter((p) => p.levels.stop).length} set · near = within 2%`,
        hitS ? "color:var(--red)" : nearS ? "color:var(--orange)" : "")
      + card("Targets", hitT ? `${hitT} hit` : nearT ? `${nearT} near` : "—",
        `${positions.filter((p) => p.levels.target).length} set`,
        hitT ? "color:var(--green)" : "")
      + card("Next maturity", next ? maturityText(next) : "—", next ? `${fmt(nextDays)} days` : "");
    $("fm-summary").innerHTML = html;
  }

  const gkey = (asset, cp, mat) => `${asset}|${String(cp).trim().toLowerCase()}|${String(mat).slice(0, 10)}`;
  const realisedOf = (group) => closedAll.filter((t) => t.group === group).reduce((s, t) => s + t.realised_usd, 0);
  const REASON = { take_profit: ["TP", "fw-lv-hit-target"], stop_loss: ["SL", "fw-lv-hit-stop"], roll: ["roll", "fw-lv-watch"], closed: ["closed", "fw-lv-watch"] };
  const reasonPill = (r) => { const [t, c] = REASON[r] || REASON.closed; return `<span class="fw-lv ${c}">${t}</span>`; };

  // ── P&L line: open MTM + realised (closed trips) + settled, per asset ──
  function pnlLine() {
    const parts = [];
    const tot = { open: 0, closed: 0, settled: 0 };
    for (const a of ASSETS) {
      const s = books[a]?.summary;
      if (!s) continue;
      const o = s.open_mtm ?? s.mtm ?? 0, c = s.realised_closed || 0, st = s.realised_settled || 0;
      tot.open += o; tot.closed += c; tot.settled += st;
      if (!s.count && !c && !st) continue;
      parts.push(`<span class="fm-pnl-a"><b>${a}</b> open <span style="${sign(o)}">${usdK(o)}</span>
        + closed <span style="${sign(c)}">${usdK(c)}</span>${st ? ` + settled <span style="${sign(st)}">${usdK(st)}</span>` : ""}
        = <b style="${sign(o + c + st)}">${usdK(o + c + st)}</b></span>`);
    }
    const T = tot.open + tot.closed + tot.settled;
    $("fm-pnl").innerHTML = `<span class="fm-pnl-h">P&amp;L</span>${parts.join('<span class="fm-pnl-sep">|</span>')}
      <span class="fm-pnl-tot">Total <b style="${sign(T)}">${usd(T)}</b>
      <span class="optv3-muted" title="Open = MTM of what is still open at live spot; closed = gains locked by offsetting trades, paid at maturity; settled = cash already fixed">(open ${usdK(tot.open)} · locked ${usdK(tot.closed + tot.settled)})</span></span>`;
  }

  // ── ladder: target ← entry / spot → stop on one price scale ──────
  function ladder(p) {
    const lv = p.levels, S = p.spot;
    const pts = [S, p.forward_price, lv.stop?.level, lv.target?.level].filter((v) => v != null && isFinite(v));
    let lo = Math.min(...pts), hi = Math.max(...pts);
    const pad = (hi - lo) * 0.12 || hi * 0.03;
    lo -= pad; hi += pad;
    const pos = (v) => ((v - lo) / (hi - lo)) * 100;
    const zone = (a, b, cls) => `<div class="fm-zone ${cls}" style="left:${pos(Math.min(a, b))}%;width:${Math.abs(pos(b) - pos(a))}%"></div>`;
    let zones = "";
    if (lv.stop) zones += short(p) ? zone(lv.stop.level, hi, "fm-zone-loss") : zone(lo, lv.stop.level, "fm-zone-loss");
    if (lv.target) zones += short(p) ? zone(lo, lv.target.level, "fm-zone-win") : zone(lv.target.level, hi, "fm-zone-win");
    // Labels alternate above/below so close levels don't collide.
    const mark = (v, cls, text, up) => `<div class="fm-mark ${cls}" style="left:${pos(v)}%"><div class="fm-tick"></div>
      <div class="fm-tag ${up ? "fm-tag-up" : "fm-tag-dn"}">${text}</div></div>`;
    const a = p.asset;
    return `<div class="fm-ladder">${zones}<div class="fm-axis"></div>
      ${lv.target ? mark(lv.target.level, "fm-m-target", `TP ${px(a, lv.target.level)}`, false) : ""}
      ${lv.stop ? mark(lv.stop.level, "fm-m-stop", `SL ${px(a, lv.stop.level)}`, false) : ""}
      ${mark(p.forward_price, "fm-m-entry", `entry ${px(a, p.forward_price)}`, true)}
      ${mark(S, "fm-m-spot", `spot ${px(a, S)}`, true)}
    </div>`;
  }

  function levelBox(p, x, kind) {
    const isStop = kind === "stop";
    if (!x) return `<div class="fm-lvbox"><div class="fm-lvbox-h">${isStop ? "Stop loss" : "Take profit"}</div><div class="optv3-muted">not set</div></div>`;
    const hit = x.status === "hit", near = x.status === "near";
    const col = hit ? (isStop ? "var(--red)" : "var(--green)") : near ? "var(--orange)" : "";
    return `<div class="fm-lvbox ${hit ? "fm-lvbox-hit" : ""}" style="${col ? "border-color:" + col : ""}">
      <div class="fm-lvbox-h">${isStop ? "Stop loss" : "Take profit"} ${hit ? "<b>· HIT</b>" : near ? "· near" : ""}</div>
      <div class="fm-lvbox-v">$${px(p.asset, x.level)} <span class="optv3-muted" style="font-size:.75rem">${hit ? "reached" : pct(x.dist_pct) + " away"}</span></div>
      <div class="optv3-muted" style="font-size:.72rem">MTM there <span style="${sign(x.mtm_at)}">${usdK(x.mtm_at)}</span>
        · ${usdK(x.mtm_at - p.mtm)} vs now</div></div>`;
  }

  function editor(p) {
    const s = short(p), a = p.asset;
    return `<div class="fm-edit">
      <label>Stop loss <input type="number" step="any" min="0" data-f="sl" value="${p.stop_loss ?? ""}" placeholder="${s ? "above" : "below"} spot"></label>
      <label>Take profit <input type="number" step="any" min="0" data-f="tp" value="${p.take_profit ?? ""}" placeholder="${s ? "below" : "above"} spot"></label>
      <label class="fm-edit-note">Note <input type="text" data-f="note" value="${esc(p.note || "")}" placeholder="e.g. desk order"></label>
      <div class="fm-edit-btns">
        <button class="btn-primary" data-act="save" style="width:auto">Save</button>
        <button class="btn-secondary" data-act="cancel" style="width:auto">Cancel</button>
        ${p.stop_loss || p.take_profit ? `<button class="btn-secondary" data-act="clear" style="width:auto">Remove levels</button>` : ""}
      </div>
      <div class="optv3-muted" style="font-size:.7rem">${s ? "Short" : "Long"}: stop ${s ? "above" : "below"}, target ${s ? "below" : "above"} · spot $${px(a, p.spot)}</div>
    </div>`;
  }

  const lockedOf = (p) => p.group ? realisedOf(p.group)
    : p.kind === "forward" ? realisedOf(gkey(p.asset, p.counterparty, p.maturity_date)) : 0;
  function lockedHtml(p) {
    const r = lockedOf(p);
    return r ? `<div class="fm-realised" title="Locked by closed legs of this position (Closed tab), paid at maturity">+ closed <span style="${sign(r)}">${usdK(r)}</span></div>` : "";
  }

  function positionCards() {
    if (!positions.length) {
      $("fm-positions").innerHTML = `<section class="card" style="text-align:center;color:var(--muted)">No open forwards or synthetic forwards.</section>`;
      return;
    }
    $("fm-positions").innerHTML = positions.map((p) => {
      const k = pk(p), s = short(p);
      const kind = p.kind === "net"
        ? `<span class="tm-fwd-badge" title="Settlement buys close part of the position and sells reopen it: netted">net of ${p.legs.length} forwards</span>`
        : p.kind === "synthetic"
        ? `<span class="tm-fwd-badge" title="${esc(p.legs.join("\n"))}">synthetic</span>${p.embedded ? ` <span class="fw-lv fw-lv-watch" title="This call/put pair is part of a larger deal">in deal</span>` : ""}`
        : `<span class="tm-fwd-badge">forward</span>`;
      const allIn = Math.abs(p.effective_price - p.forward_price) > 1e-12
        ? ` <span class="optv3-muted" title="Net premium ${usd(p.net_premium_usd)}">(all-in ${px(p.asset, p.effective_price)})</span>` : "";
      const st = p.levels.status;
      const edge = st === "stop_hit" ? "fm-card-stop" : st === "target_hit" ? "fm-card-target" : st.startsWith("near") ? "fm-card-near" : "";
      return `<section class="card fm-card ${edge} ${k === selected ? "fm-card-sel" : ""}" data-key="${esc(k)}">
        <div class="fm-card-top">
          <div><span class="fm-asset">${p.asset}</span> <b>${esc(p.counterparty)}</b> ${kind}</div>
          <div>${pill(st)}</div>
        </div>
        <div class="fm-card-mid">
          <div>
            <div class="fm-trade"><span style="color:${s ? "var(--red)" : "var(--green)"};font-weight:700">${s ? "SHORT" : "LONG"}</span>
              ${fmt(p.qty)} ${p.asset} @ ${px(p.asset, p.forward_price)}${p.kind === "net" ? ` <span class="optv3-muted" title="Break-even: sum of signed qty x price over the net qty, so realised gains on closed legs are inside it">net entry</span>` : ""}${allIn}</div>
            <div class="optv3-muted" style="font-size:.74rem">${p.kind === "net" ? "first traded" : "traded"} ${maturityText(p.trade_date)} · matures ${maturityText(p.maturity_date)} (${fmt(p.days_to_maturity)}d)${p.external_ref ? ` · <span title="${esc(p.external_ref)}">ref ${esc(String(p.external_ref).slice(0, 8))}</span>` : ""}</div>
            ${p.kind === "net" ? `<div class="optv3-muted fm-legs">${p.legs.map((t, i) => `<label class="fm-leg"><input type="checkbox" class="fm-leg-check" data-fid="${p.legData[i].forward_id}"> ${esc(t)}</label>`).join("")}</div>`
              : p.legs.length ? `<div class="optv3-muted" style="font-size:.7rem">${esc(p.legs.join(" · "))}</div>` : ""}
          </div>
          <div class="fm-mtm"><div class="optv3-muted" style="font-size:.68rem;text-transform:uppercase;letter-spacing:.05em">MTM open</div>
            <div style="${sign(p.mtm)}">${usdK(p.mtm)}</div>
            ${lockedHtml(p)}</div>
        </div>
        ${ladder(p)}
        <div class="fm-lvrow">${levelBox(p, p.levels.stop, "stop")}${levelBox(p, p.levels.target, "target")}</div>
        ${p.note && k !== editing ? `<div class="optv3-muted fm-note">“${esc(p.note)}”</div>` : ""}
        ${k === editing ? editor(p) : `<div class="fm-card-btns">
          <button class="btn-secondary" data-act="edit" style="width:auto">${p.stop_loss || p.take_profit ? "Edit levels" : "Set levels"}</button>
          <button class="btn-secondary" data-act="chart" style="width:auto">${k === selected ? "On chart ↓" : "Show on chart"}</button>
          ${p.kind === "net" || p.kind === "forward" ? `<button class="btn-secondary" data-act="markclosed" style="width:auto" title="${p.kind === "net" ? "Tick a buy and a sell leg above, then mark them as a closed round trip" : "Pair this forward with the trade that closed it"}">Mark closed…</button>` : ""}</div>`}
      </section>`;
    }).join("");
  }

  // ── price chart: the selected position's asset, its levels, to maturity ──
  function priceChart() {
    if (!window.Plotly) return;
    const p = positions.find((x) => pk(x) === selected);
    if (!p) { Plotly.purge("fm-price-chart"); return; }
    const a = p.asset, h = hist[a]?.data, d = dp(a);
    const cc = typeof chartColors === "function" ? chartColors() : { text: "#c9d1d9", paper: "transparent", plot: "transparent", muted: "#8b949e", grid: "#30363d", legendBg: "transparent" };
    const x = h ? h.ts.map((t) => new Date(t * 1000)) : [];
    const y = h ? h.closes.slice() : [];
    x.push(new Date()); y.push(p.spot);
    const mat = new Date(p.maturity_date + "T08:00:00Z");
    const tradeD = new Date(p.trade_date + "T00:00:00Z");
    const x0 = x[0], x1 = new Date(Math.max(Date.now() + 86400e3, mat.getTime() + 86400e3));
    const traces = [{ x, y, type: "scatter", mode: "lines", name: `${a} spot (hourly)`,
      line: { color: "#58a6ff", width: 1.8 }, hovertemplate: `%{x|%d %b %H:%M}<br>$%{y:.${d}f}<extra></extra>` }];
    const hline = (lvl, name, color, dash, width = 1.5) => traces.push({
      x: [x0, x1], y: [lvl, lvl], type: "scatter", mode: "lines", name, line: { color, dash, width }, hoverinfo: "skip" });
    // P&L over time of this position as held now (open legs at each hourly
    // spot, carry ignored) plus what its closed legs locked in.
    const legsNow = p.kind === "net" ? p.legData : [p];
    const locked = lockedOf(p);
    const t0 = new Date(String(legsNow.map((l) => l.trade_date).sort()[0]) + "T00:00:00Z");
    const pnlX = [], pnlY = [];
    x.forEach((tx, i) => {
      if (tx < t0) return;
      pnlX.push(tx);
      pnlY.push(locked + legsNow.reduce((s2, l) => s2 + (l.side === "buy" ? 1 : -1) * l.qty * (y[i] - l.forward_price), 0));
    });
    if (pnlX.length) traces.push({ x: pnlX, y: pnlY, yaxis: "y2", type: "scatter", mode: "lines", name: "P&L (right axis)",
      line: { color: "#d2a8ff", width: 1.6 }, hovertemplate: "P&L $%{y:,.0f}<extra></extra>" });
    const lv = p.levels;
    let yLo = Math.min(...y, p.forward_price), yHi = Math.max(...y, p.forward_price);
    hline(p.forward_price, `Entry ${px(a, p.forward_price)}`, "#8b949e", "dash");
    if (lv.stop) { hline(lv.stop.level, `Stop ${px(a, lv.stop.level)} · MTM ${usdK(lv.stop.mtm_at)}`, "#f85149", "solid", lv.stop.status === "hit" ? 3 : 2); yLo = Math.min(yLo, lv.stop.level); yHi = Math.max(yHi, lv.stop.level); }
    if (lv.target) { hline(lv.target.level, `Target ${px(a, lv.target.level)} · MTM ${usdK(lv.target.mtm_at)}`, "#3fb950", "solid", lv.target.status === "hit" ? 3 : 2); yLo = Math.min(yLo, lv.target.level); yHi = Math.max(yHi, lv.target.level); }
    const pad = (yHi - yLo) * 0.08 || yHi * 0.02;
    yLo -= pad; yHi += pad;
    const shapes = [];
    const band = (b0, b1, color) => shapes.push({ type: "rect", xref: "paper", x0: 0, x1: 1, yref: "y", y0: b0, y1: b1, fillcolor: color, line: { width: 0 }, layer: "below" });
    if (lv.stop) short(p) ? band(lv.stop.level, yHi, "rgba(248,81,73,0.10)") : band(yLo, lv.stop.level, "rgba(248,81,73,0.10)");
    if (lv.target) short(p) ? band(yLo, lv.target.level, "rgba(63,185,80,0.10)") : band(lv.target.level, yHi, "rgba(63,185,80,0.10)");
    const vline = (t, color, text) => {
      if (t < x0 || t > x1) return;
      shapes.push({ type: "line", xref: "x", yref: "paper", x0: t, x1: t, y0: 0, y1: 1, line: { color, width: 1, dash: "dot" } });
    };
    vline(tradeD, "#8b949e"); vline(mat, "#d29922");
    const ann = [];
    if (tradeD >= x0) ann.push({ x: tradeD, y: 1, yref: "paper", text: "traded", showarrow: false, yanchor: "bottom", font: { color: cc.muted, size: 10 } });
    ann.push({ x: mat, y: 1, yref: "paper", text: "maturity", showarrow: false, yanchor: "bottom", font: { color: "#d29922", size: 10 } });
    Plotly.react("fm-price-chart", traces, {
      title: { text: `${label(p)} · ${STATUS[lv.status][0]}${h ? "" : " · no history"}`, font: { color: cc.text, size: 14 } },
      paper_bgcolor: cc.paper, plot_bgcolor: cc.plot, shapes, annotations: ann,
      xaxis: { color: cc.muted, gridcolor: cc.grid, range: [x0, x1] },
      yaxis: { color: cc.muted, gridcolor: cc.grid, range: [yLo, yHi], tickformat: "." + d + "f", title: `${a} (USD)` },
      yaxis2: { overlaying: "y", side: "right", color: "#d2a8ff", showgrid: false, tickformat: "$.3s", title: "P&L", zeroline: true, zerolinecolor: "#6e40c9" },
      margin: { t: 48, r: 70, b: 40, l: 70 }, showlegend: true,
      legend: { orientation: "h", y: -0.12, font: { color: cc.muted, size: 10 }, bgcolor: cc.legendBg },
      hovermode: "x unified",
    }, { responsive: true });
  }

  // ── alerts: a level newly hit / approached since the last refresh ──
  function alerts() {
    const now = {}, fresh = [];
    for (const p of positions) {
      const st = p.levels.status, k = pk(p);
      now[k] = st;
      const was = lastStatus[k];
      if (was !== undefined && was !== st && st !== "watching" && st !== "none") fresh.push(`${STATUS[st][0]}: ${label(p)}`);
    }
    const hits = positions.filter((p) => p.levels.status === "stop_hit" || p.levels.status === "target_hit");
    const $b = $("fm-alert");
    if (hits.length || fresh.length) {
      $b.style.display = "";
      $b.innerHTML = (fresh.length ? `<b>Changed since last check:</b> ${fresh.map(esc).join(" · ")}<br>` : "")
        + (hits.length ? `<b>Levels reached:</b> ${hits.map((p) => esc(`${STATUS[p.levels.status][0]} — ${label(p)}`)).join(" · ")}` : "");
    } else {
      $b.style.display = "none";
    }
    if (fresh.length && document.hidden) document.title = "⚠ " + fresh[0];
    lastStatus = now;
  }

  // ── closed tab ─────────────────────────────────────────────────
  function closedTable() {
    const rows = closedAll.slice().sort((x, y) => y.close_date.localeCompare(x.close_date) || y.close_id - x.close_id);
    const tot = rows.reduce((s2, t) => s2 + t.realised_usd, 0);
    const opt = (t) => Object.keys(REASON).map((r) => `<option value="${r}" ${r === t.reason ? "selected" : ""}>${REASON[r][0]}</option>`).join("");
    $("fm-closed").innerHTML = rows.length ? `<table class="pf-table fw-table">
      <thead><tr><th>Closed</th><th>Asset</th><th style="text-align:left">Counterparty</th><th>Maturity</th>
        <th style="text-align:left">Opened by</th><th style="text-align:left">Closed by</th><th class="num">Qty</th>
        <th class="num">Realised</th><th>Trigger</th><th></th></tr></thead><tbody>
      ${rows.map((t) => { const a = t.asset, sh = t.side === "sell";
        return `<tr data-o="${t.open_id}" data-c="${t.close_id}" data-a="${a}" data-q="${t.qty}" data-row="${t.close_row_id ?? ""}">
        <td>${maturityText(t.close_date)}</td><td><span class="fm-asset">${a}</span></td><td style="text-align:left">${esc(t.counterparty)}</td>
        <td>${maturityText(t.maturity_date)}</td>
        <td style="text-align:left">#${t.open_id} <span style="color:${sh ? "var(--red)" : "var(--green)"}">${sh ? "Sell" : "Buy"}</span> @ ${px(a, t.open_price)} <span class="optv3-muted">${maturityText(t.open_date)}</span></td>
        <td style="text-align:left">#${t.close_id} <span style="color:${sh ? "var(--green)" : "var(--red)"}">${sh ? "Buy" : "Sell"}</span> @ ${px(a, t.close_price)}
          ${t.take_profit || t.stop_loss ? `<span class="optv3-muted" title="Levels on the opening leg">(TP ${t.take_profit ? px(a, t.take_profit) : "—"} / SL ${t.stop_loss ? px(a, t.stop_loss) : "—"})</span>` : ""}</td>
        <td class="num">${fmt(t.qty)}</td><td class="num" style="${sign(t.realised_usd)}">${usd(t.realised_usd)}</td>
        <td>${reasonPill(t.reason)} <select class="fm-reason" title="Change the trigger (saves this pair as picked by hand)">${opt(t)}</select></td>
        <td class="optv3-muted" style="white-space:nowrap">${t.source === "manual" ? `picked <button class="btn-secondary fm-unpair" style="width:auto;padding:0 .4rem" title="Back to automatic matching">unpair</button>` : "auto"}</td></tr>`; }).join("")}
      </tbody><tfoot><tr><td colspan="7" style="text-align:left">${rows.length} round trip${rows.length > 1 ? "s" : ""}</td>
        <td class="num" style="${sign(tot)};font-weight:700">${usd(tot)}</td><td colspan="2"></td></tr></tfoot></table>`
      : `<div class="optv3-muted" style="padding:.5rem">No closed round trips yet: a buy against a short forward (same counterparty and maturity) shows here.</div>`;
    const inv = ASSETS.flatMap((a) => books[a]?.invalid_closes || []);
    if (inv.length) $("fm-closed").insertAdjacentHTML("beforeend", `<div style="font-size:.74rem;margin-top:.4rem;color:var(--orange)">${inv.length} hand-picked pair(s) no longer apply (legs settled, deleted or fully used) and were skipped.</div>`);
  }

  function pairForm() {
    const pf = pairPrefill || {};
    const a0 = pf.asset || (legsAll[0] && legsAll[0].asset) || "ETH";
    const legs = legsAll.filter((l) => l.asset === a0);
    const optL = (sel) => `<option value="">—</option>` + legs.map((l) => `<option value="${l.id}" ${String(sel) === String(l.id) ? "selected" : ""}>#${l.id} ${l.side === "buy" ? "Buy" : "Sell"} ${fmt(l.qty)} @ ${px(a0, l.forward_price)} · ${esc(l.counterparty)} · ${maturityText(l.trade_date)} → ${maturityText(l.maturity_date)}${l.open_qty < l.qty - 1e-9 ? ` (open ${fmt(l.open_qty)})` : ""}</option>`).join("");
    $("fm-pair").innerHTML = `
      <label>Asset <select id="fm-pair-asset">${ASSETS.map((a) => `<option ${a === a0 ? "selected" : ""}>${a}</option>`).join("")}</select></label>
      <label>Opening trade <select id="fm-pair-a">${optL(pf.a)}</select></label>
      <label>Closing trade <select id="fm-pair-b">${optL(pf.b)}</select></label>
      <label>Qty <input type="number" id="fm-pair-q" min="0" step="any" placeholder="smaller of the two" style="width:9rem"></label>
      <label>Trigger <select id="fm-pair-r">${Object.keys(REASON).map((r) => `<option value="${r}">${REASON[r][0]}</option>`).join("")}</select></label>
      <label>Note <input type="text" id="fm-pair-n" placeholder="optional" style="width:12rem"></label>
      <button class="btn-primary" id="fm-pair-go" style="width:auto">Mark closed</button>
      <div class="optv3-muted" style="font-size:.7rem;flex-basis:100%">One buy and one sell, same counterparty and maturity. Picked pairs take priority over the automatic oldest-first matching.</div>`;
    $("fm-pair-asset").onchange = (e) => { pairPrefill = { asset: e.target.value }; pairForm(); };
    $("fm-pair-go").onclick = async () => {
      const body = { asset: $("fm-pair-asset").value, open_id: +$("fm-pair-a").value, close_id: +$("fm-pair-b").value,
        reason: $("fm-pair-r").value, note: $("fm-pair-n").value };
      const q = parseFloat($("fm-pair-q").value); if (q > 0) body.qty = q;
      if (!body.open_id || !body.close_id) { status("Pick both trades.", true); return; }
      try { await api("POST", "/api/forwards/closes", body); pairPrefill = null; status("Marked closed."); await load(true); }
      catch (e) { status("Not saved: " + e.message, true); }
    };
  }

  function settledTable() {
    const rows = settledAll.slice().sort((x, y) => String(y.settled_at || "").localeCompare(String(x.settled_at || "")));
    const tot = rows.reduce((s2, r) => s2 + (r.settled_pnl_usd || 0), 0);
    $("fm-settled").innerHTML = rows.length ? `<table class="pf-table fw-table"><thead><tr><th>Maturity</th><th>Asset</th><th style="text-align:left">Counterparty</th>
      <th>Side</th><th class="num">Qty</th><th class="num">Forward</th><th class="num">Fixing</th><th class="num">P&amp;L</th></tr></thead><tbody>
      ${rows.map((r) => `<tr><td>${maturityText(r.maturity_date)}</td><td><span class="fm-asset">${r.asset}</span></td><td style="text-align:left">${esc(r.counterparty)}</td>
        <td>${r.side === "buy" ? "Buy" : "Sell"}</td><td class="num">${fmt(r.qty)}</td><td class="num">${px(r.asset, r.forward_price)}</td>
        <td class="num">${px(r.asset, r.fixing_price)}</td><td class="num" style="${sign(r.settled_pnl_usd)}">${usd(r.settled_pnl_usd)}</td></tr>`).join("")}
      </tbody><tfoot><tr><td colspan="7" style="text-align:left">${rows.length} settled</td><td class="num" style="${sign(tot)};font-weight:700">${usd(tot)}</td></tr></tfoot></table>`
      : `<div class="optv3-muted" style="padding:.5rem">Nothing settled yet. Forwards are settled at their maturity fixing from the Trades screen.</div>`;
  }

  function showTab(t) {
    tab = t;
    document.querySelectorAll("#fm-tabs .fm-tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
    $("fm-tab-open").style.display = t === "open" ? "" : "none";
    $("fm-tab-closed").style.display = t === "closed" ? "" : "none";
    if (t === "open") priceChart();
  }

  function renderAll() {
    summary(); pnlLine(); positionCards(); priceChart(); closedTable(); pairForm(); settledTable();
    $("fm-open-count").textContent = `(${positions.length})`;
    $("fm-closed-count").textContent = `(${closedAll.length}${settledAll.length ? " + " + settledAll.length + " settled" : ""})`;
  }

  // ── netting: a settlement buy closes part of a short and a sell reopens it,
  // so booked forwards of one asset, counterparty and maturity are ONE
  // position. Legs net into a card: net qty, break-even entry (sum of signed
  // qty x price over net qty), MTM, and stop / target on the NET (MTM at a
  // level = each leg's MTM moved by its delta; linear). Levels belong to the
  // legs on the net's side; editing the card writes them to each of those.
  function levelWatch(sgn, spot, legs, lv, kind) {
    if (!lv || !(lv > 0)) return null;
    const up = kind === "stop" ? sgn < 0 : sgn > 0;
    const hit = up ? spot >= lv : spot <= lv;
    const dist = spot > 0 ? (lv / spot - 1) * 100 : null;
    const near = !hit && dist != null && Math.abs(dist) <= 2;
    const mtm_at = legs.reduce((t, l) => t + l.mtm + l.delta * (lv - spot), 0);
    return { level: lv, dist_pct: dist, mtm_at, status: hit ? "hit" : near ? "near" : "ok" };
  }

  function netForwards(list) {
    const groups = new Map(), out = [];
    for (const p of list) {
      if (p.kind !== "forward") { out.push(p); continue; }
      const g = `${p.asset}|${p.counterparty.trim().toLowerCase()}|${p.maturity_date}`;
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(p);
    }
    for (const legs of groups.values()) {
      if (legs.length === 1) { out.push(legs[0]); continue; }
      legs.sort((a, b) => a.trade_date.localeCompare(b.trade_date) || a.forward_id - b.forward_id);
      const sq = (l) => (l.side === "buy" ? 1 : -1) * l.qty;
      const net = legs.reduce((t, l) => t + sq(l), 0);
      const sgn = net > 0 ? 1 : net < 0 ? -1 : 0;
      const side = sgn > 0 ? "buy" : "sell";
      const same = legs.filter((l) => (l.side === "buy" ? 1 : -1) === sgn);
      const withLv = same.find((l) => l.stop_loss || l.take_profit) || {};
      const spot = legs[0].spot, a = legs[0].asset;
      const stop = sgn ? levelWatch(sgn, spot, legs, withLv.stop_loss, "stop") : null;
      const target = sgn ? levelWatch(sgn, spot, legs, withLv.take_profit, "target") : null;
      const st = stop?.status === "hit" ? "stop_hit" : target?.status === "hit" ? "target_hit"
        : stop?.status === "near" ? "near_stop" : target?.status === "near" ? "near_target"
        : stop || target ? "watching" : "none";
      // Same levels on every leg of the net side? Otherwise say so on the card.
      const mixed = same.some((l) => l.stop_loss !== withLv.stop_loss || l.take_profit !== withLv.take_profit);
      const entry = sgn ? legs.reduce((t, l) => t + sq(l) * l.forward_price, 0) / net : 0;
      out.push({
        key: "net:" + legs.map((l) => l.forward_id).join("-"), kind: "net", asset: a, spot,
        group: gkey(a, legs[0].counterparty, legs[0].maturity_date), legData: legs,
        counterparty: legs[0].counterparty, side, qty: Math.abs(net), forward_price: entry, effective_price: entry,
        net_premium_usd: 0, trade_date: legs[0].trade_date, maturity_date: legs[0].maturity_date,
        days_to_maturity: legs[0].days_to_maturity, mtm: legs.reduce((t, l) => t + l.mtm, 0),
        delta: legs.reduce((t, l) => t + l.delta, 0), external_ref: "",
        legs: legs.map((l) => `#${l.forward_id} ${l.side === "buy" ? "Buy" : "Sell"} ${fmt(l.qty)}${l.booked_qty && Math.abs(l.booked_qty - l.qty) > 1e-9 ? ` of ${fmt(l.booked_qty)}` : ""} @ ${px(a, l.forward_price)} (${maturityText(l.trade_date)})`),
        levelKeys: same.map((l) => l.key), stop_loss: withLv.stop_loss ?? null, take_profit: withLv.take_profit ?? null,
        note: (withLv.note || "") + (mixed ? " · legs carry different levels: saving sets them all" : ""),
        levels: { stop, target, status: st, near_pct: 2 },
      });
    }
    return out;
  }

  // ── data ─────────────────────────────────────────────────────
  const monitorUrl = (a, emb) => `/api/forwards/monitor?asset=${a}&include_embedded=${!!emb}`;

  async function loadHistory(a) {
    const c = hist[a];
    if (c && Date.now() - c.at < HIST_MS) return;
    try { hist[a] = { data: await api("GET", `/api/forwards/history?asset=${a}`), at: Date.now() }; }
    catch (e) { console.warn("history", a, e); if (!c) hist[a] = { data: null, at: Date.now() }; }
  }

  async function load(quiet) {
    if (quiet !== true) status("Loading…");
    const emb = $("fm-embedded")?.checked;
    const res = await Promise.allSettled(ASSETS.map((a) => api("GET", monitorUrl(a, emb))));
    books = {};
    positions = [];
    closedAll = []; settledAll = []; legsAll = [];
    let embedded = 0;
    res.forEach((r, i) => {
      const a = ASSETS[i];
      if (r.status === "rejected") { books[a] = { error: r.reason?.message || "unavailable" }; return; }
      books[a] = r.value;
      embedded += r.value.embedded_synthetics || 0;
      for (const p of r.value.positions) positions.push({ ...p, asset: a, spot: r.value.spot });
      closedAll.push(...(r.value.closed || []));
      settledAll.push(...(r.value.settled || []).map((x) => ({ ...x, asset: a })));
      legsAll.push(...(r.value.forward_legs || []).map((x) => ({ ...x, asset: a })));
    });
    positions = netForwards(positions);
    positions.sort((x, y) => (RANK[x.levels.status] ?? 9) - (RANK[y.levels.status] ?? 9)
      || x.maturity_date.localeCompare(y.maturity_date) || x.asset.localeCompare(y.asset)
      || x.counterparty.localeCompare(y.counterparty));
    if (!positions.some((p) => pk(p) === selected)) {
      const first = positions.find((p) => p.levels.status !== "none") || positions[0];
      selected = first ? pk(first) : null;
    }
    if (!positions.some((p) => pk(p) === editing)) editing = null;
    const selAsset = positions.find((p) => pk(p) === selected)?.asset;
    if (selAsset) await loadHistory(selAsset);
    renderAll();
    alerts();
    setBadge(positions);
    const $eh = $("fm-embedded-count");
    if ($eh) $eh.textContent = embedded ? `(${embedded})` : "(none)";
    const errs = ASSETS.filter((a) => books[a]?.error);
    status((errs.length ? `${errs.join(", ")} unavailable · ` : "") + `as of ${new Date().toLocaleTimeString()}`
      + ($("fm-auto")?.checked ? " · re-checks every minute" : ""), errs.length > 0);
    schedule();
  }

  function schedule() {
    clearTimeout(timer);
    if (!$("fm-auto")?.checked) return;
    timer = setTimeout(() => {
      if (active() && !editing) load(true); else schedule();
    }, REFRESH_MS);
  }

  async function save(k, clear) {
    const card = document.querySelector(`#fm-positions .fm-card[data-key="${CSS.escape(k)}"]`);
    const p = positions.find((x) => pk(x) === k);
    if (!card || !p) return;
    if (clear && !confirm(`Remove the stop loss and take profit for ${label(p)}?`)) return;
    const num = (f) => { const v = parseFloat(card.querySelector(`[data-f="${f}"]`)?.value); return v > 0 ? v : null; };
    const body = { asset: p.asset, key: p.key,
      stop_loss: clear ? null : num("sl"), take_profit: clear ? null : num("tp"),
      note: clear ? "" : (card.querySelector('[data-f="note"]')?.value || "").replace(/ · legs carry different levels.*$/, "") };
    try {
      // A netted card writes its levels to every leg on the net's side.
      for (const key of p.levelKeys || [p.key]) await api("PUT", "/api/forwards/levels", { ...body, key });
      editing = null;
      selected = k;
      status(clear ? "Levels removed." : "Levels saved.");
      await load(true);
      if (typeof fwReload === "function") fwReload();
    } catch (e) {
      status("Save failed: " + e.message, true);
    }
  }

  // ── sidebar badge: hits + near levels across both assets ──────
  const BADGE_MS = 180_000;
  function setBadge(ps) {
    const b = $("nav-badge-fwmonitor");
    if (!b) return;
    const n = (k) => ps.filter((p) => p.levels.status === k).length;
    const hits = n("stop_hit") + n("target_hit"), near = n("near_stop") + n("near_target");
    b.textContent = hits ? String(hits) : near ? String(near) : "";
    b.style.background = hits ? "var(--red)" : near ? "var(--orange)" : "";
    b.title = hits || near ? `${n("stop_hit")} stop hit · ${n("target_hit")} target hit · ${n("near_stop")} near stop · ${n("near_target")} near target` : "";
  }
  async function badgeTick() {
    if (!active()) {
      const res = await Promise.allSettled(ASSETS.map((a) => api("GET", monitorUrl(a, false))));
      setBadge(netForwards(res.flatMap((r, i) => (r.status === "fulfilled"
        ? r.value.positions.map((p) => ({ ...p, asset: ASSETS[i], spot: r.value.spot })) : []))));
    }
    setTimeout(badgeTick, BADGE_MS);
  }
  setTimeout(badgeTick, 5000);

  // ── wiring ────────────────────────────────────────────────────
  document.querySelector('.nav-item[data-page="fwmonitor"]')?.addEventListener("click", () => { lastStatus = {}; load(); });
  $("fm-refresh")?.addEventListener("click", () => load());
  $("fm-auto")?.addEventListener("change", schedule);
  $("fm-embedded")?.addEventListener("change", () => load());
  $("fm-positions")?.addEventListener("click", async (ev) => {
    const card = ev.target.closest(".fm-card[data-key]");
    if (!card) return;
    const k = card.dataset.key;
    const act = ev.target.closest("button[data-act]")?.dataset.act;
    if (act === "save") return save(k, false);
    if (act === "clear") return save(k, true);
    if (act === "edit") { editing = k; positionCards(); document.querySelector(`#fm-positions .fm-card[data-key="${CSS.escape(k)}"] [data-f="sl"]`)?.focus(); return; }
    if (act === "cancel") { editing = null; positionCards(); schedule(); return; }
    if (act === "markclosed") {
      const p = positions.find((x) => pk(x) === k);
      const ticked = [...card.querySelectorAll(".fm-leg-check:checked")].map((c) => +c.dataset.fid);
      pairPrefill = { asset: p.asset, a: ticked[0] ?? p.forward_id ?? "", b: ticked[1] ?? "" };
      pairForm(); showTab("closed");
      $("fm-pair-card").scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    if (ev.target.closest("input, label")) return;
    // Anywhere else on the card (or "Show on chart"): chart this position.
    if (selected !== k || act === "chart") {
      selected = k;
      const a = positions.find((p) => pk(p) === k)?.asset;
      if (a) await loadHistory(a);
      positionCards(); priceChart();
      if (act === "chart") $("fm-price-chart").scrollIntoView({ behavior: "smooth", block: "center" });
    }
  });
  $("fm-tabs")?.addEventListener("click", (ev) => { const b = ev.target.closest(".fm-tab"); if (b) showTab(b.dataset.tab); });
  $("fm-closed")?.addEventListener("change", async (ev) => {
    if (!ev.target.classList.contains("fm-reason")) return;
    const tr = ev.target.closest("tr");
    try {
      await api("POST", "/api/forwards/closes", { asset: tr.dataset.a, open_id: +tr.dataset.o, close_id: +tr.dataset.c,
        qty: +tr.dataset.q, reason: ev.target.value });
      status("Trigger saved."); await load(true);
    } catch (e) { status("Not saved: " + e.message, true); }
  });
  $("fm-closed")?.addEventListener("click", async (ev) => {
    const b = ev.target.closest(".fm-unpair"); if (!b) return;
    const id = b.closest("tr").dataset.row;
    if (!id || !confirm("Remove this hand-picked pair and go back to automatic matching?")) return;
    try { await api("DELETE", `/api/forwards/closes/${id}`); status("Unpaired."); await load(true); }
    catch (e) { status("Failed: " + e.message, true); }
  });
  $("fm-positions")?.addEventListener("keydown", (ev) => {
    if (!ev.target.closest(".fm-edit")) return;
    const k = ev.target.closest(".fm-card")?.dataset.key;
    if (ev.key === "Enter") save(k, false);
    else if (ev.key === "Escape") { editing = null; positionCards(); }
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && document.title.startsWith("⚠")) document.title = baseTitle; });
})();
