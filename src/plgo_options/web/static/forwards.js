// Forwards screen (/api/forwards): book, amend, reconcile, settle and value
// cash-settled forwards. The asset follows the sidebar's ETH/FIL switch.
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const fmt = (v, dp = 0) => (v == null || !isFinite(v)) ? "—"
    : (Number(v) + 0 === 0 || Math.abs(v) < 0.5 * Math.pow(10, -dp) ? 0 : Number(v))
      .toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
  const usd = (v) => (v == null || !isFinite(v)) ? "—" : (v < 0 ? "-$" : "$") + fmt(Math.abs(v));
  const signStyle = (v) => (v < 0 ? "color:var(--red)" : v > 0 ? "color:var(--green)" : "");
  const asset = () => (typeof currentAsset !== "undefined" && currentAsset) || "ETH";
  const pxDp = () => (asset() === "FIL" ? 4 : 2);
  const qtyDp = () => (asset() === "FIL" ? 0 : 2);
  const today = () => new Date().toISOString().slice(0, 10);

  let book = null;          // last /book response
  let editingId = null;     // trade being amended in the form
  let reconOpen = null;     // trade whose reconcile row is open

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
    const el = $("fw-status");
    el.textContent = msg || "";
    el.style.color = isErr ? "var(--red)" : "";
  }

  // ── rendering ─────────────────────────────────────────────────
  function cards() {
    const t = book.totals;
    const recon = t.breaks ? `<span style="color:var(--red)">${t.breaks} break${t.breaks > 1 ? "s" : ""}</span>`
      : t.unreconciled ? `${t.unreconciled} not reconciled` : t.count ? `<span style="color:var(--green)">All matched</span>` : "—";
    const card = (label, value, sub, style = "") => `<div class="collat-metric"><div class="collat-metric-label">${label}</div>
      <div class="collat-metric-value" style="${style}">${value}</div><div class="collat-metric-sub">${sub}</div></div>`;
    $("fw-cards").innerHTML =
      card("Open forwards", fmt(t.count), `${fmt(t.matured)} past maturity, awaiting settlement`)
      + card("Net position", fmt(t.net_qty, qtyDp()) + " " + asset(), `delta ${fmt(t.delta, qtyDp())} · ${usd(t.delta_usd)}`, signStyle(t.net_qty))
      + card("MTM", usd(t.mtm), `vs fair forward at spot ${fmt(book.spot, pxDp())}`, signStyle(t.mtm))
      + card("Carry / day", usd(t.theta), `${fmt(book.carry * 100, 2)}% a year carry`, signStyle(t.theta))
      + card("Realised P&L", usd(book.realised_pnl_usd), `${fmt(book.settled.length)} settled`, signStyle(book.realised_pnl_usd))
      + card("Reconciliation", recon, "vs counterparty figures");
  }

  function reconBadge(r) {
    if (r.status === "matched") return `<span class="fw-badge fw-ok">Matched</span>`;
    if (r.status === "break") return `<span class="fw-badge fw-break" title="${esc(r.breaks.map((b) => b.field).join(", "))}">Break</span>`;
    return `<span class="fw-badge">Not reconciled</span>`;
  }

  function reconRow(t) {
    const val = (v) => (v == null ? "" : v);
    const br = t.recon.breaks.map((b) => `<li><b>${esc(b.field)}</b>: ours ${fmt(b.ours, b.field === "mtm" ? 0 : pxDp())}, theirs ${fmt(b.theirs, b.field === "mtm" ? 0 : pxDp())} (diff ${fmt(b.diff, b.field === "mtm" ? 0 : pxDp())})</li>`).join("");
    return `<tr class="fw-recon-row"><td colspan="14">
      <div class="fw-recon">
        <b>Counterparty figures for #${t.id}</b>
        <label>Their qty <input type="number" id="fw-rq" step="any" value="${val(t.cpty_qty)}" placeholder="${fmt(t.qty, qtyDp())}"></label>
        <label>Their forward price <input type="number" id="fw-rp" step="any" value="${val(t.cpty_price)}" placeholder="${t.forward_price}"></label>
        <label title="Their MTM of this forward, from OUR side (positive = in our favour)">Their MTM (our side, $) <input type="number" id="fw-rm" step="any" value="${val(t.cpty_mtm)}"></label>
        <label>Note <input type="text" id="fw-rn" value="${esc(t.recon_note || "")}" style="width:14rem"></label>
        <button class="btn-primary" id="fw-recon-save" data-id="${t.id}" style="width:auto">Save</button>
        <button class="btn-secondary" id="fw-recon-cancel" style="width:auto">Close</button>
        ${t.recon_at ? `<span class="optv3-muted" style="font-size:.75rem">Last entered ${esc(t.recon_at)}${t.recon_by ? " by " + esc(t.recon_by) : ""}</span>` : ""}
        ${br ? `<ul class="fw-breaks">${br}</ul>` : ""}
      </div></td></tr>`;
  }

  function positions() {
    const rows = book.trades.map((t) => `
      <tr class="${t.matured ? "fw-matured" : ""}">
        <td>${t.id}</td><td title="${esc([t.external_ref, t.note].filter(Boolean).join(" · "))}">${esc(t.counterparty)}${t.external_ref || t.note
          ? `<div class="optv3-muted fw-ref">${esc([t.external_ref ? "Ref " + t.external_ref.slice(0, 8) + (t.external_ref.length > 8 ? "…" : "") : "",
                t.note.length > 28 ? t.note.slice(0, 28) + "…" : t.note].filter(Boolean).join(" · "))}</div>` : ""}</td>
        <td style="${t.side === "buy" ? "color:var(--green)" : "color:var(--red)"}">${t.side === "buy" ? "Buy" : "Sell"}</td>
        <td class="num">${fmt(t.qty, qtyDp())}</td>
        <td class="num">${fmt(t.forward_price, pxDp())}</td>
        <td>${esc(t.trade_date)}</td><td>${esc(t.maturity_date)}</td>
        <td class="num">${t.matured ? `<span style="color:var(--orange)">matured</span>` : fmt(t.days_to_maturity)}</td>
        <td class="num">${fmt(t.fair_forward, pxDp())}</td>
        <td class="num" style="${signStyle(t.mtm)}">${fmt(t.mtm)}</td>
        <td class="num" style="${signStyle(t.delta)}">${fmt(t.delta, qtyDp())}</td>
        <td class="num" style="${signStyle(t.theta)}">${fmt(t.theta)}</td>
        <td>${reconBadge(t.recon)}</td>
        <td class="fw-actions">
          <button class="btn-secondary" data-act="edit" data-id="${t.id}">Edit</button>
          <button class="btn-secondary" data-act="recon" data-id="${t.id}">Reconcile</button>
          <button class="btn-secondary" data-act="settle" data-id="${t.id}">Settle</button>
          <button class="btn-secondary" data-act="delete" data-id="${t.id}" style="color:var(--red)">Delete</button>
        </td>
      </tr>${reconOpen === t.id ? reconRow(t) : ""}`).join("")
      || `<tr><td colspan="14" style="text-align:center;color:var(--muted)">No open ${esc(asset())} forwards. Book one above.</td></tr>`;
    const t = book.totals;
    $("fw-positions").innerHTML = `
      <table class="pf-table fw-table">
        <thead><tr><th>#</th><th>Counterparty</th><th>Side</th><th class="num">Qty</th><th class="num">Forward price</th>
          <th>Trade date</th><th>Maturity</th><th class="num">Days</th><th class="num" title="Spot x (1 + carry x time to maturity)">Fair forward</th>
          <th class="num">MTM ($)</th><th class="num">Delta</th><th class="num" title="$ per day">Carry/day</th><th>Recon</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
        ${book.trades.length ? `<tfoot><tr class="rr-total-row"><td></td><td><b>Total</b></td><td></td>
          <td class="num"><b style="${signStyle(t.net_qty)}">${fmt(t.net_qty, qtyDp())}</b></td><td colspan="5"></td>
          <td class="num"><b style="${signStyle(t.mtm)}">${fmt(t.mtm)}</b></td>
          <td class="num"><b style="${signStyle(t.delta)}">${fmt(t.delta, qtyDp())}</b></td>
          <td class="num"><b style="${signStyle(t.theta)}">${fmt(t.theta)}</b></td><td colspan="2"></td></tr></tfoot>` : ""}
      </table>`;
  }

  function byCounterparty() {
    const rows = book.by_counterparty.map((c) => `<tr><td>${esc(c.counterparty)}</td><td class="num">${fmt(c.count)}</td>
      <td class="num" style="${signStyle(c.net_qty)}">${fmt(c.net_qty, qtyDp())}</td>
      <td class="num">${fmt(c.avg_forward_price, pxDp())}</td>
      <td class="num" style="${signStyle(c.mtm)}">${fmt(c.mtm)}</td>
      <td class="num">${fmt(c.notional_usd)}</td>
      <td>${c.breaks ? `<span style="color:var(--red)">${c.breaks} break(s)</span>` : c.unreconciled ? `${c.unreconciled} pending` : "Matched"}</td></tr>`).join("")
      || `<tr><td colspan="7" style="text-align:center;color:var(--muted)">—</td></tr>`;
    $("fw-bycp").innerHTML = `<table class="pf-table fw-table"><thead><tr><th>Counterparty</th><th class="num">Trades</th>
      <th class="num">Net qty</th><th class="num">Avg forward price</th><th class="num">MTM ($)</th><th class="num">Notional ($)</th><th>Recon</th></tr></thead>
      <tbody>${rows}</tbody></table>
      <p class="optv3-hint" style="margin:.4rem 0 0">Each counterparty's forward MTM nets into its collateral liability (Collateral tab), and its net position joins the options book in the optimizer, Greeks and Collateral Platform.</p>`;
  }

  function settled() {
    const rows = book.settled.map((s) => `<tr><td>${s.id}</td><td>${esc(s.counterparty)}</td><td>${s.side === "buy" ? "Buy" : "Sell"}</td>
      <td class="num">${fmt(s.qty, qtyDp())}</td><td class="num">${fmt(s.forward_price, pxDp())}</td><td>${esc(s.maturity_date)}</td>
      <td class="num">${fmt(s.fixing_price, pxDp())}</td><td class="num" style="${signStyle(s.settled_pnl_usd)}">${fmt(s.settled_pnl_usd)}</td>
      <td>${esc((s.settled_at || "").replace("T", " "))}</td><td>${esc(s.updated_by || "")}</td></tr>`).join("")
      || `<tr><td colspan="10" style="text-align:center;color:var(--muted)">No settled ${esc(asset())} forwards yet.</td></tr>`;
    $("fw-settled").innerHTML = `<table class="pf-table fw-table"><thead><tr><th>#</th><th>Counterparty</th><th>Side</th><th class="num">Qty</th>
      <th class="num">Forward price</th><th>Maturity</th><th class="num">Fixing</th><th class="num">Realised P&amp;L ($)</th><th>Settled at (UTC)</th><th>By</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
  }

  function render() {
    $("fw-asset-label").textContent = asset();
    $("fw-carry").value = +(book.carry * 100).toFixed(4);
    $("fw-carry-info").textContent = book.carry_info && book.carry_info.updated_at
      ? `set ${book.carry_info.updated_at}${book.carry_info.updated_by ? " by " + book.carry_info.updated_by : ""}` : "not set (0%)";
    cards(); positions(); byCounterparty(); settled();
  }

  async function load() {
    status("Loading…");
    try {
      book = await api("GET", `/api/forwards/book?asset=${asset()}`);
      render();
      status("Spot " + fmt(book.spot, pxDp()) + " · as of " + new Date().toLocaleTimeString());
    } catch (e) {
      status("Failed to load: " + e.message, true);
    }
    fillCounterparties();
  }

  async function fillCounterparties() {
    const dl = $("fw-cp-list");
    if (!dl || dl.dataset.loaded) return;
    try {
      const names = await api("GET", "/api/risk-report/counterparties");
      dl.innerHTML = names.map((n) => `<option value="${esc(n)}">`).join("");
      dl.dataset.loaded = "1";
    } catch (_) { /* free text still works */ }
  }

  // ── form: book / amend ────────────────────────────────────────
  function formValues() {
    return {
      asset: asset(),
      counterparty: $("fw-f-cp").value.trim(),
      side: $("fw-f-side").value,
      qty: parseFloat($("fw-f-qty").value),
      forward_price: parseFloat($("fw-f-price").value),
      trade_date: $("fw-f-tdate").value,
      maturity_date: $("fw-f-mdate").value,
      external_ref: $("fw-f-ref").value.trim(),
      note: $("fw-f-note").value.trim(),
    };
  }

  function resetForm() {
    editingId = null;
    if (!$("fw-f-cp")) return;           // booking moved to New Trade -> Forward
    ["fw-f-cp", "fw-f-qty", "fw-f-price", "fw-f-mdate", "fw-f-ref", "fw-f-note"].forEach((id) => { $(id).value = ""; });
    $("fw-f-side").value = "sell";
    $("fw-f-tdate").value = today();
    $("fw-f-submit").textContent = "Book forward";
    $("fw-f-cancel").style.display = "none";
    $("fw-form-title").textContent = "Book a forward";
  }

  async function submitForm() {
    const f = formValues();
    if (!f.counterparty || !(f.qty > 0) || !(f.forward_price > 0) || !f.trade_date || !f.maturity_date) {
      status("Fill in counterparty, qty, forward price, trade date and maturity.", true);
      return;
    }
    const verb = editingId ? "Amend" : "Book";
    const msg = `${verb}: ${f.side === "buy" ? "BUY" : "SELL"} ${fmt(f.qty, qtyDp())} ${f.asset} forward @ ${f.forward_price} with ${f.counterparty}, maturity ${f.maturity_date}?`;
    if (!confirm(msg)) return;
    try {
      if (editingId) await api("PUT", `/api/forwards/trades/${editingId}`, f);
      else await api("POST", "/api/forwards/trades", f);
      status(editingId ? `Forward #${editingId} amended.` : "Forward booked.");
      resetForm();
      await load();
      refreshTradeTotals();
    } catch (e) {
      status(`${verb} failed: ${e.message}`, true);
    }
  }

  function startEdit(t) {
    editingId = t.id;
    $("fw-f-cp").value = t.counterparty;
    $("fw-f-side").value = t.side;
    $("fw-f-qty").value = t.qty;
    $("fw-f-price").value = t.forward_price;
    $("fw-f-tdate").value = t.trade_date;
    $("fw-f-mdate").value = t.maturity_date;
    $("fw-f-ref").value = t.external_ref || "";
    $("fw-f-note").value = t.note || "";
    $("fw-f-submit").textContent = `Save changes to #${t.id}`;
    $("fw-f-cancel").style.display = "";
    $("fw-form-title").textContent = `Amend forward #${t.id}`;
    $("fw-form").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ── row actions ───────────────────────────────────────────────
  async function onAction(act, id) {
    const t = book.trades.find((x) => x.id === id);
    if (!t) return;
    try {
      if (act === "edit") { if (typeof tmEditForward === "function") tmEditForward(id); return; }
      if (act === "recon") { reconOpen = reconOpen === id ? null : id; positions(); return; }
      if (act === "delete") {
        if (!confirm(`Delete forward #${id} (${t.side} ${fmt(t.qty, qtyDp())} with ${t.counterparty})? It is kept in the database as deleted.`)) return;
        await api("DELETE", `/api/forwards/trades/${id}`);
        status(`Forward #${id} deleted.`);
      }
      if (act === "settle") {
        const raw = prompt(`Settle forward #${id}: fixing price for ${asset()}?`, String(+book.spot.toFixed(pxDp())));
        if (raw == null) return;
        const fix = parseFloat(raw);
        if (!(fix > 0)) { status("Fixing price must be a positive number.", true); return; }
        const pnl = (t.side === "buy" ? 1 : -1) * t.qty * (fix - t.forward_price);
        if (!confirm(`Cash settlement at ${fix}: ${pnl >= 0 ? "we receive" : "we pay"} ${usd(Math.abs(pnl))}. Close the forward?`)) return;
        await api("POST", `/api/forwards/trades/${id}/settle`, { fixing_price: fix });
        status(`Forward #${id} settled: realised ${usd(pnl)}.`);
      }
      await load();
      refreshTradeTotals();
    } catch (e) {
      status(`${act} failed: ${e.message}`, true);
    }
  }

  // Used by the trades table's forward rows (Settle / Delete buttons).
  window.fwReload = load;
  window.fwSettle = async (id) => { if (!book) await load(); onAction("settle", id); };
  window.fwDelete = async (id) => { if (!book) await load(); onAction("delete", id); };

  async function saveRecon(id) {
    const num = (elId) => { const v = $(elId).value; return v === "" ? null : parseFloat(v); };
    try {
      await api("PUT", `/api/forwards/trades/${id}/recon`, {
        cpty_qty: num("fw-rq"), cpty_price: num("fw-rp"), cpty_mtm: num("fw-rm"), note: $("fw-rn").value,
      });
      status(`Counterparty figures saved for #${id}.`);
      await load();
    } catch (e) {
      status("Reconcile failed: " + e.message, true);
    }
  }

  // Trade Management's totals bar reads the forwards from /pnl: reload it.
  function refreshTradeTotals() {
    try { if (typeof tmLoad === "function") tmLoad(); } catch (_) { /* totals refresh on next visit */ }
  }

  // ── wiring ────────────────────────────────────────────────────
  // Lives on Trade Management's Trades tab: load with the page (the sidebar
  // asset switch re-clicks the active page, so ETH/FIL follows too).
  document.querySelector('.nav-item[data-page="trades"]')?.addEventListener("click", () => { if (!editingId) resetForm(); load(); });
  document.querySelector('.sub-tab-btn[data-subtab="tm-trades"]')?.addEventListener("click", load);
  if (document.getElementById("page-trades")?.classList.contains("active")) { resetForm(); load(); }
  $("fw-refresh")?.addEventListener("click", load);
  $("fw-f-submit")?.addEventListener("click", submitForm);
  $("fw-f-cancel")?.addEventListener("click", resetForm);
  $("fw-carry-save")?.addEventListener("click", async () => {
    const pct = parseFloat($("fw-carry").value);
    if (!isFinite(pct)) { status("Carry must be a number (% a year).", true); return; }
    try {
      await api("PUT", "/api/forwards/carry", { asset: asset(), rate_pct: pct });
      status(`${asset()} carry set to ${pct}% a year.`);
      await load();
    } catch (e) { status("Carry save failed: " + e.message, true); }
  });
  $("fw-positions")?.addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    if (b.id === "fw-recon-save") { saveRecon(parseInt(b.dataset.id, 10)); return; }
    if (b.id === "fw-recon-cancel") { reconOpen = null; positions(); return; }
    if (b.dataset.act) onAction(b.dataset.act, parseInt(b.dataset.id, 10));
  });
})();
