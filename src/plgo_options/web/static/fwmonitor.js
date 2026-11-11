// Forwards Monitor (/api/forwards/monitor, /levels, /history): every forward-
// like position of the sidebar asset (booked forwards + synthetic forwards
// built from options, e.g. a long call + short put at one strike), with stop-
// loss / take-profit levels managed here, off the trade. Levels are spot
// prices; each refresh re-checks them against live spot.
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const fmt = (v, dp = 0) => (v == null || !isFinite(v)) ? "—"
    : Number(v).toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
  const usd = (v) => (v == null || !isFinite(v)) ? "—" : (v < 0 ? "−$" : "$") + fmt(Math.abs(v));
  const pct = (v) => (v == null || !isFinite(v)) ? "—" : (v >= 0 ? "+" : "") + fmt(v, 1) + "%";
  const sign = (v) => (v < 0 ? "color:var(--red)" : v > 0 ? "color:var(--green)" : "");
  const asset = () => (typeof currentAsset !== "undefined" && currentAsset) || "FIL";
  const pxDp = () => (asset() === "FIL" ? 4 : 2);
  const active = () => $("page-fwmonitor")?.classList.contains("active");
  const REFRESH_MS = 60_000;

  let mon = null;          // last /monitor response
  let hist = null;         // last /history response (hourly closes)
  let selected = null;     // position key shown on the price chart
  let timer = null;
  let lastStatus = {};     // key -> status at the previous refresh (alerts)
  let rangeNames = [];     // y labels of the distance chart, by position index
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
  const pill = (s) => { const [t, c] = STATUS[s] || STATUS.none; return `<span class="fw-lv ${c}">${t}</span>`; };
  const label = (p) => `${p.counterparty} ${p.side === "buy" ? "long" : "short"} ${fmt(p.qty)} @ ${fmt(p.forward_price, pxDp())}`;

  // ── cards ─────────────────────────────────────────────────────
  function cards() {
    const s = mon.summary;
    const card = (lab, val, sub, style = "") => `<div class="collat-metric"><div class="collat-metric-label">${lab}</div>
      <div class="collat-metric-value" style="${style}">${val}</div><div class="collat-metric-sub">${sub}</div></div>`;
    const withLv = s.count - s.none;
    $("fm-cards").innerHTML =
      card("Spot", "$" + fmt(mon.spot, pxDp()), `${asset()} · ${new Date().toLocaleTimeString()}`)
      + card("Positions", fmt(s.count), `${mon.positions.filter((p) => p.kind === "synthetic").length} synthetic (options) · ${withLv} with levels`)
      + card("Net position", fmt(s.net_qty) + " " + asset(), "booked + synthetic forwards", sign(s.net_qty))
      + card("MTM", usd(s.mtm), "at live spot", sign(s.mtm))
      + card("Stop hit", fmt(s.stop_hit), `${fmt(s.near_stop)} within ${mon.near_pct}%`, s.stop_hit ? "color:var(--red)" : s.near_stop ? "color:#d29922" : "")
      + card("Target hit", fmt(s.target_hit), `${fmt(s.near_target)} within ${mon.near_pct}%`, s.target_hit ? "color:var(--green)" : s.near_target ? "color:#3fb950" : "");
  }

  // ── alerts: a level newly hit / approached since the last refresh ──
  function alerts() {
    const now = {};
    const fresh = [];
    for (const p of mon.positions) {
      const st = p.levels.status;
      now[p.key] = st;
      const was = lastStatus[p.key];
      if (was !== undefined && was !== st && st !== "watching" && st !== "none") fresh.push(`${STATUS[st][0]}: ${label(p)}`);
    }
    const hits = mon.positions.filter((p) => p.levels.status === "stop_hit" || p.levels.status === "target_hit");
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

  // ── price chart: spot history with the selected position's levels ──
  function priceChart() {
    if (!window.Plotly || !hist) return;
    const p = mon.positions.find((x) => x.key === selected) || mon.positions.find((x) => x.levels.status !== "none") || mon.positions[0];
    if (p) selected = p.key;
    const x = hist.ts.map((t) => new Date(t * 1000));
    const y = hist.closes.slice();
    // Close the line at live spot.
    x.push(new Date()); y.push(mon.spot);
    const x0 = x[0], x1 = new Date(Date.now() + 2 * 86400e3);
    const traces = [{ x, y, type: "scatter", mode: "lines", name: `${asset()} spot (hourly)`,
      line: { color: "#58a6ff", width: 1.8 }, hovertemplate: "%{x|%d %b %H:%M}<br>$%{y:." + pxDp() + "f}<extra></extra>" }];
    const shapes = [];
    const hline = (lvl, name, color, dash, width = 1.5) => traces.push({
      x: [x0, x1], y: [lvl, lvl], type: "scatter", mode: "lines", name, line: { color, dash, width },
      hovertemplate: `${name}<extra></extra>` });
    let yLo = Math.min(...y), yHi = Math.max(...y);
    if (p) {
      const lv = p.levels, isShort = p.side === "sell";
      hline(p.forward_price, `Entry ${fmt(p.forward_price, pxDp())}`, "#8b949e", "dash");
      yLo = Math.min(yLo, p.forward_price); yHi = Math.max(yHi, p.forward_price);
      if (lv.stop) {
        hline(lv.stop.level, `Stop ${fmt(lv.stop.level, pxDp())} · MTM ${usd(lv.stop.mtm_at)}`, "#f85149", "solid", lv.stop.status === "hit" ? 3 : 2);
        yLo = Math.min(yLo, lv.stop.level); yHi = Math.max(yHi, lv.stop.level);
      }
      if (lv.target) {
        hline(lv.target.level, `Target ${fmt(lv.target.level, pxDp())} · MTM ${usd(lv.target.mtm_at)}`, "#3fb950", "solid", lv.target.status === "hit" ? 3 : 2);
        yLo = Math.min(yLo, lv.target.level); yHi = Math.max(yHi, lv.target.level);
      }
      const pad = (yHi - yLo) * 0.08 || yHi * 0.02;
      yLo -= pad; yHi += pad;
      // Zones beyond each level: red past the stop, green past the target.
      const band = (a, b, color) => shapes.push({ type: "rect", xref: "paper", x0: 0, x1: 1, yref: "y", y0: a, y1: b,
        fillcolor: color, line: { width: 0 }, layer: "below" });
      if (lv.stop) isShort ? band(lv.stop.level, yHi, "rgba(248,81,73,0.10)") : band(yLo, lv.stop.level, "rgba(248,81,73,0.10)");
      if (lv.target) isShort ? band(yLo, lv.target.level, "rgba(63,185,80,0.10)") : band(lv.target.level, yHi, "rgba(63,185,80,0.10)");
    }
    const cc = typeof chartColors === "function" ? chartColors() : { text: "#c9d1d9", paper: "transparent", plot: "transparent", muted: "#8b949e", grid: "#30363d", legendBg: "transparent", legendBorder: "#30363d" };
    Plotly.react("fm-price-chart", traces, {
      title: { text: p ? `${label(p)} · ${p.kind === "synthetic" ? "synthetic (options)" : "forward"} · ${STATUS[p.levels.status][0]}` : `${asset()} spot`,
        font: { color: cc.text, size: 14 } },
      paper_bgcolor: cc.paper, plot_bgcolor: cc.plot, shapes,
      xaxis: { color: cc.muted, gridcolor: cc.grid, range: [x0, x1] },
      yaxis: { color: cc.muted, gridcolor: cc.grid, range: [yLo, yHi], tickformat: "." + pxDp() + "f", title: `${asset()} (USD)` },
      margin: { t: 40, r: 20, b: 40, l: 70 }, showlegend: true,
      legend: { orientation: "h", y: -0.12, font: { color: cc.muted, size: 10 }, bgcolor: cc.legendBg },
      hovermode: "x unified",
    }, { responsive: true });
  }

  // ── distance chart: every position's stop→target range vs spot ─────
  function rangeChart() {
    if (!window.Plotly) return;
    const ps = mon.positions;
    if (!ps.length) { Plotly.purge("fm-range-chart"); return; }
    const S = mon.spot;
    const rel = (v) => (v / S - 1) * 100;
    const names = rangeNames = ps.map((p, i) => `${i + 1}. ${p.counterparty} ${p.side === "buy" ? "L" : "S"} ${fmt(p.qty / 1000)}k`);
    const traces = [];
    ps.forEach((p, i) => {
      const lv = p.levels, n = names[i];
      const pts = [lv.stop && rel(lv.stop.level), lv.target && rel(lv.target.level), rel(p.forward_price)].filter((v) => v != null);
      traces.push({ x: [Math.min(...pts), Math.max(...pts)], y: [n, n], type: "scatter", mode: "lines", showlegend: false,
        line: { color: p.key === selected ? "#c9d1d9" : "#484f58", width: p.key === selected ? 6 : 4 }, hoverinfo: "skip" });
    });
    const mk = (name, sym, color, get, size = 13) => {
      const rows = ps.map((p, i) => [get(p), names[i], p]).filter((r) => r[0] != null);
      if (!rows.length) return;
      traces.push({ x: rows.map((r) => rel(r[0])), y: rows.map((r) => r[1]), type: "scatter", mode: "markers", name,
        marker: { symbol: sym, size, color, line: { color: "#0d1117", width: 1 } },
        customdata: rows.map((r) => [r[0], r[2].key]),
        hovertemplate: `${name} $%{customdata[0]:.${pxDp()}f} · %{x:+.1f}% from spot<extra>%{y}</extra>` });
    };
    mk("Entry", "diamond", "#8b949e", (p) => p.forward_price, 11);
    mk("Stop loss", "line-ns-open", "#f85149", (p) => p.levels.stop && p.levels.stop.level, 18);
    mk("Take profit", "line-ns-open", "#3fb950", (p) => p.levels.target && p.levels.target.level, 18);
    const cc = typeof chartColors === "function" ? chartColors() : { text: "#c9d1d9", paper: "transparent", plot: "transparent", muted: "#8b949e", grid: "#30363d", legendBg: "transparent" };
    const band = mon.near_pct;
    Plotly.react("fm-range-chart", traces, {
      title: { text: `Distance from spot $${fmt(S, pxDp())} (0%) · shaded = within ${band}%`, font: { color: cc.text, size: 13 } },
      paper_bgcolor: cc.paper, plot_bgcolor: cc.plot,
      shapes: [
        { type: "rect", xref: "x", yref: "paper", x0: -band, x1: band, y0: 0, y1: 1, fillcolor: "rgba(210,153,34,0.10)", line: { width: 0 }, layer: "below" },
        { type: "line", xref: "x", yref: "paper", x0: 0, x1: 0, y0: 0, y1: 1, line: { color: "#58a6ff", width: 2 } },
      ],
      xaxis: { color: cc.muted, gridcolor: cc.grid, ticksuffix: "%", zeroline: false, title: "% from spot" },
      yaxis: { color: cc.muted, autorange: "reversed", automargin: true },
      margin: { t: 36, r: 20, b: 44, l: 10 }, height: Math.max(220, 70 + 34 * ps.length),
      legend: { orientation: "h", y: -0.25, font: { color: cc.muted, size: 10 }, bgcolor: cc.legendBg },
    }, { responsive: true });
    const el = $("fm-range-chart");
    if (!el.dataset.wired) {
      el.dataset.wired = "1";
      el.on("plotly_click", (ev) => {
        const pt = ev.points && ev.points[0];
        const idx = pt ? rangeNames.indexOf(pt.y) : -1;
        const cur = mon && mon.positions[idx >= 0 ? idx : -1];
        if (cur) { selected = cur.key; renderAll(); }
      });
    }
  }

  // ── table with inline level editing ───────────────────────────
  function levelCells(x) {
    if (!x) return `<td class="num optv3-muted">—</td><td class="num optv3-muted">—</td>`;
    const col = x.status === "hit" ? "font-weight:700" : x.status === "near" ? "color:#d29922" : "";
    return `<td class="num" style="${col}">${pct(x.dist_pct)}</td><td class="num" style="${sign(x.mtm_at)}">${usd(x.mtm_at)}</td>`;
  }

  function table() {
    const rows = mon.positions.map((p, i) => {
      const lv = p.levels;
      const kind = p.kind === "synthetic"
        ? `<span class="tm-fwd-badge" title="${esc(p.legs.join("\n"))}">Synthetic</span>${p.embedded ? ` <span class="fw-lv fw-lv-watch" title="This call/put pair is part of a larger deal">in deal</span>` : ""}`
        : `<span class="tm-fwd-badge">Forward</span>`;
      const eff = Math.abs(p.effective_price - p.forward_price) > 1e-12
        ? `<div class="optv3-muted" style="font-size:.7rem" title="All-in, with the net premium ${usd(p.net_premium_usd)}">all-in ${fmt(p.effective_price, pxDp())}</div>` : "";
      const isShort = p.side === "sell";
      return `<tr class="${p.key === selected ? "fm-row-sel" : ""}" data-key="${esc(p.key)}">
        <td>${i + 1}</td><td>${pill(lv.status)}</td><td>${kind}</td>
        <td style="text-align:left">${esc(p.counterparty)}${p.legs.length ? `<div class="optv3-muted" style="font-size:.7rem">${esc(p.legs.join(" · "))}</div>` : ""}</td>
        <td style="${isShort ? "color:var(--red)" : "color:var(--green)"}">${isShort ? "Sell" : "Buy"}</td>
        <td class="num">${fmt(p.qty)}</td>
        <td class="num">${fmt(p.forward_price, pxDp())}${eff}</td>
        <td>${esc(p.maturity_date)}<div class="optv3-muted" style="font-size:.7rem">${fmt(p.days_to_maturity)}d</div></td>
        <td class="num" style="${sign(p.mtm)}">${usd(p.mtm)}</td>
        <td><input type="number" step="any" min="0" class="fm-in" data-f="sl" value="${p.stop_loss ?? ""}" placeholder="${isShort ? "above" : "below"} spot"></td>
        ${levelCells(lv.stop)}
        <td><input type="number" step="any" min="0" class="fm-in" data-f="tp" value="${p.take_profit ?? ""}" placeholder="${isShort ? "below" : "above"} spot"></td>
        ${levelCells(lv.target)}
        <td><input type="text" class="fm-in fm-note" data-f="note" value="${esc(p.note || "")}" placeholder="note"></td>
        <td style="white-space:nowrap">
          <button class="btn-secondary" data-act="save" style="width:auto">Save</button>
          <button class="btn-secondary" data-act="clear" style="width:auto" title="Remove stop and target">Clear</button>
          <button class="btn-secondary" data-act="chart" style="width:auto" title="Show on the price chart">Chart</button>
        </td></tr>`;
    }).join("") || `<tr><td colspan="17" style="text-align:center;color:var(--muted)">No open ${esc(asset())} forwards or synthetic forwards.</td></tr>`;
    $("fm-table").innerHTML = `<table class="pf-table fw-table">
      <thead><tr><th>#</th><th>Status</th><th>Kind</th><th>Counterparty</th><th>Side</th><th class="num">Qty</th>
        <th class="num">Forward price</th><th>Maturity</th><th class="num">MTM</th>
        <th title="Spot price: short = above spot, long = below">Stop loss</th><th class="num" title="Stop vs live spot">to stop</th><th class="num" title="This position's MTM if spot reaches the stop">MTM at stop</th>
        <th title="Spot price: short = below spot, long = above">Take profit</th><th class="num">to target</th><th class="num">MTM at target</th>
        <th>Note</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  function renderAll() {
    $("fm-asset-label").textContent = asset();
    cards(); priceChart(); rangeChart(); table();
  }

  // ── data ─────────────────────────────────────────────────────
  async function load(quiet) {
    if (quiet !== true) status("Loading…");
    try {
      const [m, h] = await Promise.all([
        api("GET", `/api/forwards/monitor?asset=${asset()}&include_embedded=${!!$("fm-embedded")?.checked}`),
        api("GET", `/api/forwards/history?asset=${asset()}`).catch((e) => { console.warn("history:", e); return null; }),
      ]);
      if (m.asset !== asset()) return;          // asset switched mid-flight
      mon = m; if (h) hist = h;
      if (!mon.positions.some((p) => p.key === selected)) selected = null;
      renderAll();
      alerts();
      setBadge(mon);
      const $eh = $("fm-embedded-count");
      if ($eh) $eh.textContent = mon.embedded_synthetics ? `(${mon.embedded_synthetics})` : "(none)";
      status(`Spot $${fmt(mon.spot, pxDp())} · ${hist ? hist.source + " hourly" : "no history"} · as of ${new Date().toLocaleTimeString()}`
        + ($("fm-auto")?.checked ? " · re-checks every minute" : ""));
    } catch (e) {
      status("Failed to load: " + e.message, true);
    }
    schedule();
  }

  function schedule() {
    clearTimeout(timer);
    if (!$("fm-auto")?.checked) return;
    timer = setTimeout(() => {
      const editing = document.activeElement && document.activeElement.classList.contains("fm-in");
      if (active() && !editing) load(true); else schedule();
    }, REFRESH_MS);
  }

  async function save(key, clear) {
    const tr = document.querySelector(`#fm-table tr[data-key="${CSS.escape(key)}"]`);
    const num = (f) => { const v = parseFloat(tr.querySelector(`[data-f="${f}"]`).value); return v > 0 ? v : null; };
    const body = { asset: asset(), key,
      stop_loss: clear ? null : num("sl"), take_profit: clear ? null : num("tp"),
      note: clear ? "" : tr.querySelector('[data-f="note"]').value };
    if (clear && !confirm("Remove the stop loss and take profit for this position?")) return;
    try {
      await api("PUT", "/api/forwards/levels", body);
      status(clear ? "Levels cleared." : "Levels saved.");
      selected = key;
      await load(true);
      if (typeof fwReload === "function") fwReload();
    } catch (e) {
      status("Save failed: " + e.message, true);
    }
  }

  // ── sidebar badge: hits + near levels, from any page ──────────
  // A light poll so a level coming into reach shows even while you are on
  // another screen. The page itself refreshes every minute when open.
  const BADGE_MS = 180_000;
  function setBadge(m) {
    const b = $("nav-badge-fwmonitor");
    if (!b || !m) return;
    const s = m.summary, hits = s.stop_hit + s.target_hit, near = s.near_stop + s.near_target;
    b.textContent = hits ? String(hits) : near ? String(near) : "";
    b.style.background = hits ? "var(--red)" : near ? "#d29922" : "";
    b.title = hits || near ? `${s.stop_hit} stop hit · ${s.target_hit} target hit · ${s.near_stop} near stop · ${s.near_target} near target` : "";
  }
  async function badgeTick() {
    try { if (!active()) setBadge(await api("GET", `/api/forwards/monitor?asset=${asset()}`)); } catch (_) { /* next tick */ }
    setTimeout(badgeTick, BADGE_MS);
  }
  setTimeout(badgeTick, 5000);

  // ── wiring ────────────────────────────────────────────────────
  document.querySelector('.nav-item[data-page="fwmonitor"]')?.addEventListener("click", () => { lastStatus = {}; load(); });
  $("fm-refresh")?.addEventListener("click", load);
  $("fm-auto")?.addEventListener("change", schedule);
  $("fm-embedded")?.addEventListener("change", () => load());
  $("fm-table")?.addEventListener("click", (ev) => {
    const b = ev.target.closest("button[data-act]");
    const tr = ev.target.closest("tr[data-key]");
    if (!b || !tr) return;
    const key = tr.dataset.key;
    if (b.dataset.act === "save") save(key, false);
    else if (b.dataset.act === "clear") save(key, true);
    else if (b.dataset.act === "chart") { selected = key; renderAll(); $("fm-price-chart").scrollIntoView({ behavior: "smooth", block: "center" }); }
  });
  $("fm-table")?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && ev.target.classList.contains("fm-in")) save(ev.target.closest("tr").dataset.key, false);
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && document.title.startsWith("⚠")) document.title = baseTitle; });
})();
