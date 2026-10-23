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
  // Greeks cell: red when negative, green when positive, shaded by its size
  // against the largest in its column so the big exposures stand out.
  const GREEK_COLS = ["delta", "delta_usd", "gamma", "gamma_usd", "theta", "vega", "mtm"];
  function shade(v, maxAbs) {
    if (!v || !maxAbs) return "";
    const a = (0.08 + Math.min(Math.abs(v) / maxAbs, 1) * 0.5).toFixed(3);
    return v < 0
      ? `background:rgba(218,54,51,${a});color:#ff9a95`
      : `background:rgba(46,160,67,${a});color:#7ee2a0`;
  }
  const signColor = (v) => (v < 0 ? "color:var(--red)" : v > 0 ? "color:var(--green)" : "");

  // Option legs (and perps) only; the collateral has its own table below.
  function positionsTable(sec, showCpty) {
    const a = sec.asset;
    const rows = sec.rows.filter((r) => r.kind !== "collateral");
    const maxAbs = {};
    GREEK_COLS.forEach((c) => { maxAbs[c] = Math.max(0, ...rows.map((r) => Math.abs(r[c] || 0))); });
    const cell = (r, c, dp) => `<td class="num" style="${shade(r[c], maxAbs[c])}">${fmt(r[c], dp)}</td>`;
    const body = rows.map((r) => `
      <tr>
        <td class="num" style="${signColor(r.size)}">${fmt(r.size, a === "FIL" ? 0 : 2)}</td>
        <td>${esc(r.symbol)}</td>
        ${showCpty ? `<td>${esc(r.counterparty || "")}</td>` : ""}
        ${cell(r, "delta", unitDp(a))}
        ${cell(r, "delta_usd", 0)}
        ${cell(r, "gamma", gammaDp(a))}
        ${cell(r, "gamma_usd", 0)}
        ${cell(r, "theta", 0)}
        ${cell(r, "vega", 0)}
        <td class="num">${r.iv_pct ? fmt(r.iv_pct, 1) + "%" : "—"}</td>
        ${cell(r, "mtm", 0)}
      </tr>`).join("")
      || `<tr><td colspan="${showCpty ? 11 : 10}" style="text-align:center;color:var(--muted)">No open option positions.</td></tr>`;
    const t = sec.options_totals;
    const tot = (c, dp) => `<td class="num" style="${signColor(t[c])}"><b>${fmt(t[c], dp)}</b></td>`;
    return `
      <div class="chain-wrapper">
      <table class="pf-table rr-table rr-greeks">
        <thead><tr>
          <th class="num">Size</th><th>Symbol</th>${showCpty ? "<th>Counterparty</th>" : ""}
          <th class="num">Delta</th><th class="num">Delta ($)</th>
          <th class="num">Gamma</th><th class="num" title="Change in Delta ($) for a 1% spot move">Gamma ($)</th>
          <th class="num" title="$ per day">Theta</th><th class="num" title="$ per vol point">Vega</th>
          <th class="num">IV</th><th class="num">MTM ($)</th>
        </tr></thead>
        <tbody>${body}</tbody>
        <tfoot><tr class="rr-total-row">
          <td></td><td><b>Options total:</b></td>${showCpty ? "<td></td>" : ""}
          ${tot("delta", unitDp(a))}${tot("delta_usd", 0)}${tot("gamma", gammaDp(a))}${tot("gamma_usd", 0)}
          ${tot("theta", 0)}${tot("vega", 0)}
          <td></td>
          ${tot("mtm", 0)}
        </tr></tfoot>
      </table></div>`;
  }

  // The spot collateral on its own, and the book's delta once it is added.
  function collateralTable(sec) {
    const a = sec.asset;
    const q = sec.collateral_qty || 0;
    const t = sec.totals;
    return `
      <div class="chain-wrapper" style="margin-top:.9rem">
      <table class="pf-table rr-table rr-collat">
        <thead><tr><th>${esc(a)} collateral</th><th class="num">Size</th><th class="num">Delta</th><th class="num">Delta ($)</th></tr></thead>
        <tbody>
          <tr><td>${esc(a)} Spot Collateral</td><td class="num">${fmt(q, unitDp(a))}</td>
            <td class="num">${fmt(q, unitDp(a))}</td><td class="num">${fmt(q * sec.spot, 0)}</td></tr>
        </tbody>
        <tfoot><tr class="rr-total-row"><td><b>Book delta incl. collateral</b></td><td></td>
          <td class="num" style="${signColor(t.delta)}"><b>${fmt(t.delta, unitDp(a))}</b></td>
          <td class="num" style="${signColor(t.delta_usd)}"><b>${fmt(t.delta_usd, 0)}</b></td></tr></tfoot>
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
    const rel = s.vol_mode === "relative";
    const head = s.vol_shocks.map((v) => `<th class="num">${v > 0 ? "+" : ""}${rel ? pct(v) : Math.round(v * 100)}</th>`).join("");
    const rows = s.spot_shocks.map((ds, i) => `
      <tr>
        <th class="rr-shock">${ds > 0 ? "+" : ""}${pct(ds)}</th>
        <td class="num rr-spot">${fmt(s.spots[i], spotDp(sec.asset))}</td>
        ${s.pnl[i].map((v) => `<td class="num" style="${heat(v, maxAbs)}">${fmt(v, 0)}</td>`).join("")}
      </tr>`).join("");
    return `
      <div class="chain-wrapper">
      <table class="pf-table rr-slides">
        <thead>
          <tr><th rowspan="2" title="Rows: spot moves, in % of today's spot">Spot move</th><th rowspan="2" class="num" title="Spot after the move">Spot</th>
            <th colspan="${s.vol_shocks.length}" class="rr-volhead" title="${rel ? "IV x (1 + shift)" : "Vol points added to every leg's IV: +5 = 50% to 55%"}">Vol shift (${rel ? "relative %" : "vol points"})</th></tr>
          <tr>${head}</tr>
        </thead>
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

  // ── notes (one per counterparty, saved in the database) ───────
  function notesSection(cp) {
    return `<section class="card">
      <h3 class="rr-h">Notes — ${esc(cp)}</h3>
      <textarea id="cp-notes" class="rr-notes" rows="8" placeholder="Notes on ${esc(cp)}: top-up requests, agreed levels, things to watch…"></textarea>
      <div class="rr-notes-bar">
        <span id="cp-notes-status" class="optv3-muted" style="font-size:.75rem"></span>
        <button id="cp-notes-save" class="btn-primary" style="width:auto" disabled>Save notes</button>
      </div></section>`;
  }

  function notesStamp(n) {
    if (!n.updated_at) return "Not saved yet";
    const when = new Date(n.updated_at.replace(" ", "T") + "Z").toLocaleString();
    return "Last saved " + when + (n.updated_by ? " by " + n.updated_by : "");
  }

  async function wireNotes(cp) {
    const $t = document.getElementById("cp-notes");
    const $save = document.getElementById("cp-notes-save");
    const $st = document.getElementById("cp-notes-status");
    if (!$t) return;
    let saved = "";
    try {
      const n = await getJSON("/api/risk-report/notes?counterparty=" + encodeURIComponent(cp));
      saved = n.notes || "";
      $t.value = saved;
      $st.textContent = notesStamp(n);
    } catch (e) {
      $st.textContent = "Could not load notes: " + e.message;
    }
    $t.addEventListener("input", () => {
      const dirty = $t.value !== saved;
      $save.disabled = !dirty;
      if (dirty) $st.textContent = "Unsaved changes";
    });
    $save.addEventListener("click", async () => {
      $save.disabled = true;
      $st.textContent = "Saving…";
      try {
        const r = await fetch("/api/risk-report/notes", {
          method: "PUT", credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ counterparty: cp, notes: $t.value }),
        });
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
        const n = await r.json();
        saved = n.notes || "";
        $st.textContent = notesStamp(n);
      } catch (e) {
        $save.disabled = false;
        $st.textContent = "Save failed: " + e.message;
      }
    });
  }

  // Unsaved notes would be lost on reload (counterparty / asset switch).
  function notesDirty() {
    const $save = document.getElementById("cp-notes-save");
    return $save && !$save.disabled;
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
    if (opts.collateral && notesDirty() && !confirm("You have unsaved notes. Discard them?")) {
      if (sel.dataset.current) sel.value = sel.dataset.current;
      return;
    }
    const cp = sel.value || "ALL";
    sel.dataset.current = cp;
    // The sidebar's ETH/FIL switch picks the book (app.js currentAsset).
    const asset = (typeof currentAsset !== "undefined" && currentAsset) || "ETH";
    const volMode = document.getElementById(prefix + "-volmode")?.value || "points";
    $status.textContent = "Loading…";
    try {
      const data = await getJSON(`/api/risk-report?counterparty=${encodeURIComponent(cp)}&asset=${asset}&vol_mode=${volMode}`);
      const showCpty = data.counterparty === "ALL";
      let html = opts.collateral ? `<section class="card"><h3 class="rr-h">Collateral — ${esc(data.counterparty)}</h3>${collateralCards(data)}</section>` : "";
      if (!data.sections.length) html += `<section class="card"><p class="optv3-hint">No open positions${showCpty ? "" : " with " + esc(data.counterparty)}.</p></section>`;
      data.sections.forEach((sec) => {
        if (opts.greeks) {
          html += `<section class="card">
            <h3 class="rr-h">${esc(sec.asset)} Greeks <span class="rr-spot-pill">Spot ${fmt(sec.spot, spotDp(sec.asset))}</span></h3>
            ${positionsTable(sec, showCpty)}${sec.collateral_qty ? collateralTable(sec) : ""}${bullets(sec)}</section>`;
        }
        if (opts.slides) {
          html += `<section class="card"><h3 class="rr-h">${esc(sec.asset)} Collateral Ladder <span class="rr-spot-pill">Spot ${fmt(sec.spot, spotDp(sec.asset))}</span></h3>${slidesTable(sec)}</section>`;
        }
      });
      const withNotes = opts.collateral && data.counterparty !== "ALL";
      if (withNotes) html += notesSection(data.counterparty);
      $out.innerHTML = html;
      if (withNotes) wireNotes(data.counterparty);
      $status.textContent = "As of " + new Date(data.as_of).toLocaleString();
    } catch (e) {
      $status.textContent = "Failed: " + e.message;
    }
  }

  const PAGES = {
    collatplat: { prefix: "cp", includeAll: false, defaultCpty: "Flowdesk", collateral: true, greeks: false, slides: true },
    greeks: { prefix: "gk", includeAll: true, defaultCpty: "ALL", collateral: false, greeks: true, slides: false },
  };

  Object.entries(PAGES).forEach(([page, opts]) => {
    const go = () => load(opts.prefix, opts);
    document.querySelector(`.nav-item[data-page="${page}"]`)?.addEventListener("click", go);
    ["cpty", "asset", "volmode"].forEach((id) => document.getElementById(`${opts.prefix}-${id}`)?.addEventListener("change", go));
    document.getElementById(`${opts.prefix}-refresh`)?.addEventListener("click", go);
    document.getElementById(`${opts.prefix}-print`)?.addEventListener("click", () => window.print());
  });
})();
