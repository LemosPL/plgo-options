// ═══════════════════════════════════════════════════════════════
// Counterparty Reconciliation — Trade Management sub-tab.
// Loaded after app.js; shares its global helpers (currentAsset, post,
// api, tmLoad, fmtStrike, tmEnriched) and the global XLSX (SheetJS).
// ═══════════════════════════════════════════════════════════════
let reconLoaded = false;
let reconReportMd = "";
const RECON_COLLATERAL_ASSETS = ["ETH", "FIL", "USD", "USDC"];

function reconInit() {
  const label = document.getElementById("recon-asset-label");
  if (label) label.textContent = currentAsset;
  reconPopulateCounterparties();          // cheap — refresh every time
  if (reconLoaded) return;
  reconLoaded = true;

  reconSeedCollateral();
  if (!document.querySelectorAll("#recon-trades-body tr").length) reconAddTradeRow();

  document.getElementById("btn-recon-add-row").addEventListener("click", () => reconAddTradeRow());
  document.getElementById("btn-recon-clear").addEventListener("click", () => {
    document.getElementById("recon-trades-body").innerHTML = "";
    reconAddTradeRow();
  });
  document.getElementById("btn-recon-template").addEventListener("click", reconDownloadTemplateCsv);
  document.getElementById("btn-recon-template-xlsx").addEventListener("click", reconDownloadTemplateXlsx);
  document.getElementById("recon-file").addEventListener("change", reconHandleFile);
  document.getElementById("btn-recon-run").addEventListener("click", reconRun);
  document.getElementById("btn-recon-report").addEventListener("click", reconDownloadReport);
}

function reconPopulateCounterparties() {
  const dl = document.getElementById("recon-cp-list");
  if (!dl) return;
  const cps = [...new Set((typeof tmEnriched !== "undefined" ? tmEnriched : [])
    .map(t => t.counterparty).filter(c => c && c.trim()))].sort();
  dl.innerHTML = cps.map(c => `<option value="${c}"></option>`).join("");
}

function reconSeedCollateral() {
  const body = document.getElementById("recon-collat-body");
  body.innerHTML = "";
  for (const a of RECON_COLLATERAL_ASSETS) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><strong>${a}</strong></td>` +
      `<td><input type="number" step="any" data-asset="${a}" class="recon-collat-input" ` +
      `style="width:160px" placeholder="0"></td>`;
    body.appendChild(tr);
  }
}

function reconAddTradeRow(d) {
  d = d || {};
  const body = document.getElementById("recon-trades-body");
  const tr = document.createElement("tr");
  const cell = (field, val, w, ph) =>
    `<td><input data-field="${field}" value="${val != null ? String(val).replace(/"/g, "&quot;") : ""}" ` +
    `style="width:${w}px" placeholder="${ph || ""}"></td>`;
  tr.innerHTML =
    cell("trade_id", d.trade_id, 90) +
    cell("trade_date", d.trade_date, 90, "YYYY-MM-DD") +
    cell("side", d.side, 55, "Buy") +
    cell("option_type", d.option_type, 55, "Call") +
    cell("strike", d.strike, 70) +
    cell("expiry", d.expiry, 90, "YYYY-MM-DD") +
    cell("qty", d.qty, 100) +
    cell("premium_usd", d.premium_usd, 100) +
    `<td><button class="btn-remove recon-del" title="Remove row">✕</button></td>`;
  tr.querySelector(".recon-del").addEventListener("click", () => { tr.remove(); reconUpdateCount(); });
  body.appendChild(tr);
  reconUpdateCount();
}

function reconUpdateCount() {
  const el = document.getElementById("recon-trades-count");
  const n = document.querySelectorAll("#recon-trades-body tr").length;
  if (el) el.textContent = n ? `(${n})` : "";
}

