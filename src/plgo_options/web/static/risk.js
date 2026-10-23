// Collateral Platform + Greeks screens (/api/risk-report).
// Laid out like the counterparty position report (Flowdesk, "Protocol Labs
// Positions"): per asset a positions & greeks table with the spot collateral
// on top, the spot x vol "slides" P&L grid, and the summary bullets.
(function () {
  "use strict";

  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const fmt = (v, dp = 0) => (v == null || !isFinite(v)) ? "—"
    : Number(v).toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
  const pct = (v) => Math.round(v * 100) + "%";
  const usd = (v) => (v < 0 ? "-$" : "$") + fmt(Math.abs(v));
  const spotDp = (asset) => (asset === "FIL" ? 4 : 2);
  // Greeks in FIL are millions of units; ETH needs two decimals on gamma.
  const gammaDp = (asset) => (asset === "FIL" ? 0 : 2);
  const unitDp = (asset) => (asset === "FIL" ? 0 : 2);

  async function getJSON(url) {
    const r = await fetch(url, { credentials: "same-origin" });
    if (!r.ok) {
      let msg = r.status + " " + r.statusText;
      try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) { /* not JSON */ }
      throw new Error(msg);
    }
    return r.json();
  }

  // ── tables ────────────────────────────────────────────────────
  function positionsTable(sec, showCpty) {
    const a = sec.asset;
    const body = sec.rows.map((r) => `
      <tr class="${r.kind === "collateral" ? "rr-collat-row" : ""}">
        <td class="num">${fmt(r.size, r.kind === "collateral" || a === "FIL" ? 0 : 2)}</td>
        <td>${esc(r.symbol)}</td>
        ${showCpty ? `<td>${esc(r.counterparty || "")}</td>` : ""}
        <td class="num">${fmt(r.delta, unitDp(a))}</td>
        <td class="num">${fmt(r.delta_usd, 0)}</td>
        <td class="num">${fmt(r.gamma, gammaDp(a))}</td>
        <td class="num">${fmt(r.gamma_usd, 0)}</td>
        <td class="num">${fmt(r.theta, 0)}</td>
        <td class="num">${fmt(r.vega, 0)}</td>
        <td class="num">${r.iv_pct ? fmt(r.iv_pct, 1) + "%" : "—"}</td>
        <td class="num">${r.kind === "collateral" ? "—" : fmt(r.mtm, 0)}</td>
      </tr>`).join("");
    const t = sec.totals;
    return `
      <div class="chain-wrapper">
      <table class="pf-table rr-table">
        <thead><tr>
          <th class="num">Size</th><th>Symbol</th>${showCpty ? "<th>Counterparty</th>" : ""}
          <th class="num">Delta</th><th class="num">Delta ($)</th>
          <th class="num">Gamma</th><th class="num" title="Change in Delta ($) for a 1% spot move">Gamma ($)</th>
          <th class="num" title="$ per day">Theta</th><th class="num" title="$ per vol point">Vega</th>
          <th class="num">IV</th><th class="num">MTM ($)</th>
        </tr></thead>
        <tbody>${body}</tbody>
        <tfoot><tr class="rr-total-row">
          <td></td><td><b>Total:</b></td>${showCpty ? "<td></td>" : ""}
          <td class="num"><b>${fmt(t.delta, unitDp(a))}</b></td>
          <td class="num"><b>${fmt(t.delta_usd, 0)}</b></td>
          <td class="num"><b>${fmt(t.gamma, gammaDp(a))}</b></td>
          <td class="num"><b>${fmt(t.gamma_usd, 0)}</b></td>
          <td class="num"><b>${fmt(t.theta, 0)}</b></td>
          <td class="num"><b>${fmt(t.vega, 0)}</b></td>
          <td></td>
          <td class="num"><b>${fmt(t.mtm, 0)}</b></td>
        </tr></tfoot>
      </table></div>`;
  }

  function bullets(sec) {
    return `<ul class="rr-bullets">${sec.bullets.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>`;
  }

  // Green for gains, red for losses, deeper with size (like the report).
  function heat(v, maxAbs) {
    if (!maxAbs || !v) return "";
    const a = Math.min(Math.abs(v) / maxAbs, 1) * 0.75 + 0.04;
    return v > 0 ? `background:rgba(46,160,67,${a.toFixed(3)})` : `background:rgba(218,54,51,${a.toFixed(3)})`;
  }

  function slidesTable(sec) {
    const s = sec.slides;
    let maxAbs = 0;
    s.pnl.forEach((row) => row.forEach((v) => { maxAbs = Math.max(maxAbs, Math.abs(v)); }));
    const head = s.vol_shocks.map((v) => `<th class="num">${v > 0 ? "+" : ""}${s.vol_mode === "relative" ? pct(v) : Math.round(v * 100) + "pt"}</th>`).join("");
    const rows = s.spot_shocks.map((ds, i) => `
      <tr>
        <th class="rr-shock">${ds > 0 ? "+" : ""}${pct(ds)}</th>
        <td class="num rr-spot">${fmt(s.spots[i], spotDp(sec.asset))}</td>
        ${s.pnl[i].map((v) => `<td class="num" style="${heat(v, maxAbs)}">${fmt(v, 0)}</td>`).join("")}
      </tr>`).join("");
    return `
      <div class="chain-wrapper">
      <table class="pf-table rr-slides">
        <thead><tr><th>ΔSpot / ΔVol</th><th class="num">Spot</th>${head}</tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
      <p class="optv3-hint" style="margin:.4rem 0 0">P&amp;L against today of the options plus the ${esc(sec.asset)} collateral, repriced instantly (no decay) at each spot move and vol ${s.vol_mode === "relative" ? "move (relative: IV × (1 + shift))" : "move (vol points added to every leg's IV)"}.</p>`;
  }

  function collateralCards(data) {
    const c = data.collateral;
    const rows = c.rows.map((r) => `
      <tr><td>${esc(r.asset)}</td><td class="num">${fmt(r.qty, r.asset === "FIL" ? 0 : 2)}</td>
      <td class="num">${fmt(r.price, r.asset === "FIL" ? 4 : 2)}</td><td class="num">${fmt(r.usd, 0)}</td>
      <td class="num">${r.haircut ? fmt(r.haircut * 100, 1) + "%" : "—"}</td><td class="num">${fmt(r.usd_haircut, 0)}</td></tr>`).join("")
      || `<tr><td colspan="6" style="text-align:center;color:var(--muted)">No collateral posted with ${esc(data.counterparty)}.</td></tr>`;
    const bal = c.balance;
    return `
      <div class="rr-cards">
        <div class="collat-metric"><div class="collat-metric-label">Collateral posted</div>
          <div class="collat-metric-value">${usd(c.posted_usd_haircut)}</div>
          <div class="collat-metric-sub">after haircuts · ${usd(c.posted_usd)} at market</div></div>
        <div class="collat-metric"><div class="collat-metric-label">Options MTM</div>
          <div class="collat-metric-value" style="color:${c.options_mtm >= 0 ? "var(--green)" : "var(--red)"}">${usd(c.options_mtm)}</div>
          <div class="collat-metric-sub">our side, all books with them</div></div>
        <div class="collat-metric"><div class="collat-metric-label">Net balance</div>
          <div class="collat-metric-value" style="color:${bal >= 0 ? "var(--green)" : "var(--red)"}">${usd(bal)}</div>
          <div class="collat-metric-sub">${bal >= 0 ? "surplus over their exposure to us" : "top-up needed"}</div></div>
      </div>
      <div class="chain-wrapper" style="margin-top:.75rem">
      <table class="pf-table"><thead><tr><th>Asset</th><th class="num">Qty</th><th class="num">Price</th>
        <th class="num">USD</th><th class="num">Haircut</th><th class="num">USD after haircut</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`;
  }

  // ── pages ─────────────────────────────────────────────────────
  async function fillCounterparties(sel, includeAll, preferred) {
    if (sel.dataset.loaded) return;
    const names = await getJSON("/api/risk-report/counterparties");
    sel.innerHTML = (includeAll ? `<option value="ALL">All counterparties</option>` : "")
      + names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
    const want = names.find((n) => n.toLowerCase() === (preferred || "").toLowerCase());
    if (want) sel.value = want;
    sel.dataset.loaded = "1";
  }

  async function load(prefix, opts) {
    const $out = document.getElementById(prefix + "-content");
    const $status = document.getElementById(prefix + "-status");
    const sel = document.getElementById(prefix + "-cpty");
    try {
      await fillCounterparties(sel, opts.includeAll, opts.defaultCpty);
    } catch (e) {
      $status.textContent = "Could not load counterparties: " + e.message;
      return;
    }
    const cp = sel.value || "ALL";
    const asset = document.getElementById(prefix + "-asset").value;
    const volMode = document.getElementById(prefix + "-volmode")?.value || "points";
    $status.textContent = "Loading…";
    try {
      const data = await getJSON(`/api/risk-report?counterparty=${encodeURIComponent(cp)}&asset=${asset}&vol_mode=${volMode}`);
      const showCpty = data.counterparty === "ALL";
      let html = opts.collateral ? `<section class="card"><h3 class="rr-h">Collateral — ${esc(data.counterparty)}</h3>${collateralCards(data)}</section>` : "";
      if (!data.sections.length) html += `<section class="card"><p class="optv3-hint">No open positions${showCpty ? "" : " with " + esc(data.counterparty)}.</p></section>`;
      data.sections.forEach((sec) => {
        html += `<section class="card">
          <h3 class="rr-h">${esc(sec.asset)} Positions <span class="rr-spot-pill">Spot ${fmt(sec.spot, spotDp(sec.asset))}</span></h3>
          ${positionsTable(sec, showCpty)}${bullets(sec)}</section>`;
        if (opts.slides) {
          html += `<section class="card"><h3 class="rr-h">${esc(sec.asset)} Slides</h3>${slidesTable(sec)}</section>`;
        }
      });
      $out.innerHTML = html;
      $status.textContent = "As of " + new Date(data.as_of).toLocaleString();
    } catch (e) {
      $status.textContent = "Failed: " + e.message;
    }
  }

  const PAGES = {
    collatplat: { prefix: "cp", includeAll: false, defaultCpty: "Flowdesk", collateral: true, slides: true },
    greeks: { prefix: "gk", includeAll: true, defaultCpty: "ALL", collateral: false, slides: true },
  };

  Object.entries(PAGES).forEach(([page, opts]) => {
    const go = () => load(opts.prefix, opts);
    document.querySelector(`.nav-item[data-page="${page}"]`)?.addEventListener("click", go);
    ["cpty", "asset", "volmode"].forEach((id) => document.getElementById(`${opts.prefix}-${id}`)?.addEventListener("change", go));
    document.getElementById(`${opts.prefix}-refresh`)?.addEventListener("click", go);
    document.getElementById(`${opts.prefix}-print`)?.addEventListener("click", () => window.print());
  });
})();
