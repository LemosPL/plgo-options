// Sandbox screen (/api/sandbox): price and compare dealer offers on a token we
// don't trade yet (ZAMA), solve fair collars, and keep a what-if book.
// Isolated: nothing here reaches the real trades or any risk view.
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const fmt = (v, dp = 0) => (v == null || !isFinite(v)) ? "—"
    : Number(v).toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
  const usd = (v) => (v == null || !isFinite(v)) ? "—" : (v < 0 ? "-$" : "$") + fmt(Math.abs(v));
  const px = (v) => (v == null || !isFinite(v)) ? "—" : fmt(v, v < 1 ? 4 : 2);
  const mln = (v) => (v == null || !isFinite(v)) ? "—" : fmt(v / 1e6, 1) + "m";
  const pct = (v, dp = 1) => (v == null || !isFinite(v)) ? "—" : fmt(v * 100, dp) + "%";
  const signStyle = (v) => (v < 0 ? "color:var(--red)" : v > 0 ? "color:var(--green)" : "");
  const num = (id) => { const v = $(id).value.trim(); return v === "" ? null : Number(v); };

  let market = null;        // /market
  let data = null;          // /offers
  let overrides = {};       // what-if inputs, not saved
  let editing = null;       // offer in the form (id or "new")
  let hidden = new Set();   // offers taken off the chart
  let lastSolve = null;
  let lastProgram = null;

  const asset = () => ($("sb-asset").value || "ZAMA").trim().toUpperCase();

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
    $("sb-status").textContent = msg || "";
    $("sb-status").style.color = isErr ? "var(--red)" : "";
  }

  function query() {
    const q = new URLSearchParams({ asset: asset() });
    Object.entries(overrides).forEach(([k, v]) => { if (v != null) q.set(k, v); });
    return q.toString();
  }

  function colors() {
    if (typeof chartColors === "function") return chartColors();
    return { paper: "rgba(0,0,0,0)", plot: "rgba(0,0,0,0)", text: "#e6edf3", muted: "#8b949e", grid: "#30363d", zeroline: "#30363d" };
  }

  // ── market ────────────────────────────────────────────────────
  function renderMarket() {
    const m = market, h = m.history, v = h.vols || {};
    const card = (label, value, sub) => `<div class="collat-metric"><div class="collat-metric-label">${label}</div>
      <div class="collat-metric-value">${value}</div><div class="collat-metric-sub">${sub}</div></div>`;
    const venues = m.spot.quotes.map((q) => q.price
      ? `<span class="sb-pill ${q.used ? "" : "off"}" title="${q.used ? "used" : "excluded: more than 5% off the median"}">${esc(q.source)} ${px(q.price)}</span>`
      : `<span class="sb-pill off" title="${esc(q.error || "")}">${esc(q.source)} —</span>`).join("");
    const vols = ["30d", "60d", "90d", "180d", "all"].filter((k) => v[k] != null)
      .map((k) => `${k === "all" ? "since listing" : k} <b>${pct(v[k], 0)}</b>`).join(" · ");
    const fund = (m.funding || []).map((f) => `${esc(f.venue)} <b>${pct(f.annual, 1)}</b>`).join(" · ") || "no perp";
    const range = h.closes && h.closes.length ? `listed ${esc(h.first_date)} · range ${px(h.low)}–${px(h.high)}` : esc(h.error || "no history");
    $("sb-market").innerHTML = `
      <div class="sb-cards">
        ${card(esc(m.asset) + " spot", px(m.spot.spot), `median of ${m.spot.n_used} venue(s)<div>${venues}</div>`)}
        ${card("Realised vol", v["90d"] != null ? pct(v["90d"], 0) + " <small style='font-size:.7rem'>90d</small>" : "—", vols || "—")}
        ${card("Perp funding (annualised)", (m.funding || []).length ? pct(m.funding[0].annual, 1) : "—", fund + "<br>positive = a short perp hedge is paid")}
        ${card("History", h.closes ? fmt(h.closes.length) + " days" : "—", range + (h.source ? " · " + esc(h.source) : ""))}
      </div>
      <div id="sb-hist" style="height:230px"></div>`;
    if (h.dates && h.dates.length && typeof Plotly !== "undefined") {
      const cc = colors();
      Plotly.newPlot("sb-hist", [{ x: h.dates, y: h.closes, type: "scatter", mode: "lines", line: { width: 1.6, color: "#58a6ff" }, name: m.asset }], {
        paper_bgcolor: cc.paper, plot_bgcolor: cc.plot, margin: { t: 10, r: 10, b: 30, l: 50 }, showlegend: false,
        xaxis: { color: cc.muted, gridcolor: cc.grid }, yaxis: { color: cc.muted, gridcolor: cc.grid, title: "USD" },
      }, { responsive: true, displayModeBar: false });
    }
  }

  function fillInputs() {
    const s = market.settings;
    $("sb-vol").value = s.atm_vol != null ? +(s.atm_vol * 100).toFixed(2) : "";
    $("sb-volwin").value = s.vol_window || "90d";
    $("sb-skew").value = s.skew_pts ?? 0;
    $("sb-carry").value = +((s.carry || 0) * 100).toFixed(4);
    $("sb-crate").value = +((s.collateral_rate || 0) * 100).toFixed(4);
    $("sb-spot").value = "";
    $("sb-hold").value = s.holdings || "";
    $("sb-cliff").value = s.cliff_date || "";
    $("sb-cliffpct").value = s.cliff_pct != null ? +(s.cliff_pct * 100).toFixed(4) : "";
    $("sb-months").value = s.linear_months ?? "";
  }

  function readOverrides() {
    const o = {};
    const vol = num("sb-vol");
    if (vol != null) o.atm_vol = vol / 100;
    else if ($("sb-volwin").value !== (market.settings.vol_window || "90d")) {
      const r = market.history.vols?.[$("sb-volwin").value];
      if (r != null) o.atm_vol = r;
    }
    if (num("sb-skew") != null) o.skew_pts = num("sb-skew");
    if (num("sb-carry") != null) o.carry = num("sb-carry") / 100;
    if (num("sb-crate") != null) o.collateral_rate = num("sb-crate") / 100;
    if (num("sb-spot") != null) o.spot = num("sb-spot");
    return o;
  }

  function renderInputsInfo() {
    const i = data.inputs;
    const what = Object.keys(overrides).length ? ` <b style="color:var(--orange)">What-if (not saved)</b>` : "";
    $("sb-inputs-info").innerHTML = `Pricing at spot <b>${px(i.spot)}</b>, ATM vol <b>${pct(i.atm_vol, 1)}</b> (${esc(i.vol_source)}), skew <b>${fmt(i.skew_pts, 1)}</b> pts / 10%, carry <b>${pct(i.carry, 2)}</b>, collateral cost <b>${pct(i.collateral_rate, 2)}</b>.${what}
      ${market.settings.updated_at ? `<span class="optv3-muted"> Defaults saved ${esc(market.settings.updated_at)}${market.settings.updated_by ? " by " + esc(market.settings.updated_by) : ""}.</span>` : ""}`;
  }

  // ── offers ────────────────────────────────────────────────────
  const legText = (l) => `${l.side === "buy" ? "Buy" : "Sell"} ${mln(l.qty)} ${l.kind === "forward" ? "fwd @" : (l.kind === "put" ? "P " : "C ")}${px(l.strike)}`;
  const visibleOffers = () => (data?.offers || []).filter((o) => !o.error && ($("sb-show-rejected").checked || o.status !== "rejected"));

  function renderOffers() {
    const rows = (data.offers || []).filter((o) => $("sb-show-rejected").checked || o.status !== "rejected").map((o) => {
      if (o.error) return `<tr><td>${o.id}</td><td class="sb-left" colspan="14" style="color:var(--red)">${esc(o.name)}: ${esc(o.error)}</td></tr>`;
      const L = o.live, fc = o.floor_cap, st = o.stats;
      const legs = (L.legs || []).map((l) => `<div title="our fair ${px(l.unit_price)} / token${l.vol ? " at " + pct(l.vol, 0) + " vol" : ""}">${esc(legText(l))}${l.vol ? `<div class="optv3-muted" style="font-size:.68rem">σ ${pct(l.vol, 0)} · fair ${px(l.unit_price)}</div>` : ""}</div>`).join("");
      const ref = o.at_ref ? `<div class="optv3-muted" style="font-size:.7rem" title="value at the quote's reference spot ${px(o.ref_spot)}">@ref ${px(o.ref_spot)}: ${usd(o.at_ref.net_value)}</div>` : "";
      const iskew = o.implied_skew_at_ref != null ? o.implied_skew_at_ref : o.implied_skew;
      const carry = o.implied_carry_at_ref != null ? o.implied_carry_at_ref : o.implied_carry;
      const warn = (o.warnings || []).map((w) => `<div class="sb-warn">⚠ ${esc(w)}</div>`).join("");
      return `<tr class="${o.status === "rejected" ? "sb-rejected" : ""}">
        <td><input type="checkbox" data-act="chart" data-id="${o.id}" ${hidden.has(o.id) ? "" : "checked"} title="Show on the chart"></td>
        <td class="sb-left"><b>${esc(o.counterparty || "—")}</b> · ${esc(o.name)}
          <div><span class="sb-st ${esc(o.status)}">${esc(o.status)}</span> <span class="optv3-muted" style="font-size:.7rem">${esc(o.settlement)}${o.collateral_usd != null ? " · collateral " + usd(o.collateral_usd) : ""}</span></div>
          <div class="sb-actions">${o.status !== "booked" ? `<button class="btn-secondary" data-act="book" data-id="${o.id}" style="color:var(--green)">Book</button>` : `<button class="btn-secondary" data-act="unbook" data-id="${o.id}">Unbook</button>`}
          ${o.status !== "rejected" ? `<button class="btn-secondary" data-act="reject" data-id="${o.id}">Reject</button>` : ""}
          <button class="btn-secondary" data-act="edit" data-id="${o.id}">Edit</button>
          <button class="btn-secondary" data-act="dup" data-id="${o.id}">Copy</button>
          <button class="btn-secondary" data-act="delete" data-id="${o.id}" style="color:var(--red)">Delete</button></div>
          ${o.notes ? `<div class="optv3-muted" style="font-size:.7rem" title="${esc(o.notes)}">${esc(o.notes.length > 90 ? o.notes.slice(0, 90) + "…" : o.notes)}</div>` : ""}${warn}</td>
        <td class="sb-left sb-legs">${legs}</td>
        <td>${esc(o.expiry)}<div class="optv3-muted" style="font-size:.7rem">${fmt(L.days)}d</div></td>
        <td class="num">${mln(L.hedge_qty)}<div class="optv3-muted" style="font-size:.7rem">${usd(L.notional_usd)}</div></td>
        <td class="num">${o.ref_spot ? px(o.ref_spot) : "<span class='optv3-muted'>?</span>"}</td>
        <td class="num" style="${signStyle(L.net_value)}" title="legs ${usd(L.legs_value)} + premium ${usd(L.premium_usd)} − collateral cost ${usd(L.collateral_cost)}"><b>${usd(L.net_value)}</b>
          <div style="font-size:.7rem">${pct(L.net_value_pct, 2)} of notional</div>${ref}</td>
        <td class="num" title="${o.implied_skew_at_ref != null ? "at the quote's reference spot" : "at live spot"}">${iskew == null ? "—" : fmt(iskew, 1)}</td>
        <td class="num" title="Annual carry that makes the forward price fair${o.implied_carry_at_ref != null ? " at the reference spot" : " at live spot"}">${carry == null ? "—" : pct(carry, 1)}</td>
        <td class="num">${px(fc.floor)}</td>
        <td class="num">${fc.cap == null ? "∞" : px(fc.cap)}</td>
        <td class="num">${px(st.expected)}</td>
        <td class="num">${px(st.p05)}</td>
        <td class="num">${fmt(L.delta / 1e6, 1)}m<div class="optv3-muted" style="font-size:.7rem">vega ${usd(L.vega)}</div></td>
      </tr>`;
    }).join("") || `<tr><td colspan="14" style="text-align:center;color:var(--muted)">No ${esc(asset())} offers yet. Add one with + New offer.</td></tr>`;
    $("sb-offers").innerHTML = `<table class="pf-table"><thead><tr><th></th><th>Offer</th><th>Legs (our side)</th><th>Expiry</th>
      <th class="num" title="Tokens hedged">Hedged</th><th class="num" title="Spot the dealer quoted against">Ref spot</th>
      <th class="num">Value to us</th><th class="num" title="Skew (vol pts / 10%) that makes the quote fair at our ATM vol">Implied skew</th>
      <th class="num" title="Forwards: annual carry implied by the forward price">Implied carry</th>
      <th class="num" title="Worst effective sale price">Floor</th><th class="num" title="Best effective sale price">Cap</th>
      <th class="num" title="Expected effective sale price">Expected</th><th class="num" title="5th percentile effective sale price">5th pct</th>
      <th class="num" title="Delta in tokens (from our side)">Delta</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  // ── chart + scenarios ────────────────────────────────────────
  const PALETTE = ["#58a6ff", "#3fb950", "#d29922", "#bc8cff", "#f778ba", "#39c5cf", "#ff7b72", "#e3b341"];

  // Which expiry's distribution to draw: the user's pick, else the expiry
  // most of the visible offers share.
  function distExpiry(offers) {
    const ds = data.distributions || {};
    const keys = Object.keys(ds);
    const sel = $("sb-dist-exp");
    const cur = sel.value;
    const counts = {};
    offers.forEach((o) => { counts[o.expiry] = (counts[o.expiry] || 0) + 1; });
    const common = keys.slice().sort((a, b) => (counts[b] || 0) - (counts[a] || 0))[0];
    sel.innerHTML = keys.map((k) => `<option value="${k}">${k} (${ds[k].days}d)</option>`).join("");
    sel.value = keys.includes(cur) ? cur : (common || "");
    return sel.value;
  }

  function renderChart() {
    const cc = colors();
    const offers = visibleOffers().filter((o) => !hidden.has(o.id));
    const g = data.grid;
    const traces = [];
    const shapes = [{ type: "line", x0: data.inputs.spot, x1: data.inputs.spot, yref: "paper", y0: 0, y1: 1, line: { color: cc.muted, dash: "dash", width: 1 } }];
    const annotations = [{ x: data.inputs.spot, yref: "paper", y: 1.02, text: "spot", showarrow: false, font: { color: cc.muted, size: 11 } }];
    const exp = distExpiry(offers);
    const dist = $("sb-dist-show").checked && exp ? data.distributions[exp] : null;
    if (dist) {
      const pc = dist.percentiles;
      traces.push({
        x: g, y: dist.density, yaxis: "y2", name: `Probability at ${exp}`, mode: "lines", fill: "tozeroy",
        line: { width: 1, color: "rgba(139,148,158,.9)" }, fillcolor: "rgba(139,148,158,.18)",
        customdata: dist.cdf,
        hovertemplate: `${data.asset} %{x:.4f}<br>P(below) ≈ %{customdata:.0%}<extra>probability</extra>`,
      });
      // 50% and 90% ranges as bands, the median as a line.
      shapes.push({ type: "rect", x0: pc.p05, x1: pc.p95, yref: "paper", y0: 0, y1: 1, fillcolor: "rgba(88,166,255,.05)", line: { width: 0 }, layer: "below" });
      shapes.push({ type: "rect", x0: pc.p25, x1: pc.p75, yref: "paper", y0: 0, y1: 1, fillcolor: "rgba(88,166,255,.09)", line: { width: 0 }, layer: "below" });
      shapes.push({ type: "line", x0: pc.p50, x1: pc.p50, yref: "paper", y0: 0, y1: 1, line: { color: "rgba(88,166,255,.7)", width: 1, dash: "dot" } });
      annotations.push({ x: pc.p50, yref: "paper", y: 0.97, text: "median", showarrow: false, font: { color: "#58a6ff", size: 10 } });
      annotations.push({ x: (pc.p25 + pc.p75) / 2, yref: "paper", y: 0.92, text: "50%", showarrow: false, font: { color: "#58a6ff", size: 10 } });
      annotations.push({ x: pc.p05, yref: "paper", y: 0.92, xanchor: "left", text: " 90% range", showarrow: false, font: { color: "#58a6ff", size: 10 } });
      $("sb-dist-info").innerHTML = `Where ${esc(data.asset)} lands on <b>${esc(exp)}</b> (${dist.days}d), from the vol ${pct(data.inputs.atm_vol, 0)} and skew ${fmt(data.inputs.skew_pts, 1)} the offers are priced on:
        median <b>${px(pc.p50)}</b> · 50% between <b>${px(pc.p25)}–${px(pc.p75)}</b> · 90% between <b>${px(pc.p05)}–${px(pc.p95)}</b> ·
        ${pct(dist.prob_below_spot, 0)} chance it ends below today's spot (a high-vol token's median sits below its forward ${px(dist.forward)}).
        Risk-neutral odds, not a forecast: no drift view is in them.`;
    } else {
      $("sb-dist-info").innerHTML = "";
    }
    traces.push({ x: g, y: g, name: "Unhedged (sell at spot)", mode: "lines", line: { dash: "dot", width: 1.5, color: cc.muted } });
    offers.forEach((o, i) => traces.push({
      x: g, y: o.curve, mode: "lines", line: { width: 2.2, color: PALETTE[i % PALETTE.length] },
      name: `${o.counterparty} · ${o.name}`.slice(0, 60),
    }));
    if (typeof Plotly === "undefined") return;
    Plotly.newPlot("sb-chart", traces, {
      paper_bgcolor: cc.paper, plot_bgcolor: cc.plot, margin: { t: 24, r: dist ? 60 : 20, b: 50, l: 60 },
      xaxis: { title: `${data.asset} at expiry (USD)`, color: cc.muted, gridcolor: cc.grid },
      yaxis: { title: "Effective sale price per hedged token (USD)", color: cc.muted, gridcolor: cc.grid },
      yaxis2: { title: "Probability density", overlaying: "y", side: "right", color: cc.muted, showgrid: false, rangemode: "tozero", showticklabels: false, visible: !!dist },
      legend: { font: { color: cc.muted }, orientation: "h", y: -0.2 },
      hovermode: "x unified", shapes, annotations,
    }, { responsive: true });
    renderZones(offers);
  }

  // Chance each offer finishes protected / in between / capped.
  function renderZones(offers) {
    const bar = (parts) => `<div style="display:flex;height:.55rem;border-radius:3px;overflow:hidden;min-width:9rem">${parts.map(([v, c]) =>
      `<div style="width:${(v * 100).toFixed(1)}%;background:${c}"></div>`).join("")}</div>`;
    const rows = offers.map((o) => {
      const z = o.stats.zones;
      if (!z) return "";
      if (z.kind === "forward") {
        return `<tr><td class="sb-left"><b>${esc(o.counterparty)}</b> · ${esc(o.name)}</td><td>${esc(o.expiry)}</td>
          <td class="num" style="color:var(--green)" title="Ends below the forward price: selling forward beat waiting">${pct(z.below, 0)}<div class="optv3-muted" style="font-size:.68rem">below ${px(z.level)}: forward wins</div></td>
          <td class="num">—</td>
          <td class="num" style="color:var(--red)" title="Ends above the forward price: we'd have done better unhedged">${pct(z.above, 0)}<div class="optv3-muted" style="font-size:.68rem">above ${px(z.level)}: upside given up</div></td>
          <td>${bar([[z.below, "var(--green)"], [z.above, "var(--red)"]])}</td>
          <td class="num">${px(o.stats.expected)}</td><td class="num">${px(o.stats.p05)}</td></tr>`;
      }
      return `<tr><td class="sb-left"><b>${esc(o.counterparty)}</b> · ${esc(o.name)}</td><td>${esc(o.expiry)}</td>
        <td class="num" style="color:var(--green)">${z.put_level ? pct(z.protected, 0) : "—"}<div class="optv3-muted" style="font-size:.68rem">${z.put_level ? "below " + px(z.put_level) + ": put pays" : "no put"}</div></td>
        <td class="num">${pct(z.between, 0)}<div class="optv3-muted" style="font-size:.68rem">sell at market</div></td>
        <td class="num" style="color:var(--red)">${z.call_level ? pct(z.capped, 0) : "—"}<div class="optv3-muted" style="font-size:.68rem">${z.call_level ? "above " + px(z.call_level) + ": capped" : "no call"}</div></td>
        <td>${bar([[z.protected, "var(--green)"], [z.between, "var(--muted)"], [z.capped, "var(--red)"]])}</td>
        <td class="num">${px(o.stats.expected)}</td><td class="num">${px(o.stats.p05)}</td></tr>`;
    }).join("");
    $("sb-zones").innerHTML = rows ? `<table class="pf-table"><thead><tr><th>Where each offer lands</th><th>Expiry</th>
      <th class="num">Protected</th><th class="num">In between</th><th class="num">Capped / given up</th><th></th>
      <th class="num" title="Expected effective sale price">Expected</th><th class="num" title="5th percentile effective sale price">5th pct</th></tr></thead><tbody>${rows}</tbody></table>` : "";
  }

  function renderScenarios() {
    const offers = visibleOffers().filter((o) => !hidden.has(o.id));
    const S = data.inputs.spot;
    const head = offers.map((o) => `<th class="num" title="${esc(o.name)}">${esc(o.counterparty)}<div class="optv3-muted" style="font-size:.68rem;font-weight:400">${esc(o.name.length > 26 ? o.name.slice(0, 26) + "…" : o.name)}</div></th>`).join("");
    const body = data.scenario_prices.map((p, i) => {
      const vals = offers.map((o) => o.scenarios[i]);
      const best = Math.max(p, ...vals);
      const cell = (v) => `<td class="num" style="${Math.abs(v - best) < 1e-12 ? "color:var(--green);font-weight:700" : ""}">${px(v)}</td>`;
      return `<tr><td class="num"><b>${px(p)}</b> <span class="optv3-muted" style="font-size:.7rem">${p / S - 1 >= 0 ? "+" : ""}${pct(p / S - 1, 0)}</span></td>${cell(p)}${vals.map(cell).join("")}</tr>`;
    }).join("");
    $("sb-scen").innerHTML = `<table class="pf-table"><thead><tr><th>${esc(data.asset)} at expiry</th><th class="num">Unhedged</th>${head}</tr></thead><tbody>${body}</tbody></table>
      <p class="optv3-hint" style="margin:.4rem 0 0">Effective sale price per hedged token, after premium and collateral cost. Green = best in the row. Each offer is per its own hedged size; the expiries differ slightly.</p>`;
  }

  // ── book ─────────────────────────────────────────────────────
  function renderBook() {
    const b = data.book;
    const card = (label, value, sub, style = "") => `<div class="collat-metric"><div class="collat-metric-label">${label}</div>
      <div class="collat-metric-value" style="${style}">${value}</div><div class="collat-metric-sub">${sub}</div></div>`;
    const cov = (b.coverage || []).map((c) => `<tr><td>${esc(c.date)}</td><td class="num">${mln(c.unlock)}</td><td class="num">${mln(c.hedged)}</td>
      <td class="num" style="${c.hedged > c.unlock * 1.000001 ? "color:var(--red)" : ""}">${pct(c.unlock ? c.hedged / c.unlock : 0, 0)}</td></tr>`).join("");
    $("sb-book").innerHTML = `<div class="fw-cards">
        ${card("Booked offers", fmt(b.count), "status Booked")}
        ${card("Hedged", mln(b.hedge_qty) + " " + esc(data.asset), "tokens")}
        ${card("Value to us", usd(b.net_value), "at current inputs", signStyle(b.net_value))}
        ${card("Delta", fmt(b.delta / 1e6, 1) + "m", "tokens", signStyle(b.delta))}
        ${card("Vega", usd(b.vega), "per vol point", signStyle(b.vega))}
        ${card("Collateral posted", usd(b.collateral_usd), "cost " + usd(b.collateral_cost) + " to expiry")}
      </div>
      ${cov ? `<h4 style="margin:1rem 0 .3rem">Unlock coverage</h4><table class="pf-table"><thead><tr><th>Unlock</th><th class="num">Tokens unlocking</th><th class="num">Hedged (booked, nearest expiry)</th><th class="num">Coverage</th></tr></thead><tbody>${cov}</tbody></table>` : `<p class="optv3-hint">Set holdings and the cliff date in Pricing inputs to see unlock coverage.</p>`}`;
  }

  // ── offer form ───────────────────────────────────────────────
  const TEMPLATES = {
    forward: (S) => [{ kind: "forward", side: "sell", qty: 10_000_000, strike: +S.toFixed(4) }],
    collar: (S) => [{ kind: "put", side: "buy", qty: 36_600_000, strike: +(S * 0.7).toFixed(4) }, { kind: "call", side: "sell", qty: 36_600_000, strike: +(S * 1.4).toFixed(4) }],
    put: (S) => [{ kind: "put", side: "buy", qty: 36_600_000, strike: +(S * 0.7).toFixed(4) }],
  };

  function legRow(l, i) {
    const opt = (v, cur, t) => `<option value="${v}" ${v === cur ? "selected" : ""}>${t}</option>`;
    return `<div class="sb-leg-row" data-i="${i}">
      <select data-f="side">${opt("buy", l.side, "We buy")}${opt("sell", l.side, "We sell")}</select>
      <select data-f="kind">${opt("put", l.kind, "Put")}${opt("call", l.kind, "Call")}${opt("forward", l.kind, "Forward")}</select>
      <input type="number" data-f="qty" step="any" value="${l.qty ?? ""}" placeholder="qty (tokens)">
      <input type="number" data-f="strike" step="any" value="${l.strike ?? ""}" placeholder="strike / fwd price">
      <button class="btn-secondary" data-act="rmleg" data-i="${i}" title="Remove leg">✕</button></div>`;
  }

  function openForm(o) {
    editing = o.id || "new";
    const v = (x) => (x == null ? "" : esc(x));
    const opt = (val, cur, t) => `<option value="${val}" ${val === cur ? "selected" : ""}>${t}</option>`;
    $("sb-form").style.display = "";
    $("sb-form").innerHTML = `<div class="sb-form">
      <b>${o.id ? "Edit offer #" + o.id : "New offer"}</b>
      <span class="optv3-muted" style="font-size:.75rem;margin-left:.6rem">Template:</span>
      <button class="btn-secondary" data-tpl="forward" style="width:auto;padding:.1rem .5rem">Forward sale</button>
      <button class="btn-secondary" data-tpl="collar" style="width:auto;padding:.1rem .5rem">Collar</button>
      <button class="btn-secondary" data-tpl="put" style="width:auto;padding:.1rem .5rem">Put</button>
      <div class="fw-form-grid" style="margin-top:.6rem">
        <label>Name<input type="text" id="of-name" value="${v(o.name)}"></label>
        <label>Counterparty<input type="text" id="of-cpty" value="${v(o.counterparty)}" list="sb-cptys"></label>
        <label>Expiry<input type="date" id="of-exp" value="${v(o.expiry)}"></label>
        <label>Settlement<select id="of-settle">${opt("physical", o.settlement, "Physical")}${opt("cash", o.settlement, "Cash")}${opt("tbc", o.settlement || "tbc", "TBC")}</select></label>
        <label title="Spot the dealer quoted against">Reference spot<input type="number" id="of-ref" step="any" value="${v(o.ref_spot)}"></label>
        <label>Quote date<input type="date" id="of-qdate" value="${v(o.quote_date)}"></label>
        <label title="Upfront, USD: + = we receive, - = we pay. 0 for a zero-cost structure.">Premium to us ($)<input type="number" id="of-prem" step="any" value="${v(o.premium_usd ?? 0)}"></label>
        <label title="Collateral we post, USD">Collateral ($)<input type="number" id="of-coll" step="any" value="${v(o.collateral_usd)}"></label>
        <label>Collateral note<input type="text" id="of-collnote" value="${v(o.collateral_note)}"></label>
        <label title="Blank = computed from the legs">Hedged qty override<input type="number" id="of-hq" step="any" value="${v(o.hedge_qty)}"></label>
        <label>Status<select id="of-status">${opt("indicative", o.status || "indicative", "Indicative")}${opt("booked", o.status, "Booked")}${opt("rejected", o.status, "Rejected")}</select></label>
      </div>
      <label style="display:flex;flex-direction:column;font-size:.75rem;color:var(--muted);gap:.25rem">Notes / original message<textarea id="of-notes" rows="2">${v(o.notes)}</textarea></label>
      <div style="margin-top:.6rem"><b style="font-size:.8rem">Legs, from our side</b> <span class="optv3-muted" style="font-size:.72rem">(we hold the tokens: a hedge is usually buy puts / sell calls / sell forward)</span>
        <div id="of-legs">${(o.legs || []).map(legRow).join("")}</div>
        <button class="btn-secondary" id="of-addleg" style="width:auto;margin-top:.3rem">+ Leg</button></div>
      <div style="margin-top:.8rem;display:flex;gap:.5rem">
        <button class="btn-primary" id="of-save" style="width:auto">Save</button>
        <button class="btn-secondary" id="of-cancel" style="width:auto">Cancel</button></div>
      <datalist id="sb-cptys">${["FalconX", "Flowdesk", "KeyRock", "Wintermute", "GSR", "Galaxy", "Our model"].map((c) => `<option value="${c}">`).join("")}</datalist>
    </div>`;
    $("sb-form").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function readLegs() {
    return [...document.querySelectorAll("#of-legs .sb-leg-row")].map((r) => {
      const f = (k) => r.querySelector(`[data-f="${k}"]`).value;
      return { side: f("side"), kind: f("kind"), qty: Number(f("qty")), strike: Number(f("strike")) };
    });
  }

  function readForm() {
    const n = (id) => { const x = $(id).value.trim(); return x === "" ? null : Number(x); };
    return {
      asset: asset(), name: $("of-name").value.trim(), counterparty: $("of-cpty").value.trim(),
      expiry: $("of-exp").value, settlement: $("of-settle").value, ref_spot: n("of-ref"),
      quote_date: $("of-qdate").value || null, premium_usd: n("of-prem") || 0, collateral_usd: n("of-coll"),
      collateral_note: $("of-collnote").value.trim(), hedge_qty: n("of-hq"), status: $("of-status").value,
      notes: $("of-notes").value.trim(), legs: readLegs(),
    };
  }

  async function saveForm() {
    const body = readForm();
    try {
      if (editing === "new") await api("POST", "/api/sandbox/offers", body);
      else await api("PUT", `/api/sandbox/offers/${editing}`, body);
      closeForm();
      await loadOffers();
      status("Saved.");
    } catch (e) { status("Save failed: " + e.message, true); }
  }

  function closeForm() { editing = null; $("sb-form").style.display = "none"; $("sb-form").innerHTML = ""; }

  // ── solver + program ─────────────────────────────────────────
  async function solve() {
    const body = {
      asset: asset(), solve_for: $("sv-for").value, fixed_strike: Number($("sv-k").value), expiry: $("sv-exp").value,
      target_premium_per_token: Number($("sv-prem").value || 0), call_ratio: Number($("sv-ratio").value || 1), ...overrides,
    };
    delete body.collateral_rate;
    try {
      const r = await api("POST", "/api/sandbox/solve", body);
      lastSolve = { ...r, qty: Number($("sv-qty").value || 0), ratio: body.call_ratio, expiry: body.expiry, prem: body.target_premium_per_token };
      if (!r.strike) {
        $("sv-out").innerHTML = `<p style="color:var(--red)">No strike achieves that at these inputs (e.g. the put is worth more than any call can pay for). Try a lower put, a premium, or more calls per put.</p>`;
        return;
      }
      const q = lastSolve.qty;
      $("sv-out").innerHTML = `<p>Fair at spot ${px(r.inputs.spot)}, vol ${pct(r.inputs.atm_vol, 0)}, skew ${fmt(r.inputs.skew_pts, 1)}, forward ${px(r.forward)} (${fmt(r.T * 365)}d):
        <b>buy ${px(r.put_strike)} put</b> (${pct(r.put_pct_spot, 0)} of spot, worth ${px(r.put_price)})
        / <b>sell ${fmt(body.call_ratio, 2)}× ${px(r.call_strike)} call</b> (${pct(r.call_pct_spot, 0)} of spot, worth ${px(r.call_price)}).
        ${q ? `On ${mln(q)} tokens: floor ${usd(q * r.put_strike)}, ${body.call_ratio === 1 ? "cap " + usd(q * r.call_strike) : "calls on " + mln(q * body.call_ratio)}.` : ""}</p>
        <button class="btn-secondary" id="sv-add" style="width:auto">Add as offer (Our model)</button>`;
    } catch (e) { $("sv-out").innerHTML = `<p style="color:var(--red)">${esc(e.message)}</p>`; }
  }

  async function addSolved() {
    const r = lastSolve;
    if (!r || !r.strike || !r.qty) return;
    const body = {
      asset: asset(), name: `Fair collar ${px(r.put_strike)}P / ${px(r.call_strike)}C${r.ratio !== 1 ? " x" + r.ratio : ""}`,
      counterparty: "Our model", expiry: r.expiry, settlement: "physical", ref_spot: r.inputs.spot,
      quote_date: new Date().toISOString().slice(0, 10), premium_usd: r.prem * r.qty, status: "indicative",
      notes: `Solver: fair at vol ${pct(r.inputs.atm_vol, 0)}, skew ${fmt(r.inputs.skew_pts, 1)}, carry ${pct(r.inputs.carry, 1)}. Counter-proposal benchmark.`,
      legs: [{ kind: "put", side: "buy", qty: r.qty, strike: +r.put_strike.toFixed(5) },
             { kind: "call", side: "sell", qty: r.qty * r.ratio, strike: +r.call_strike.toFixed(5) }],
    };
    try { await api("POST", "/api/sandbox/offers", body); await loadOffers(); status("Added to the offers."); }
    catch (e) { status("Add failed: " + e.message, true); }
  }

  async function program() {
    const q = new URLSearchParams({ asset: asset(), put_pct: Number($("pg-pct").value) / 100 });
    ["spot", "atm_vol", "skew_pts", "carry"].forEach((k) => { if (overrides[k] != null) q.set(k, overrides[k]); });
    try {
      const r = await api("GET", "/api/sandbox/program?" + q);
      lastProgram = r;
      const rows = r.tranches.map((t) => `<tr><td>${esc(t.date)}</td><td class="num">${fmt(t.days)}</td><td class="num">${mln(t.qty)}</td>
        <td class="num">${px(t.put_strike)}</td><td class="num">${t.call_strike ? px(t.call_strike) : "—"}</td>
        <td class="num">${t.call_strike ? pct(t.call_strike / r.inputs.spot, 0) : "—"}</td>
        <td class="num">${usd(t.floor_usd)}</td><td class="num">${t.cap_usd ? usd(t.cap_usd) : "—"}</td></tr>`).join("");
      $("pg-out").innerHTML = `<table class="pf-table"><thead><tr><th>Unlock / expiry</th><th class="num">Days</th><th class="num">Tokens</th>
        <th class="num">Put (we buy)</th><th class="num">Zero-cost call (we sell)</th><th class="num">Call % spot</th><th class="num">Floor proceeds</th><th class="num">Cap proceeds</th></tr></thead>
        <tbody>${rows}</tbody><tfoot><tr class="rr-total-row"><td colspan="6"><b>Guaranteed floor, whole program</b></td><td class="num"><b>${usd(r.floor_total)}</b></td><td></td></tr></tfoot></table>
        <p class="optv3-hint" style="margin:.4rem 0 0">Fair at spot ${px(r.inputs.spot)}, vol ${pct(r.inputs.atm_vol, 0)}, skew ${fmt(r.inputs.skew_pts, 1)}, carry ${pct(r.inputs.carry, 1)}: longer tenors carry more time value, so the same floor buys a higher cap. Use it to judge each dealer's monthly roll.</p>
        <button class="btn-secondary" id="pg-add" style="width:auto">Add all as offers (Our model)</button>`;
    } catch (e) { $("pg-out").innerHTML = `<p style="color:var(--red)">${esc(e.message)}</p>`; }
  }

  async function addProgram() {
    const r = lastProgram;
    if (!r) return;
    try {
      for (const t of r.tranches.filter((x) => x.call_strike)) {
        await api("POST", "/api/sandbox/offers", {
          asset: asset(), name: `Program ${t.date}: ${px(t.put_strike)}P / ${px(t.call_strike)}C`, counterparty: "Our model",
          expiry: t.date, settlement: "physical", ref_spot: r.inputs.spot, quote_date: new Date().toISOString().slice(0, 10),
          status: "indicative", notes: `Unlock program, put ${pct(r.put_pct, 0)} of spot, fair zero-cost call.`,
          legs: [{ kind: "put", side: "buy", qty: t.qty, strike: +t.put_strike.toFixed(5) },
                 { kind: "call", side: "sell", qty: t.qty, strike: +t.call_strike.toFixed(5) }],
        });
      }
      await loadOffers();
      status(`Added ${r.tranches.length} program collars.`);
    } catch (e) { status("Add failed: " + e.message, true); }
  }

  // ── load ─────────────────────────────────────────────────────
  function renderAll() {
    renderInputsInfo();
    renderOffers();
    renderChart();
    renderScenarios();
    renderBook();
  }

  async function loadOffers() {
    data = await api("GET", "/api/sandbox/offers?" + query());
    renderAll();
  }

  async function load() {
    $("sb-asset-label").textContent = asset();
    status("Loading " + asset() + "…");
    try {
      market = await api("GET", "/api/sandbox/market?asset=" + encodeURIComponent(asset()));
      renderMarket();
      fillInputs();
      overrides = {};
      await loadOffers();
      status("As of " + new Date().toLocaleString());
    } catch (e) {
      status("Failed: " + e.message, true);
    }
  }

  // ── wiring ───────────────────────────────────────────────────
  document.querySelector('.nav-item[data-page="sandbox"]')?.addEventListener("click", () => { if (!market) load(); });
  $("sb-refresh").addEventListener("click", load);
  $("sb-asset").addEventListener("change", load);
  $("sb-show-rejected").addEventListener("change", () => data && renderAll());
  $("sb-dist-show").addEventListener("change", () => data && renderChart());
  $("sb-dist-exp").addEventListener("change", () => data && renderChart());
  $("sb-apply").addEventListener("click", async () => {
    overrides = readOverrides();
    try { await loadOffers(); status("What-if applied (not saved)."); } catch (e) { status("Failed: " + e.message, true); }
  });
  $("sb-save").addEventListener("click", async () => {
    const n = num;
    const body = {
      atm_vol: n("sb-vol") != null ? n("sb-vol") / 100 : null, vol_window: $("sb-volwin").value,
      skew_pts: n("sb-skew") ?? 0, carry: (n("sb-carry") ?? 0) / 100, collateral_rate: (n("sb-crate") ?? 0) / 100,
      holdings: n("sb-hold") ?? 0, cliff_date: $("sb-cliff").value || null,
      cliff_pct: (n("sb-cliffpct") ?? 0) / 100, linear_months: n("sb-months") ?? 0,
    };
    try {
      await api("PUT", "/api/sandbox/settings?asset=" + encodeURIComponent(asset()), body);
      await load();
      status("Defaults saved.");
    } catch (e) { status("Save failed: " + e.message, true); }
  });
  $("sb-new").addEventListener("click", () => openForm({ legs: TEMPLATES.collar(data?.inputs.spot || 0.07), expiry: "2027-02-03", settlement: "tbc", premium_usd: 0 }));

  $("sb-offers").addEventListener("click", async (ev) => {
    const b = ev.target.closest("[data-act]");
    if (!b) return;
    const id = Number(b.dataset.id);
    const o = data.offers.find((x) => x.id === id);
    const act = b.dataset.act;
    try {
      if (act === "chart") {
        b.checked ? hidden.delete(id) : hidden.add(id);
        renderChart(); renderScenarios();
        return;
      }
      if (act === "edit") return openForm(o);
      if (act === "dup") return openForm({ ...o, id: null, name: o.name + " (copy)", status: "indicative" });
      if (act === "delete") {
        if (!confirm(`Delete sandbox offer #${id} "${o.name}"?`)) return;
        await api("DELETE", `/api/sandbox/offers/${id}`);
      }
      if (act === "book") await api("POST", `/api/sandbox/offers/${id}/status`, { status: "booked" });
      if (act === "unbook") await api("POST", `/api/sandbox/offers/${id}/status`, { status: "indicative" });
      if (act === "reject") await api("POST", `/api/sandbox/offers/${id}/status`, { status: "rejected" });
      await loadOffers();
    } catch (e) { status(e.message, true); }
  });

  $("sb-form").addEventListener("click", (ev) => {
    const t = ev.target;
    if (t.id === "of-save") return saveForm();
    if (t.id === "of-cancel") return closeForm();
    if (t.id === "of-addleg") {
      const legs = readLegs();
      legs.push({ kind: "put", side: "buy", qty: legs[0]?.qty || "", strike: "" });
      $("of-legs").innerHTML = legs.map(legRow).join("");
      return;
    }
    if (t.dataset.act === "rmleg") {
      const legs = readLegs();
      legs.splice(Number(t.dataset.i), 1);
      $("of-legs").innerHTML = legs.map(legRow).join("");
      return;
    }
    if (t.dataset.tpl) {
      $("of-legs").innerHTML = TEMPLATES[t.dataset.tpl](data?.inputs.spot || 0.07).map(legRow).join("");
      if (!$("of-name").value) $("of-name").value = { forward: "Forward sale", collar: "Zero-cost collar", put: "Put" }[t.dataset.tpl];
    }
  });

  $("sv-go").addEventListener("click", solve);
  $("sv-out").addEventListener("click", (ev) => { if (ev.target.id === "sv-add") addSolved(); });
  $("pg-go").addEventListener("click", program);
  $("pg-out").addEventListener("click", (ev) => { if (ev.target.id === "pg-add") addProgram(); });
})();