function reconReadTrades() {
  const rows = [];
  for (const tr of document.querySelectorAll("#recon-trades-body tr")) {
    const o = {};
    tr.querySelectorAll("input[data-field]").forEach(i => o[i.dataset.field] = i.value.trim());
    if (!o.strike && !o.qty && !o.expiry && !o.trade_id) continue;   // skip blank rows
    rows.push({
      trade_id: o.trade_id || "", trade_date: o.trade_date || "",
      side: o.side || "", option_type: o.option_type || "",
      strike: parseFloat(o.strike) || 0, expiry: o.expiry || "",
      qty: parseFloat((o.qty || "").replace(/,/g, "")) || 0,
      premium_usd: parseFloat((o.premium_usd || "").replace(/,/g, "")) || 0,
    });
  }
  return rows;
}

function reconReadCollateral() {
  const rows = [];
  document.querySelectorAll(".recon-collat-input").forEach(i => {
    const v = parseFloat(i.value);
    if (!isNaN(v)) rows.push({ asset: i.dataset.asset, qty: v });
  });
  return rows;
}

// ── Template download ────────────────────────────────────────────
function reconDownloadTemplateCsv() {
  const cp = (document.getElementById("recon-cp").value.trim()) || "____";
  const lines = [
    "# PLGO RECONCILIATION TEMPLATE — fill the rows under each [SECTION]. Do NOT change the header lines.",
    `# ASSET: ${currentAsset}    COUNTERPARTY: ${cp}`,
    "# TRADES: one row per option leg. Side = the side PLGO took (Buy = PLGO bought).",
    "# Expiry accepts YYYY-MM-DD or 31JUL26. Delete the EXAMPLE row before sending back.",
    "[TRADES]",
    "trade_id,trade_date,side,option_type,strike,expiry,qty,premium_usd",
    "EXAMPLE-1,2026-05-01,Buy,Call,2.50,2026-07-31,500000,12345",
    "",
    "# COLLATERAL: total quantity of each asset PLGO has stored with you.",
    "[COLLATERAL]",
    "asset,qty",
    "ETH,0", "FIL,0", "USD,0", "USDC,0",
  ];
  reconDownload(`recon_template_${currentAsset}_${cp}.csv`, lines.join("\n"), "text/csv");
}

function reconDownloadTemplateXlsx() {
  const cp = (document.getElementById("recon-cp").value.trim()) || "____";
  const wb = XLSX.utils.book_new();
  const trades = [
    ["trade_id", "trade_date", "side", "option_type", "strike", "expiry", "qty", "premium_usd"],
    ["EXAMPLE-1", "2026-05-01", "Buy", "Call", 2.50, "2026-07-31", 500000, 12345],
  ];
  const collat = [["asset", "qty"], ["ETH", 0], ["FIL", 0], ["USD", 0], ["USDC", 0]];
  const ws1 = XLSX.utils.aoa_to_sheet(trades);
  const ws2 = XLSX.utils.aoa_to_sheet(collat);
  XLSX.utils.book_append_sheet(wb, ws1, "Trades");
  XLSX.utils.book_append_sheet(wb, ws2, "Collateral");
  XLSX.writeFile(wb, `recon_template_${currentAsset}_${cp}.xlsx`);
}

// ── File upload / parse ──────────────────────────────────────────
function reconHandleFile(e) {
  const file = e.target.files[0];
  if (!file) return;
  const isXlsx = /\.(xlsx|xls)$/i.test(file.name);
  const reader = new FileReader();
  reader.onload = (ev) => {
    try {
      if (isXlsx) reconParseXlsx(ev.target.result);
      else reconParseCsv(ev.target.result);
    } catch (err) {
      alert("Could not parse file: " + err.message);
    }
    e.target.value = "";   // allow re-uploading the same filename
  };
  if (isXlsx) reader.readAsArrayBuffer(file);
  else reader.readAsText(file);
}

