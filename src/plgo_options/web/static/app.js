"use strict";

// ─── State ──────────────────────────────────────────────────
let ethSpot = null;
let optionChain = [];   // OptionTicker[]
let legs = [];          // {side, type, strike, premium, quantity}
let volSurface = null;  // cached Deribit vol surface {eth_spot, smiles: [{expiry_code, dte, strikes, ivs}]}

// ─── DOM refs ───────────────────────────────────────────────
const $spot       = document.getElementById("eth-spot");
const $expSel     = document.getElementById("expiry-select");
const $btnLoad    = document.getElementById("btn-load-chain");
const $legsBody   = document.getElementById("legs-body");
const $btnAdd     = document.getElementById("btn-add-leg");
const $btnCompute = document.getElementById("btn-compute");
const $btnRepl    = document.getElementById("btn-replicate");
const $spotMin    = document.getElementById("spot-min");
const $spotMax    = document.getElementById("spot-max");
const $chainSec   = document.getElementById("chain-section");
const $chainExp   = document.getElementById("chain-expiry");
const $chainBody  = document.getElementById("chain-body");

// ─── API helpers ────────────────────────────────────────────
async function api(method, path, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

const get  = (path) => api("GET", path);
const post = (path, body) => api("POST", path, body);

// ─── Vol surface helpers (for pricer) ────────────────────────
function lookupSmileIv(expiryCode, strike) {
  if (!volSurface || !volSurface.smiles) return null;
  const smile = volSurface.smiles.find(s => s.expiry_code === expiryCode);
  if (!smile) return null;
  const { strikes, ivs } = smile;
  if (!strikes || strikes.length === 0) return null;
  if (strike <= strikes[0]) return ivs[0];
  if (strike >= strikes[strikes.length - 1]) return ivs[ivs.length - 1];
  for (let i = 0; i < strikes.length - 1; i++) {
    if (strike >= strikes[i] && strike <= strikes[i + 1]) {
      const t = (strike - strikes[i]) / (strikes[i + 1] - strikes[i]);
      return ivs[i] + t * (ivs[i + 1] - ivs[i]);
    }
  }
  return ivs[ivs.length - 1];
}

function getSmileForExpiry(expiryCode) {
  if (!volSurface || !volSurface.smiles) return null;
  return volSurface.smiles.find(s => s.expiry_code === expiryCode) || null;
}

// Simple BS for the pricer (reusing the one from portfolio section causes ordering issues)
function pricerBs(S, K, T, r, sigma, type) {
  if (T <= 0) return type === "C" ? Math.max(S - K, 0) : Math.max(K - S, 0);
  const d1 = (Math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * Math.sqrt(T));
  const d2 = d1 - sigma * Math.sqrt(T);
  // normCdf defined later in file, use inline Horner approx
  function _ncdf(x) {
    const a1 = 0.254829592, a2 = -0.284496736, a3 = 1.421413741;
    const a4 = -1.453152027, a5 = 1.061405429, p = 0.3275911;
    const sign = x < 0 ? -1 : 1;
    x = Math.abs(x) / Math.SQRT2;
    const t = 1.0 / (1.0 + p * x);
    const y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * Math.exp(-x * x);
    return 0.5 * (1.0 + sign * y);
  }
  if (type === "C") return S * _ncdf(d1) - K * Math.exp(-r * T) * _ncdf(d2);
  return K * Math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1);
}

// ─── Bootstrap ──────────────────────────────────────────────
async function init() {
  // Fetch spot + vol surface in parallel
  const spotP = get("/api/market/spot").catch(e => { console.error("Spot fetch failed:", e); return null; });
  const volP = get("/api/market/vol-surface").catch(e => { console.error("Vol surface fetch failed:", e); return null; });
  const expP = get("/api/market/expirations").catch(e => { console.error("Expiry fetch failed:", e); return null; });

  const [spotData, volData, expiries] = await Promise.all([spotP, volP, expP]);

  // Spot
  if (spotData) {
    ethSpot = spotData.eth_spot;
    $spot.textContent = `$${ethSpot.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
    $spotMin.value = Math.round(ethSpot * 0.4);
    $spotMax.value = Math.round(ethSpot * 2.0);
  } else {
    $spot.textContent = "Error";
  }

  // Vol surface cache
  if (volData) {
    volSurface = volData;
    console.log(`Vol surface loaded: ${volData.smiles.length} expiries`);
  }

  // Expirations dropdown — prefer vol surface expiries (only ones with smile data)
  if (volData && volData.smiles.length > 0) {
    $expSel.innerHTML = "";
    for (const s of volData.smiles) {
      const opt = document.createElement("option");
      opt.value = s.expiry_code;
      opt.textContent = `${s.expiry_code} (${s.dte}d)`;
      $expSel.appendChild(opt);
    }
    $btnLoad.disabled = false;
  } else if (expiries) {
    $expSel.innerHTML = "";
    for (const exp of expiries) {
      const opt = document.createElement("option");
      opt.value = exp;
      opt.textContent = exp;
      $expSel.appendChild(opt);
    }
    $btnLoad.disabled = false;
  } else {
    $expSel.innerHTML = '<option value="">Failed to load</option>';
  }

  renderLegs();
  drawEmptyChart();
}

// ─── Payoff chart ───────────────────────────────────────────
function drawEmptyChart() {
  const layout = chartLayout();
  Plotly.newPlot("payoff-chart", [], layout, { responsive: true });
}

function chartLayout() {
  return {
    title: { text: "Strategy Payoff at Expiry", font: { color: "#e6edf3", size: 16 } },
    paper_bgcolor: "#161b22",
    plot_bgcolor:  "#0d1117",
    xaxis: {
      title: "ETH Spot Price (USD)",
      color: "#8b949e",
      gridcolor: "#21262d",
      zerolinecolor: "#30363d",
    },
    yaxis: {
      title: "P&L (USD)",
      color: "#8b949e",
      gridcolor: "#21262d",
      zerolinecolor: "#f85149",
      zerolinewidth: 2,
    },
    margin: { t: 50, r: 30, b: 50, l: 60 },
    showlegend: true,
    legend: { font: { color: "#8b949e" } },
  };
}

async function computePayoff() {
  if (legs.length === 0) return;
  $btnCompute.classList.add("loading");

  const apiLegs = legs.map(l => ({
    strike:  parseFloat(l.strike),
    type:    l.type,
    premium: parseFloat(l.premium),
    quantity: parseFloat(l.quantity),
    is_long: l.side === "buy",
  }));

  try {
    const data = await post("/api/pricing/payoff", {
      spot_min: parseFloat($spotMin.value),
      spot_max: parseFloat($spotMax.value),
      legs: apiLegs,
      num_points: 500,
    });

    const traces = [
      {
        x: data.spots,
        y: data.pnl,
        type: "scatter",
        mode: "lines",
        name: "Total P&L",
        line: { color: "#58a6ff", width: 2.5 },
        fill: "tozeroy",
        fillcolor: "rgba(88,166,255,0.08)",
      },
    ];

    const uniqueStrikes = [...new Set(legs.map(l => parseFloat(l.strike)))];
    for (const k of uniqueStrikes) {
      traces.push({
        x: [k, k],
        y: [Math.min(...data.pnl), Math.max(...data.pnl)],
        type: "scatter",
        mode: "lines",
        name: `K=${k}`,
        line: { color: "#d29922", width: 1, dash: "dot" },
        showlegend: false,
      });
    }

    if (ethSpot) {
      traces.push({
        x: [ethSpot, ethSpot],
        y: [Math.min(...data.pnl), Math.max(...data.pnl)],
        type: "scatter",
        mode: "lines",
        name: `Spot $${ethSpot.toFixed(0)}`,
        line: { color: "#3fb950", width: 1.5, dash: "dash" },
      });
    }

    Plotly.react("payoff-chart", traces, chartLayout(), { responsive: true });
  } catch (e) {
    console.error("Payoff compute failed:", e);
    alert("Failed to compute payoff — check console.");
  } finally {
    $btnCompute.classList.remove("loading");
  }
}

// ─── Legs management ────────────────────────────────────────
function getDefaultExpiry() {
  return $expSel.value || (volSurface && volSurface.smiles.length > 0 ? volSurface.smiles[0].expiry_code : "");
}

function addLeg(side = "buy", type = "C", strike = "", premium = "0", quantity = "1", expiry = null) {
  legs.push({ side, type, strike: String(strike), premium: String(premium), quantity: String(quantity), expiry: expiry || getDefaultExpiry() });
  renderLegs();
}

function removeLeg(idx) {
  legs.splice(idx, 1);
  renderLegs();
}

function renderLegs() {
  $legsBody.innerHTML = "";
  const expiryOptions = volSurface && volSurface.smiles
    ? volSurface.smiles.map(s => s.expiry_code)
    : [];

  legs.forEach((leg, i) => {
    const expOpts = expiryOptions.map(e =>
      `<option value="${e}" ${leg.expiry === e ? "selected" : ""}>${e}</option>`
    ).join("");

    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>
        <select data-i="${i}" data-field="expiry" style="font-size:.72rem">${expOpts}</select>
      </td>
      <td class="side-type-cell">
        <select data-i="${i}" data-field="side" style="width:48%">
          <option value="buy"  ${leg.side === "buy"  ? "selected" : ""}>Buy</option>
          <option value="sell" ${leg.side === "sell" ? "selected" : ""}>Sell</option>
        </select>
        <select data-i="${i}" data-field="type" style="width:48%">
          <option value="C" ${leg.type === "C" ? "selected" : ""}>Call</option>
          <option value="P" ${leg.type === "P" ? "selected" : ""}>Put</option>
        </select>
      </td>
      <td><input type="number" data-i="${i}" data-field="strike" value="${leg.strike}" step="10"></td>
      <td><input type="number" data-i="${i}" data-field="quantity" value="${leg.quantity}" step="1" min="1"></td>
      <td><button class="btn-remove" data-i="${i}">✕</button></td>
    `;
    $legsBody.appendChild(tr);
  });

  $legsBody.querySelectorAll("select, input").forEach(el => {
    el.addEventListener("change", () => {
      const idx = parseInt(el.dataset.i);
      legs[idx][el.dataset.field] = el.value;
    });
  });

  $legsBody.querySelectorAll(".btn-remove").forEach(btn => {
    btn.addEventListener("click", () => removeLeg(parseInt(btn.dataset.i)));
  });
}

// ─── Strategy templates ─────────────────────────────────────
function applyTemplate(name) {
  const s = ethSpot ? Math.round(ethSpot / 100) * 100 : 2800;
  legs = [];

  switch (name) {
    case "long_call":
      addLeg("buy", "C", s, "0");
      break;
    case "long_put":
      addLeg("buy", "P", s, "0");
      break;
    case "bull_call_spread":
      addLeg("buy",  "C", s,       "0");
      addLeg("sell", "C", s + 500,  "0");
      break;
    case "bear_put_spread":
      addLeg("buy",  "P", s,       "0");
      addLeg("sell", "P", s - 500,  "0");
      break;
    case "straddle":
      addLeg("buy", "C", s, "0");
      addLeg("buy", "P", s, "0");
      break;
    case "strangle":
      addLeg("buy", "C", s + 300, "0");
      addLeg("buy", "P", s - 300, "0");
      break;
    case "iron_condor":
      addLeg("buy",  "P", s - 600, "0");
      addLeg("sell", "P", s - 200, "0");
      addLeg("sell", "C", s + 200, "0");
      addLeg("buy",  "C", s + 600, "0");
      break;
  }
  renderLegs();

  document.querySelectorAll(".btn-template").forEach(b => b.classList.remove("active"));
  const active = document.querySelector(`.btn-template[data-strategy="${name}"]`);
  if (active) active.classList.add("active");
}

// ─── Option chain loading ───────────────────────────────────
async function loadChain() {
  const expiry = $expSel.value;
  if (!expiry) return;

  $btnLoad.disabled = true;
  $btnLoad.textContent = "Loading…";

  try {
    optionChain = await get(`/api/market/options?expiry=${expiry}`);

    optionChain.sort((a, b) => {
      const sa = parseFloat(a.instrument_name.split("-")[2]);
      const sb = parseFloat(b.instrument_name.split("-")[2]);
      if (sa !== sb) return sa - sb;
      return a.instrument_name < b.instrument_name ? -1 : 1;
    });

    $chainExp.textContent = expiry;
    $chainBody.innerHTML = "";

    for (const opt of optionChain) {
      const parts = opt.instrument_name.split("-");
      const strike = parts[2];
      const optType = parts[3];
      const isCall = optType === "C";

      const tr = document.createElement("tr");
      tr.className = isCall ? "call-row" : "put-row";
      tr.innerHTML = `
        <td style="text-align:left; font-family:monospace; font-size:.72rem">${opt.instrument_name}</td>
        <td style="text-align:center; color:${isCall ? 'var(--green)' : 'var(--red)'}">${optType}</td>
        <td>${strike}</td>
        <td>${opt.mark_price != null ? opt.mark_price.toFixed(4) : "—"}</td>
        <td>${opt.mark_iv != null ? opt.mark_iv.toFixed(1) : "—"}</td>
        <td>${opt.delta != null ? opt.delta.toFixed(3) : "—"}</td>
        <td>${opt.gamma != null ? opt.gamma.toFixed(5) : "—"}</td>
        <td>${opt.theta != null ? opt.theta.toFixed(4) : "—"}</td>
        <td>${opt.vega != null ? opt.vega.toFixed(4) : "—"}</td>
        <td>${opt.best_bid != null ? opt.best_bid.toFixed(4) : "—"}</td>
        <td>${opt.best_ask != null ? opt.best_ask.toFixed(4) : "—"}</td>
        <td><button class="btn-add-chain" data-name="${opt.instrument_name}">+ Add</button></td>
      `;
      $chainBody.appendChild(tr);
    }

    $chainBody.querySelectorAll(".btn-add-chain").forEach(btn => {
      btn.addEventListener("click", () => addFromChain(btn.dataset.name));
    });

    $chainSec.style.display = "block";
  } catch (e) {
    console.error("Chain load failed:", e);
    alert("Failed to load option chain. See console.");
  } finally {
    $btnLoad.disabled = false;
    $btnLoad.textContent = "Load Option Chain";
  }
}

function addFromChain(instrumentName) {
  const opt = optionChain.find(o => o.instrument_name === instrumentName);
  if (!opt) return;

  const parts = instrumentName.split("-");
  const chainExpiry = parts[1];  // e.g. "27JUN25"
  const strike = parts[2];
  const type = parts[3];
  const premiumEth = opt.mark_price || 0;
  const premiumUsd = ethSpot ? (premiumEth * ethSpot).toFixed(2) : premiumEth.toFixed(4);

  addLeg("buy", type, strike, premiumUsd, "1", chainExpiry);
}

// ─── Deribit Replication Pricing (client-side, uses cached vol surface) ──
let lastReplPremiums = [];

function replicateStrategy() {
  if (legs.length === 0) { alert("Add at least one leg."); return; }
  for (const l of legs) {
    const s = parseFloat(l.strike);
    if (isNaN(s) || s <= 0) { alert("Each leg must have a valid strike price."); return; }
    if (!l.expiry) { alert("Each leg must have an expiry selected."); return; }
  }
  if (!volSurface) { alert("Vol surface not loaded yet — wait for page init."); return; }

  // Validate all expiries have smile data
  const uniqueExpiries = [...new Set(legs.map(l => l.expiry))];
  for (const exp of uniqueExpiries) {
    if (!getSmileForExpiry(exp)) { alert(`No vol smile data for expiry ${exp}.`); return; }
  }

  const spot = ethSpot;
  const spotMin = parseFloat($spotMin.value);
  const spotMax = parseFloat($spotMax.value);
  const nPts = 500;
  const spots = [];
  for (let i = 0; i < nPts; i++) spots.push(spotMin + (spotMax - spotMin) * i / (nPts - 1));

  // P&L at all-expired (every leg at intrinsic)
  const pnlAllExpired = new Array(nPts).fill(0);
  // P&L now (BS value with each leg's own T)
  const pnlNow = new Array(nPts).fill(0);
  // P&L at first expiry (near-term legs at intrinsic, far legs at BS with remaining T)
  const sortedExpiries = uniqueExpiries.map(e => ({ code: e, dte: getSmileForExpiry(e).dte })).sort((a, b) => a.dte - b.dte);
  const firstDte = sortedExpiries[0].dte;
  const lastDte = sortedExpiries[sortedExpiries.length - 1].dte;
  const pnlFirstExpiry = new Array(nPts).fill(0);

  const legDetails = [];

  for (const l of legs) {
    const K = parseFloat(l.strike);
    const qty = parseFloat(l.quantity);
    const dir = l.side === "buy" ? 1 : -1;
    const smile = getSmileForExpiry(l.expiry);
    const dte = smile.dte;
    const T = Math.max(dte, 0) / 365.25;
    const ivPct = lookupSmileIv(l.expiry, K);
    if (ivPct == null) { alert(`Cannot interpolate IV for strike ${K} on ${l.expiry}.`); return; }
    const sigma = ivPct / 100;

    const prem = pricerBs(spot, K, T, 0, sigma, l.type);
    const premEth = spot > 0 ? prem / spot : 0;

    for (let i = 0; i < nPts; i++) {
      const intrinsic = l.type === "C" ? Math.max(spots[i] - K, 0) : Math.max(K - spots[i], 0);

      // All expired
      pnlAllExpired[i] += dir * qty * (intrinsic - prem);

      // Now (full BS value)
      const valNow = pricerBs(spots[i], K, T, 0, sigma, l.type);
      pnlNow[i] += dir * qty * (valNow - prem);

      // At first expiry: legs expiring then → intrinsic, others → BS with remaining time
      const remainingDte = dte - firstDte;
      if (remainingDte <= 0) {
        pnlFirstExpiry[i] += dir * qty * (intrinsic - prem);
      } else {
        const Tremain = remainingDte / 365.25;
        const valRemain = pricerBs(spots[i], K, Tremain, 0, sigma, l.type);
        pnlFirstExpiry[i] += dir * qty * (valRemain - prem);
      }
    }

    legDetails.push({
      expiry: l.expiry, dte, strike: K, type: l.type, side: l.side, quantity: qty,
      iv_pct: ivPct, sigma, bs_premium_usd: prem, bs_premium_eth: premEth,
    });
  }

  lastReplPremiums = legDetails.map(l => l.bs_premium_usd);

  // Total cost breakdown: pay (buys) vs receive (sells)
  let totalPay = 0, totalReceive = 0;
  for (const d of legDetails) {
    const amt = d.quantity * d.bs_premium_usd;
    if (d.side === "buy") totalPay += amt;
    else totalReceive += amt;
  }
  const totalCostUsd = totalPay - totalReceive;
  const totalCostEth = spot > 0 ? totalCostUsd / spot : 0;

  // Summary
  const expiryLabel = uniqueExpiries.length === 1
    ? uniqueExpiries[0]
    : uniqueExpiries.join(" / ");
  const fmtUsd = v => "$" + v.toLocaleString(undefined, {maximumFractionDigits: 0});
  const payColor = totalPay > 0 ? "var(--red)" : "var(--muted)";
  const rcvColor = totalReceive > 0 ? "var(--green)" : "var(--muted)";
  const netColor = totalCostUsd > 0 ? "var(--red)" : "var(--green)";
  const $summary = document.getElementById("repl-summary");
  $summary.innerHTML =
    `<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.5rem">` +
    `<span>Expiry: <strong>${expiryLabel}</strong> &nbsp;|&nbsp; ETH = $${spot.toLocaleString()}</span>` +
    `<span style="display:flex;gap:1rem;font-size:.95rem">` +
    `<span>Pay: <strong style="color:${payColor}">${fmtUsd(totalPay)}</strong></span>` +
    `<span>Receive: <strong style="color:${rcvColor}">${fmtUsd(totalReceive)}</strong></span>` +
    `<span>Net: <strong style="color:${netColor}">${fmtUsd(totalCostUsd)}</strong> (${totalCostEth.toFixed(4)} ETH)</span>` +
    `</span></div>`;

  // Per-leg table
  const $body = document.getElementById("repl-body");
  $body.innerHTML = "";
  for (const d of legDetails) {
    const tr = document.createElement("tr");
    const sideColor = d.side === "buy" ? "var(--accent)" : "var(--orange)";
    const typeColor = d.type === "C" ? "var(--green)" : "var(--red)";
    tr.innerHTML = `
      <td style="font-size:.75rem">${d.expiry}</td>
      <td style="color:${sideColor}">${d.side.toUpperCase()}</td>
      <td style="color:${typeColor}">${d.type === "C" ? "Call" : "Put"}</td>
      <td>${d.strike}</td>
      <td>${d.dte}d</td>
      <td>${d.iv_pct.toFixed(1)}%</td>
      <td style="font-family:monospace">${d.bs_premium_eth.toFixed(6)}</td>
      <td style="font-family:monospace">$${d.bs_premium_usd.toFixed(2)}</td>
    `;
    $body.appendChild(tr);
  }
  document.getElementById("repl-results-section").style.display = "block";

  // Payoff chart
  const allPnl = [...pnlAllExpired, ...pnlNow, ...pnlFirstExpiry];
  const yMin = Math.min(...allPnl);
  const yMax = Math.max(...allPnl);

  const traces = [];

  // If multi-expiry, show first expiry curve
  const isMultiExpiry = uniqueExpiries.length > 1;
  if (isMultiExpiry) {
    traces.push({ x: spots, y: pnlFirstExpiry, type: "scatter", mode: "lines",
      name: `P&L at 1st Expiry (${sortedExpiries[0].code}, ${firstDte}d)`,
      line: { color: "#bc8cff", width: 2, dash: "dash" } });
  }

  traces.push({ x: spots, y: pnlAllExpired, type: "scatter", mode: "lines",
    name: isMultiExpiry ? `P&L All Expired (${sortedExpiries[sortedExpiries.length-1].code})` : "P&L at Expiry",
    line: { color: "#58a6ff", width: 2.5 } });

  traces.push({ x: spots, y: pnlNow, type: "scatter", mode: "lines",
    name: "P&L Now", line: { color: "#d29922", width: 2, dash: "dash" } });

  const uniqueStrikes = [...new Set(legDetails.map(l => l.strike))];
  for (const k of uniqueStrikes) {
    traces.push({ x: [k, k], y: [yMin, yMax], type: "scatter", mode: "lines",
      name: `K=${k}`, showlegend: false, line: { color: "#8b949e", width: 1, dash: "dot" } });
  }

  traces.push({ x: [spot, spot], y: [yMin, yMax], type: "scatter", mode: "lines",
    name: `Spot $${spot.toFixed(0)}`, line: { color: "#3fb950", width: 1.5, dash: "dash" } });

  Plotly.react("payoff-chart", traces, chartLayout(), { responsive: true });

  // Vol smile chart — show the first expiry's smile with all legs marked
  const primarySmile = getSmileForExpiry(sortedExpiries[0].code);
  drawSmileChart(primarySmile, legDetails);
}

function drawSmileChart(smile, pricedLegs) {
  const $el = document.getElementById("smile-chart");
  $el.style.display = "block";

  // smile can be either {smile_strikes, smile_ivs, observed_strikes, observed_ivs} (old backend format)
  // or {strikes, ivs, expiry_code, dte} (new cached format)
  const obsStrikes = smile.observed_strikes || smile.strikes || [];
  const obsIvs = smile.observed_ivs || smile.ivs || [];

  // Generate a dense interpolated curve from the observed points
  let interpStrikes = obsStrikes;
  let interpIvs = obsIvs;
  if (smile.smile_strikes) {
    interpStrikes = smile.smile_strikes;
    interpIvs = smile.smile_ivs;
  } else if (obsStrikes.length >= 2) {
    // Linearly interpolate a dense curve
    const lo = obsStrikes[0] * 0.85;
    const hi = obsStrikes[obsStrikes.length - 1] * 1.15;
    interpStrikes = [];
    interpIvs = [];
    for (let k = lo; k <= hi; k += (hi - lo) / 200) {
      interpStrikes.push(k);
      interpIvs.push(lookupSmileIv(smile.expiry_code, k) ?? obsIvs[0]);
    }
  }

  const traces = [
    {
      x: interpStrikes, y: interpIvs,
      type: "scatter", mode: "lines",
      name: "Vol Smile (interpolated)",
      line: { color: "#58a6ff", width: 2 },
    },
    {
      x: obsStrikes, y: obsIvs,
      type: "scatter", mode: "markers",
      name: "Deribit Market IV",
      marker: { color: "#e6edf3", size: 5, symbol: "circle", opacity: 0.6 },
    },
  ];

  for (const leg of pricedLegs) {
    traces.push({
      x: [leg.strike], y: [leg.iv_pct],
      type: "scatter", mode: "markers+text",
      name: `${leg.side.toUpperCase()} ${leg.type} ${leg.strike}`,
      marker: {
        color: leg.type === "C" ? "#3fb950" : "#f85149",
        size: 12,
        symbol: leg.side === "buy" ? "triangle-up" : "triangle-down",
        line: { color: "#fff", width: 1 },
      },
      text: [`${leg.iv_pct.toFixed(1)}%`],
      textposition: "top center",
      textfont: { color: "#e6edf3", size: 10 },
    });
  }

  const title = smile.expiry_code
    ? `Deribit IV Smile — ${smile.expiry_code} (${smile.dte}d)`
    : "Deribit Implied Volatility Smile";

  const layout = {
    title: { text: title, font: { color: "#e6edf3", size: 16 } },
    paper_bgcolor: "#161b22",
    plot_bgcolor: "#0d1117",
    xaxis: { title: "Strike (USD)", color: "#8b949e", gridcolor: "#21262d" },
    yaxis: { title: "Implied Volatility (%)", color: "#8b949e", gridcolor: "#21262d" },
    margin: { t: 50, r: 30, b: 50, l: 60 },
    showlegend: true,
    legend: { font: { color: "#8b949e" } },
  };

  Plotly.react("smile-chart", traces, layout, { responsive: true });
}

function applyReplPremiums() {
  if (lastReplPremiums.length !== legs.length) return;
  for (let i = 0; i < legs.length; i++) {
    legs[i].premium = lastReplPremiums[i].toFixed(2);
  }
  renderLegs();
  document.getElementById("repl-results-section").style.display = "none";
}

// ─── Event binding ──────────────────────────────────────────
$btnAdd.addEventListener("click", () => addLeg());
$btnCompute.addEventListener("click", computePayoff);
$btnRepl.addEventListener("click", replicateStrategy);
document.getElementById("btn-apply-repl").addEventListener("click", applyReplPremiums);
$btnLoad.addEventListener("click", loadChain);

document.querySelectorAll(".btn-template").forEach(btn => {
  btn.addEventListener("click", () => applyTemplate(btn.dataset.strategy));
});

[$spotMin, $spotMax].forEach(el => {
  el.addEventListener("keydown", e => { if (e.key === "Enter") computePayoff(); });
});