function reconParseXlsx(buf) {
  const wb = XLSX.read(buf, { type: "array" });
  const find = (kw) => wb.SheetNames.find(s => s.toLowerCase().includes(kw));
  const ts = find("trade");
  const cs = find("collat");
  const trades = ts ? XLSX.utils.sheet_to_json(wb.Sheets[ts], { defval: "" }) : [];
  const collat = cs ? XLSX.utils.sheet_to_json(wb.Sheets[cs], { defval: "" }) : [];
  reconLoadParsed(trades.map(reconLowerKeys), collat.map(reconLowerKeys));
}

function reconLowerKeys(r) {
  const o = {};
  for (const k of Object.keys(r)) o[String(k).toLowerCase().trim()] = r[k];
  return o;
}

function reconSplitCsv(line) {
  const out = []; let cur = "", q = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (c === '"') { if (q && line[i + 1] === '"') { cur += '"'; i++; } else q = !q; }
    else if (c === "," && !q) { out.push(cur); cur = ""; }
    else cur += c;
  }
  out.push(cur);
  return out.map(s => s.trim());
}

function reconParseCsv(text) {
  const lines = text.split(/\r?\n/);
  const hasSections = /\[TRADES\]/i.test(text);
  let section = hasSections ? null : "trades";   // sectionless file => treat all as trades
  let tHeader = null, cHeader = null;
  const trades = [], collat = [];
  for (const raw of lines) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const up = line.toUpperCase();
    if (up === "[TRADES]") { section = "trades"; tHeader = null; continue; }
    if (up === "[COLLATERAL]") { section = "collateral"; cHeader = null; continue; }
    const cells = reconSplitCsv(line);
    if (section === "collateral") {
      if (!cHeader) { cHeader = cells.map(c => c.toLowerCase()); continue; }
      const o = {}; cHeader.forEach((h, i) => o[h] = cells[i] != null ? cells[i] : ""); collat.push(o);
    } else {
      if (!tHeader) { tHeader = cells.map(c => c.toLowerCase()); continue; }
      const o = {}; tHeader.forEach((h, i) => o[h] = cells[i] != null ? cells[i] : ""); trades.push(o);
    }
  }
  reconLoadParsed(trades, collat);
}

function reconLoadParsed(trades, collat) {
  const body = document.getElementById("recon-trades-body");
  body.innerHTML = "";
  let n = 0;
  for (const r of trades) {
    const g = (k) => (r[k] != null ? String(r[k]).trim() : "");
    const tid = g("trade_id");
    const strike = g("strike"), qty = g("qty"), expiry = g("expiry");
    if (!strike && !qty && !expiry && !tid) continue;
    if (tid.toUpperCase() === "EXAMPLE-1") continue;   // drop the template example
    reconAddTradeRow({
      trade_id: tid, trade_date: g("trade_date"), side: g("side"),
      option_type: g("option_type"), strike, expiry, qty, premium_usd: g("premium_usd"),
    });
    n++;
  }
  if (!n) reconAddTradeRow();

  // Collateral — map onto seeded inputs, append unknown assets.
  reconSeedCollateral();
  const map = {};
  for (const c of collat) {
    const a = String(c.asset != null ? c.asset : "").trim().toUpperCase();
    const q = c.qty != null ? c.qty : (c.quantity != null ? c.quantity : "");
    if (a) map[a] = q;
  }
  document.querySelectorAll(".recon-collat-input").forEach(i => {
    const v = map[i.dataset.asset];
    if (v != null && v !== "") i.value = v;
  });
  for (const a of Object.keys(map)) {
    if (!RECON_COLLATERAL_ASSETS.includes(a) && map[a] !== "") {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td><strong>${a}</strong></td>` +
        `<td><input type="number" step="any" data-asset="${a}" class="recon-collat-input" ` +
        `style="width:160px" value="${map[a]}"></td>`;
      document.getElementById("recon-collat-body").appendChild(tr);
    }
  }
  reconUpdateCount();
}

// ── Run reconciliation ───────────────────────────────────────────
async function reconRun() {
  const cp = document.getElementById("recon-cp").value.trim();
  if (!cp) { alert("Enter the counterparty name first."); return; }
  const their_trades = reconReadTrades();
  const their_collateral = reconReadCollateral();
  if (!their_trades.length && !their_collateral.length) {
    alert("Add at least one trade or collateral value to reconcile.");
    return;
  }
  const $btn = document.getElementById("btn-recon-run");
  $btn.disabled = true; $btn.textContent = "Running…";
  try {
    const res = await post("/api/reconciliation/run", {
      asset: currentAsset, counterparty: cp, their_trades, their_collateral,
    });
    reconRenderResults(res);
    reconReportMd = res.report_md || "";
    document.getElementById("btn-recon-report").disabled = !reconReportMd;
  } catch (e) {
    alert("Reconciliation error: " + e.message);
  }
  $btn.disabled = false; $btn.textContent = "Run reconciliation";
}

function reconStatusBadge(status) {
  const map = {
    match: ["OK", "var(--green)"],
    qty_mismatch: ["QTY MISMATCH", "var(--orange)"],
    only_ours: ["ONLY OURS", "var(--red)"],
    only_theirs: ["MISSING", "var(--accent)"],
  };
  const [txt, col] = map[status] || [status, "var(--muted)"];
  return `<span style="color:${col};font-weight:600;font-size:.72rem">${txt}</span>`;
}

function reconRenderResults(res) {
  const s = res.summary;
  const totalDisc = s.qty_mismatch + s.only_ours + s.only_theirs + s.collateral_mismatch;
  const verdict = totalDisc === 0
    ? `<span style="color:var(--green);font-weight:600">✓ Fully reconciled</span>`
    : `<span style="color:var(--orange);font-weight:600">⚠ ${totalDisc} discrepancy(ies)</span>`;

  let html = `<div class="card" style="margin:0 0 .5rem">` +
    `<div style="display:flex;gap:1.2rem;flex-wrap:wrap;align-items:center;font-size:.82rem">` +
    `${verdict}` +
    `<span>Matched: <strong>${s.match}</strong></span>` +
    `<span>Qty mismatch: <strong style="color:${s.qty_mismatch ? 'var(--orange)' : 'inherit'}">${s.qty_mismatch}</strong></span>` +
    `<span>Only ours: <strong style="color:${s.only_ours ? 'var(--red)' : 'inherit'}">${s.only_ours}</strong></span>` +
    `<span>Missing (add?): <strong style="color:${s.only_theirs ? 'var(--accent)' : 'inherit'}">${s.only_theirs}</strong></span>` +
    `<span>Collateral: <strong style="color:${s.collateral_mismatch ? 'var(--orange)' : 'inherit'}">${s.collateral_mismatch} off</strong></span>` +
    `<span style="color:var(--muted)">our ${s.our_trade_count} vs their ${s.their_trade_count} legs</span>` +
    `</div></div>`;

  html += `<table><thead><tr>` +
    `<th>Status</th><th>Type</th><th>Strike</th><th>Expiry</th>` +
    `<th>Our net</th><th>Their net</th><th>Diff</th><th>Action</th>` +
    `</tr></thead><tbody>`;
  res.trades.forEach((t, i) => {
    let action;
    if (t.status === "only_theirs") action = `<button class="btn-secondary recon-add" data-i="${i}">+ Add to ours</button>`;
    else if (t.status === "only_ours") action = `<button class="btn-secondary btn-delete recon-remove" data-i="${i}">Remove ours</button>`;
    else if (t.status === "qty_mismatch") action = `<span style="color:var(--orange);font-size:.7rem">review manually</span>`;
    else action = `<span style="color:var(--muted)">—</span>`;
    const diffCol = Math.abs(t.qty_diff) > 1 ? "var(--orange)" : "var(--muted)";
    html += `<tr><td>${reconStatusBadge(t.status)}</td><td>${t.type}</td>` +
      `<td>${fmtStrike(t.strike)}</td><td>${t.expiry}</td>` +
      `<td style="font-family:monospace">${t.our_net.toLocaleString()}</td>` +
      `<td style="font-family:monospace">${t.their_net.toLocaleString()}</td>` +
      `<td style="font-family:monospace;color:${diffCol}">${t.qty_diff.toLocaleString()}</td>` +
      `<td>${action}</td></tr>`;
  });
  html += `</tbody></table>`;

  if (res.collateral.length) {
    html += `<h3 style="margin:1rem 0 .35rem">Collateral</h3>` +
      `<table style="max-width:560px"><thead><tr>` +
      `<th>Asset</th><th>Our record</th><th>Their record</th><th>Diff</th></tr></thead><tbody>`;
    for (const c of res.collateral) {
      const oq = c.our_qty == null ? "—" : c.our_qty.toLocaleString(undefined, { maximumFractionDigits: 4 });
      const tq = c.their_qty == null ? "—" : c.their_qty.toLocaleString(undefined, { maximumFractionDigits: 4 });
      const col = c.match ? "var(--green)" : "var(--orange)";
      html += `<tr><td><strong>${c.asset}</strong></td>` +
        `<td style="font-family:monospace">${oq}</td>` +
        `<td style="font-family:monospace">${tq}</td>` +
        `<td style="font-family:monospace;color:${col}">${c.diff.toLocaleString(undefined, { maximumFractionDigits: 4 })} ${c.match ? "✓" : "⚠"}</td></tr>`;
    }
    html += `</tbody></table>`;
  }

  const container = document.getElementById("recon-results");
  container.innerHTML = html;
  container.querySelectorAll(".recon-add").forEach(b =>
    b.addEventListener("click", () => reconApplyAdd(res.trades[+b.dataset.i], b)));
  container.querySelectorAll(".recon-remove").forEach(b =>
    b.addEventListener("click", () => reconApplyRemove(res.trades[+b.dataset.i], b)));
}

// ── Apply decisions via the audited /api/trades endpoints ────────
async function reconApplyAdd(t, btn) {
  const s = t.suggested_add;
  if (!s) return;
  if (!confirm(`Add to OUR portfolio:\n${s.side} ${s.qty.toLocaleString()} ${s.option_type} ${s.strike} exp ${s.expiry} (${s.counterparty})?`)) return;
  btn.disabled = true; btn.textContent = "Adding…";
  try {
    await post("/api/trades/", {
      asset: s.asset, counterparty: s.counterparty,
      trade_id: s.trade_id || "", trade_date: s.trade_date || "",
      side: s.side, option_type: s.option_type, instrument: s.instrument,
      expiry: s.expiry, strike: s.strike, qty: s.qty,
      premium_usd: s.premium_usd, premium_per: s.qty ? s.premium_usd / s.qty : 0,
    });
    btn.textContent = "✓ Added";
    if (typeof tmLoad === "function") await tmLoad();
    reconPopulateCounterparties();
  } catch (e) {
    alert("Add failed: " + e.message);
    btn.disabled = false; btn.textContent = "+ Add to ours";
  }
}

async function reconApplyRemove(t, btn) {
  const ids = t.our_ids || [];
  if (!ids.length) return;
  if (!confirm(`Remove ${ids.length} trade(s) from OUR portfolio (${t.type} ${t.strike} exp ${t.expiry})?\nThis soft-deletes them (recoverable, audit-logged).`)) return;
  btn.disabled = true; btn.textContent = "Removing…";
  try {
    for (const id of ids) await api("DELETE", `/api/trades/${id}`);
    btn.textContent = "✓ Removed";
    if (typeof tmLoad === "function") await tmLoad();
  } catch (e) {
    alert("Remove failed: " + e.message);
    btn.disabled = false; btn.textContent = "Remove ours";
  }
}

// ── Downloads ────────────────────────────────────────────────────
function reconDownload(filename, content, mime) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

function reconDownloadReport() {
  if (!reconReportMd) return;
  const cp = (document.getElementById("recon-cp").value.trim() || "cp").replace(/\s+/g, "_");
  reconDownload(`recon_report_${currentAsset}_${cp}.md`, reconReportMd, "text/markdown");
}