// ─── Tab switching ──────────────────────────────────────────
document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    // Deactivate all
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-pane").forEach(p => p.classList.remove("active"));

    // Activate selected
    btn.classList.add("active");
    const pane = document.getElementById(`tab-${btn.dataset.tab}`);
    pane.classList.add("active");

    // Full-width layout for portfolio and positions tabs
    const $main = document.querySelector("main");
    const fullWidthTabs = ["portfolio", "positions", "roll"];
    if (fullWidthTabs.includes(btn.dataset.tab)) {
      $main.classList.add("portfolio-active");
    } else {
      $main.classList.remove("portfolio-active");
    }

    // Lazy-load positions on first visit
    if (btn.dataset.tab === "positions" && !positionsLoaded) {
      loadPositions();
    }

    // Lazy-load portfolio on first visit
    if (btn.dataset.tab === "portfolio" && !portfolioLoaded) {
      loadPortfolio();
    }

    // Lazy-load roll analysis on first visit
    if (btn.dataset.tab === "roll" && !rollLoaded) {
      rollInit();
    }
  });
});

// ─── Positions / Risk ───────────────────────────────────────
let positionsLoaded = false;

async function loadPositions() {
  try {
    const [trades, summary] = await Promise.all([
      get("/api/positions/trades"),
      get("/api/positions/summary"),
    ]);

    const t = summary.totals;

    // Totals banner
    document.getElementById("tot-positions").textContent = t.positions_count;
    document.getElementById("tot-trades").textContent    = t.trades_count;
    document.getElementById("tot-long").textContent      = t.long_count;
    document.getElementById("tot-short").textContent     = t.short_count;
    document.getElementById("tot-net-qty").textContent   = t.total_net_qty.toLocaleString(undefined, { maximumFractionDigits: 2 });
    document.getElementById("tot-premium").textContent   = `$${t.total_premium_usd.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
    document.getElementById("tot-notional").textContent  = `$${t.total_notional_mm.toFixed(2)}mm`;

    // Positions table
    const $posBody = document.getElementById("positions-body");
    $posBody.innerHTML = "";

    for (const p of summary.positions) {
      const sideClass = p.side === "Long" ? "qty-long" : p.side === "Short" ? "qty-short" : "qty-flat";
      const typeColor = (p.option_type || "").toUpperCase().includes("CALL") ? "var(--green)" : "var(--red)";
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td style="text-align:left" class="${sideClass}"><strong>${p.side}</strong></td>
        <td style="text-align:left; color:${typeColor}">${p.option_type}</td>
        <td>${p.strike.toLocaleString()}</td>
        <td>${p.expiry}</td>
        <td>${p.days_remaining.toFixed(0)}</td>
        <td>${(p.pct_otm * 100).toFixed(1)}%</td>
        <td class="${sideClass}" style="font-family:monospace">${p.net_qty.toLocaleString(undefined, { maximumFractionDigits: 2 })}</td>
        <td style="font-family:monospace">$${p.avg_premium_per_contract.toFixed(2)}</td>
        <td style="font-family:monospace">$${p.total_premium_usd.toLocaleString(undefined, { maximumFractionDigits: 0 })}</td>
        <td style="font-family:monospace">$${p.total_notional_mm.toFixed(2)}mm</td>
        <td>${p.trade_count}</td>
        <td style="text-align:left; font-size:.7rem">${p.counterparties.join(", ")}</td>
      `;
      $posBody.appendChild(tr);
    }

    // Exposure chart
    drawExposureChart(summary.positions);

    // Raw trades table
    document.getElementById("trades-count").textContent = `(${trades.length} trades)`;
    if (trades.length > 0) {
      const displayCols = [
        "Counterparty", "Initial Trade Date", "Buy / Sell / Unwind",
        "Option Type", "Option Expiry Date", "Days Remaining to Expiry",
        "Strike", "Ref. Spot Price", "% OTM", "ETH Options",
        "$ Notional (mm)", "Premium per Contract", "Premium USD",
      ];
      const keys = displayCols.filter(k => k in trades[0]);

      const $thead = document.getElementById("trades-thead");
      $thead.innerHTML = "<tr>" + keys.map(k => `<th>${k}</th>`).join("") + "</tr>";

      const $tbody = document.getElementById("trades-body");
      $tbody.innerHTML = "";
      for (const row of trades) {
        const tr = document.createElement("tr");
        tr.innerHTML = keys.map(k => {
          let v = row[k];
          if (v == null) return "<td>—</td>";
          if (typeof v === "number") {
            if (Math.abs(v) >= 1000) v = v.toLocaleString(undefined, { maximumFractionDigits: 2 });
            else v = v.toFixed(4);
          }
          return `<td>${v}</td>`;
        }).join("");
        $tbody.appendChild(tr);
      }
    }

    positionsLoaded = true;
  } catch (e) {
    console.error("Failed to load positions:", e);
    alert("Failed to load positions — check console.\n" + e.message);
  }
}

function drawExposureChart(positions) {
  const calls = positions.filter(p => (p.option_type || "").toUpperCase().includes("CALL"));
  const puts  = positions.filter(p => (p.option_type || "").toUpperCase().includes("PUT"));

  const traces = [];

  if (calls.length) {
    traces.push({
      x: calls.map(p => `${p.strike}`),
      y: calls.map(p => p.net_qty),
      type: "bar",
      name: "Calls",
      marker: { color: "#3fb950" },
      text: calls.map(p => p.expiry),
      hovertemplate: "Strike %{x}<br>Qty: %{y}<br>Expiry: %{text}<extra>Call</extra>",
    });
  }

  if (puts.length) {
    traces.push({
      x: puts.map(p => `${p.strike}`),
      y: puts.map(p => p.net_qty),
      type: "bar",
      name: "Puts",
      marker: { color: "#f85149" },
      text: puts.map(p => p.expiry),
      hovertemplate: "Strike %{x}<br>Qty: %{y}<br>Expiry: %{text}<extra>Put</extra>",
    });
  }

  const layout = {
    title: { text: "Net Exposure by Strike", font: { color: "#e6edf3", size: 16 } },
    paper_bgcolor: "#161b22",
    plot_bgcolor: "#0d1117",
    barmode: "group",
    xaxis: {
      title: "Strike (USD)",
      color: "#8b949e",
      gridcolor: "#21262d",
      type: "category",
    },
    yaxis: {
      title: "Net Quantity (ETH Options)",
      color: "#8b949e",
      gridcolor: "#21262d",
      zerolinecolor: "#f85149",
      zerolinewidth: 2,
    },
    margin: { t: 50, r: 30, b: 60, l: 60 },
    showlegend: true,
    legend: { font: { color: "#8b949e" } },
  };

  Plotly.newPlot("exposure-chart", traces, layout, { responsive: true });
}

// ─── Portfolio P&L (interactive) ───────────────────────────
let portfolioLoaded = false;
let pfData = null;              // raw API response
let pfSelected = new Set();     // selected position indices
let pfRolled = new Map();       // idx → { newDte }
let pfSortCol = "expiry";
let pfSortAsc = true;

// ── JS Black-Scholes ──────────────────────────────────────
function normCdf(x) {
  const a1 = 0.254829592, a2 = -0.284496736, a3 = 1.421413741;
  const a4 = -1.453152027, a5 = 1.061405429, p = 0.3275911;
  const sign = x < 0 ? -1 : 1;
  x = Math.abs(x) / Math.SQRT2;
  const t = 1.0 / (1.0 + p * x);
  const y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * Math.exp(-x * x);
  return 0.5 * (1.0 + sign * y);
}

function bsPrice(S, K, T, r, sigma, type) {
  if (T <= 0) return type === "C" ? Math.max(S - K, 0) : Math.max(K - S, 0);
  const d1 = (Math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * Math.sqrt(T));
  const d2 = d1 - sigma * Math.sqrt(T);
  if (type === "C") return S * normCdf(d1) - K * Math.exp(-r * T) * normCdf(d2);
  return K * Math.exp(-r * T) * normCdf(-d2) - S * normCdf(-d1);
}

function bsVec(spots, K, T, r, sigma, type) {
  return spots.map(S => bsPrice(S, K, T, r, sigma, type));
}

// ── Vol surface lookup for rolls ─────────────────────────
// Given a target DTE and strike, find the closest Deribit expiry smile
// and interpolate IV at that strike. Returns IV in % (e.g. 80.0).
function pfLookupIv(targetDte, strike) {
  if (!pfData || !pfData.vol_surface || pfData.vol_surface.length === 0) return null;
  const surface = pfData.vol_surface;

  // Find the closest expiry by DTE
  let best = surface[0];
  let bestDiff = Math.abs(best.dte - targetDte);
  for (const entry of surface) {
    const diff = Math.abs(entry.dte - targetDte);
    if (diff < bestDiff) { bestDiff = diff; best = entry; }
  }

  // Interpolate IV at the given strike using the smile's strikes/ivs arrays
  const strikes = best.strikes;
  const ivs = best.ivs;
  if (!strikes || strikes.length === 0) return null;
  if (strike <= strikes[0]) return ivs[0];
  if (strike >= strikes[strikes.length - 1]) return ivs[ivs.length - 1];

  // Linear interpolation between the two bracketing strikes
  for (let i = 0; i < strikes.length - 1; i++) {
    if (strike >= strikes[i] && strike <= strikes[i + 1]) {
      const t = (strike - strikes[i]) / (strikes[i + 1] - strikes[i]);
      return ivs[i] + t * (ivs[i + 1] - ivs[i]);
    }
  }
  return ivs[ivs.length - 1];
}

// ── Load ──────────────────────────────────────────────────
async function loadPortfolio() {
  const $btn = document.getElementById("btn-refresh-portfolio");
  $btn.classList.add("loading");
  $btn.textContent = "Loading…";

  try {
    pfData = await get("/api/portfolio/pnl");
    pfSelected = new Set(pfData.positions.map(p => p.id));
    pfRolled = new Map();

    // Populate expiry filter
    const expiries = [...new Set(pfData.positions.map(p => p.expiry.split("T")[0]))].sort();
    const $expF = document.getElementById("pf-filter-expiry");
    $expF.innerHTML = '<option value="">All</option>';
    expiries.forEach(e => {
      const opt = document.createElement("option");
      opt.value = e; opt.textContent = e;
      $expF.appendChild(opt);
    });

    pfRenderAll();
    portfolioLoaded = true;
  } catch (e) {
    console.error("Failed to load portfolio:", e);
    alert("Failed to load portfolio P&L — check console.\n" + e.message);
  } finally {
    $btn.classList.remove("loading");
    $btn.textContent = "Refresh";
  }
}

// ── Render all ────────────────────────────────────────────
function pfRenderAll() {
  if (!pfData) return;
  pfRenderSummary();
  pfRenderTable();
  pfRenderPayoffChart();
  pfRenderMtmGrid();
}

// ── Summary banner ────────────────────────────────────────
function pfRenderSummary() {
  const all = pfData.positions;
  const sel = all.filter(p => pfSelected.has(p.id));

  document.getElementById("pf-spot").textContent =
    `$${pfData.eth_spot.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  document.getElementById("pf-count").textContent = all.length;
  document.getElementById("pf-selected-count").textContent = sel.length;

  const baselineMtm = all.reduce((s, p) => s + p.current_mtm, 0);
  const scenarioMtm = pfCalcScenarioMtm();
  const delta = scenarioMtm - baselineMtm;

  document.getElementById("pf-entry-prem").textContent =
    `$${sel.reduce((s, p) => s + Math.abs(p.premium_usd), 0).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;

  const mtmTotal = sel.reduce((s, p) => s + (p.current_mtm || 0), 0);
  const $remPrem = document.getElementById("pf-remaining-prem");
  $remPrem.textContent = `$${mtmTotal.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  $remPrem.className = `risk-value ${mtmTotal >= 0 ? "mtm-pos" : "mtm-neg"}`;

  const $base = document.getElementById("pf-baseline-mtm");
  $base.textContent = `$${baselineMtm.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  $base.className = `risk-value ${baselineMtm >= 0 ? "mtm-pos" : "mtm-neg"}`;

  const $scen = document.getElementById("pf-scenario-mtm");
  $scen.textContent = `$${scenarioMtm.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  $scen.className = `risk-value ${scenarioMtm >= 0 ? "mtm-pos" : "mtm-neg"}`;

  const $delta = document.getElementById("pf-delta-mtm");
  $delta.textContent = `${delta >= 0 ? "+" : ""}$${delta.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  $delta.className = `risk-value ${delta >= 0 ? "mtm-pos" : "mtm-neg"}`;

  // Portfolio greeks totals
  if (pfData.totals) {
    const t = pfData.totals;
    document.getElementById("pf-total-delta").textContent = t.portfolio_delta != null ? t.portfolio_delta.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "--";
    document.getElementById("pf-total-gamma").textContent = t.portfolio_gamma != null ? t.portfolio_gamma.toFixed(4) : "--";
    document.getElementById("pf-total-theta").textContent = t.portfolio_theta != null ? t.portfolio_theta.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "--";
    document.getElementById("pf-total-vega").textContent = t.portfolio_vega != null ? t.portfolio_vega.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "--";
  }
}

function pfCalcScenarioMtm() {
  let total = 0;
  for (const p of pfData.positions) {
    if (!pfSelected.has(p.id)) continue;
    if (pfRolled.has(p.id)) {
      const newDte = pfRolled.get(p.id).newDte;
      const T = Math.max(newDte, 0) / 365.25;
      const rollIvPct = pfLookupIv(newDte, p.strike) ?? p.iv_pct;
      const sigma = rollIvPct / 100;
      const val = bsPrice(pfData.eth_spot, p.strike, T, 0, sigma, p.opt);
      total += p.net_qty * val;
    } else {
      total += p.current_mtm;
    }
  }
  return total;
}

// ── Position table ────────────────────────────────────────
function pfGetFilteredPositions() {
  const fExpiry = document.getElementById("pf-filter-expiry").value;
  const fType = document.getElementById("pf-filter-type").value;
  const fSide = document.getElementById("pf-filter-side").value;

  let list = [...pfData.positions];
  if (fExpiry) list = list.filter(p => p.expiry.split("T")[0] === fExpiry);
  if (fType) list = list.filter(p => p.opt === fType);
  if (fSide) list = list.filter(p => p.side === fSide);

  // Sort
  if (pfSortCol) {
    list.sort((a, b) => {
      let va = a[pfSortCol], vb = b[pfSortCol];
      if (typeof va === "string") { va = va.toLowerCase(); vb = (vb || "").toLowerCase(); }
      if (va < vb) return pfSortAsc ? -1 : 1;
      if (va > vb) return pfSortAsc ? 1 : -1;
      return 0;
    });
  }
  return list;
}

function pfRenderTable() {
  const list = pfGetFilteredPositions();
  document.getElementById("pf-table-count").textContent = `(${list.length} shown)`;

  // Update sort indicators
  document.querySelectorAll("#pf-positions-table .sortable").forEach(th => {
    th.classList.remove("sort-asc", "sort-desc");
    if (th.dataset.col === pfSortCol) {
      th.classList.add(pfSortAsc ? "sort-asc" : "sort-desc");
    }
  });

  // Update header checkbox
  const allVisible = list.every(p => pfSelected.has(p.id));
  document.getElementById("pf-check-all").checked = allVisible && list.length > 0;

  const $tbody = document.getElementById("pf-positions-body");
  $tbody.innerHTML = "";

  for (const pos of list) {
    const checked = pfSelected.has(pos.id);
    const rolled = pfRolled.has(pos.id);
    const typeColor = pos.opt === "C" ? "var(--green)" : "var(--red)";
    const sideClass = pos.side === "Long" ? "qty-long" : pos.side === "Short" ? "qty-short" : "";
    const rowClass = (!checked ? "pf-row-excluded" : "") + (rolled ? " pf-row-rolled" : "");
    const rollDte = rolled ? pfRolled.get(pos.id).newDte : Math.round(pos.days_remaining + 90);
    const fmtNum = (v, d=0) => v != null ? v.toLocaleString(undefined, { maximumFractionDigits: d }) : "—";
    const otmClass = pos.pct_otm_live > 0 ? "mtm-neg" : pos.pct_otm_live < 0 ? "mtm-pos" : "";
    const fmtGreek = (v, d) => v != null ? v.toFixed(d) : "—";

    // Roll cost: close current + open new
    // = signed_qty * (current_mark - new_mark)
    // negative = pay, positive = receive
    let rollCostHtml = "—";
    let rollCurMarkHtml = "—";
    let rollNewMarkHtml = "—";
    let rollIvHtml = "—";
    if (rolled) {
      const newDte = pfRolled.get(pos.id).newDte;
      const T = Math.max(newDte, 0) / 365.25;
      const rollIvPct = pfLookupIv(newDte, pos.strike) ?? pos.iv_pct;
      const sigma = rollIvPct / 100;
      const newMark = bsPrice(pfData.eth_spot, pos.strike, T, 0, sigma, pos.opt);
      const curMark = pos.mark_price_usd ?? 0;
      const cost = pos.net_qty * (curMark - newMark);
      const rounded = Math.round(cost);
      const cls = rounded >= 0 ? "mtm-pos" : "mtm-neg";
      const label = rounded >= 0 ? "Receive" : "Pay";
      rollCostHtml = `<span class="${cls}">${label} $${Math.abs(rounded).toLocaleString()}</span>`;
      rollCurMarkHtml = `$${curMark.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
      rollNewMarkHtml = `$${newMark.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
      rollIvHtml = `${rollIvPct.toFixed(1)}%`;
    }

    const tr = document.createElement("tr");
    tr.className = rowClass;
    tr.innerHTML =
      `<td><input type="checkbox" class="pf-check" data-id="${pos.id}" ${checked ? "checked" : ""}></td>` +
      `<td style="text-align:left">${pos.counterparty}</td>` +
      `<td>${pos.trade_id ?? ""}</td>` +
      `<td>${pos.trade_date}</td>` +
      `<td style="text-align:left" class="${sideClass}">${pos.side_raw}</td>` +
      `<td style="text-align:left;color:${typeColor}">${pos.option_type}</td>` +
      `<td style="text-align:left;font-size:.8rem">${pos.instrument}</td>` +
      `<td>${pos.expiry}</td>` +
      `<td>${Math.round(pos.days_remaining)}</td>` +
      `<td style="font-family:monospace">${fmtNum(pos.strike)}</td>` +
      `<td class="${otmClass}">${pos.pct_otm_live}%</td>` +
      `<td style="font-family:monospace">${fmtNum(pos.qty)}</td>` +
      `<td style="font-family:monospace">${fmtNum(pos.premium_per, 2)}</td>` +
      `<td style="font-family:monospace">$${fmtNum(pos.premium_usd)}</td>` +
      `<td>${pos.iv_pct.toFixed(1)}%</td>` +
      `<td style="font-family:monospace">${fmtGreek(pos.delta, 3)}</td>` +
      `<td style="font-family:monospace">${fmtGreek(pos.gamma, 5)}</td>` +
      `<td style="font-family:monospace">${fmtGreek(pos.theta, 4)}</td>` +
      `<td style="font-family:monospace">${fmtGreek(pos.vega, 4)}</td>` +
      `<td class="${pos.current_mtm >= 0 ? 'mtm-pos' : 'mtm-neg'}" style="font-family:monospace">` +
        `$${fmtNum(pos.current_mtm)}</td>` +
      `<td style="text-align:center"><input type="checkbox" class="pf-roll" data-id="${pos.id}" ${rolled ? "checked" : ""}></td>` +
      `<td><input type="number" class="roll-dte-input" data-id="${pos.id}" value="${rollDte}" ` +
        `${rolled ? "" : "disabled"} min="1" step="1"></td>` +
      `<td style="font-family:monospace;white-space:nowrap">${rollCurMarkHtml}</td>` +
      `<td style="font-family:monospace;white-space:nowrap">${rollNewMarkHtml}</td>` +
      `<td style="font-family:monospace;white-space:nowrap">${rollIvHtml}</td>` +
      `<td style="font-family:monospace;white-space:nowrap">${rollCostHtml}</td>`;
    $tbody.appendChild(tr);
  }

  // Wire events
  $tbody.querySelectorAll(".pf-check").forEach(cb => {
    cb.addEventListener("change", () => {
      const id = parseInt(cb.dataset.id);
      if (cb.checked) pfSelected.add(id); else pfSelected.delete(id);
      pfOnSelectionChange();
    });
  });

  $tbody.querySelectorAll(".pf-roll").forEach(cb => {
    cb.addEventListener("change", () => {
      const id = parseInt(cb.dataset.id);
      const pos = pfData.positions.find(p => p.id === id);
      const dteInput = $tbody.querySelector(`.roll-dte-input[data-id="${id}"]`);
      if (cb.checked) {
        pfRolled.set(id, { newDte: parseInt(dteInput.value) || Math.round(pos.days_remaining + 90) });
        dteInput.disabled = false;
      } else {
        pfRolled.delete(id);
        dteInput.disabled = true;
      }
      // Full re-render to update roll cost column
      pfRenderTable();
      pfOnSelectionChange();
    });
  });

  $tbody.querySelectorAll(".roll-dte-input").forEach(inp => {
    inp.addEventListener("change", () => {
      const id = parseInt(inp.dataset.id);
      if (pfRolled.has(id)) {
        pfRolled.get(id).newDte = parseInt(inp.value) || 90;
        // Full re-render to update roll cost column
        pfRenderTable();
        pfOnSelectionChange();
      }
    });
  });
}

function pfOnSelectionChange() {
  pfRenderSummary();
  pfRenderPayoffChart();
  pfRenderMtmGrid();
  // Update row styling without full re-render
  document.querySelectorAll("#pf-positions-body tr").forEach(tr => {
    const cb = tr.querySelector(".pf-check");
    if (!cb) return;
    const id = parseInt(cb.dataset.id);
    tr.classList.toggle("pf-row-excluded", !pfSelected.has(id));
    tr.classList.toggle("pf-row-rolled", pfRolled.has(id));
  });
  // Sync header checkbox
  const visible = pfGetFilteredPositions();
  document.getElementById("pf-check-all").checked =
    visible.length > 0 && visible.every(p => pfSelected.has(p.id));
}

// ── Curve helpers ─────────────────────────────────────────
function pfSumCurves(positionIds, horizon) {
  const key = String(horizon);
  const spots = pfData.spot_ladder;
  const result = new Array(spots.length).fill(0);

  for (const p of pfData.positions) {
    if (!positionIds.has(p.id)) continue;

    if (pfRolled.has(p.id) && horizon !== "__baseline__") {
      // Rolled: recompute with new DTE using vol surface IV
      const newDte = pfRolled.get(p.id).newDte;
      const T = Math.max(newDte - horizon, 0) / 365.25;
      const rollIvPct = pfLookupIv(newDte, p.strike) ?? p.iv_pct;
      const sigma = rollIvPct / 100;
      for (let i = 0; i < spots.length; i++) {
        const val = bsPrice(spots[i], p.strike, T, 0, sigma, p.opt);
        result[i] += p.net_qty * val;
      }
    } else {
      // Use pre-computed curves
      const curve = p.payoff_by_horizon[key];
      if (curve) {
        for (let i = 0; i < spots.length; i++) result[i] += curve[i];
      }
    }
  }
  return result;
}

function pfBaselineCurve(horizon) {
  // All positions, no rolls, using pre-computed data
  const key = String(horizon);
  const spots = pfData.spot_ladder;
  const result = new Array(spots.length).fill(0);
  for (const p of pfData.positions) {
    const curve = p.payoff_by_horizon[key];
    if (curve) for (let i = 0; i < spots.length; i++) result[i] += curve[i];
  }
  return result;
}

// ── Payoff chart ──────────────────────────────────────────
function pfRenderPayoffChart() {
  const spots = pfData.spot_ladder;
  const horizons = pfData.chart_horizons;
  const hasScenarioChange = pfSelected.size !== pfData.positions.length || pfRolled.size > 0;

  const colors = ["#8b949e", "#f85149", "#d29922", "#3fb950", "#58a6ff", "#bc8cff"];
  const scenColors = ["#f85149", "#d29922", "#e3b341", "#3fb950", "#58a6ff", "#bc8cff"];
  const traces = [];

  // Baseline curves (dashed gray if scenario differs)
  horizons.forEach((h, i) => {
    const curve = pfBaselineCurve(h);
    const label = h === 0 ? "Baseline: Expiry" : `Baseline: T+${h}d`;
    traces.push({
      x: spots, y: curve,
      type: "scatter", mode: "lines",
      name: label,
      line: {
        color: hasScenarioChange ? "#484f58" : scenColors[i % scenColors.length],
        width: hasScenarioChange ? 1.5 : 2.5,
        dash: hasScenarioChange ? "dot" : "solid",
      },
      visible: true,
      legendgroup: "baseline",
    });
  });

  // Scenario curves (solid) — only if different from baseline
  if (hasScenarioChange) {
    horizons.forEach((h, i) => {
      const curve = pfSumCurves(pfSelected, h);
      const label = h === 0 ? "Scenario: Expiry" : `Scenario: T+${h}d`;
      traces.push({
        x: spots, y: curve,
        type: "scatter", mode: "lines",
        name: label,
        line: { color: scenColors[i % scenColors.length], width: 2.5 },
        legendgroup: "scenario",
      });
    });
  }

  // Spot line
  const allY = traces.flatMap(t => t.y);
  traces.push({
    x: [pfData.eth_spot, pfData.eth_spot],
    y: [Math.min(...allY), Math.max(...allY)],
    type: "scatter", mode: "lines",
    name: `Spot $${pfData.eth_spot.toFixed(0)}`,
    line: { color: "#3fb950", width: 1.5, dash: "dashdot" },
    legendgroup: "spot",
  });

  const layout = {
    title: {
      text: hasScenarioChange
        ? "Portfolio Payoff — Baseline (dotted) vs Scenario (solid)"
        : "Portfolio Payoff Profile — All Positions",
      font: { color: "#e6edf3", size: 16 },
    },
    paper_bgcolor: "#161b22", plot_bgcolor: "#0d1117",
    xaxis: { title: "ETH Spot Price (USD)", color: "#8b949e", gridcolor: "#21262d", zerolinecolor: "#30363d", dtick: 500 },
    yaxis: { title: "Portfolio P&L (USD)", color: "#8b949e", gridcolor: "#21262d", zerolinecolor: "#f85149", zerolinewidth: 2 },
    margin: { t: 50, r: 200, b: 50, l: 80 },
    showlegend: true,
    legend: {
      font: { color: "#8b949e", size: 10 },
      orientation: "v",
      x: 1.02, y: 1,
      xanchor: "left", yanchor: "top",
      bgcolor: "rgba(22,27,34,0.8)",
      bordercolor: "#30363d",
      borderwidth: 1,
    },
  };

  Plotly.react("portfolio-payoff-chart", traces, layout, { responsive: true });
}

// ── MTM Matrix (HTML table with dollar values) ───────────
function pfRenderMtmGrid() {
  const spots = pfData.spot_ladder;
  const horizons = pfData.matrix_horizons; // [30, 45, 60, 90, 120, 150, 180, 270, 360]
  const ethSpot = pfData.eth_spot;

  // Build header row
  const thead = document.getElementById("pf-mtm-grid-thead");
  thead.innerHTML = `<tr><th style="text-align:left">Spot</th>${horizons.map(h => `<th>${h}d</th>`).join("")}</tr>`;

  // Pick spots at $500 increments, reversed ($500 on top, $7000 on bottom)
  const step = 500;
  const displaySpots = [];
  for (let s = step; s <= 7000; s += step) {
    const idx = spots.indexOf(s);
    if (idx !== -1) displaySpots.push({ spot: s, idx });
  }
  displaySpots.reverse(); // $500 on top

  // Find the row closest to current spot
  let closestSpot = displaySpots[0]?.spot ?? 0;
  let closestDiff = Infinity;
  for (const ds of displaySpots) {
    const diff = Math.abs(ds.spot - ethSpot);
    if (diff < closestDiff) { closestDiff = diff; closestSpot = ds.spot; }
  }

  // Build rows
  const tbody = document.getElementById("pf-mtm-grid-body");
  let html = "";
  for (const { spot, idx } of displaySpots) {
    const isSpotRow = spot === closestSpot;
    html += `<tr class="${isSpotRow ? "pf-mtm-spot-row" : ""}">`;
    html += `<td style="text-align:left;font-weight:600;white-space:nowrap">$${spot.toLocaleString()}</td>`;
    for (const h of horizons) {
      let pnl = 0;
      for (const p of pfData.positions) {
        if (!pfSelected.has(p.id)) continue;
        if (pfRolled.has(p.id)) {
          const newDte = pfRolled.get(p.id).newDte;
          const T = Math.max(newDte - h, 0) / 365.25;
          const rollIvPct = pfLookupIv(newDte, p.strike) ?? p.iv_pct;
          const sigma = rollIvPct / 100;
          const val = bsPrice(spot, p.strike, T, 0, sigma, p.opt);
          pnl += p.net_qty * val;
        } else {
          const curve = p.payoff_by_horizon[String(h)];
          if (curve) pnl += curve[idx];
        }
      }
      const rounded = Math.round(pnl);
      const cls = rounded >= 0 ? "mtm-pos" : "mtm-neg";
      const formatted = rounded >= 0
        ? `$${rounded.toLocaleString()}`
        : `-$${Math.abs(rounded).toLocaleString()}`;
      html += `<td class="${cls}">${formatted}</td>`;
    }
    html += "</tr>";
  }
  tbody.innerHTML = html;
}

// ── Portfolio event wiring ────────────────────────────────
document.getElementById("btn-refresh-portfolio").addEventListener("click", () => {
  portfolioLoaded = false;
  loadPortfolio();
});

document.getElementById("btn-pf-select-all").addEventListener("click", () => {
  const visible = pfGetFilteredPositions();
  visible.forEach(p => pfSelected.add(p.id));
  pfRenderTable();
  pfOnSelectionChange();
});

document.getElementById("btn-pf-deselect-all").addEventListener("click", () => {
  const visible = pfGetFilteredPositions();
  visible.forEach(p => pfSelected.delete(p.id));
  pfRenderTable();
  pfOnSelectionChange();
});

document.getElementById("btn-pf-expiring-10d").addEventListener("click", () => {
  if (!pfData) return;
  // Deselect all first, then select only positions expiring within 10 days
  pfSelected.clear();
  pfData.positions.forEach(p => {
    if (p.days_remaining <= 10) pfSelected.add(p.id);
  });
  pfRenderTable();
  pfOnSelectionChange();
});

document.getElementById("pf-check-all").addEventListener("change", (e) => {
  const visible = pfGetFilteredPositions();
  visible.forEach(p => { if (e.target.checked) pfSelected.add(p.id); else pfSelected.delete(p.id); });
  pfRenderTable();
  pfOnSelectionChange();
});

["pf-filter-expiry", "pf-filter-type", "pf-filter-side"].forEach(id => {
  document.getElementById(id).addEventListener("change", () => { if (pfData) pfRenderTable(); });
});

document.querySelectorAll("#pf-positions-table .sortable").forEach(th => {
  th.addEventListener("click", () => {
    const col = th.dataset.col;
    if (pfSortCol === col) pfSortAsc = !pfSortAsc;
    else { pfSortCol = col; pfSortAsc = true; }
    pfRenderTable();
  });
});

// ── Add Trade (frontend-only what-if) ────────────────────
let pfNextId = 90000; // synthetic IDs for added trades

document.getElementById("btn-pf-add-trade").addEventListener("click", () => {
  const instrument = prompt("Instrument name (e.g. ETH-27JUN25-4000-C):");
  if (!instrument) return;
  const side = prompt("Side (buy/sell):", "buy");
  if (!side) return;
  const qtyStr = prompt("Quantity (ETH options):", "1000");
  if (!qtyStr) return;
  const premStr = prompt("Total premium USD (negative=paid, positive=received):", "0");
  if (premStr === null) return;

  const qty = parseFloat(qtyStr) || 0;
  const premUsd = parseFloat(premStr) || 0;
  if (qty <= 0) { alert("Quantity must be positive."); return; }

  const name = instrument.trim().toUpperCase();

  // Fetch Deribit ticker for live data
  get(`/api/portfolio/ticker/${encodeURIComponent(name)}`)
    .then(tk => {
      const sign = side.toLowerCase() === "sell" ? -1 : 1;
      const signedQty = sign * qty;
      const markUsd = tk.mark_price_usd || 0;

      // Generate payoff curves using JS BS
      const sigma = (tk.mark_iv || 80) / 100;
      const opt = tk.opt || "C";
      const strike = tk.strike || 0;
      const dte = tk.days_remaining || 0;
      const spots = pfData.spot_ladder;
      const allHorizons = pfData.all_horizons;
      const payoff = {};
      for (const h of allHorizons) {
        const T = Math.max(dte - h, 0) / 365.25;
        const vals = bsVec(spots, strike, T, 0, sigma, opt);
        payoff[String(h)] = vals.map(v => Math.round(signedQty * v * 100) / 100);
      }

      // Build MTM horizon array
      const mtmHorizon = pfData.matrix_horizons.map(h => {
        const T = Math.max(dte - h, 0) / 365.25;
        const val = bsPrice(pfData.eth_spot, strike, T, 0, sigma, opt);
        return Math.round(signedQty * val * 100) / 100;
      });

      const curMtm = signedQty * markUsd;

      const newPos = {
        id: pfNextId++,
        counterparty: "What-If",
        trade_id: null,
        trade_date: new Date().toISOString().split("T")[0],
        side_raw: side.toLowerCase() === "sell" ? "Sell" : "Buy",
        option_type: opt === "C" ? "Call" : "Put",
        instrument: name,
        expiry: tk.expiry || "",
        days_remaining: dte,
        strike: strike,
        pct_otm_entry: 0,
        qty: qty,
        notional_mm: 0,
        premium_per: premUsd / qty,
        premium_usd: premUsd,
        opt: opt,
        side: sign > 0 ? "Long" : "Short",
        net_qty: signedQty,
        pct_otm_live: pfData.eth_spot > 0 ? Math.round((strike / pfData.eth_spot - 1) * 1000) / 10 : 0,
        iv_pct: tk.mark_iv || 80,
        delta: tk.delta,
        gamma: tk.gamma,
        theta: tk.theta,
        vega: tk.vega,
        mark_price_usd: markUsd,
        current_mtm: Math.round(curMtm * 100) / 100,
        notional_live: Math.round(qty * pfData.eth_spot * 100) / 100,
        mtm_by_horizon: mtmHorizon,
        payoff_by_horizon: payoff,
      };

      pfData.positions.push(newPos);
      pfSelected.add(newPos.id);
      pfRenderAll();
    })
    .catch(e => {
      console.error("Failed to fetch ticker:", e);
      alert("Failed to fetch instrument data from Deribit.\n" + e.message);
    });
});

// ── Remove Selected (frontend-only) ─────────────────────
document.getElementById("btn-pf-remove-selected").addEventListener("click", () => {
  if (!pfData) return;
  const toRemove = new Set(pfSelected);
  if (toRemove.size === 0) { alert("No trades selected to remove."); return; }
  if (!confirm(`Remove ${toRemove.size} selected trade(s) from the scenario?`)) return;

  pfData.positions = pfData.positions.filter(p => !toRemove.has(p.id));
  pfSelected = new Set(pfData.positions.map(p => p.id));
  pfRolled = new Map([...pfRolled].filter(([k]) => pfSelected.has(k)));
  pfRenderAll();
});

// ── Export to Excel ───────────────────────────────────────
document.getElementById("btn-pf-export-xlsx").addEventListener("click", () => {
  if (!pfData) return;

  const positions = pfGetFilteredPositions().filter(p => pfSelected.has(p.id));
  if (positions.length === 0) { alert("No selected trades to export."); return; }

  const spot = pfData.eth_spot;
  const rows = positions.map(pos => {
    const rolled = pfRolled.has(pos.id);
    const newDte = rolled ? pfRolled.get(pos.id).newDte : null;

    // Roll cost calculation (mirrors table render logic)
    let rollCost = null;
    let rollLabel = "";
    let rollCurMark = null;
    let rollNewMark = null;
    let rollIvUsed = null;
    if (rolled && newDte != null) {
      const T = Math.max(newDte, 0) / 365.25;
      const rollIvPct = pfLookupIv(newDte, pos.strike) ?? pos.iv_pct;
      const sigma = rollIvPct / 100;
      const newMark = bsPrice(spot, pos.strike, T, 0, sigma, pos.opt);
      const curMark = pos.mark_price_usd ?? 0;
      rollCost = Math.round(pos.net_qty * (curMark - newMark));
      rollLabel = rollCost >= 0 ? "Receive" : "Pay";
      rollCurMark = Math.round(curMark * 100) / 100;
      rollNewMark = Math.round(newMark * 100) / 100;
      rollIvUsed = Math.round(rollIvPct * 10) / 10;
    }

    return {
      "Counterparty": pos.counterparty,
      "ID": pos.trade_id,
      "Trade Date": pos.trade_date,
      "Buy/Sell": pos.side_raw,
      "Type": pos.option_type,
      "Instrument": pos.instrument,
      "Expiry": pos.expiry,
      "DTE": Math.round(pos.days_remaining),
      "Strike": pos.strike,
      "% OTM": pos.pct_otm_live,
      "ETH Qty": pos.qty,
      "Net Qty": pos.net_qty,
      "Entry Prem/Contract": pos.premium_per,
      "Entry USD": pos.premium_usd,
      "IV%": pos.iv_pct,
      "Delta": pos.delta,
      "Gamma": pos.gamma,
      "Theta": pos.theta,
      "Vega": pos.vega,
      "Mark Price USD": pos.mark_price_usd,
      "MTM": pos.current_mtm,
      "Roll": rolled ? "Yes" : "",
      "New DTE": newDte,
      "Cur Mark (Close)": rollCurMark,
      "New Mark (Open)": rollNewMark,
      "IV Used": rollIvUsed,
      "Roll Cost": rollCost,
      "Roll Direction": rollLabel,
    };
  });

  // Summary row
  const totalMtm = positions.reduce((s, p) => s + (p.current_mtm || 0), 0);
  const totalEntry = positions.reduce((s, p) => s + Math.abs(p.premium_usd || 0), 0);
  const totalRollCost = rows.reduce((s, r) => s + (r["Roll Cost"] || 0), 0);
  rows.push({});  // blank row
  rows.push({
    "Counterparty": "TOTALS",
    "ETH Qty": positions.reduce((s, p) => s + p.qty, 0),
    "Net Qty": positions.reduce((s, p) => s + p.net_qty, 0),
    "Entry USD": totalEntry,
    "MTM": totalMtm,
    "Roll Cost": totalRollCost || null,
    "Roll Direction": totalRollCost ? (totalRollCost >= 0 ? "Net Receive" : "Net Pay") : "",
  });

  // Portfolio greeks summary
  const gd = (field) => positions.reduce((s, p) => s + ((p[field] || 0) * p.net_qty), 0);
  rows.push({
    "Counterparty": "PORTFOLIO GREEKS",
    "Delta": Math.round(gd("delta") * 100) / 100,
    "Gamma": Math.round(gd("gamma") * 10000) / 10000,
    "Theta": Math.round(gd("theta") * 100) / 100,
    "Vega": Math.round(gd("vega") * 100) / 100,
  });

  // Create workbook
  const wb = XLSX.utils.book_new();
  const ws = XLSX.utils.json_to_sheet(rows);

  // Column widths
  ws["!cols"] = [
    { wch: 15 }, // Counterparty
    { wch: 8 },  // ID
    { wch: 12 }, // Trade Date
    { wch: 8 },  // Buy/Sell
    { wch: 6 },  // Type
    { wch: 24 }, // Instrument
    { wch: 12 }, // Expiry
    { wch: 6 },  // DTE
    { wch: 8 },  // Strike
    { wch: 7 },  // % OTM
    { wch: 10 }, // ETH Qty
    { wch: 10 }, // Net Qty
    { wch: 14 }, // Entry Prem
    { wch: 14 }, // Entry USD
    { wch: 7 },  // IV%
    { wch: 8 },  // Delta
    { wch: 10 }, // Gamma
    { wch: 10 }, // Theta
    { wch: 8 },  // Vega
    { wch: 14 }, // Mark Price USD
    { wch: 14 }, // MTM
    { wch: 5 },  // Roll
    { wch: 8 },  // New DTE
    { wch: 14 }, // Cur Mark (Close)
    { wch: 14 }, // New Mark (Open)
    { wch: 8 },  // IV Used
    { wch: 14 }, // Roll Cost
    { wch: 12 }, // Roll Direction
  ];

  XLSX.utils.book_append_sheet(wb, ws, "Portfolio");

  // Filename with date
  const dateStr = new Date().toISOString().split("T")[0];
  XLSX.writeFile(wb, `PLGO_Portfolio_${dateStr}.xlsx`);
});

// ─── Roll Analysis ──────────────────────────────────────────
let rollLoaded = false;
let rollSelected = new Set();
let rollIncluded = new Set();  // positions included in portfolio chart/matrix
let rollLastResults = null;    // last computed roll results (for refreshing visuals)
// Multi-level sort: clicking a new column makes it primary, old primary becomes secondary
let rollSortStack = [{ col: "expiry", asc: true }];

async function rollInit() {
  if (!pfData) {
    try {
      pfData = await get("/api/portfolio/pnl");
      portfolioLoaded = true;
      if (pfSelected.size === 0) {
        pfSelected = new Set(pfData.positions.map(p => p.id));
      }
    } catch (e) {
      console.error("Failed to load portfolio for roll analysis:", e);
      alert("Failed to load portfolio data.\n" + e.message);
      return;
    }
  }
  rollPopulate();
}

function rollPopulate() {
  // Populate position expiry filter
  const expiries = [...new Set(pfData.positions.map(p => p.expiry.split("T")[0]))].sort();
  const $expF = document.getElementById("roll-filter-expiry");
  $expF.innerHTML = '<option value="">All</option>';
  expiries.forEach(e => {
    const opt = document.createElement("option");
    opt.value = e; opt.textContent = e;
    $expF.appendChild(opt);
  });

  // Populate target expiry dropdown from Deribit vol surface
  const $targetExp = document.getElementById("roll-target-expiry");
  $targetExp.innerHTML = "";
  const smiles = (pfData.vol_surface || []).filter(s => s.dte > 0);
  smiles.sort((a, b) => a.dte - b.dte);
  smiles.forEach(s => {
    const opt = document.createElement("option");
    opt.value = s.expiry_code;
    opt.textContent = `${s.expiry_code} (${s.dte}d)`;
    $targetExp.appendChild(opt);
  });
  // Default to nearest expiry
  if (smiles.length > 0) $targetExp.value = smiles[0].expiry_code;

  rollSelected = new Set();
  rollIncluded = new Set(pfData.positions.map(p => p.id));
  rollRenderTable();
  rollDrawChart(null);
  rollRenderMtmGrid(null);
  rollLoaded = true;
}

function rollGetFilteredPositions() {
  const fExpiry = document.getElementById("roll-filter-expiry").value;
  const fType = document.getElementById("roll-filter-type").value;
  const fSide = document.getElementById("roll-filter-side").value;
  let list = [...pfData.positions];
  if (fExpiry) list = list.filter(p => p.expiry.split("T")[0] === fExpiry);
  if (fType) list = list.filter(p => p.opt === fType);
  if (fSide) list = list.filter(p => p.side === fSide);

  if (rollSortStack.length > 0) {
    list.sort((a, b) => {
      for (const { col, asc } of rollSortStack) {
        let va = a[col], vb = b[col];
        if (typeof va === "string") { va = va.toLowerCase(); vb = (vb || "").toLowerCase(); }
        if (va < vb) return asc ? -1 : 1;
        if (va > vb) return asc ? 1 : -1;
      }
      return 0;
    });
  }
  return list;
}

function rollGetExpiryOptions(selectedCode) {
  const smiles = (pfData.vol_surface || []).filter(s => s.dte > 0);
  smiles.sort((a, b) => a.dte - b.dte);
  return smiles.map(s =>
    `<option value="${s.expiry_code}" ${s.expiry_code === selectedCode ? "selected" : ""}>${s.expiry_code} (${s.dte}d)</option>`
  ).join("");
}

function rollGetTargetExpiry() {
  return document.getElementById("roll-target-expiry").value;
}

function rollDteForExpiry(code) {
  const s = (pfData.vol_surface || []).find(s => s.expiry_code === code);
  return s ? s.dte : 90;
}

function rollComputeRow(pos) {
  const row = pos._rollRow || document.querySelector(`#roll-trades-body tr[data-pos-id="${pos.id}"]`);
  const expirySelect = row ? row.querySelector(".roll-expiry-select") : null;
  const strikeInput = row ? row.querySelector(".roll-strike-input") : null;
  const newExpiryCode = expirySelect ? expirySelect.value : rollGetTargetExpiry();
  const newStrike = strikeInput ? parseFloat(strikeInput.value) || pos.strike : pos.strike;
  const newDte = rollDteForExpiry(newExpiryCode);
  const T = Math.max(newDte, 0) / 365.25;
  const spot = pfData.eth_spot;
  const rollIvPct = pfLookupIv(newDte, newStrike) ?? pos.iv_pct;
  const sigma = rollIvPct / 100;
  const newMark = bsPrice(spot, newStrike, T, 0, sigma, pos.opt);
  const curMark = pos.mark_price_usd ?? 0;
  const cost = pos.net_qty * (curMark - newMark);
  return { newExpiryCode, newDte, newStrike, newMark, rollIvPct, curMark, cost };
}

function rollInlineUpdate(row) {
  const posId = parseInt(row.dataset.posId);
  const pos = pfData.positions.find(p => p.id === posId);
  if (!pos) return;
  const newExpCode = row.querySelector(".roll-expiry-select").value;
  const newStrike = parseFloat(row.querySelector(".roll-strike-input").value) || pos.strike;
  const newAbsQty = parseFloat(row.querySelector(".roll-qty-input").value);
  const sign = pos.net_qty >= 0 ? 1 : -1;
  const newNetQty = isNaN(newAbsQty) ? pos.net_qty : sign * newAbsQty;
  const newDte = rollDteForExpiry(newExpCode);
  const T = Math.max(newDte, 0) / 365.25;
  const iv = pfLookupIv(newDte, newStrike) ?? pos.iv_pct;
  const sigma = iv / 100;
  const nm = bsPrice(pfData.eth_spot, newStrike, T, 0, sigma, pos.opt);
  const cm = pos.mark_price_usd ?? 0;
  // Close current position + open new at new qty
  const closeVal = pos.net_qty * cm;
  const openVal = newNetQty * nm;
  const cost = closeVal - openVal;
  const cr = Math.round(cost);
  row.querySelector(".roll-new-mark").textContent = `$${nm.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
  row.querySelector(".roll-iv-used").textContent = `${iv.toFixed(1)}%`;
  const costCell = row.querySelector(".roll-cost");
  costCell.textContent = `${cr >= 0 ? "Rcv" : "Pay"} $${Math.abs(cr).toLocaleString()}`;
  costCell.className = `${cr >= 0 ? "mtm-pos" : "mtm-neg"} roll-cost`;
  costCell.style.fontFamily = "monospace";
  costCell.style.fontWeight = "600";
}

function rollRenderTable() {
  const list = rollGetFilteredPositions();
  document.getElementById("roll-table-count").textContent = `(${list.length} trades)`;
  const globalExpiry = rollGetTargetExpiry();
  const allVisibleRoll = list.length > 0 && list.every(p => rollSelected.has(p.id));
  const allVisibleIncl = list.length > 0 && list.every(p => rollIncluded.has(p.id));
  document.getElementById("roll-check-all").checked = allVisibleRoll;

  // Update sort indicators (primary = arrow, secondary = dot)
  const primarySort = rollSortStack[0] || null;
  const secondarySort = rollSortStack[1] || null;
  document.querySelectorAll("#roll-trades-table .sortable").forEach(th => {
    th.classList.remove("sort-asc", "sort-desc", "sort-secondary");
    if (primarySort && th.dataset.col === primarySort.col) {
      th.classList.add(primarySort.asc ? "sort-asc" : "sort-desc");
    } else if (secondarySort && th.dataset.col === secondarySort.col) {
      th.classList.add("sort-secondary");
    }
  });
  document.getElementById("roll-include-all").checked = allVisibleIncl;

  const fmtNum = (v, d=0) => v != null ? v.toLocaleString(undefined, { maximumFractionDigits: d }) : "---";
  const spot = pfData.eth_spot;
  const $tbody = document.getElementById("roll-trades-body");
  $tbody.innerHTML = "";

  for (const pos of list) {
    const included = rollIncluded.has(pos.id);
    const checked = rollSelected.has(pos.id);
    const typeColor = pos.opt === "C" ? "var(--green)" : "var(--red)";
    const sideClass = pos.side === "Long" ? "qty-long" : "qty-short";
    const rowClass = (checked ? "roll-row-selected" : "") + (!included ? " pf-row-excluded" : "");
    const absQty = Math.abs(pos.net_qty);

    // Compute roll inline (same qty by default)
    const newDte = rollDteForExpiry(globalExpiry);
    const T = Math.max(newDte, 0) / 365.25;
    const rollIvPct = pfLookupIv(newDte, pos.strike) ?? pos.iv_pct;
    const sigma = rollIvPct / 100;
    const newMark = bsPrice(spot, pos.strike, T, 0, sigma, pos.opt);
    const curMark = pos.mark_price_usd ?? 0;
    const closeVal = pos.net_qty * curMark;
    const openVal = pos.net_qty * newMark;
    const cost = closeVal - openVal;
    const costRounded = Math.round(cost);
    const costCls = costRounded >= 0 ? "mtm-pos" : "mtm-neg";
    const costLabel = costRounded >= 0 ? "Rcv" : "Pay";

    const expiryOpts = rollGetExpiryOptions(globalExpiry);

    const tr = document.createElement("tr");
    tr.className = rowClass;
    tr.dataset.posId = pos.id;
    tr.innerHTML =
      `<td class="roll-include-cell"><input type="checkbox" class="roll-include" data-id="${pos.id}" ${included ? "checked" : ""}></td>` +
      `<td><input type="checkbox" class="roll-check" data-id="${pos.id}" ${checked ? "checked" : ""}></td>` +
      `<td style="text-align:left;font-size:.72rem">${pos.instrument}</td>` +
      `<td style="text-align:left" class="${sideClass}">${pos.side_raw}</td>` +
      `<td style="text-align:left;color:${typeColor}">${pos.opt === "C" ? "Call" : "Put"}</td>` +
      `<td style="font-family:monospace">${fmtNum(pos.strike)}</td>` +
      `<td>${pos.expiry}</td>` +
      `<td>${Math.round(pos.days_remaining)}</td>` +
      `<td style="font-family:monospace">${fmtNum(pos.net_qty)}</td>` +
      `<td style="font-family:monospace">$${fmtNum(curMark, 2)}</td>` +
      `<td>${pos.iv_pct != null ? pos.iv_pct.toFixed(1) + "%" : "---"}</td>` +
      `<td class="${pos.current_mtm >= 0 ? 'mtm-pos' : 'mtm-neg'}" style="font-family:monospace">` +
        `$${fmtNum(pos.current_mtm)}</td>` +
      `<td><select class="roll-expiry-select" data-id="${pos.id}" style="font-size:.72rem;width:100%;margin:0">${expiryOpts}</select></td>` +
      `<td><input type="number" class="roll-strike-input" data-id="${pos.id}" ` +
        `value="${pos.strike}" step="50" style="width:100%;font-size:.75rem;padding:.2rem .3rem;text-align:center"></td>` +
      `<td><input type="number" class="roll-qty-input" data-id="${pos.id}" ` +
        `value="${absQty}" step="1" min="0" style="width:100%;font-size:.75rem;padding:.2rem .3rem;text-align:center"></td>` +
      `<td style="font-family:monospace" class="roll-new-mark">$${fmtNum(newMark, 2)}</td>` +
      `<td class="roll-iv-used">${rollIvPct.toFixed(1)}%</td>` +
      `<td class="${costCls} roll-cost" style="font-family:monospace;font-weight:600">${costLabel} $${Math.abs(costRounded).toLocaleString()}</td>`;
    $tbody.appendChild(tr);
  }

  // Wire include checkbox events
  $tbody.querySelectorAll(".roll-include").forEach(cb => {
    cb.addEventListener("change", () => {
      const id = parseInt(cb.dataset.id);
      if (cb.checked) rollIncluded.add(id); else rollIncluded.delete(id);
      const tr = cb.closest("tr");
      tr.classList.toggle("pf-row-excluded", !cb.checked);
      const visible = rollGetFilteredPositions();
      document.getElementById("roll-include-all").checked =
        visible.length > 0 && visible.every(p => rollIncluded.has(p.id));
      rollRefreshVisuals();
    });
  });

  // Wire roll checkbox events
  $tbody.querySelectorAll(".roll-check").forEach(cb => {
    cb.addEventListener("change", () => {
      const id = parseInt(cb.dataset.id);
      if (cb.checked) rollSelected.add(id); else rollSelected.delete(id);
      cb.closest("tr").classList.toggle("roll-row-selected", cb.checked);
      const visible = rollGetFilteredPositions();
      document.getElementById("roll-check-all").checked =
        visible.length > 0 && visible.every(p => rollSelected.has(p.id));
    });
  });

  // Wire per-row expiry/strike/qty changes to update inline roll values
  $tbody.querySelectorAll(".roll-expiry-select, .roll-strike-input, .roll-qty-input").forEach(el => {
    el.addEventListener("change", () => rollInlineUpdate(el.closest("tr")));
  });
}

// Refresh chart + matrix based on current include/roll state (no full recompute)
function rollRefreshVisuals() {
  rollDrawChart(rollLastResults);
  rollRenderMtmGrid(rollLastResults);
}

function rollCompute() {
  if (!pfData) return;
  const selectedPositions = pfData.positions.filter(p => rollSelected.has(p.id));
  if (selectedPositions.length === 0) { alert("Select at least one leg to roll."); return; }

  const globalExpiry = rollGetTargetExpiry();
  const spot = pfData.eth_spot;
  const results = [];
  let totalCloseValue = 0, totalOpenValue = 0, totalRollCost = 0;

  for (const pos of selectedPositions) {
    const row = document.querySelector(`#roll-trades-body tr[data-pos-id="${pos.id}"]`);
    const newExpCode = row ? row.querySelector(".roll-expiry-select").value : globalExpiry;
    const newStrike = row ? (parseFloat(row.querySelector(".roll-strike-input").value) || pos.strike) : pos.strike;
    const newAbsQty = row ? parseFloat(row.querySelector(".roll-qty-input").value) : Math.abs(pos.net_qty);
    const sign = pos.net_qty >= 0 ? 1 : -1;
    const newNetQty = isNaN(newAbsQty) ? pos.net_qty : sign * newAbsQty;
    const newDte = rollDteForExpiry(newExpCode);
    const T = Math.max(newDte, 0) / 365.25;
    const rollIvPct = pfLookupIv(newDte, newStrike) ?? pos.iv_pct;
    const sigma = rollIvPct / 100;
    const newMark = bsPrice(spot, newStrike, T, 0, sigma, pos.opt);
    const curMark = pos.mark_price_usd ?? 0;

    // Close current position fully, open new at (possibly different) qty
    const closeValue = pos.net_qty * curMark;
    const openValue = newNetQty * newMark;
    const cost = closeValue - openValue;

    // Breakdown: DTE impact vs strike impact (at original qty for comparison)
    const strikeChanged = newStrike !== pos.strike;
    const qtyChanged = newAbsQty !== Math.abs(pos.net_qty);
    let dteImpact = cost, strikeImpact = 0, qtyImpact = 0;
    if (strikeChanged || qtyChanged) {
      // DTE-only: same strike, same qty, new DTE
      const dteIv = pfLookupIv(newDte, pos.strike) ?? pos.iv_pct;
      const dteOnlyMark = bsPrice(spot, pos.strike, T, 0, dteIv / 100, pos.opt);
      const dteOnlyCost = pos.net_qty * curMark - pos.net_qty * dteOnlyMark;
      dteImpact = dteOnlyCost;

      // Strike change: same qty, new strike vs old strike at new DTE
      const strikeOnlyCost = pos.net_qty * dteOnlyMark - pos.net_qty * newMark;
      strikeImpact = strikeChanged ? strikeOnlyCost : 0;

      // Qty change: difference from changing qty at new strike/DTE
      qtyImpact = cost - dteImpact - strikeImpact;
    }

    totalCloseValue += closeValue;
    totalOpenValue += openValue;
    totalRollCost += cost;
    results.push({ pos, newExpCode, newDte, newStrike, newNetQty, curMark, newMark, rollIvPct,
      cost, closeValue, openValue, strikeChanged, qtyChanged, dteImpact, strikeImpact, qtyImpact });
  }
  rollRenderResults(results, totalCloseValue, totalOpenValue, totalRollCost, globalExpiry);
}

function rollRenderResults(results, totalCloseValue, totalOpenValue, totalRollCost, globalExpiry) {
  rollLastResults = results;
  document.getElementById("roll-results-section").style.display = "block";

  document.getElementById("roll-legs-count").textContent = results.length;
  const globalDte = rollDteForExpiry(globalExpiry);
  document.getElementById("roll-target-dte-display").textContent = `${globalExpiry} (${globalDte}d)`;

  const fmtUsd = v => "$" + Math.abs(Math.round(v)).toLocaleString();
  const fmtCost = v => { const r = Math.round(v); return `<span class="${r >= 0 ? "mtm-pos" : "mtm-neg"}" style="font-family:monospace;font-weight:600">${r >= 0 ? "Rcv" : "Pay"} $${Math.abs(r).toLocaleString()}</span>`; };

  const $close = document.getElementById("roll-total-close");
  $close.textContent = (totalCloseValue >= 0 ? "" : "-") + fmtUsd(totalCloseValue);
  $close.className = "risk-value " + (totalCloseValue >= 0 ? "mtm-pos" : "mtm-neg");

  const $open = document.getElementById("roll-total-open");
  $open.textContent = (totalOpenValue >= 0 ? "" : "-") + fmtUsd(totalOpenValue);
  $open.className = "risk-value " + (totalOpenValue >= 0 ? "mtm-pos" : "mtm-neg");

  const rounded = Math.round(totalRollCost);
  const $net = document.getElementById("roll-net-cost");
  $net.textContent = (rounded >= 0 ? "" : "-") + fmtUsd(totalRollCost);
  $net.className = "risk-value " + (rounded >= 0 ? "mtm-pos" : "mtm-neg");

  const $dir = document.getElementById("roll-direction");
  $dir.textContent = rounded >= 0 ? "Net Receive" : "Net Pay";
  $dir.className = "risk-value " + (rounded >= 0 ? "mtm-pos" : "mtm-neg");

  // Check if any strike or qty changed — show breakdown columns
  const hasStrikeChange = results.some(r => r.strikeChanged);
  const hasQtyChange = results.some(r => r.qtyChanged);
  const hasBreakdown = hasStrikeChange || hasQtyChange;

  // Dynamic header
  const $thead = document.getElementById("roll-results-thead");
  let hdr = `<tr>
    <th style="text-align:left">Instrument</th>
    <th style="text-align:left">Side</th>
    <th>Strike</th>
    <th>Qty</th>
    <th>Cur DTE</th>
    <th>New Expiry</th>
    <th>Cur Mark</th>
    <th>New Mark</th>
    <th>IV Used</th>`;
  if (hasBreakdown) {
    hdr += `<th title="Cost from DTE change only (same strike, same qty)">DTE Impact</th>`;
    if (hasStrikeChange) hdr += `<th title="Additional cost from strike change">Strike Impact</th>`;
    if (hasQtyChange) hdr += `<th title="Additional cost from qty change">Qty Impact</th>`;
  }
  hdr += `<th>Total Cost</th></tr>`;
  $thead.innerHTML = hdr;

  const fmtNum = (v, d=2) => v != null ? v.toLocaleString(undefined, { maximumFractionDigits: d }) : "---";
  const $tbody = document.getElementById("roll-results-body");
  $tbody.innerHTML = "";

  let totalDteImpact = 0, totalStrikeImpact = 0, totalQtyImpact = 0;

  for (const r of results) {
    totalDteImpact += r.dteImpact;
    totalStrikeImpact += r.strikeImpact;
    totalQtyImpact += r.qtyImpact;
    const sideClass = r.pos.side === "Long" ? "qty-long" : "qty-short";
    const strikeLabel = r.strikeChanged
      ? `${fmtNum(r.pos.strike, 0)} → ${fmtNum(r.newStrike, 0)}`
      : fmtNum(r.pos.strike, 0);
    const qtyLabel = r.qtyChanged
      ? `${fmtNum(r.pos.net_qty, 0)} → ${fmtNum(r.newNetQty, 0)}`
      : fmtNum(r.pos.net_qty, 0);

    let rowHtml =
      `<td style="text-align:left;font-size:.72rem">${r.pos.instrument}</td>` +
      `<td style="text-align:left" class="${sideClass}">${r.pos.side_raw}</td>` +
      `<td style="font-family:monospace">${strikeLabel}</td>` +
      `<td style="font-family:monospace">${qtyLabel}</td>` +
      `<td>${Math.round(r.pos.days_remaining)}d</td>` +
      `<td>${r.newExpCode} (${r.newDte}d)</td>` +
      `<td style="font-family:monospace">$${fmtNum(r.curMark)}</td>` +
      `<td style="font-family:monospace">$${fmtNum(r.newMark)}</td>` +
      `<td>${r.rollIvPct.toFixed(1)}%</td>`;
    if (hasBreakdown) {
      rowHtml += `<td>${fmtCost(r.dteImpact)}</td>`;
      if (hasStrikeChange) rowHtml += `<td>${r.strikeChanged ? fmtCost(r.strikeImpact) : '<span style="color:var(--muted)">—</span>'}</td>`;
      if (hasQtyChange) rowHtml += `<td>${r.qtyChanged ? fmtCost(r.qtyImpact) : '<span style="color:var(--muted)">—</span>'}</td>`;
    }
    rowHtml += `<td>${fmtCost(r.cost)}</td>`;

    const tr = document.createElement("tr");
    tr.innerHTML = rowHtml;
    $tbody.appendChild(tr);
  }

  // Footer
  const $tfoot = document.getElementById("roll-results-foot");
  let footHtml = `<tr><td style="text-align:left;font-weight:700" colspan="4">TOTAL</td>` +
    `<td></td><td></td><td></td><td></td><td></td>`;
  if (hasBreakdown) {
    footHtml += `<td>${fmtCost(totalDteImpact)}</td>`;
    if (hasStrikeChange) footHtml += `<td>${fmtCost(totalStrikeImpact)}</td>`;
    if (hasQtyChange) footHtml += `<td>${fmtCost(totalQtyImpact)}</td>`;
  }
  footHtml += `<td>${fmtCost(totalRollCost)}</td></tr>`;
  $tfoot.innerHTML = footHtml;

  // ── Payoff chart + MTM matrix ───────────────────────────
  rollDrawChart(results);
  rollRenderMtmGrid(results);
}

// Compute MTM across a spot ladder for a set of positions,
// optionally replacing some with rolled versions.
// rollMap: Map of posId → { newDte, newStrike, rollIvPct }
function rollSumCurve(spotLadder, positions, horizon, rollMap) {
  const result = new Array(spotLadder.length).fill(0);
  for (const p of positions) {
    const rolled = rollMap.get(p.id);
    if (rolled) {
      const qty = rolled.newNetQty != null ? rolled.newNetQty : p.net_qty;
      const T = Math.max(rolled.newDte - horizon, 0) / 365.25;
      const sigma = rolled.rollIvPct / 100;
      for (let i = 0; i < spotLadder.length; i++) {
        result[i] += qty * bsPrice(spotLadder[i], rolled.newStrike, T, 0, sigma, p.opt);
      }
    } else {
      const curve = p.payoff_by_horizon[String(horizon)];
      if (curve) {
        for (let i = 0; i < spotLadder.length; i++) result[i] += curve[i];
      }
    }
  }
  return result;
}

function rollBaselineCurve(spotLadder, positions, horizon) {
  const result = new Array(spotLadder.length).fill(0);
  for (const p of positions) {
    const curve = p.payoff_by_horizon[String(horizon)];
    if (curve) for (let i = 0; i < spotLadder.length; i++) result[i] += curve[i];
  }
  return result;
}

function rollBuildRollMap(results) {
  const m = new Map();
  for (const r of results) {
    m.set(r.pos.id, {
      newDte: r.newDte,
      newStrike: r.newStrike || r.pos.strike,
      rollIvPct: r.rollIvPct,
      newNetQty: r.newNetQty,
    });
  }
  return m;
}

function rollDrawChart(results) {
  const $chart = document.getElementById("roll-payoff-chart");
  $chart.style.display = "block";

  const $controls = document.getElementById("roll-chart-controls");
  const showBaseline = document.getElementById("roll-show-baseline").checked;
  const showScenario = document.getElementById("roll-show-scenario").checked;

  const spots = pfData.spot_ladder;
  const horizons = pfData.chart_horizons;
  const spot = pfData.eth_spot;
  const hasRolls = results && results.length > 0;
  const rollMap = hasRolls ? rollBuildRollMap(results) : new Map();
  const allPositions = pfData.positions;
  const includedPositions = allPositions.filter(p => rollIncluded.has(p.id));
  const hasExclusion = rollIncluded.size !== allPositions.length;
  const hasScenarioChange = hasRolls || hasExclusion;

  // Show toggle controls only when there's a scenario to compare
  $controls.style.display = hasScenarioChange ? "block" : "none";

  const scenColors = ["#f85149", "#d29922", "#e3b341", "#3fb950", "#58a6ff", "#bc8cff"];
  const traces = [];

  // Baseline = full portfolio (all positions, no rolls)
  if (!hasScenarioChange || showBaseline) {
    horizons.forEach((h, i) => {
      const curve = rollBaselineCurve(spots, allPositions, h);
      const label = h === 0 ? "Baseline: Expiry" : `Baseline: T+${h}d`;
      const onlyBaseline = hasScenarioChange && !showScenario;
      traces.push({
        x: spots, y: curve, type: "scatter", mode: "lines",
        name: label,
        line: {
          color: (hasScenarioChange && !onlyBaseline) ? "#484f58" : scenColors[i % scenColors.length],
          width: (hasScenarioChange && !onlyBaseline) ? 1.5 : 2.5,
          dash: (hasScenarioChange && !onlyBaseline) ? "dot" : "solid",
        },
        legendgroup: "baseline",
      });
    });
  }

  // Scenario = included positions with rolls applied (solid colored)
  if (hasScenarioChange && showScenario) {
    horizons.forEach((h, i) => {
      const curve = rollSumCurve(spots, includedPositions, h, rollMap);
      const label = h === 0 ? "Scenario: Expiry" : `Scenario: T+${h}d`;
      traces.push({
        x: spots, y: curve, type: "scatter", mode: "lines",
        name: label,
        line: { color: scenColors[i % scenColors.length], width: 2.5 },
        legendgroup: "scenario",
      });
    });
  }

  // Spot marker
  const allY = traces.flatMap(t => t.y);
  if (allY.length > 0) {
    traces.push({
      x: [spot, spot], y: [Math.min(...allY), Math.max(...allY)],
      type: "scatter", mode: "lines",
      name: `Spot $${spot.toFixed(0)}`,
      line: { color: "#3fb950", width: 1.5, dash: "dashdot" },
      legendgroup: "spot",
    });
  }

  let titleText = "Portfolio Payoff Profile — All Positions";
  if (hasRolls && hasExclusion) titleText = "Portfolio Payoff — Baseline (dotted) vs Scenario (solid)";
  else if (hasRolls) titleText = "Portfolio Payoff — Baseline (dotted) vs Rolled (solid)";
  else if (hasExclusion) titleText = "Portfolio Payoff — Baseline (dotted) vs Selected (solid)";

  const layout = {
    title: { text: titleText, font: { color: "#e6edf3", size: 16 } },
    paper_bgcolor: "#161b22", plot_bgcolor: "#0d1117",
    xaxis: { title: "ETH Spot Price (USD)", color: "#8b949e", gridcolor: "#21262d", zerolinecolor: "#30363d", dtick: 500 },
    yaxis: { title: "Portfolio P&L (USD)", color: "#8b949e", gridcolor: "#21262d", zerolinecolor: "#f85149", zerolinewidth: 2 },
    margin: { t: 50, r: 200, b: 50, l: 80 },
    showlegend: true,
    legend: {
      font: { color: "#8b949e", size: 10 },
      orientation: "v", x: 1.02, y: 1, xanchor: "left", yanchor: "top",
      bgcolor: "rgba(22,27,34,0.8)", bordercolor: "#30363d", borderwidth: 1,
    },
  };

  Plotly.react("roll-payoff-chart", traces, layout, { responsive: true });
}

function rollRenderMtmGrid(results) {
  const $section = document.getElementById("roll-mtm-section");
  $section.style.display = "block";

  const spots = pfData.spot_ladder;
  const horizons = pfData.matrix_horizons;
  const ethSpot = pfData.eth_spot;
  const allPositions = pfData.positions;
  const includedPositions = allPositions.filter(p => rollIncluded.has(p.id));
  const hasRolls = results && results.length > 0;
  const hasExclusion = rollIncluded.size !== allPositions.length;
  const hasScenarioChange = hasRolls || hasExclusion;
  const rollMap = hasRolls ? rollBuildRollMap(results) : new Map();

  const scenLabel = hasRolls && hasExclusion ? "Scenario" : hasRolls ? "Rolled" : "Selected";

  // Header
  const thead = document.getElementById("roll-mtm-grid-thead");
  if (hasScenarioChange) {
    let hdrHtml = `<tr><th rowspan="2" style="text-align:left">Spot</th>`;
    for (const h of horizons) hdrHtml += `<th colspan="2">${h}d</th>`;
    hdrHtml += `</tr><tr>`;
    for (const h of horizons) hdrHtml += `<th class="roll-mtm-base-hdr">Base</th><th class="roll-mtm-rolled-hdr">${scenLabel}</th>`;
    hdrHtml += `</tr>`;
    thead.innerHTML = hdrHtml;
  } else {
    thead.innerHTML = `<tr><th style="text-align:left">Spot</th>${horizons.map(h => `<th>${h}d</th>`).join("")}</tr>`;
  }

  // Pick spots at $500 increments, reversed
  const step = 500;
  const displaySpots = [];
  for (let s = step; s <= 7000; s += step) {
    const idx = spots.indexOf(s);
    if (idx !== -1) displaySpots.push({ spot: s, idx });
  }
  displaySpots.reverse();

  let closestSpot = displaySpots[0]?.spot ?? 0;
  let closestDiff = Infinity;
  for (const ds of displaySpots) {
    const diff = Math.abs(ds.spot - ethSpot);
    if (diff < closestDiff) { closestDiff = diff; closestSpot = ds.spot; }
  }

  // Pre-compute curves: baseline = full portfolio, scenario = included + rolls
  const baseCurves = {};
  const scenCurves = {};
  for (const h of horizons) {
    baseCurves[h] = rollBaselineCurve(spots, allPositions, h);
    if (hasScenarioChange) scenCurves[h] = rollSumCurve(spots, includedPositions, h, rollMap);
  }

  const fmtVal = v => {
    const r = Math.round(v);
    return r >= 0 ? `$${r.toLocaleString()}` : `-$${Math.abs(r).toLocaleString()}`;
  };

  const tbody = document.getElementById("roll-mtm-grid-body");
  let html = "";
  for (const { spot, idx } of displaySpots) {
    const isSpotRow = spot === closestSpot;
    html += `<tr class="${isSpotRow ? "roll-mtm-spot-row" : ""}">`;
    html += `<td style="text-align:left;font-weight:600;white-space:nowrap">$${spot.toLocaleString()}</td>`;
    for (const h of horizons) {
      const base = baseCurves[h][idx];
      const baseCls = base >= 0 ? "mtm-pos" : "mtm-neg";
      if (hasScenarioChange) {
        const scen = scenCurves[h][idx];
        const scenCls = scen >= 0 ? "mtm-pos" : "mtm-neg";
        html += `<td class="${baseCls} roll-mtm-base-cell">${fmtVal(base)}</td>`;
        html += `<td class="${scenCls} roll-mtm-rolled-cell">${fmtVal(scen)}</td>`;
      } else {
        html += `<td class="${baseCls}">${fmtVal(base)}</td>`;
      }
    }
    html += "</tr>";
  }
  tbody.innerHTML = html;
}

// ── Roll Analysis event wiring ──────────────────────────
document.getElementById("btn-roll-compute").addEventListener("click", rollCompute);

document.getElementById("btn-roll-select-all").addEventListener("click", () => {
  rollGetFilteredPositions().forEach(p => rollSelected.add(p.id));
  rollRenderTable();
});

document.getElementById("btn-roll-deselect-all").addEventListener("click", () => {
  rollSelected.clear();
  rollLastResults = null;
  rollRenderTable();
  document.getElementById("roll-results-section").style.display = "none";
  rollDrawChart(null);
  rollRenderMtmGrid(null);
});

document.getElementById("roll-check-all").addEventListener("change", (e) => {
  rollGetFilteredPositions().forEach(p => {
    if (e.target.checked) rollSelected.add(p.id); else rollSelected.delete(p.id);
  });
  rollRenderTable();
});

document.getElementById("roll-include-all").addEventListener("change", (e) => {
  rollGetFilteredPositions().forEach(p => {
    if (e.target.checked) rollIncluded.add(p.id); else rollIncluded.delete(p.id);
  });
  rollRenderTable();
  rollRefreshVisuals();
});

["roll-filter-expiry", "roll-filter-type", "roll-filter-side"].forEach(id => {
  document.getElementById(id).addEventListener("change", () => { if (pfData) rollRenderTable(); });
});

document.getElementById("roll-target-expiry").addEventListener("change", () => {
  if (pfData) rollRenderTable();
});

document.querySelectorAll("#roll-trades-table .sortable").forEach(th => {
  th.addEventListener("click", () => {
    const col = th.dataset.col;
    const primary = rollSortStack[0];
    if (primary && primary.col === col) {
      // Same column: toggle direction
      primary.asc = !primary.asc;
    } else {
      // New column: push old primary as secondary, new column becomes primary
      const prev = rollSortStack[0] || null;
      rollSortStack = [{ col, asc: true }];
      if (prev) rollSortStack.push(prev);
    }
    rollRenderTable();
  });
});

document.getElementById("btn-roll-expiring-10d").addEventListener("click", () => {
  if (!pfData) return;
  // Select for rolling only positions expiring within 10 days
  rollSelected.clear();
  pfData.positions.forEach(p => {
    if (p.days_remaining <= 10) rollSelected.add(p.id);
  });
  // Sort by DTE ascending so soonest show first
  rollSortStack = [{ col: "days_remaining", asc: true }];
  rollRenderTable();
});

document.getElementById("btn-roll-refresh").addEventListener("click", () => {
  rollLoaded = false;
  rollLastResults = null;
  rollSelected.clear();
  document.getElementById("roll-results-section").style.display = "none";
  // Re-fetch data from backend
  pfData = null;
  portfolioLoaded = false;
  rollInit();
});

["roll-show-baseline", "roll-show-scenario"].forEach(id => {
  document.getElementById(id).addEventListener("change", () => {
    rollDrawChart(rollLastResults);
  });
});

// ── Roll Export to Excel ────────────────────────────────
document.getElementById("btn-roll-export-xlsx").addEventListener("click", () => {
  if (!pfData) return;
  const positions = pfData.positions.filter(p => rollIncluded.has(p.id));
  if (positions.length === 0) { alert("No positions included for export."); return; }

  const spot = pfData.eth_spot;
  const globalExpiry = rollGetTargetExpiry();
  const rows = [];

  for (const pos of positions) {
    const isRolled = rollSelected.has(pos.id);
    const row = document.querySelector(`#roll-trades-body tr[data-pos-id="${pos.id}"]`);

    let newExpCode = null, newStrike = null, newAbsQty = null, newNetQty = null;
    let newMark = null, rollIvPct = null, rollCost = null;
    let closeValue = null, openValue = null;

    if (isRolled && row) {
      newExpCode = row.querySelector(".roll-expiry-select").value;
      newStrike = parseFloat(row.querySelector(".roll-strike-input").value) || pos.strike;
      newAbsQty = parseFloat(row.querySelector(".roll-qty-input").value);
      const sign = pos.net_qty >= 0 ? 1 : -1;
      newNetQty = isNaN(newAbsQty) ? pos.net_qty : sign * newAbsQty;
      const newDte = rollDteForExpiry(newExpCode);
      const T = Math.max(newDte, 0) / 365.25;
      rollIvPct = pfLookupIv(newDte, newStrike) ?? pos.iv_pct;
      const sigma = rollIvPct / 100;
      newMark = bsPrice(spot, newStrike, T, 0, sigma, pos.opt);
      const curMark = pos.mark_price_usd ?? 0;
      closeValue = pos.net_qty * curMark;
      openValue = newNetQty * newMark;
      rollCost = Math.round(closeValue - openValue);
    }

    rows.push({
      "Include": rollIncluded.has(pos.id) ? "Yes" : "",
      "Roll": isRolled ? "Yes" : "",
      "Instrument": pos.instrument,
      "Buy/Sell": pos.side_raw,
      "Type": pos.option_type,
      "Strike": pos.strike,
      "Expiry": pos.expiry,
      "DTE": Math.round(pos.days_remaining),
      "Net Qty": pos.net_qty,
      "Cur Mark": pos.mark_price_usd != null ? Math.round(pos.mark_price_usd * 100) / 100 : null,
      "IV%": pos.iv_pct,
      "MTM": pos.current_mtm != null ? Math.round(pos.current_mtm) : null,
      "New Expiry": newExpCode,
      "New Strike": newStrike,
      "New Qty": newNetQty,
      "New Mark": newMark != null ? Math.round(newMark * 100) / 100 : null,
      "IV Used": rollIvPct != null ? Math.round(rollIvPct * 10) / 10 : null,
      "Close Value": closeValue != null ? Math.round(closeValue) : null,
      "Open Value": openValue != null ? Math.round(openValue) : null,
      "Roll Cost": rollCost,
      "Direction": rollCost != null ? (rollCost >= 0 ? "Receive" : "Pay") : "",
    });
  }

  // Totals row
  const totalMtm = positions.reduce((s, p) => s + (p.current_mtm || 0), 0);
  const rolledRows = rows.filter(r => r["Roll"] === "Yes");
  const totalRollCost = rolledRows.reduce((s, r) => s + (r["Roll Cost"] || 0), 0);
  const totalClose = rolledRows.reduce((s, r) => s + (r["Close Value"] || 0), 0);
  const totalOpen = rolledRows.reduce((s, r) => s + (r["Open Value"] || 0), 0);
  rows.push({});
  rows.push({
    "Instrument": "TOTALS",
    "Net Qty": positions.reduce((s, p) => s + p.net_qty, 0),
    "MTM": Math.round(totalMtm),
    "Close Value": Math.round(totalClose),
    "Open Value": Math.round(totalOpen),
    "Roll Cost": totalRollCost,
    "Direction": totalRollCost ? (totalRollCost >= 0 ? "Net Receive" : "Net Pay") : "",
  });

  const wb = XLSX.utils.book_new();
  const ws = XLSX.utils.json_to_sheet(rows);
  ws["!cols"] = [
    { wch: 8 }, { wch: 5 }, { wch: 24 }, { wch: 8 }, { wch: 6 },
    { wch: 8 }, { wch: 12 }, { wch: 6 }, { wch: 10 }, { wch: 12 },
    { wch: 7 }, { wch: 12 }, { wch: 12 }, { wch: 10 }, { wch: 10 },
    { wch: 12 }, { wch: 8 }, { wch: 12 }, { wch: 12 }, { wch: 12 }, { wch: 10 },
  ];
  XLSX.utils.book_append_sheet(wb, ws, "Roll Analysis");
  const dateStr = new Date().toISOString().split("T")[0];
  XLSX.writeFile(wb, `PLGO_Roll_Analysis_${dateStr}.xlsx`);
});

// ─── Go! ────────────────────────────────────────────────────
init();