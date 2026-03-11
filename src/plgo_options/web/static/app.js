"use strict";

// ─── State ──────────────────────────────────────────────────
let currentAsset = "ETH";  // "ETH" or "FIL"
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

// ─── Chart theme helper ────────────────────────────────────
function chartColors() {
  const light = document.documentElement.dataset.theme === "light";
  return light
    ? { paper: "#ffffff", plot: "#f6f8fa", text: "#1f2328", muted: "#656d76", grid: "#d0d7de", zeroline: "#afb8c1", legendBg: "rgba(255,255,255,0.8)", legendBorder: "#d0d7de" }
    : { paper: "#161b22", plot: "#0d1117", text: "#e6edf3", muted: "#8b949e", grid: "#21262d", zeroline: "#30363d", legendBg: "rgba(22,27,34,0.8)", legendBorder: "#30363d" };
}

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

  // Load trade management on startup (it's the default page)
  tmLoad();
}

// ─── Payoff chart ───────────────────────────────────────────
function drawEmptyChart() {
  const layout = chartLayout();
  Plotly.newPlot("payoff-chart", [], layout, { responsive: true });
}

function chartLayout() {
  const cc = chartColors();
  return {
    title: { text: "Strategy Payoff at Expiry", font: { color: cc.text, size: 16 } },
    paper_bgcolor: cc.paper,
    plot_bgcolor:  cc.plot,
    xaxis: {
      title: "ETH Spot Price (USD)",
      color: cc.muted,
      gridcolor: cc.grid,
      zerolinecolor: cc.zeroline,
    },
    yaxis: {
      title: "P&L (USD)",
      color: cc.muted,
      gridcolor: cc.grid,
      zerolinecolor: "#f85149",
      zerolinewidth: 2,
    },
    margin: { t: 50, r: 30, b: 50, l: 60 },
    showlegend: true,
    legend: { font: { color: cc.muted } },
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
  const net = totalReceive - totalPay;
  const netEth = spot > 0 ? net / spot : 0;

  // Summary
  const expiryLabel = uniqueExpiries.length === 1
    ? uniqueExpiries[0]
    : uniqueExpiries.join(" / ");
  const fmtUsd = v => "$" + Math.abs(Math.round(v)).toLocaleString();
  const payColor = totalPay > 0 ? "var(--red)" : "var(--muted)";
  const rcvColor = totalReceive > 0 ? "var(--green)" : "var(--muted)";
  const netLabel = net >= 0 ? "Rcv" : "Pay";
  const netColor = net >= 0 ? "var(--green)" : "var(--red)";
  const $summary = document.getElementById("repl-summary");
  $summary.innerHTML =
    `<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.5rem">` +
    `<span>Expiry: <strong>${expiryLabel}</strong> &nbsp;|&nbsp; ETH = $${spot.toLocaleString()}</span>` +
    `<span style="display:flex;gap:1rem;font-size:.95rem">` +
    `<span>Pay: <strong style="color:${payColor}">${fmtUsd(totalPay)}</strong></span>` +
    `<span>Receive: <strong style="color:${rcvColor}">${fmtUsd(totalReceive)}</strong></span>` +
    `<span>Net: <strong style="color:${netColor}">${netLabel} ${fmtUsd(net)}</strong> (${netEth.toFixed(4)} ETH)</span>` +
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
      textfont: { color: chartColors().text, size: 10 },
    });
  }

  const title = smile.expiry_code
    ? `Deribit IV Smile — ${smile.expiry_code} (${smile.dte}d)`
    : "Deribit Implied Volatility Smile";

  const cc = chartColors();
  const layout = {
    title: { text: title, font: { color: cc.text, size: 16 } },
    paper_bgcolor: cc.paper,
    plot_bgcolor: cc.plot,
    xaxis: { title: "Strike (USD)", color: cc.muted, gridcolor: cc.grid },
    yaxis: { title: "Implied Volatility (%)", color: cc.muted, gridcolor: cc.grid },
    margin: { t: 50, r: 30, b: 50, l: 60 },
    showlegend: true,
    legend: { font: { color: cc.muted } },
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

// ─── Asset switcher ─────────────────────────────────────────
document.querySelectorAll(".asset-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    const asset = btn.dataset.asset;
    if (asset === currentAsset) return;
    currentAsset = asset;
    document.querySelectorAll(".asset-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("asset-label").textContent = asset;

    // Reset all page caches so they reload with new asset
    tmLoaded = false;
    portfolioLoaded = false;
    sbLoaded = false;
    pfData = null;

    // Update spot display
    if (asset === "ETH" && ethSpot) {
      document.getElementById("eth-spot").textContent = `$${ethSpot.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
    } else if (asset === "FIL") {
      document.getElementById("eth-spot").textContent = "N/A (no live feed)";
    }

    // Reload current page
    const activePage = document.querySelector(".nav-item.active");
    if (activePage) activePage.click();
  });
});

// ─── Sidebar page switching ─────────────────────────────────
document.querySelectorAll(".nav-item").forEach(item => {
  item.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach(i => i.classList.remove("active"));
    document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
    item.classList.add("active");
    const page = document.getElementById(`page-${item.dataset.page}`);
    if (page) page.classList.add("active");

    const pg = item.dataset.page;

    // Show/hide FIL under-construction banners
    const isFil = currentAsset === "FIL";
    const pricingBanner = document.getElementById("pricing-fil-banner");
    const rollBanner = document.getElementById("roll-fil-banner");
    if (pricingBanner) pricingBanner.style.display = (isFil && pg === "pricing") ? "" : "none";
    if (rollBanner) rollBanner.style.display = (isFil && pg === "roll") ? "" : "none";

    // Lazy-load pages
    if (pg === "trades" && !tmLoaded) tmLoad();
    if (pg === "pricing") {
      // Plotly may have rendered at 0 width while page was hidden — trigger resize
      setTimeout(() => {
        const pc = document.getElementById("payoff-chart");
        const sc = document.getElementById("smile-chart");
        if (pc && pc.data) Plotly.Plots.resize(pc);
        if (sc && sc.data) Plotly.Plots.resize(sc);
      }, 50);
    }
    if (pg === "portfolio" && !portfolioLoaded) loadPortfolio();
    if (pg === "roll" && !rollLoaded && !isFil) rollInit();
    if (pg === "structurer" && !sbLoaded) sbInit();
  });
});

// ─── Sub-tab switching (Roll & Optimizer page) ──────────────
document.querySelectorAll(".sub-tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    const parent = btn.closest(".page");
    parent.querySelectorAll(".sub-tab-btn").forEach(b => b.classList.remove("active"));
    parent.querySelectorAll(".sub-tab-pane").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
    const pane = document.getElementById(`subtab-${btn.dataset.subtab}`);
    if (pane) pane.classList.add("active");

    // Lazy-load optimizer
    if (btn.dataset.subtab === "optimizer" && !optLoaded) optInit();
  });
});

// ─── Theme toggle ───────────────────────────────────────────
(function initTheme() {
  const saved = localStorage.getItem("plgo-theme");
  if (saved) document.documentElement.dataset.theme = saved;
  updateThemeIcons();
})();

document.getElementById("btn-theme-toggle").addEventListener("click", () => {
  const current = document.documentElement.dataset.theme;
  const next = current === "light" ? "dark" : "light";
  if (next === "dark") {
    delete document.documentElement.dataset.theme;
  } else {
    document.documentElement.dataset.theme = next;
  }
  localStorage.setItem("plgo-theme", next);
  updateThemeIcons();
  recolorCharts();
});

function updateThemeIcons() {
  const isLight = document.documentElement.dataset.theme === "light";
  document.getElementById("icon-moon").style.display = isLight ? "none" : "block";
  document.getElementById("icon-sun").style.display = isLight ? "block" : "none";
}

function recolorCharts() {
  const cc = chartColors();
  const update = {
    paper_bgcolor: cc.paper,
    plot_bgcolor: cc.plot,
    "title.font.color": cc.text,
    "xaxis.color": cc.muted,
    "xaxis.gridcolor": cc.grid,
    "yaxis.color": cc.muted,
    "yaxis.gridcolor": cc.grid,
    "legend.font.color": cc.muted,
    "legend.bgcolor": cc.legendBg,
    "legend.bordercolor": cc.legendBorder,
    "xaxis.zerolinecolor": cc.zeroline,
    "xaxis.tickfont.color": cc.muted,
    "yaxis.tickfont.color": cc.muted,
  };
  document.querySelectorAll(".js-plotly-plot").forEach(el => {
    Plotly.relayout(el, update).catch(() => {});
  });
}

// ─── Trade Management ───────────────────────────────────────
let tmTrades = [];
let tmEnriched = [];
let tmLoaded = false;
let tmShowExpired = false;
let tmSortCol = "expiry";
let tmSortAsc = true;

async function tmLoad() {
  const tmBody = document.getElementById("tm-body");
  tmBody.innerHTML = '<tr><td colspan="22" style="text-align:center;padding:2rem;color:var(--muted)">Loading trades...</td></tr>';
  tmSelected.clear();

  try {
    // Fetch trades from DB
    const includeExp = document.getElementById("tm-show-expired").checked;
    const data = await get(`/api/trades/?include_expired=${includeExp}&asset=${currentAsset}`);
    tmTrades = data.trades || [];

    // Fetch enriched data from portfolio endpoint for live Greeks/MTM
    let enriched = [];
    try {
      const pfData = await get(`/api/portfolio/pnl?asset=${currentAsset}`);
      enriched = pfData.positions || [];
      ethSpot = pfData.eth_spot;
      document.getElementById("eth-spot").textContent = `$${ethSpot.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
    } catch (e) {
      console.warn("Could not fetch enriched data:", e);
    }

    // Merge DB trades with enriched data
    tmEnriched = tmTrades.map(t => {
      // Find matching enriched position by DB id
      const match = enriched.find(e => e.id === t.id);
      if (match) {
        return { ...t, ...match, db_id: t.id, db_status: t.status };
      }
      // No enrichment — fill defaults
      return {
        ...t,
        db_id: t.id,
        db_status: t.status,
        days_remaining: 0,
        pct_otm_live: 0,
        iv_pct: 0,
        delta: null, gamma: null, theta: null, vega: null,
        mark_price_usd: 0,
        current_mtm: 0,
      };
    });

    tmLoaded = true;
    tmRender();
  } catch (e) {
    tmBody.innerHTML = `<tr><td colspan="22" style="text-align:center;padding:2rem;color:var(--red)">Error: ${e.message}</td></tr>`;
  }
}

function tmRender() {
  tmRenderTable();
  tmRenderSummary();
  tmRenderInsights();
  tmUpdateBadge();
}

function tmRenderSummary() {
  const active = tmEnriched.filter(t => t.db_status === "active");
  const totalNotional = active.reduce((s, t) => s + (t.notional_mm || 0), 0);
  const totalMtm = active.reduce((s, t) => s + (t.current_mtm || 0), 0);
  const totalDelta = active.reduce((s, t) => s + ((t.delta || 0) * (t.net_qty || 0)), 0);
  const totalGamma = active.reduce((s, t) => s + ((t.gamma || 0) * (t.net_qty || 0)), 0);
  const totalTheta = active.reduce((s, t) => s + ((t.theta || 0) * (t.net_qty || 0)), 0);
  const totalVega = active.reduce((s, t) => s + ((t.vega || 0) * (t.net_qty || 0)), 0);

  document.getElementById("tm-count").textContent = active.length;

  // Format notional: convert from raw to $mm with commas
  const notionalRaw = active.reduce((s, t) => s + Math.abs(t.notional_mm || 0) * (t.qty || 0) * (ethSpot || 0), 0);
  const notionalMm = totalNotional;
  const $notional = document.getElementById("tm-notional");
  if (Math.abs(notionalMm) >= 1) {
    $notional.textContent = `$${notionalMm.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}mm`;
  } else {
    // Show in full USD if less than 1mm
    const fullUsd = active.reduce((s, t) => s + Math.abs((t.notional_mm || 0) * 1e6), 0);
    $notional.textContent = `$${fullUsd.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  }

  const $mtm = document.getElementById("tm-mtm");
  $mtm.textContent = `$${totalMtm.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  $mtm.style.color = totalMtm >= 0 ? "var(--green)" : "var(--red)";

  const $delta = document.getElementById("tm-delta");
  $delta.textContent = totalDelta.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  $delta.style.color = totalDelta >= 0 ? "var(--green)" : "var(--red)";

  const $gamma = document.getElementById("tm-gamma");
  $gamma.textContent = totalGamma.toLocaleString(undefined, { minimumFractionDigits: 4, maximumFractionDigits: 4 });

  const $theta = document.getElementById("tm-theta");
  $theta.textContent = totalTheta.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  $theta.style.color = totalTheta >= 0 ? "var(--green)" : "var(--red)";

  const $vega = document.getElementById("tm-vega");
  $vega.textContent = totalVega.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function tmRenderTable() {
  const tbody = document.getElementById("tm-body");
  let rows = [...tmEnriched];

  // Filters
  const fExpiry = document.getElementById("tm-filter-expiry").value;
  const fType = document.getElementById("tm-filter-type").value;
  const fSide = document.getElementById("tm-filter-side").value;
  const fSearch = document.getElementById("tm-search").value.toLowerCase().trim();

  if (fExpiry) rows = rows.filter(r => r.expiry === fExpiry);
  if (fType) rows = rows.filter(r => r.option_type === fType);
  if (fSide) rows = rows.filter(r => r.side === fSide);
  if (fSearch) rows = rows.filter(r =>
    (r.counterparty || "").toLowerCase().includes(fSearch) ||
    (r.instrument || "").toLowerCase().includes(fSearch) ||
    (r.trade_id || "").toLowerCase().includes(fSearch)
  );

  // Sort
  rows.sort((a, b) => {
    let va = a[tmSortCol], vb = b[tmSortCol];
    if (va == null) va = "";
    if (vb == null) vb = "";
    if (typeof va === "number" && typeof vb === "number") return tmSortAsc ? va - vb : vb - va;
    return tmSortAsc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
  });

  // Populate expiry filter
  const expiries = [...new Set(tmEnriched.map(t => t.expiry))].sort();
  const expSel = document.getElementById("tm-filter-expiry");
  const curExp = expSel.value;
  expSel.innerHTML = '<option value="">All</option>';
  expiries.forEach(e => {
    const o = document.createElement("option");
    o.value = e; o.textContent = e;
    if (e === curExp) o.selected = true;
    expSel.appendChild(o);
  });

  document.getElementById("tm-table-count").textContent = `(${rows.length})`;

  const fmt = (v, d = 2) => v != null && v !== "" ? Number(v).toFixed(d) : "--";
  const fmtK = (v) => v != null && v !== "" ? Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 }) : "--";
  const fmtMoney = (v) => v != null && v !== "" ? `$${Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}` : "--";
  const fmtMm = (v) => v != null && v !== "" ? `$${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}mm` : "--";

  tbody.innerHTML = rows.map(t => {
    const tid = t.db_id || t.id;
    const isExpired = t.db_status === "expired";
    const isSelected = tmSelected.has(tid);
    const rowClass = isExpired ? "tm-row-expired" : isSelected ? "tm-row-selected" : "";
    const statusClass = t.db_status === "active" ? "status-active" : t.db_status === "expired" ? "status-expired" : "status-deleted";
    const mtmColor = (t.current_mtm || 0) >= 0 ? "mtm-pos" : "mtm-neg";
    const sideColor = (t.side || "").toLowerCase().includes("buy") || (t.side || "").toLowerCase().includes("long") ? "qty-long" : "qty-short";

    return `<tr class="${rowClass}" data-tid="${tid}">
      <td><input type="checkbox" class="tm-row-check" data-tid="${tid}" ${isSelected ? "checked" : ""} ${isExpired ? "disabled" : ""}></td>
      <td><span class="status-dot ${statusClass}"></span></td>
      <td style="text-align:left">${t.counterparty || ""}</td>
      <td>${t.trade_date || ""}</td>
      <td style="text-align:left" class="${sideColor}">${t.side || ""}</td>
      <td style="text-align:left">${t.option_type || ""}</td>
      <td style="text-align:left">${t.instrument || ""}</td>
      <td>${t.expiry || ""}</td>
      <td>${t.days_remaining || 0}</td>
      <td>${fmtK(t.strike)}</td>
      <td>${fmt(t.pct_otm_live, 1)}%</td>
      <td>${fmtK(t.qty)}</td>
      <td>${fmtMm(t.notional_mm)}</td>
      <td>${fmtMoney(t.premium_usd)}</td>
      <td>${fmt(t.iv_pct, 1)}</td>
      <td>${fmt(t.delta, 4)}</td>
      <td>${fmt(t.gamma, 6)}</td>
      <td>${fmt(t.theta, 4)}</td>
      <td>${fmt(t.vega, 4)}</td>
      <td>${fmtMoney(t.mark_price_usd)}</td>
      <td class="${mtmColor}">${fmtMoney(t.current_mtm)}</td>
      <td>
        <div class="tm-actions">
          <button class="tm-action-btn" onclick="tmEditTrade(${t.db_id})" title="Edit">&#9998;</button>
          ${t.db_status === "active" ? `<button class="tm-action-btn btn-expire" onclick="tmExpireTrade(${t.db_id})" title="Expire">&#9201;</button>` : ""}
          <button class="tm-action-btn btn-delete" onclick="tmDeleteTrade(${t.db_id})" title="Delete">&#10005;</button>
          <button class="tm-action-btn" onclick="tmShowHistory(${t.db_id})" title="History">&#128336;</button>
        </div>
      </td>
    </tr>`;
  }).join("");

  // Wire row checkboxes
  tbody.querySelectorAll(".tm-row-check").forEach(cb => {
    cb.addEventListener("change", () => {
      const tid = parseInt(cb.dataset.tid);
      if (cb.checked) tmSelected.add(tid); else tmSelected.delete(tid);
      const row = cb.closest("tr");
      row.classList.toggle("tm-row-selected", cb.checked);
      tmUpdateBulkUI();
    });
  });
  tmUpdateBulkUI();
}

function tmRenderInsights() {
  const container = document.getElementById("tm-insights");
  const active = tmEnriched.filter(t => t.db_status === "active");
  const insights = [];

  // Upcoming expiries
  const expiringSoon = active.filter(t => t.days_remaining > 0 && t.days_remaining <= 7);
  const expiringMed = active.filter(t => t.days_remaining > 7 && t.days_remaining <= 14);
  if (expiringSoon.length > 0) {
    insights.push({
      type: "danger",
      title: "Expiring within 7 days",
      body: expiringSoon.map(t => `${t.instrument} (${t.days_remaining}d)`).join(", ")
    });
  }
  if (expiringMed.length > 0) {
    insights.push({
      type: "warning",
      title: "Expiring within 14 days",
      body: expiringMed.map(t => `${t.instrument} (${t.days_remaining}d)`).join(", ")
    });
  }

  // Concentrated positions
  const totalNotional = active.reduce((s, t) => s + Math.abs(t.notional_mm || 0), 0);
  if (totalNotional > 0) {
    const concentrated = active.filter(t => Math.abs(t.notional_mm || 0) / totalNotional > 0.25);
    if (concentrated.length > 0) {
      insights.push({
        type: "warning",
        title: "Concentrated positions (>25% of notional)",
        body: concentrated.map(t => `${t.instrument}: $${(t.notional_mm || 0).toFixed(2)}mm (${(Math.abs(t.notional_mm || 0) / totalNotional * 100).toFixed(0)}%)`).join(", ")
      });
    }
  }

  // Large unrealized P&L
  const largePnl = active.filter(t => t.premium_usd && Math.abs(t.current_mtm || 0) > 0.5 * Math.abs(t.premium_usd));
  if (largePnl.length > 0) {
    insights.push({
      type: "info",
      title: "Large unrealized P&L (>50% of premium)",
      body: largePnl.map(t => `${t.instrument}: MTM $${(t.current_mtm || 0).toLocaleString(undefined, { maximumFractionDigits: 0 })} vs premium $${(t.premium_usd || 0).toLocaleString(undefined, { maximumFractionDigits: 0 })}`).join("; ")
    });
  }

  // High IV
  const highIv = active.filter(t => (t.iv_pct || 0) > 100);
  if (highIv.length > 0) {
    insights.push({
      type: "info",
      title: "High IV trades (>100%)",
      body: highIv.map(t => `${t.instrument}: ${(t.iv_pct || 0).toFixed(1)}%`).join(", ")
    });
  }

  if (insights.length === 0) {
    insights.push({ type: "info", title: "No alerts", body: "All positions within normal parameters." });
  }

  container.innerHTML = insights.map(i => `
    <div class="insight-card insight-${i.type}">
      <div class="insight-title">${i.title}</div>
      <div class="insight-body">${i.body}</div>
    </div>
  `).join("");
}

function tmUpdateBadge() {
  const badge = document.getElementById("nav-badge-trades");
  const active = tmEnriched.filter(t => t.db_status === "active");
  badge.textContent = active.length || "";
}

// Sort handler for TM table
document.getElementById("tm-table").addEventListener("click", e => {
  const th = e.target.closest("th.sortable");
  if (!th) return;
  const col = th.dataset.col;
  if (tmSortCol === col) {
    tmSortAsc = !tmSortAsc;
  } else {
    tmSortCol = col;
    tmSortAsc = true;
  }
  // Update sort indicators
  document.querySelectorAll("#tm-table th.sortable").forEach(h => {
    h.classList.remove("sort-asc", "sort-desc");
  });
  th.classList.add(tmSortAsc ? "sort-asc" : "sort-desc");
  tmRenderTable();
});

// Filter handlers
["tm-filter-expiry", "tm-filter-type", "tm-filter-side"].forEach(id => {
  document.getElementById(id).addEventListener("change", () => tmRenderTable());
});
document.getElementById("tm-search").addEventListener("input", () => tmRenderTable());
document.getElementById("tm-show-expired").addEventListener("change", () => tmLoad());
document.getElementById("btn-tm-refresh").addEventListener("click", () => tmLoad());

// ─── Trade CRUD ─────────────────────────────────────────────
document.getElementById("btn-tm-new").addEventListener("click", () => tmOpenModal());
document.getElementById("btn-modal-cancel").addEventListener("click", () => tmCloseModal());

// ─── Multi-leg state ────────────────────────────────────────
let tfPreset = "single";
let tfLegs = []; // [{side, type, strike, premium_per, premium_usd}]

const TF_PRESETS = {
  single:       null,
  put_spread:   [{ side: "Buy", type: "Put", strike: 0 }, { side: "Sell", type: "Put", strike: 0 }],
  call_spread:  [{ side: "Buy", type: "Call", strike: 0 }, { side: "Sell", type: "Call", strike: 0 }],
  straddle:     [{ side: "Buy", type: "Call", strike: 0 }, { side: "Buy", type: "Put", strike: 0 }],
  strangle:     [{ side: "Buy", type: "Call", strike: 0 }, { side: "Buy", type: "Put", strike: 0 }],
  iron_condor:  [
    { side: "Buy", type: "Put", strike: 0 },
    { side: "Sell", type: "Put", strike: 0 },
    { side: "Sell", type: "Call", strike: 0 },
    { side: "Buy", type: "Call", strike: 0 },
  ],
};

// Preset buttons
document.querySelectorAll(".tf-preset").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tf-preset").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    tfPreset = btn.dataset.preset;
    tfApplyPreset();
  });
});

function tfApplyPreset() {
  const isSingle = tfPreset === "single";
  document.getElementById("tf-single-section").style.display = isSingle ? "" : "none";
  document.getElementById("tf-multi-section").style.display = isSingle ? "none" : "";

  if (!isSingle) {
    const template = TF_PRESETS[tfPreset];
    const spot = ethSpot || 0;
    // Pre-fill strikes relative to spot
    tfLegs = template.map((leg, i) => {
      let strike = leg.strike;
      if (spot > 0 && strike === 0) {
        if (tfPreset === "put_spread") strike = i === 0 ? Math.round(spot * 0.9) : Math.round(spot * 0.8);
        else if (tfPreset === "call_spread") strike = i === 0 ? Math.round(spot * 1.1) : Math.round(spot * 1.2);
        else if (tfPreset === "straddle") strike = Math.round(spot / 50) * 50;
        else if (tfPreset === "strangle") strike = i === 0 ? Math.round(spot * 1.1) : Math.round(spot * 0.9);
        else if (tfPreset === "iron_condor") {
          const offsets = [0.85, 0.9, 1.1, 1.15];
          strike = Math.round(spot * offsets[i]);
        }
      }
      return { ...leg, strike, premium_per: 0, premium_usd: 0 };
    });
    tfRenderLegs();
  }
}

function tfRenderLegs() {
  const tbody = document.getElementById("tf-legs-body");
  tbody.innerHTML = tfLegs.map((leg, i) => `<tr>
    <td style="color:var(--muted)">${i + 1}</td>
    <td><select class="tf-leg-side" data-idx="${i}">
      <option value="Buy" ${leg.side === "Buy" ? "selected" : ""}>Buy</option>
      <option value="Sell" ${leg.side === "Sell" ? "selected" : ""}>Sell</option>
    </select></td>
    <td><select class="tf-leg-type" data-idx="${i}">
      <option value="Call" ${leg.type === "Call" ? "selected" : ""}>Call</option>
      <option value="Put" ${leg.type === "Put" ? "selected" : ""}>Put</option>
    </select></td>
    <td><input type="number" class="tf-leg-strike" data-idx="${i}" value="${leg.strike}" step="any" min="0"></td>
    <td><input type="number" class="tf-leg-prem" data-idx="${i}" value="${leg.premium_per}" step="0.01"></td>
    <td><input type="number" class="tf-leg-premusd" data-idx="${i}" value="${leg.premium_usd}" step="0.01"></td>
    <td><button type="button" class="tf-leg-remove" data-idx="${i}">&times;</button></td>
  </tr>`).join("");

  // Wire leg inputs
  tbody.querySelectorAll(".tf-leg-side").forEach(el => el.addEventListener("change", () => { tfLegs[el.dataset.idx].side = el.value; }));
  tbody.querySelectorAll(".tf-leg-type").forEach(el => el.addEventListener("change", () => { tfLegs[el.dataset.idx].type = el.value; }));
  tbody.querySelectorAll(".tf-leg-strike").forEach(el => el.addEventListener("input", () => { tfLegs[el.dataset.idx].strike = parseFloat(el.value) || 0; }));
  tbody.querySelectorAll(".tf-leg-prem").forEach(el => el.addEventListener("input", () => { tfLegs[el.dataset.idx].premium_per = parseFloat(el.value) || 0; }));
  tbody.querySelectorAll(".tf-leg-premusd").forEach(el => el.addEventListener("input", () => { tfLegs[el.dataset.idx].premium_usd = parseFloat(el.value) || 0; }));
  tbody.querySelectorAll(".tf-leg-remove").forEach(el => el.addEventListener("click", () => {
    tfLegs.splice(parseInt(el.dataset.idx), 1);
    tfRenderLegs();
  }));
}

document.getElementById("btn-tf-add-leg").addEventListener("click", () => {
  tfLegs.push({ side: "Buy", type: "Call", strike: Math.round(ethSpot || 2000), premium_per: 0, premium_usd: 0 });
  tfRenderLegs();
});

// Auto-fill ref spot and compute % OTM / notional when strike or qty changes
function tfAutoCalc() {
  const spot = parseFloat(document.getElementById("tf-ref-spot").value) || 0;
  const strike = parseFloat(document.getElementById("tf-strike").value) || 0;
  const qty = parseFloat(document.getElementById("tf-qty").value) || 0;
  if (spot > 0 && strike > 0) {
    const otm = ((strike / spot) - 1) * 100;
    document.getElementById("tf-pct-otm").value = otm.toFixed(2);
  }
  if (spot > 0 && qty > 0) {
    document.getElementById("tf-notional").value = ((qty * spot) / 1e6).toFixed(4);
  }
}
["tf-strike", "tf-qty", "tf-ref-spot"].forEach(id => {
  document.getElementById(id).addEventListener("input", tfAutoCalc);
});

function tmOpenModal(trade = null) {
  const modal = document.getElementById("modal-trade");
  const title = document.getElementById("modal-trade-title");
  const form = document.getElementById("trade-form");
  const stratBar = document.getElementById("tf-strategy-bar");

  if (trade) {
    // Edit mode — single leg only, hide strategy bar
    title.textContent = `Edit Trade #${trade.id}`;
    stratBar.style.display = "none";
    document.getElementById("tf-single-section").style.display = "";
    document.getElementById("tf-multi-section").style.display = "none";
    tfPreset = "single";

    document.getElementById("tf-id").value = trade.id;
    document.getElementById("tf-counterparty").value = trade.counterparty || "";
    document.getElementById("tf-trade-date").value = trade.trade_date || "";
    document.getElementById("tf-side").value = trade.side || "Buy";
    document.getElementById("tf-option-type").value = trade.option_type || "Call";
    document.getElementById("tf-expiry").value = trade.expiry || "";
    document.getElementById("tf-strike").value = trade.strike || "";
    document.getElementById("tf-qty").value = trade.qty || "";
    document.getElementById("tf-ref-spot").value = trade.ref_spot || "";
    document.getElementById("tf-premium-per").value = trade.premium_per || "";
    document.getElementById("tf-premium-usd").value = trade.premium_usd || "";
    document.getElementById("tf-notional").value = trade.notional_mm || "";
    document.getElementById("tf-pct-otm").value = trade.pct_otm || "";
  } else {
    // New trade mode — show strategy bar, default to single
    title.textContent = "New Trade";
    form.reset();
    stratBar.style.display = "";
    document.getElementById("tf-id").value = "";
    document.getElementById("tf-trade-date").value = new Date().toISOString().split("T")[0];
    // Auto-fill ref spot from live price
    if (ethSpot) document.getElementById("tf-ref-spot").value = ethSpot;
    // Reset to single preset
    document.querySelectorAll(".tf-preset").forEach(b => b.classList.remove("active"));
    document.querySelector('.tf-preset[data-preset="single"]').classList.add("active");
    tfPreset = "single";
    tfLegs = [];
    document.getElementById("tf-single-section").style.display = "";
    document.getElementById("tf-multi-section").style.display = "none";
  }

  modal.style.display = "flex";
}

function tmCloseModal() {
  document.getElementById("modal-trade").style.display = "none";
}

document.getElementById("trade-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = document.getElementById("tf-id").value;
  const counterparty = document.getElementById("tf-counterparty").value;
  const trade_date = document.getElementById("tf-trade-date").value;
  const expiry = document.getElementById("tf-expiry").value;
  const ref_spot = parseFloat(document.getElementById("tf-ref-spot").value) || 0;

  // Basic validation
  if (tfPreset === "single" || id) {
    const strike = parseFloat(document.getElementById("tf-strike").value);
    const qty = parseFloat(document.getElementById("tf-qty").value);
    if (!strike || strike <= 0) { alert("Strike is required"); return; }
    if (!qty || qty <= 0) { alert("Qty is required"); return; }
  } else {
    const qty = parseFloat(document.getElementById("tf-multi-qty").value);
    if (!qty || qty <= 0) { alert("Qty per leg is required"); return; }
    if (tfLegs.length === 0) { alert("Add at least one leg"); return; }
    if (tfLegs.some(l => !l.strike || l.strike <= 0)) { alert("All legs need a strike"); return; }
  }
  if (!expiry) { alert("Expiry date is required"); return; }

  try {
    if (id) {
      // Edit single trade
      const data = {
        counterparty, trade_date, expiry, ref_spot,
        side: document.getElementById("tf-side").value,
        option_type: document.getElementById("tf-option-type").value,
        strike: parseFloat(document.getElementById("tf-strike").value) || 0,
        qty: parseFloat(document.getElementById("tf-qty").value) || 0,
        premium_per: parseFloat(document.getElementById("tf-premium-per").value) || 0,
        premium_usd: parseFloat(document.getElementById("tf-premium-usd").value) || 0,
        notional_mm: parseFloat(document.getElementById("tf-notional").value) || 0,
        pct_otm: parseFloat(document.getElementById("tf-pct-otm").value) || 0,
      };
      await api("PUT", `/api/trades/${id}`, data);
    } else if (tfPreset === "single") {
      // Create single leg
      const data = {
        asset: currentAsset, counterparty, trade_date, expiry, ref_spot,
        side: document.getElementById("tf-side").value,
        option_type: document.getElementById("tf-option-type").value,
        strike: parseFloat(document.getElementById("tf-strike").value) || 0,
        qty: parseFloat(document.getElementById("tf-qty").value) || 0,
        premium_per: parseFloat(document.getElementById("tf-premium-per").value) || 0,
        premium_usd: parseFloat(document.getElementById("tf-premium-usd").value) || 0,
        notional_mm: parseFloat(document.getElementById("tf-notional").value) || 0,
        pct_otm: parseFloat(document.getElementById("tf-pct-otm").value) || 0,
      };
      await post("/api/trades/", data);
    } else {
      // Create multi-leg strategy — one trade per leg
      const qty = parseFloat(document.getElementById("tf-multi-qty").value) || 0;
      for (const leg of tfLegs) {
        const otm = ref_spot > 0 ? ((leg.strike / ref_spot) - 1) * 100 : 0;
        const notional = ref_spot > 0 ? (qty * ref_spot) / 1e6 : 0;
        await post("/api/trades/", {
          asset: currentAsset, counterparty, trade_date, expiry, ref_spot,
          side: leg.side,
          option_type: leg.type,
          strike: leg.strike,
          qty,
          premium_per: leg.premium_per,
          premium_usd: leg.premium_usd,
          notional_mm: notional,
          pct_otm: otm,
        });
      }
    }
    tmCloseModal();
    tmInvalidatePortfolio();
    tmLoad();
  } catch (err) {
    alert("Failed to save trade: " + err.message);
  }
});

async function tmEditTrade(id) {
  const trade = tmTrades.find(t => t.id === id);
  if (trade) tmOpenModal(trade);
}

async function tmExpireTrade(id) {
  if (!confirm("Mark this trade as expired?")) return;
  try {
    await post(`/api/trades/${id}/expire`);
    tmInvalidatePortfolio();
    tmLoad();
  } catch (err) {
    alert("Failed to expire trade: " + err.message);
  }
}

async function tmDeleteTrade(id) {
  if (!confirm("Delete this trade? (soft delete)")) return;
  try {
    await api("DELETE", `/api/trades/${id}`);
    tmInvalidatePortfolio();
    tmLoad();
  } catch (err) {
    alert("Failed to delete trade: " + err.message);
  }
}

/** Invalidate cached portfolio/strategy builder so they re-fetch from DB on next visit. */
function tmInvalidatePortfolio() {
  portfolioLoaded = false;
  sbLoaded = false;
  pfData = null;
}

// ─── Bulk select & actions ──────────────────────────────────
let tmSelected = new Set();

function tmUpdateBulkUI() {
  const n = tmSelected.size;
  document.getElementById("btn-tm-bulk-expire").style.display = n > 0 ? "" : "none";
  document.getElementById("btn-tm-bulk-delete").style.display = n > 0 ? "" : "none";
  document.getElementById("btn-tm-bulk-expire").textContent = `Expire Selected (${n})`;
  document.getElementById("btn-tm-bulk-delete").textContent = `Delete Selected (${n})`;
  // Update header checkbox
  const visibleActive = tmEnriched.filter(t => t.db_status === "active");
  const allChecked = visibleActive.length > 0 && visibleActive.every(t => tmSelected.has(t.db_id || t.id));
  document.getElementById("tm-check-all").checked = allChecked;
}

document.getElementById("tm-check-all").addEventListener("change", (e) => {
  const visibleActive = tmEnriched.filter(t => t.db_status === "active");
  if (e.target.checked) {
    visibleActive.forEach(t => tmSelected.add(t.db_id || t.id));
  } else {
    tmSelected.clear();
  }
  tmRenderTable();
  tmUpdateBulkUI();
});

document.getElementById("btn-tm-bulk-expire").addEventListener("click", async () => {
  const ids = [...tmSelected];
  if (!confirm(`Expire ${ids.length} trade(s)?`)) return;
  try {
    await post("/api/trades/bulk-expire", { ids });
    tmSelected.clear();
    tmInvalidatePortfolio();
    tmLoad();
  } catch (err) {
    alert("Bulk expire failed: " + err.message);
  }
});

document.getElementById("btn-tm-bulk-delete").addEventListener("click", async () => {
  const ids = [...tmSelected];
  if (!confirm(`Delete ${ids.length} trade(s)?`)) return;
  try {
    for (const id of ids) await api("DELETE", `/api/trades/${id}`);
    tmSelected.clear();
    tmInvalidatePortfolio();
    tmLoad();
  } catch (err) {
    alert("Bulk delete failed: " + err.message);
  }
});

async function tmShowHistory(id) {
  const modal = document.getElementById("modal-history");
  document.getElementById("history-trade-id").textContent = `#${id}`;
  const tbody = document.getElementById("history-body");
  tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:1rem;color:var(--muted)">Loading...</td></tr>';
  modal.style.display = "flex";

  try {
    const data = await get(`/api/trades/${id}/history`);
    const history = data.history || [];
    if (history.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--muted)">No history found</td></tr>';
      return;
    }
    tbody.innerHTML = history.map(h => {
      const cls = `history-${h.action}`;
      return `<tr>
        <td style="text-align:left">${h.timestamp || ""}</td>
        <td style="text-align:left" class="${cls}">${h.action}</td>
        <td style="text-align:left">${h.field_changed || "--"}</td>
        <td style="text-align:left">${h.old_value || "--"}</td>
        <td style="text-align:left">${h.new_value || "--"}</td>
        <td style="text-align:left">${h.changed_by || ""}</td>
      </tr>`;
    }).join("");
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--red)">${err.message}</td></tr>`;
  }
}

document.getElementById("btn-history-close").addEventListener("click", () => {
  document.getElementById("modal-history").style.display = "none";
});

// Close modals on overlay click
["modal-trade", "modal-history"].forEach(id => {
  document.getElementById(id).addEventListener("click", e => {
    if (e.target === e.currentTarget) e.currentTarget.style.display = "none";
  });
});

// Export trades to Excel
document.getElementById("btn-tm-export").addEventListener("click", () => {
  if (!tmEnriched.length) return;
  const ws = XLSX.utils.json_to_sheet(tmEnriched.map(t => ({
    ID: t.db_id,
    Status: t.db_status,
    Counterparty: t.counterparty,
    "Trade Date": t.trade_date,
    Side: t.side,
    Type: t.option_type,
    Instrument: t.instrument,
    Expiry: t.expiry,
    DTE: t.days_remaining,
    Strike: t.strike,
    "% OTM": t.pct_otm_live,
    Qty: t.qty,
    "Notional ($mm)": t.notional_mm,
    "Premium USD": t.premium_usd,
    "IV%": t.iv_pct,
    Delta: t.delta,
    Gamma: t.gamma,
    Theta: t.theta,
    Vega: t.vega,
    "Mark Price": t.mark_price_usd,
    MTM: t.current_mtm,
  })));
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, "Trades");
  XLSX.writeFile(wb, `PLGO_Trades_${new Date().toISOString().split("T")[0]}.xlsx`);
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

  const cc = chartColors();
  const layout = {
    title: { text: "Net Exposure by Strike", font: { color: cc.text, size: 16 } },
    paper_bgcolor: cc.paper,
    plot_bgcolor: cc.plot,
    barmode: "group",
    xaxis: {
      title: "Strike (USD)",
      color: cc.muted,
      gridcolor: cc.grid,
      type: "category",
    },
    yaxis: {
      title: "Net Quantity (ETH Options)",
      color: cc.muted,
      gridcolor: cc.grid,
      zerolinecolor: "#f85149",
      zerolinewidth: 2,
    },
    margin: { t: 50, r: 30, b: 60, l: 60 },
    showlegend: true,
    legend: { font: { color: cc.muted } },
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

// ── Compare mode state ────────────────────────────────────
let pfCompareMode = false;
let pfExpiredData = null; // portfolio data including expired trades (for compare)
let pfOldSet = new Set();  // trade IDs in old portfolio (user-selected)
let pfNewSet = new Set();  // trade IDs in new portfolio (user-selected)

// ── Load ──────────────────────────────────────────────────
async function loadPortfolio() {
  const $btn = document.getElementById("btn-refresh-portfolio");
  $btn.classList.add("loading");
  $btn.textContent = "Loading…";

  const includeExpired = document.getElementById("pf-include-expired").checked;

  try {
    pfData = await get(`/api/portfolio/pnl?asset=${currentAsset}&include_expired=${includeExpired}`);
    pfSelected = new Set(pfData.positions.filter(p => p.db_status === "active").map(p => p.id));
    pfRolled = new Map();

    // Show/hide compare button when expired trades are included
    const hasExpired = pfData.positions.some(p => p.db_status === "expired");
    document.getElementById("btn-pf-compare").style.display = hasExpired ? "" : "none";
    if (!hasExpired) pfCompareMode = false;

    // Populate expiry filter
    const expiries = [...new Set(pfData.positions.map(p => p.expiry.split("T")[0]))].sort();
    const $expF = document.getElementById("pf-filter-expiry");
    $expF.innerHTML = '<option value="">All</option>';
    expiries.forEach(e => {
      const opt = document.createElement("option");
      opt.value = e; opt.textContent = e;
      $expF.appendChild(opt);
    });

    // Show/hide no-live-data banner for FIL
    const banner = document.getElementById("pf-no-live-banner");
    if (banner) banner.style.display = pfData.no_live_data ? "" : "none";
    document.getElementById("pf-spot-label").textContent = `${currentAsset} Spot`;

    pfRenderAll();
    portfolioLoaded = true;
  } catch (e) {
    console.error("Failed to load portfolio:", e);
    if (e.message && e.message.includes("404")) {
      // No trades for this asset yet
      const banner = document.getElementById("pf-no-live-banner");
      if (banner) banner.style.display = "none";
      portfolioLoaded = true;
      return;
    }
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
    const isExpired = pos.db_status === "expired";
    const rowClass = (isExpired ? "pf-row-expired " : "") + (!checked ? "pf-row-excluded" : "") + (rolled ? " pf-row-rolled" : "");
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
    const firstCol = pfCompareMode
      ? `<td style="white-space:nowrap;font-size:.7rem">` +
        `<label style="color:var(--orange);margin-right:4px"><input type="checkbox" class="pf-old-check" data-id="${pos.id}" ${pfOldSet.has(pos.id) ? "checked" : ""}> Old</label>` +
        `<label style="color:var(--accent)"><input type="checkbox" class="pf-new-check" data-id="${pos.id}" ${pfNewSet.has(pos.id) ? "checked" : ""}> New</label></td>`
      : `<td><input type="checkbox" class="pf-check" data-id="${pos.id}" ${checked ? "checked" : ""}></td>`;
    tr.innerHTML = firstCol +
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

  // Compare mode: Old/New checkboxes
  $tbody.querySelectorAll(".pf-old-check").forEach(cb => {
    cb.addEventListener("change", () => {
      const id = parseInt(cb.dataset.id);
      if (cb.checked) pfOldSet.add(id); else pfOldSet.delete(id);
      pfRenderPayoffChart();
    });
  });
  $tbody.querySelectorAll(".pf-new-check").forEach(cb => {
    cb.addEventListener("change", () => {
      const id = parseInt(cb.dataset.id);
      if (cb.checked) pfNewSet.add(id); else pfNewSet.delete(id);
      pfRenderPayoffChart();
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

function pfSumCurveForSet(positionIds, horizon) {
  const key = String(horizon);
  const spots = pfData.spot_ladder;
  const result = new Array(spots.length).fill(0);
  for (const p of pfData.positions) {
    if (!positionIds.has(p.id)) continue;
    const curve = p.payoff_by_horizon[key];
    if (curve) for (let i = 0; i < spots.length; i++) result[i] += curve[i];
  }
  return result;
}

/** Initialize compare sets with sensible defaults:
 *  Old = expired trades, New = active trades. User can override.
 */
function pfInitCompareSets() {
  pfOldSet = new Set();
  pfNewSet = new Set();
  for (const p of pfData.positions) {
    if (p.db_status === "expired") {
      pfOldSet.add(p.id);
    } else {
      pfNewSet.add(p.id);
    }
  }
}

function pfRenderPayoffChart() {
  const spots = pfData.spot_ladder;
  const horizons = pfData.chart_horizons;
  const hasScenarioChange = pfSelected.size !== pfData.positions.length || pfRolled.size > 0;

  const colors = ["#8b949e", "#f85149", "#d29922", "#3fb950", "#58a6ff", "#bc8cff"];
  const scenColors = ["#f85149", "#d29922", "#e3b341", "#3fb950", "#58a6ff", "#bc8cff"];
  const expiredColors = ["#f0883e", "#da3633", "#d29922", "#e3b341", "#f78166"];
  const activeColors = ["#58a6ff", "#3fb950", "#bc8cff", "#79c0ff", "#56d364"];
  const traces = [];

  if (pfCompareMode) {
    // ── Compare mode: Old portfolio vs New portfolio ──────────
    // Old portfolio curves (dotted) — user-selected
    horizons.forEach((h, i) => {
      const curve = pfSumCurveForSet(pfOldSet, h);
      const label = h === 0 ? "Old Portfolio: Expiry" : `Old Portfolio: T+${h}d`;
      traces.push({
        x: spots, y: curve,
        type: "scatter", mode: "lines",
        name: label,
        line: { color: expiredColors[i % expiredColors.length], width: 2, dash: "dot" },
        legendgroup: "old",
      });
    });

    // New portfolio curves (solid) — user-selected
    horizons.forEach((h, i) => {
      const curve = pfSumCurveForSet(pfNewSet, h);
      const label = h === 0 ? "New Portfolio: Expiry" : `New Portfolio: T+${h}d`;
      traces.push({
        x: spots, y: curve,
        type: "scatter", mode: "lines",
        name: label,
        line: { color: activeColors[i % activeColors.length], width: 2.5 },
        legendgroup: "new",
      });
    });
  } else {
    // ── Normal mode ──────────────────────────────────────────
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

  const cc = chartColors();
  const assetLabel = currentAsset + " Spot Price (USD)";
  const titleText = pfCompareMode
    ? "Portfolio Comparison — Old Portfolio (dotted) vs New Portfolio (solid)"
    : hasScenarioChange
      ? "Portfolio Payoff — Baseline (dotted) vs Scenario (solid)"
      : "Portfolio Payoff Profile — All Positions";
  const layout = {
    title: {
      text: titleText,
      font: { color: cc.text, size: 16 },
    },
    paper_bgcolor: cc.paper, plot_bgcolor: cc.plot,
    xaxis: { title: assetLabel, color: cc.muted, gridcolor: cc.grid, zerolinecolor: cc.zeroline, dtick: currentAsset === "FIL" ? 1 : 500 },
    yaxis: { title: "Portfolio P&L (USD)", color: cc.muted, gridcolor: cc.grid, zerolinecolor: "#f85149", zerolinewidth: 2 },
    margin: { t: 50, r: 200, b: 50, l: 80 },
    showlegend: true,
    legend: {
      font: { color: cc.muted, size: 10 },
      orientation: "v",
      x: 1.02, y: 1,
      xanchor: "left", yanchor: "top",
      bgcolor: cc.legendBg,
      bordercolor: cc.legendBorder,
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

document.getElementById("pf-include-expired").addEventListener("change", () => {
  portfolioLoaded = false;
  loadPortfolio();
});

document.getElementById("btn-pf-compare").addEventListener("click", () => {
  pfCompareMode = !pfCompareMode;
  const btn = document.getElementById("btn-pf-compare");
  btn.textContent = pfCompareMode ? "Exit Compare" : "Compare Old vs New";
  btn.classList.toggle("active", pfCompareMode);
  if (pfCompareMode) pfInitCompareSets();
  pfRenderTable();
  pfRenderPayoffChart();
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
      pfData = await get(`/api/portfolio/pnl?asset=${currentAsset}`);
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
  const newType = row.querySelector(".roll-type-select").value;
  const sign = pos.net_qty >= 0 ? 1 : -1;
  const newNetQty = isNaN(newAbsQty) ? pos.net_qty : sign * newAbsQty;
  const newDte = rollDteForExpiry(newExpCode);
  const T = Math.max(newDte, 0) / 365.25;
  const iv = pfLookupIv(newDte, newStrike) ?? pos.iv_pct;
  const sigma = iv / 100;
  const nm = bsPrice(pfData.eth_spot, newStrike, T, 0, sigma, newType);
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
      `<td style="text-align:left;font-size:.72rem">${pos.counterparty || ""}</td>` +
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
        `value="${pos.strike}" step="any" style="width:100%;font-size:.75rem;padding:.2rem .3rem;text-align:center"></td>` +
      `<td><input type="number" class="roll-qty-input" data-id="${pos.id}" ` +
        `value="${absQty}" step="1" min="0" style="width:100%;font-size:.75rem;padding:.2rem .3rem;text-align:center"></td>` +
      `<td><select class="roll-type-select" data-id="${pos.id}" style="font-size:.72rem;width:100%;margin:0">` +
        `<option value="C" ${pos.opt === "C" ? "selected" : ""}>Call</option>` +
        `<option value="P" ${pos.opt === "P" ? "selected" : ""}>Put</option></select></td>` +
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
  $tbody.querySelectorAll(".roll-expiry-select, .roll-strike-input, .roll-qty-input, .roll-type-select").forEach(el => {
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
    const newType = row ? row.querySelector(".roll-type-select").value : pos.opt;
    const sign = pos.net_qty >= 0 ? 1 : -1;
    const newNetQty = isNaN(newAbsQty) ? pos.net_qty : sign * newAbsQty;
    const newDte = rollDteForExpiry(newExpCode);
    const T = Math.max(newDte, 0) / 365.25;
    const rollIvPct = pfLookupIv(newDte, newStrike) ?? pos.iv_pct;
    const sigma = rollIvPct / 100;
    const newMark = bsPrice(spot, newStrike, T, 0, sigma, newType);
    const curMark = pos.mark_price_usd ?? 0;

    // Close current position fully, open new at (possibly different) qty
    const closeValue = pos.net_qty * curMark;
    const openValue = newNetQty * newMark;
    const cost = closeValue - openValue;

    // Breakdown: DTE impact vs strike impact vs type impact (at original qty for comparison)
    const strikeChanged = newStrike !== pos.strike;
    const qtyChanged = newAbsQty !== Math.abs(pos.net_qty);
    const typeChanged = newType !== pos.opt;
    let dteImpact = cost, strikeImpact = 0, qtyImpact = 0, typeImpact = 0;
    if (strikeChanged || qtyChanged || typeChanged) {
      // DTE-only: same strike, same qty, same type, new DTE
      const dteIv = pfLookupIv(newDte, pos.strike) ?? pos.iv_pct;
      const dteOnlyMark = bsPrice(spot, pos.strike, T, 0, dteIv / 100, pos.opt);
      const dteOnlyCost = pos.net_qty * curMark - pos.net_qty * dteOnlyMark;
      dteImpact = dteOnlyCost;

      // Strike change: same qty, same type, new strike vs old strike at new DTE
      const strikeOnlyMark = bsPrice(spot, newStrike, T, 0, sigma, pos.opt);
      const strikeOnlyCost = pos.net_qty * dteOnlyMark - pos.net_qty * strikeOnlyMark;
      strikeImpact = strikeChanged ? strikeOnlyCost : 0;

      // Type change: same qty, new strike, new type vs old type at new DTE
      const typeOnlyCost = pos.net_qty * strikeOnlyMark - pos.net_qty * newMark;
      typeImpact = typeChanged ? typeOnlyCost : 0;

      // Qty change: remainder
      qtyImpact = cost - dteImpact - strikeImpact - typeImpact;
    }

    totalCloseValue += closeValue;
    totalOpenValue += openValue;
    totalRollCost += cost;
    results.push({ pos, newExpCode, newDte, newStrike, newNetQty, newType, curMark, newMark, rollIvPct,
      cost, closeValue, openValue, strikeChanged, qtyChanged, typeChanged,
      dteImpact, strikeImpact, qtyImpact, typeImpact });
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

  // Check if any strike, qty, or type changed — show breakdown columns
  const hasStrikeChange = results.some(r => r.strikeChanged);
  const hasQtyChange = results.some(r => r.qtyChanged);
  const hasTypeChange = results.some(r => r.typeChanged);
  const hasBreakdown = hasStrikeChange || hasQtyChange || hasTypeChange;

  // Dynamic header
  const $thead = document.getElementById("roll-results-thead");
  let hdr = `<tr>
    <th style="text-align:left">Instrument</th>
    <th style="text-align:left">Side</th>
    <th>Strike</th>
    <th>Type</th>
    <th>Qty</th>
    <th>Cur DTE</th>
    <th>New Expiry</th>
    <th>Cur Mark</th>
    <th>New Mark</th>
    <th>IV Used</th>`;
  if (hasBreakdown) {
    hdr += `<th title="Cost from DTE change only (same strike, same qty, same type)">DTE Impact</th>`;
    if (hasStrikeChange) hdr += `<th title="Additional cost from strike change">Strike Impact</th>`;
    if (hasTypeChange) hdr += `<th title="Additional cost from type change (C↔P)">Type Impact</th>`;
    if (hasQtyChange) hdr += `<th title="Additional cost from qty change">Qty Impact</th>`;
  }
  hdr += `<th>Total Cost</th></tr>`;
  $thead.innerHTML = hdr;

  const fmtNum = (v, d=2) => v != null ? v.toLocaleString(undefined, { maximumFractionDigits: d }) : "---";
  const $tbody = document.getElementById("roll-results-body");
  $tbody.innerHTML = "";

  let totalDteImpact = 0, totalStrikeImpact = 0, totalQtyImpact = 0, totalTypeImpact = 0;

  for (const r of results) {
    totalDteImpact += r.dteImpact;
    totalStrikeImpact += r.strikeImpact;
    totalQtyImpact += r.qtyImpact;
    totalTypeImpact += r.typeImpact || 0;
    const sideClass = r.pos.side === "Long" ? "qty-long" : "qty-short";
    const strikeLabel = r.strikeChanged
      ? `${fmtNum(r.pos.strike, 0)} → ${fmtNum(r.newStrike, 0)}`
      : fmtNum(r.pos.strike, 0);
    const qtyLabel = r.qtyChanged
      ? `${fmtNum(r.pos.net_qty, 0)} → ${fmtNum(r.newNetQty, 0)}`
      : fmtNum(r.pos.net_qty, 0);
    const typeLabel = r.typeChanged
      ? `${r.pos.opt === "C" ? "Call" : "Put"} → ${r.newType === "C" ? "Call" : "Put"}`
      : (r.pos.opt === "C" ? "Call" : "Put");

    const typeColor = r.typeChanged ? "var(--accent)" : "inherit";
    let rowHtml =
      `<td style="text-align:left;font-size:.72rem">${r.pos.instrument}</td>` +
      `<td style="text-align:left" class="${sideClass}">${r.pos.side_raw}</td>` +
      `<td style="font-family:monospace">${strikeLabel}</td>` +
      `<td style="color:${typeColor}">${typeLabel}</td>` +
      `<td style="font-family:monospace">${qtyLabel}</td>` +
      `<td>${Math.round(r.pos.days_remaining)}d</td>` +
      `<td>${r.newExpCode} (${r.newDte}d)</td>` +
      `<td style="font-family:monospace">$${fmtNum(r.curMark)}</td>` +
      `<td style="font-family:monospace">$${fmtNum(r.newMark)}</td>` +
      `<td>${r.rollIvPct.toFixed(1)}%</td>`;
    if (hasBreakdown) {
      rowHtml += `<td>${fmtCost(r.dteImpact)}</td>`;
      if (hasStrikeChange) rowHtml += `<td>${r.strikeChanged ? fmtCost(r.strikeImpact) : '<span style="color:var(--muted)">—</span>'}</td>`;
      if (hasTypeChange) rowHtml += `<td>${r.typeChanged ? fmtCost(r.typeImpact) : '<span style="color:var(--muted)">—</span>'}</td>`;
      if (hasQtyChange) rowHtml += `<td>${r.qtyChanged ? fmtCost(r.qtyImpact) : '<span style="color:var(--muted)">—</span>'}</td>`;
    }
    rowHtml += `<td>${fmtCost(r.cost)}</td>`;

    const tr = document.createElement("tr");
    tr.innerHTML = rowHtml;
    $tbody.appendChild(tr);
  }

  // Footer
  const $tfoot = document.getElementById("roll-results-foot");
  let footHtml = `<tr><td style="text-align:left;font-weight:700" colspan="5">TOTAL</td>` +
    `<td></td><td></td><td></td><td></td><td></td>`;
  if (hasBreakdown) {
    footHtml += `<td>${fmtCost(totalDteImpact)}</td>`;
    if (hasStrikeChange) footHtml += `<td>${fmtCost(totalStrikeImpact)}</td>`;
    if (hasTypeChange) footHtml += `<td>${fmtCost(totalTypeImpact)}</td>`;
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
      const optType = rolled.newOpt || p.opt;
      for (let i = 0; i < spotLadder.length; i++) {
        result[i] += qty * bsPrice(spotLadder[i], rolled.newStrike, T, 0, sigma, optType);
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
      newOpt: r.newType || r.pos.opt,
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

  const cc = chartColors();
  const layout = {
    title: { text: titleText, font: { color: cc.text, size: 16 } },
    paper_bgcolor: cc.paper, plot_bgcolor: cc.plot,
    xaxis: { title: "ETH Spot Price (USD)", color: cc.muted, gridcolor: cc.grid, zerolinecolor: cc.zeroline, dtick: 500 },
    yaxis: { title: "Portfolio P&L (USD)", color: cc.muted, gridcolor: cc.grid, zerolinecolor: "#f85149", zerolinewidth: 2 },
    margin: { t: 50, r: 200, b: 50, l: 80 },
    showlegend: true,
    legend: {
      font: { color: cc.muted, size: 10 },
      orientation: "v", x: 1.02, y: 1, xanchor: "left", yanchor: "top",
      bgcolor: cc.legendBg, bordercolor: cc.legendBorder, borderwidth: 1,
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

// ── Roll Add Trade ───────────────────────────────────────
document.getElementById("btn-roll-add-trade").addEventListener("click", () => {
  const $form = document.getElementById("roll-add-trade-form");
  $form.style.display = $form.style.display === "none" ? "block" : "none";
  if ($form.style.display === "block") {
    // Populate counterparty dropdown from existing positions
    if (pfData) {
      const $cp = document.getElementById("roll-add-counterparty");
      const existing = new Set();
      pfData.positions.forEach(p => { if (p.counterparty) existing.add(p.counterparty); });
      existing.add("What-If");
      const prev = $cp.value;
      $cp.innerHTML = [...existing].sort().map(c =>
        `<option value="${c}" ${c === prev ? "selected" : ""}>${c}</option>`
      ).join("");
    }
    document.getElementById("roll-add-instrument").focus();
  }
});

document.getElementById("btn-roll-add-cancel").addEventListener("click", () => {
  document.getElementById("roll-add-trade-form").style.display = "none";
});

document.getElementById("btn-roll-add-confirm").addEventListener("click", () => {
  const instrument = document.getElementById("roll-add-instrument").value.trim().toUpperCase();
  if (!instrument) { alert("Enter an instrument name."); return; }
  const counterparty = document.getElementById("roll-add-counterparty").value;
  const side = document.getElementById("roll-add-side").value;
  const qty = parseFloat(document.getElementById("roll-add-qty").value) || 0;
  const premUsd = parseFloat(document.getElementById("roll-add-premium").value) || 0;
  if (qty <= 0) { alert("Quantity must be positive."); return; }
  if (!pfData) { alert("Portfolio data not loaded yet."); return; }

  get(`/api/portfolio/ticker/${encodeURIComponent(instrument)}`)
    .then(tk => {
      const sign = side === "sell" ? -1 : 1;
      const signedQty = sign * qty;
      const markUsd = tk.mark_price_usd || 0;
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
      const mtmHorizon = pfData.matrix_horizons.map(h => {
        const T = Math.max(dte - h, 0) / 365.25;
        const val = bsPrice(pfData.eth_spot, strike, T, 0, sigma, opt);
        return Math.round(signedQty * val * 100) / 100;
      });

      const newPos = {
        id: pfNextId++,
        counterparty: counterparty,
        trade_id: null,
        trade_date: new Date().toISOString().split("T")[0],
        side_raw: side === "sell" ? "Sell" : "Buy",
        option_type: opt === "C" ? "Call" : "Put",
        instrument: instrument,
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
        current_mtm: Math.round(signedQty * markUsd * 100) / 100,
        notional_live: Math.round(qty * pfData.eth_spot * 100) / 100,
        mtm_by_horizon: mtmHorizon,
        payoff_by_horizon: payoff,
      };

      pfData.positions.push(newPos);
      pfSelected.add(newPos.id);
      rollIncluded.add(newPos.id);
      rollRenderTable();
      rollRefreshVisuals();

      document.getElementById("roll-add-trade-form").style.display = "none";
      document.getElementById("roll-add-instrument").value = "";
    })
    .catch(e => {
      console.error("Failed to fetch ticker:", e);
      alert("Failed to fetch instrument data from Deribit.\n" + e.message);
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
    let newMark = null, rollIvPct = null, rollCost = null, newType = null;
    let closeValue = null, openValue = null;

    if (isRolled && row) {
      newExpCode = row.querySelector(".roll-expiry-select").value;
      newStrike = parseFloat(row.querySelector(".roll-strike-input").value) || pos.strike;
      newAbsQty = parseFloat(row.querySelector(".roll-qty-input").value);
      newType = row.querySelector(".roll-type-select").value;
      const sign = pos.net_qty >= 0 ? 1 : -1;
      newNetQty = isNaN(newAbsQty) ? pos.net_qty : sign * newAbsQty;
      const newDte = rollDteForExpiry(newExpCode);
      const T = Math.max(newDte, 0) / 365.25;
      rollIvPct = pfLookupIv(newDte, newStrike) ?? pos.iv_pct;
      const sigma = rollIvPct / 100;
      newMark = bsPrice(spot, newStrike, T, 0, sigma, newType);
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
      "New Type": newType ? (newType === "C" ? "Call" : "Put") : null,
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

// ═══════════════════════════════════════════════════════════
// ═══  OPTIMIZER TAB  ═══════════════════════════════════════
// ═══════════════════════════════════════════════════════════

let optLoaded = false;
let optSelected = new Set();
let optScenarios = [];
let optHighlightIdx = -1;

async function optInit() {
  if (!pfData) {
    try {
      pfData = await get(`/api/portfolio/pnl?asset=${currentAsset}`);
      portfolioLoaded = true;
      if (pfSelected.size === 0) pfSelected = new Set(pfData.positions.map(p => p.id));
    } catch (e) {
      console.error("Failed to load portfolio for optimizer:", e);
      alert("Failed to load portfolio data.\n" + e.message);
      return;
    }
  }
  optPopulate();
}

function optPopulate() {
  const expiries = [...new Set(pfData.positions.map(p => p.expiry.split("T")[0]))].sort();
  const $expF = document.getElementById("opt-filter-expiry");
  $expF.innerHTML = '<option value="">All</option>';
  expiries.forEach(e => { const o = document.createElement("option"); o.value = e; o.textContent = e; $expF.appendChild(o); });

  optSelected = new Set();
  optScenarios = [];
  optHighlightIdx = -1;
  optRenderPositions();
  optUpdateSelectionSummary();
  document.getElementById("opt-results-section").style.display = "none";
  document.getElementById("opt-payoff-chart").style.display = "none";
  document.getElementById("opt-detail-section").style.display = "none";
  optLoaded = true;
}

function optGetFilteredPositions() {
  const fExpiry = document.getElementById("opt-filter-expiry").value;
  const fType = document.getElementById("opt-filter-type").value;
  const fSide = document.getElementById("opt-filter-side").value;
  let list = [...pfData.positions];
  if (fExpiry) list = list.filter(p => p.expiry.split("T")[0] === fExpiry);
  if (fType) list = list.filter(p => p.opt === fType);
  if (fSide) list = list.filter(p => p.side === fSide);
  list.sort((a, b) => a.days_remaining - b.days_remaining);
  return list;
}

function optRenderPositions() {
  const list = optGetFilteredPositions();
  document.getElementById("opt-table-count").textContent = `(${list.length} positions)`;
  const allChecked = list.length > 0 && list.every(p => optSelected.has(p.id));
  document.getElementById("opt-check-all").checked = allChecked;

  const fmtNum = (v, d=0) => v != null ? v.toLocaleString(undefined, { maximumFractionDigits: d }) : "---";
  const $tbody = document.getElementById("opt-positions-body");
  $tbody.innerHTML = "";

  for (const pos of list) {
    const checked = optSelected.has(pos.id);
    const typeColor = pos.opt === "C" ? "var(--green)" : "var(--red)";
    const sideClass = pos.side === "Long" ? "qty-long" : "qty-short";
    const curMark = pos.mark_price_usd ?? 0;
    const tr = document.createElement("tr");
    if (checked) tr.classList.add("roll-row-selected");
    tr.innerHTML =
      `<td><input type="checkbox" class="opt-check" data-id="${pos.id}" ${checked ? "checked" : ""}></td>` +
      `<td style="text-align:left;font-size:.72rem">${pos.counterparty || ""}</td>` +
      `<td style="text-align:left;font-size:.72rem">${pos.instrument}</td>` +
      `<td style="text-align:left" class="${sideClass}">${pos.side_raw}</td>` +
      `<td style="text-align:left;color:${typeColor}">${pos.opt === "C" ? "Call" : "Put"}</td>` +
      `<td style="font-family:monospace">${fmtNum(pos.strike)}</td>` +
      `<td>${pos.expiry}</td>` +
      `<td>${Math.round(pos.days_remaining)}</td>` +
      `<td style="font-family:monospace">${fmtNum(pos.net_qty)}</td>` +
      `<td style="font-family:monospace">$${fmtNum(curMark, 2)}</td>` +
      `<td>${pos.iv_pct != null ? pos.iv_pct.toFixed(1) + "%" : "---"}</td>` +
      `<td class="${pos.current_mtm >= 0 ? 'mtm-pos' : 'mtm-neg'}" style="font-family:monospace">$${fmtNum(pos.current_mtm)}</td>` +
      `<td style="font-family:monospace">${pos.delta != null ? pos.delta.toFixed(3) : "---"}</td>`;
    $tbody.appendChild(tr);
  }

  $tbody.querySelectorAll(".opt-check").forEach(cb => {
    cb.addEventListener("change", () => {
      const id = parseInt(cb.dataset.id);
      if (cb.checked) optSelected.add(id); else optSelected.delete(id);
      cb.closest("tr").classList.toggle("roll-row-selected", cb.checked);
      const visible = optGetFilteredPositions();
      document.getElementById("opt-check-all").checked = visible.length > 0 && visible.every(p => optSelected.has(p.id));
      optUpdateSelectionSummary();
    });
  });
}

function optUpdateSelectionSummary() {
  const sel = pfData.positions.filter(p => optSelected.has(p.id));
  document.getElementById("opt-sel-count").textContent = sel.length;
  const totalMtm = sel.reduce((s, p) => s + (p.current_mtm || 0), 0);
  const $mtm = document.getElementById("opt-sel-mtm");
  $mtm.textContent = `$${Math.round(totalMtm).toLocaleString()}`;
  $mtm.className = `risk-value ${totalMtm >= 0 ? "mtm-pos" : "mtm-neg"}`;
  const totalDelta = sel.reduce((s, p) => s + ((p.delta || 0) * p.net_qty), 0);
  document.getElementById("opt-sel-delta").textContent = totalDelta.toFixed(2);
  const minDte = sel.length > 0 ? Math.min(...sel.map(p => p.days_remaining)) : 0;
  document.getElementById("opt-sel-min-dte").textContent = sel.length > 0 ? `${Math.round(minDte)}d` : "--";
}

// ── Scenario computation helpers ─────────────────────────

function optComputeLegRoll(pos, targetExpCode, newStrike, newOpt, spot) {
  const newDte = rollDteForExpiry(targetExpCode);
  const T = Math.max(newDte, 0) / 365.25;
  const rollIvPct = pfLookupIv(newDte, newStrike) ?? pos.iv_pct;
  const sigma = rollIvPct / 100;
  const newMark = bsPrice(spot, newStrike, T, 0, sigma, newOpt);
  const curMark = pos.mark_price_usd ?? 0;
  const closeValue = pos.net_qty * curMark;
  const openValue = pos.net_qty * newMark;
  const cost = closeValue - openValue;
  return { newDte, newMark, rollIvPct, curMark, cost, closeValue, openValue };
}

function optCalcScenarioDelta(selectedPositions, scenarioLegs, spot) {
  const portfolioDelta = pfData.totals?.portfolio_delta || 0;
  const removedDelta = selectedPositions.reduce((s, p) => s + ((p.delta || 0) * p.net_qty), 0);
  let addedDelta = 0;
  for (const leg of scenarioLegs) {
    if (leg.action === "Close") continue;
    const T = Math.max(leg.newDte, 0) / 365.25;
    if (T <= 0) continue;
    const sigma = (leg.rollIvPct || leg.pos.iv_pct || 80) / 100;
    const d1 = (Math.log(spot / leg.newStrike) + (0.5 * sigma * sigma) * T) / (sigma * Math.sqrt(T));
    const nd1 = normCdf(d1);
    const legDelta = leg.newOpt === "C" ? nd1 : nd1 - 1;
    addedDelta += leg.newNetQty * legDelta;
  }
  return portfolioDelta - removedDelta + addedDelta;
}

function optCalcScenarioPayoff(selectedPositions, scenarioLegs, spot, horizonDte) {
  const spots = pfData.spot_ladder;
  const selectedIds = new Set(selectedPositions.map(p => p.id));
  const horizon = Math.min(horizonDte, 30);
  // Unmodified positions contribution
  const curve = new Array(spots.length).fill(0);
  for (const p of pfData.positions) {
    if (selectedIds.has(p.id)) continue;
    const c = p.payoff_by_horizon[String(horizon)];
    if (c) for (let i = 0; i < spots.length; i++) curve[i] += c[i];
  }
  // Scenario legs contribution
  for (const leg of scenarioLegs) {
    if (leg.action === "Close") continue;
    const T = Math.max(leg.newDte - horizon, 0) / 365.25;
    const sigma = (leg.rollIvPct || 80) / 100;
    for (let i = 0; i < spots.length; i++) {
      curve[i] += leg.newNetQty * bsPrice(spots[i], leg.newStrike, T, 0, sigma, leg.newOpt);
    }
  }
  const spotUp = Math.round(spot * 1.2);
  const spotDown = Math.round(spot * 0.8);
  const idxUp = spots.findIndex(s => s >= spotUp);
  const idxDown = spots.findIndex(s => s >= spotDown);
  return { curve, up20: idxUp >= 0 ? curve[idxUp] : 0, down20: idxDown >= 0 ? curve[idxDown] : 0 };
}

// ── Scenario builders ────────────────────────────────────

function optBuildRollScenario(positions, targetExpCode, spot) {
  const dte = rollDteForExpiry(targetExpCode);
  let totalClose = 0, totalOpen = 0, totalCost = 0;
  const legs = [];
  for (const pos of positions) {
    const info = optComputeLegRoll(pos, targetExpCode, pos.strike, pos.opt, spot);
    totalClose += info.closeValue; totalOpen += info.openValue; totalCost += info.cost;
    legs.push({ pos, action: "Roll", newExpCode: targetExpCode, newDte: info.newDte,
      newStrike: pos.strike, newOpt: pos.opt, newNetQty: pos.net_qty,
      curMark: info.curMark, newMark: info.newMark, rollIvPct: info.rollIvPct, cost: info.cost });
  }
  const delta = optCalcScenarioDelta(positions, legs, spot);
  const payoff = optCalcScenarioPayoff(positions, legs, spot, dte);
  return { name: `Roll to ${targetExpCode} (${dte}d)`, type: "roll",
    legsRolled: positions.length, legsClosed: 0, netCost: totalCost, totalClose, totalOpen,
    deltaAfter: delta, payoffUp20: payoff.up20, payoffDown20: payoff.down20,
    payoffCurve: payoff.curve, medianDte: dte, legs };
}

function optAdjustStrike(pos, adjDelta, spot) {
  if (pos.opt === "C") return Math.round((pos.strike + adjDelta) / 50) * 50;
  return Math.round((pos.strike - adjDelta) / 50) * 50;
}

function optBuildStrikeAdjScenario(positions, targetExpCode, adj, spot) {
  const dte = rollDteForExpiry(targetExpCode);
  let totalClose = 0, totalOpen = 0, totalCost = 0;
  const legs = [];
  for (const pos of positions) {
    const newStrike = Math.max(50, optAdjustStrike(pos, adj.delta, spot));
    const info = optComputeLegRoll(pos, targetExpCode, newStrike, pos.opt, spot);
    totalClose += info.closeValue; totalOpen += info.openValue; totalCost += info.cost;
    legs.push({ pos, action: "Roll+Adj", newExpCode: targetExpCode, newDte: info.newDte,
      newStrike, newOpt: pos.opt, newNetQty: pos.net_qty,
      curMark: info.curMark, newMark: info.newMark, rollIvPct: info.rollIvPct, cost: info.cost });
  }
  const delta = optCalcScenarioDelta(positions, legs, spot);
  const payoff = optCalcScenarioPayoff(positions, legs, spot, dte);
  return { name: `Roll to ${targetExpCode} / ${adj.label}`, type: "adjust",
    legsRolled: positions.length, legsClosed: 0, netCost: totalCost, totalClose, totalOpen,
    deltaAfter: delta, payoffUp20: payoff.up20, payoffDown20: payoff.down20,
    payoffCurve: payoff.curve, medianDte: dte, legs };
}

function optBuildStaggerScenario(positions, exp1Code, exp2Code, spot) {
  const dte1 = rollDteForExpiry(exp1Code);
  const dte2 = rollDteForExpiry(exp2Code);
  const medDte = Math.round((dte1 + dte2) / 2);
  let totalClose = 0, totalOpen = 0, totalCost = 0;
  const legs = [];
  positions.forEach((pos, i) => {
    const expCode = i % 2 === 0 ? exp1Code : exp2Code;
    const legDte = i % 2 === 0 ? dte1 : dte2;
    const info = optComputeLegRoll(pos, expCode, pos.strike, pos.opt, spot);
    totalClose += info.closeValue; totalOpen += info.openValue; totalCost += info.cost;
    legs.push({ pos, action: "Roll", newExpCode: expCode, newDte: legDte,
      newStrike: pos.strike, newOpt: pos.opt, newNetQty: pos.net_qty,
      curMark: info.curMark, newMark: info.newMark, rollIvPct: info.rollIvPct, cost: info.cost });
  });
  const delta = optCalcScenarioDelta(positions, legs, spot);
  const payoff = optCalcScenarioPayoff(positions, legs, spot, medDte);
  return { name: `Stagger ${exp1Code}/${exp2Code} (50/50)`, type: "stagger",
    legsRolled: positions.length, legsClosed: 0, netCost: totalCost, totalClose, totalOpen,
    deltaAfter: delta, payoffUp20: payoff.up20, payoffDown20: payoff.down20,
    payoffCurve: payoff.curve, medianDte: medDte, legs };
}

function optBuildCloseScenario(positions, spot) {
  let totalClose = 0;
  const legs = [];
  for (const pos of positions) {
    const curMark = pos.mark_price_usd ?? 0;
    const closeValue = pos.net_qty * curMark;
    totalClose += closeValue;
    legs.push({ pos, action: "Close", newExpCode: null, newDte: 0,
      newStrike: null, newOpt: null, newNetQty: 0,
      curMark, newMark: 0, rollIvPct: null, cost: closeValue });
  }
  const selectedIds = new Set(positions.map(p => p.id));
  const spots = pfData.spot_ladder;
  const baseCurve = rollBaselineCurve(spots, pfData.positions, 0);
  const closedCurve = rollBaselineCurve(spots, positions, 0);
  const curve = baseCurve.map((v, i) => v - closedCurve[i]);
  const spotUp = Math.round(spot * 1.2);
  const spotDown = Math.round(spot * 0.8);
  const idxUp = spots.findIndex(s => s >= spotUp);
  const idxDown = spots.findIndex(s => s >= spotDown);
  const portfolioDelta = pfData.totals?.portfolio_delta || 0;
  const closedDelta = positions.reduce((s, p) => s + ((p.delta || 0) * p.net_qty), 0);
  return { name: "Close All Selected", type: "close",
    legsRolled: 0, legsClosed: positions.length, netCost: totalClose, totalClose, totalOpen: 0,
    deltaAfter: portfolioDelta - closedDelta,
    payoffUp20: idxUp >= 0 ? curve[idxUp] : 0, payoffDown20: idxDown >= 0 ? curve[idxDown] : 0,
    payoffCurve: curve, medianDte: 0, legs };
}

function optBuildPartialCloseScenario(positions, closedIds, rollExpCode, spot, nClosed) {
  const dte = rollDteForExpiry(rollExpCode);
  let totalClose = 0, totalOpen = 0, totalCost = 0;
  const legs = [];
  let legsRolled = 0, legsClosed = 0;
  for (const pos of positions) {
    if (closedIds.has(pos.id)) {
      const curMark = pos.mark_price_usd ?? 0;
      const closeValue = pos.net_qty * curMark;
      totalClose += closeValue; totalCost += closeValue; legsClosed++;
      legs.push({ pos, action: "Close", newExpCode: null, newDte: 0,
        newStrike: null, newOpt: null, newNetQty: 0,
        curMark, newMark: 0, rollIvPct: null, cost: closeValue });
    } else {
      const info = optComputeLegRoll(pos, rollExpCode, pos.strike, pos.opt, spot);
      totalClose += info.closeValue; totalOpen += info.openValue; totalCost += info.cost; legsRolled++;
      legs.push({ pos, action: "Roll", newExpCode: rollExpCode, newDte: info.newDte,
        newStrike: pos.strike, newOpt: pos.opt, newNetQty: pos.net_qty,
        curMark: info.curMark, newMark: info.newMark, rollIvPct: info.rollIvPct, cost: info.cost });
    }
  }
  const delta = optCalcScenarioDelta(positions, legs, spot);
  const payoff = optCalcScenarioPayoff(positions, legs, spot, dte);
  return { name: `Close ${nClosed} expensive, roll rest → ${rollExpCode}`, type: "partial",
    legsRolled, legsClosed, netCost: totalCost, totalClose, totalOpen,
    deltaAfter: delta, payoffUp20: payoff.up20, payoffDown20: payoff.down20,
    payoffCurve: payoff.curve, medianDte: dte, legs };
}

// ── Main scenario generation ─────────────────────────────

function optGenerateScenarios() {
  if (!pfData) return;
  const selectedPositions = pfData.positions.filter(p => optSelected.has(p.id));
  if (selectedPositions.length === 0) { alert("Select at least one position to optimize."); return; }

  const spot = pfData.eth_spot;
  const allSmiles = (pfData.vol_surface || []).filter(s => s.dte > 0).sort((a, b) => a.dte - b.dte);
  if (allSmiles.length === 0) { alert("No vol surface data available."); return; }

  // Only offer roll targets that extend at least 7 days beyond the
  // longest-dated selected position — rolling to an earlier or same date is useless
  const maxSelectedDte = Math.max(...selectedPositions.map(p => p.days_remaining));
  const smiles = allSmiles.filter(s => s.dte >= maxSelectedDte + 7);
  if (smiles.length === 0) { alert("No valid roll targets found. All available expiries are too close to the selected positions' expiry."); return; }

  const scenarios = [];

  // Family 1: Roll to single expiry
  for (const smile of smiles) {
    scenarios.push(optBuildRollScenario(selectedPositions, smile.expiry_code, spot));
  }

  // Family 2: Roll + strike adjustment (3 nearest valid expiries)
  const nearExpiries = smiles.slice(0, 3);
  const strikeAdj = [
    { delta: -200, label: "Tighten $200" },   // closer to ATM → better downside protection
    { delta: -100, label: "Tighten $100" },
    { delta: 100,  label: "Widen $100" },      // further OTM → cheaper roll cost
    { delta: 200,  label: "Widen $200" },
  ];
  for (const smile of nearExpiries) {
    for (const adj of strikeAdj) {
      scenarios.push(optBuildStrikeAdjScenario(selectedPositions, smile.expiry_code, adj, spot));
    }
  }

  // Family 3: Stagger across 2 valid expiries
  for (let i = 0; i < Math.min(smiles.length - 1, 3); i++) {
    scenarios.push(optBuildStaggerScenario(selectedPositions, smiles[i].expiry_code, smiles[i + 1].expiry_code, spot));
  }

  // Family 4: Close all (de-emphasized — user prefers rolling)
  scenarios.push(optBuildCloseScenario(selectedPositions, spot));

  // Family 5: Partial close + roll (use nearest valid roll target)
  if (selectedPositions.length >= 2 && smiles.length > 0) {
    const nearestExp = smiles[0].expiry_code;
    const legCosts = selectedPositions.map(pos => {
      const info = optComputeLegRoll(pos, nearestExp, pos.strike, pos.opt, spot);
      return { pos, absCost: Math.abs(info.cost) };
    });
    legCosts.sort((a, b) => b.absCost - a.absCost);
    const maxClose = Math.min(selectedPositions.length - 1, 3);
    for (let n = 1; n <= maxClose; n++) {
      const closedIds = new Set(legCosts.slice(0, n).map(lc => lc.pos.id));
      scenarios.push(optBuildPartialCloseScenario(selectedPositions, closedIds, nearestExp, spot, n));
    }
  }

  // Composite score: minimize roll cost + maximize downside protection
  // Higher score = better (least cost + best protection at -20%)
  for (const sc of scenarios) {
    sc.score = sc.netCost + 0.3 * sc.payoffDown20;
  }
  scenarios.sort((a, b) => b.score - a.score);
  scenarios.forEach((s, i) => s.rank = i + 1);

  optScenarios = scenarios;
  optHighlightIdx = 0;
  optRenderResults();
  optDrawChart();
  optRenderDetail(scenarios[0]);
}

// ── Rendering ────────────────────────────────────────────

function optRenderResults() {
  const $section = document.getElementById("opt-results-section");
  $section.style.display = "block";
  document.getElementById("opt-results-count").textContent = `(${optScenarios.length} scenarios)`;

  const fmtCost = v => {
    const r = Math.round(v);
    return `<span class="${r >= 0 ? 'mtm-pos' : 'mtm-neg'}" style="font-family:monospace;font-weight:600">${r >= 0 ? "+" : ""}$${Math.abs(r).toLocaleString()}</span>`;
  };

  const badgeCls = { roll: "opt-badge-roll", adjust: "opt-badge-adjust", stagger: "opt-badge-stagger", close: "opt-badge-close", partial: "opt-badge-partial" };
  const badgeLbl = { roll: "Roll", adjust: "Adjust", stagger: "Stagger", close: "Close", partial: "Partial" };

  const $tbody = document.getElementById("opt-results-body");
  $tbody.innerHTML = "";

  optScenarios.forEach((sc, idx) => {
    const costR = Math.round(sc.netCost);
    const scoreR = Math.round(sc.score);
    // Color by composite score (cost + downside protection)
    const rowCls = (scoreR >= 0 ? "opt-row-positive" : "opt-row-negative") + (idx === optHighlightIdx ? " opt-row-selected" : "") + (sc.type === "close" ? " opt-row-close-warn" : "");
    const tr = document.createElement("tr");
    tr.className = rowCls;
    tr.dataset.scenIdx = idx;
    tr.innerHTML =
      `<td>${sc.rank}</td>` +
      `<td style="text-align:left">${sc.name}</td>` +
      `<td style="text-align:left"><span class="opt-badge ${badgeCls[sc.type] || ''}">${badgeLbl[sc.type] || sc.type}</span></td>` +
      `<td>${sc.legsRolled}</td>` +
      `<td>${sc.legsClosed}</td>` +
      `<td>${fmtCost(sc.netCost)}</td>` +
      `<td class="${costR >= 0 ? 'mtm-pos' : 'mtm-neg'}" style="font-weight:600">${costR >= 0 ? "Receive" : "Pay"}</td>` +
      `<td style="font-weight:700">${fmtCost(sc.payoffDown20)}</td>` +
      `<td>${fmtCost(sc.payoffUp20)}</td>` +
      `<td style="font-family:monospace">${sc.deltaAfter.toFixed(2)}</td>` +
      `<td style="font-family:monospace;font-weight:600">${scoreR >= 0 ? "+" : ""}${scoreR.toLocaleString()}</td>`;
    $tbody.appendChild(tr);

    tr.addEventListener("click", () => {
      optHighlightIdx = idx;
      optRenderResults();
      optDrawChart();
      optRenderDetail(sc);
    });
  });
}

function optDrawChart() {
  const $chart = document.getElementById("opt-payoff-chart");
  $chart.style.display = "block";

  const spots = pfData.spot_ladder;
  const spot = pfData.eth_spot;
  const scenColors = ["#58a6ff", "#f85149", "#d29922", "#3fb950", "#bc8cff"];
  const traces = [];

  // Baseline
  const baselineCurve = rollBaselineCurve(spots, pfData.positions, 0);
  traces.push({ x: spots, y: baselineCurve, type: "scatter", mode: "lines",
    name: "Current Portfolio", line: { color: "#484f58", width: 2, dash: "dot" } });

  // Top 5 scenarios (highlighted one first)
  let toPlot = [];
  if (optHighlightIdx >= 0) toPlot.push(optHighlightIdx);
  for (let i = 0; i < optScenarios.length && toPlot.length < 5; i++) {
    if (!toPlot.includes(i)) toPlot.push(i);
  }
  toPlot.forEach((idx, ci) => {
    const sc = optScenarios[idx];
    const isHl = idx === optHighlightIdx;
    traces.push({ x: spots, y: sc.payoffCurve, type: "scatter", mode: "lines",
      name: `#${sc.rank}: ${sc.name}`,
      line: { color: scenColors[ci % scenColors.length], width: isHl ? 3 : 1.8 },
      opacity: isHl ? 1 : 0.6 });
  });

  // Spot marker
  const allY = traces.flatMap(t => t.y);
  if (allY.length > 0) {
    traces.push({ x: [spot, spot], y: [Math.min(...allY), Math.max(...allY)],
      type: "scatter", mode: "lines", name: `Spot $${spot.toFixed(0)}`,
      line: { color: "#3fb950", width: 1.5, dash: "dashdot" } });
  }

  const cc = chartColors();
  Plotly.react("opt-payoff-chart", traces, {
    title: { text: "Optimizer: Scenario Payoff Comparison", font: { color: cc.text, size: 16 } },
    paper_bgcolor: cc.paper, plot_bgcolor: cc.plot,
    xaxis: { title: "ETH Spot Price (USD)", color: cc.muted, gridcolor: cc.grid, zerolinecolor: cc.zeroline, dtick: 500 },
    yaxis: { title: "Portfolio P&L (USD)", color: cc.muted, gridcolor: cc.grid, zerolinecolor: "#f85149", zerolinewidth: 2 },
    margin: { t: 50, r: 250, b: 50, l: 80 },
    showlegend: true,
    legend: { font: { color: cc.muted, size: 10 }, orientation: "v", x: 1.02, y: 1,
      xanchor: "left", yanchor: "top", bgcolor: cc.legendBg, bordercolor: cc.legendBorder, borderwidth: 1 },
  }, { responsive: true });
}

function optRenderDetail(sc) {
  const $section = document.getElementById("opt-detail-section");
  $section.style.display = "block";
  document.getElementById("opt-detail-name").textContent = sc.name;

  const fmtCost = v => {
    const r = Math.round(v);
    return `<span class="${r >= 0 ? 'mtm-pos' : 'mtm-neg'}" style="font-family:monospace;font-weight:600">${r >= 0 ? "Rcv" : "Pay"} $${Math.abs(r).toLocaleString()}</span>`;
  };

  const costR = Math.round(sc.netCost);
  const $cost = document.getElementById("opt-detail-cost");
  $cost.textContent = `${costR >= 0 ? "" : "-"}$${Math.abs(costR).toLocaleString()}`;
  $cost.className = `risk-value ${costR >= 0 ? "mtm-pos" : "mtm-neg"}`;
  const $dir = document.getElementById("opt-detail-dir");
  $dir.textContent = costR >= 0 ? "Net Receive" : "Net Pay";
  $dir.className = `risk-value ${costR >= 0 ? "mtm-pos" : "mtm-neg"}`;
  document.getElementById("opt-detail-delta").textContent = sc.deltaAfter.toFixed(2);
  document.getElementById("opt-detail-up").innerHTML = fmtCost(sc.payoffUp20);
  document.getElementById("opt-detail-down").innerHTML = fmtCost(sc.payoffDown20);

  const fmtNum = (v, d=2) => v != null ? v.toLocaleString(undefined, { maximumFractionDigits: d }) : "---";
  const $tbody = document.getElementById("opt-detail-body");
  $tbody.innerHTML = "";

  for (const leg of sc.legs) {
    const sideClass = leg.pos.side === "Long" ? "qty-long" : "qty-short";
    const actionColor = leg.action === "Close" ? "var(--red)" : "var(--accent)";
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td style="text-align:left;font-size:.72rem">${leg.pos.instrument}</td>` +
      `<td style="text-align:left;color:${actionColor};font-weight:600">${leg.action}</td>` +
      `<td style="text-align:left" class="${sideClass}">${leg.pos.side_raw}</td>` +
      `<td>${leg.newOpt ? (leg.newOpt === "C" ? "Call" : "Put") : "---"}</td>` +
      `<td style="font-family:monospace">${leg.newStrike != null ? fmtNum(leg.newStrike, 0) : "---"}</td>` +
      `<td style="font-family:monospace">${fmtNum(leg.newNetQty, 0)}</td>` +
      `<td style="font-family:monospace">$${fmtNum(leg.curMark)}</td>` +
      `<td>${leg.newExpCode || "---"}</td>` +
      `<td style="font-family:monospace">${leg.newMark ? "$" + fmtNum(leg.newMark) : "---"}</td>` +
      `<td>${leg.rollIvPct != null ? leg.rollIvPct.toFixed(1) + "%" : "---"}</td>` +
      `<td>${fmtCost(leg.cost)}</td>`;
    $tbody.appendChild(tr);
  }

  document.getElementById("opt-detail-foot").innerHTML =
    `<tr><td style="text-align:left;font-weight:700" colspan="6">TOTAL</td>` +
    `<td></td><td></td><td></td><td></td><td>${fmtCost(sc.netCost)}</td></tr>`;

  $section.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// ── Optimizer event wiring ───────────────────────────────

document.getElementById("btn-opt-generate").addEventListener("click", optGenerateScenarios);

document.getElementById("btn-opt-select-all").addEventListener("click", () => {
  optGetFilteredPositions().forEach(p => optSelected.add(p.id));
  optRenderPositions();
  optUpdateSelectionSummary();
});

document.getElementById("btn-opt-deselect-all").addEventListener("click", () => {
  optSelected.clear();
  optScenarios = [];
  optRenderPositions();
  optUpdateSelectionSummary();
  document.getElementById("opt-results-section").style.display = "none";
  document.getElementById("opt-payoff-chart").style.display = "none";
  document.getElementById("opt-detail-section").style.display = "none";
});

document.getElementById("opt-check-all").addEventListener("change", (e) => {
  optGetFilteredPositions().forEach(p => {
    if (e.target.checked) optSelected.add(p.id); else optSelected.delete(p.id);
  });
  optRenderPositions();
  optUpdateSelectionSummary();
});

document.getElementById("btn-opt-expiring-10d").addEventListener("click", () => {
  if (!pfData) return;
  optSelected.clear();
  pfData.positions.forEach(p => { if (p.days_remaining <= 10) optSelected.add(p.id); });
  optRenderPositions();
  optUpdateSelectionSummary();
});

["opt-filter-expiry", "opt-filter-type", "opt-filter-side"].forEach(id => {
  document.getElementById(id).addEventListener("change", () => { if (pfData) optRenderPositions(); });
});

// ═══════════════════════════════════════════════════════════
// ═══  STRATEGY BUILDER TAB  ════════════════════════════════
// ═══════════════════════════════════════════════════════════

let sbLoaded = false;
let sbSelected = new Set();
let sbIncluded = new Set();
let sbLastResults = null;
let sbSortStack = [{ col: "expiry", asc: true }];
let sbLegs = [];
let sbLegsPriced = [];
let sbStructureCurves = null;
let sbOriginalPositionIds = null;  // snapshot before adding structures
let sbRemovedPositions = [];       // positions removed via Remove button (kept for baseline)

// ── Init & populate ──────────────────────────────────────

async function sbInit() {
  if (!pfData) {
    try {
      pfData = await get(`/api/portfolio/pnl?asset=${currentAsset}`);
      portfolioLoaded = true;
      if (pfSelected.size === 0) pfSelected = new Set(pfData.positions.map(p => p.id));
    } catch (e) {
      console.error("Failed to load portfolio for strategy builder:", e);
      alert("Failed to load portfolio data.\n" + e.message);
      return;
    }
  }
  if (!volSurface) {
    try { volSurface = await get("/api/market/vol-surface"); } catch (e) { /* optional */ }
  }
  sbPopulate();
}

function sbPopulate() {
  const expiries = [...new Set(pfData.positions.map(p => p.expiry.split("T")[0]))].sort();
  const $expF = document.getElementById("sb-filter-expiry");
  $expF.innerHTML = '<option value="">All</option>';
  expiries.forEach(e => { const o = document.createElement("option"); o.value = e; o.textContent = e; $expF.appendChild(o); });

  const $targetExp = document.getElementById("sb-target-expiry");
  $targetExp.innerHTML = "";
  const smiles = (pfData.vol_surface || []).filter(s => s.dte > 0).sort((a, b) => a.dte - b.dte);
  smiles.forEach(s => {
    const o = document.createElement("option");
    o.value = s.expiry_code; o.textContent = `${s.expiry_code} (${s.dte}d)`;
    $targetExp.appendChild(o);
  });
  if (smiles.length > 0) $targetExp.value = smiles[0].expiry_code;

  sbSelected = new Set();
  sbIncluded = new Set(pfData.positions.map(p => p.id));
  sbLastResults = null;
  sbStructureCurves = null;
  sbLegsPriced = [];
  sbRemovedPositions = [];
  sbOriginalPositionIds = new Set(pfData.positions.map(p => p.id));
  sbRenderTable();
  sbRenderLegs();
  sbDrawStructureChart();
  sbDrawChart(null);
  sbRenderMtmGrid(null);
  sbLoaded = true;
}

// ── Filtering & sorting ──────────────────────────────────

function sbGetFilteredPositions() {
  const fExpiry = document.getElementById("sb-filter-expiry").value;
  const fType = document.getElementById("sb-filter-type").value;
  const fSide = document.getElementById("sb-filter-side").value;
  let list = [...pfData.positions];
  if (fExpiry) list = list.filter(p => p.expiry.split("T")[0] === fExpiry);
  if (fType) list = list.filter(p => p.opt === fType);
  if (fSide) list = list.filter(p => p.side === fSide);
  if (sbSortStack.length > 0) {
    list.sort((a, b) => {
      for (const { col, asc } of sbSortStack) {
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

function sbGetTargetExpiry() {
  return document.getElementById("sb-target-expiry").value;
}

// ── Position table ───────────────────────────────────────

function sbRenderTable() {
  const list = sbGetFilteredPositions();
  document.getElementById("sb-table-count").textContent = `(${list.length} trades)`;
  const globalExpiry = sbGetTargetExpiry();
  const allVisibleRoll = list.length > 0 && list.every(p => sbSelected.has(p.id));
  const allVisibleIncl = list.length > 0 && list.every(p => sbIncluded.has(p.id));
  document.getElementById("sb-check-all").checked = allVisibleRoll;
  document.getElementById("sb-include-all").checked = allVisibleIncl;

  const primarySort = sbSortStack[0] || null;
  const secondarySort = sbSortStack[1] || null;
  document.querySelectorAll("#sb-trades-table .sortable").forEach(th => {
    th.classList.remove("sort-asc", "sort-desc", "sort-secondary");
    if (primarySort && th.dataset.col === primarySort.col) th.classList.add(primarySort.asc ? "sort-asc" : "sort-desc");
    else if (secondarySort && th.dataset.col === secondarySort.col) th.classList.add("sort-secondary");
  });

  const fmtNum = (v, d=0) => v != null ? v.toLocaleString(undefined, { maximumFractionDigits: d }) : "---";
  const spot = pfData.eth_spot;
  const $tbody = document.getElementById("sb-trades-body");
  $tbody.innerHTML = "";

  for (const pos of list) {
    const included = sbIncluded.has(pos.id);
    const checked = sbSelected.has(pos.id);
    const typeColor = pos.opt === "C" ? "var(--green)" : "var(--red)";
    const sideClass = pos.side === "Long" ? "qty-long" : "qty-short";
    const rowClass = (checked ? "roll-row-selected" : "") + (!included ? " pf-row-excluded" : "");
    const absQty = Math.abs(pos.net_qty);
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
      `<td style="text-align:left;font-size:.72rem">${pos.counterparty || ""}</td>` +
      `<td style="text-align:left;font-size:.72rem">${pos.instrument}</td>` +
      `<td style="text-align:left" class="${sideClass}">${pos.side_raw}</td>` +
      `<td style="text-align:left;color:${typeColor}">${pos.opt === "C" ? "Call" : "Put"}</td>` +
      `<td style="font-family:monospace">${fmtNum(pos.strike)}</td>` +
      `<td>${pos.expiry}</td>` +
      `<td>${Math.round(pos.days_remaining)}</td>` +
      `<td style="font-family:monospace">${fmtNum(pos.net_qty)}</td>` +
      `<td style="font-family:monospace">$${fmtNum(curMark, 2)}</td>` +
      `<td>${pos.iv_pct != null ? pos.iv_pct.toFixed(1) + "%" : "---"}</td>` +
      `<td class="${pos.current_mtm >= 0 ? 'mtm-pos' : 'mtm-neg'}" style="font-family:monospace">$${fmtNum(pos.current_mtm)}</td>` +
      `<td><select class="roll-expiry-select" data-id="${pos.id}" style="font-size:.72rem;width:100%;margin:0">${expiryOpts}</select></td>` +
      `<td><input type="number" class="roll-strike-input" data-id="${pos.id}" value="${pos.strike}" step="any" style="width:100%;font-size:.75rem;padding:.2rem .3rem;text-align:center"></td>` +
      `<td><input type="number" class="roll-qty-input" data-id="${pos.id}" value="${absQty}" step="1" min="0" style="width:100%;font-size:.75rem;padding:.2rem .3rem;text-align:center"></td>` +
      `<td><select class="roll-type-select" data-id="${pos.id}" style="font-size:.72rem;width:100%;margin:0">` +
        `<option value="C" ${pos.opt === "C" ? "selected" : ""}>Call</option>` +
        `<option value="P" ${pos.opt === "P" ? "selected" : ""}>Put</option></select></td>` +
      `<td style="font-family:monospace" class="roll-new-mark">$${fmtNum(newMark, 2)}</td>` +
      `<td class="roll-iv-used">${rollIvPct.toFixed(1)}%</td>` +
      `<td class="${costCls} roll-cost" style="font-family:monospace;font-weight:600">${costLabel} $${Math.abs(costRounded).toLocaleString()}</td>` +
      `<td><button class="btn-remove-pos" data-id="${pos.id}" title="Remove trade" style="background:none;border:none;color:var(--red);cursor:pointer;font-size:.85rem;padding:.2rem .4rem;opacity:.6">&times;</button></td>`;
    $tbody.appendChild(tr);
  }

  // Wire remove buttons
  $tbody.querySelectorAll(".btn-remove-pos").forEach(btn => {
    btn.addEventListener("click", () => {
      const id = parseInt(btn.dataset.id);
      sbRemovePosition(id);
    });
  });

  // Wire include checkboxes
  $tbody.querySelectorAll(".roll-include").forEach(cb => {
    cb.addEventListener("change", () => {
      const id = parseInt(cb.dataset.id);
      if (cb.checked) sbIncluded.add(id); else sbIncluded.delete(id);
      cb.closest("tr").classList.toggle("pf-row-excluded", !cb.checked);
      const visible = sbGetFilteredPositions();
      document.getElementById("sb-include-all").checked = visible.length > 0 && visible.every(p => sbIncluded.has(p.id));
      sbRefreshVisuals();
    });
  });

  // Wire roll checkboxes
  $tbody.querySelectorAll(".roll-check").forEach(cb => {
    cb.addEventListener("change", () => {
      const id = parseInt(cb.dataset.id);
      if (cb.checked) sbSelected.add(id); else sbSelected.delete(id);
      cb.closest("tr").classList.toggle("roll-row-selected", cb.checked);
      const visible = sbGetFilteredPositions();
      document.getElementById("sb-check-all").checked = visible.length > 0 && visible.every(p => sbSelected.has(p.id));
    });
  });

  // Wire inline edits
  $tbody.querySelectorAll(".roll-expiry-select, .roll-strike-input, .roll-qty-input, .roll-type-select").forEach(el => {
    el.addEventListener("change", () => sbInlineUpdate(el.closest("tr")));
  });
}

function sbInlineUpdate(row) {
  const posId = parseInt(row.dataset.posId);
  const pos = pfData.positions.find(p => p.id === posId);
  if (!pos) return;
  const newExpCode = row.querySelector(".roll-expiry-select").value;
  const newStrike = parseFloat(row.querySelector(".roll-strike-input").value) || pos.strike;
  const newAbsQty = parseFloat(row.querySelector(".roll-qty-input").value);
  const newType = row.querySelector(".roll-type-select").value;
  const sign = pos.net_qty >= 0 ? 1 : -1;
  const newNetQty = isNaN(newAbsQty) ? pos.net_qty : sign * newAbsQty;
  const newDte = rollDteForExpiry(newExpCode);
  const T = Math.max(newDte, 0) / 365.25;
  const iv = pfLookupIv(newDte, newStrike) ?? pos.iv_pct;
  const sigma = iv / 100;
  const nm = bsPrice(pfData.eth_spot, newStrike, T, 0, sigma, newType);
  const cm = pos.mark_price_usd ?? 0;
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

// ── Remove position ──────────────────────────────────────

function sbRemovePosition(id) {
  const idx = pfData.positions.findIndex(p => p.id === id);
  if (idx === -1) return;
  const pos = pfData.positions[idx];
  sbRemovedPositions.push(pos);
  pfData.positions.splice(idx, 1);
  sbIncluded.delete(id);
  sbSelected.delete(id);
  pfSelected.delete(id);
  sbRenderTable();
  sbRefreshVisuals();
}

// ── Roll computation ─────────────────────────────────────

function sbComputeRoll() {
  if (!pfData) return;
  const selectedPositions = pfData.positions.filter(p => sbSelected.has(p.id));
  if (selectedPositions.length === 0) { alert("Select at least one leg to roll."); return; }

  const globalExpiry = sbGetTargetExpiry();
  const spot = pfData.eth_spot;
  const results = [];
  let totalCloseValue = 0, totalOpenValue = 0, totalRollCost = 0;

  for (const pos of selectedPositions) {
    const row = document.querySelector(`#sb-trades-body tr[data-pos-id="${pos.id}"]`);
    const newExpCode = row ? row.querySelector(".roll-expiry-select").value : globalExpiry;
    const newStrike = row ? (parseFloat(row.querySelector(".roll-strike-input").value) || pos.strike) : pos.strike;
    const newAbsQty = row ? parseFloat(row.querySelector(".roll-qty-input").value) : Math.abs(pos.net_qty);
    const newType = row ? row.querySelector(".roll-type-select").value : pos.opt;
    const sign = pos.net_qty >= 0 ? 1 : -1;
    const newNetQty = isNaN(newAbsQty) ? pos.net_qty : sign * newAbsQty;
    const newDte = rollDteForExpiry(newExpCode);
    const T = Math.max(newDte, 0) / 365.25;
    const rollIvPct = pfLookupIv(newDte, newStrike) ?? pos.iv_pct;
    const sigma = rollIvPct / 100;
    const newMark = bsPrice(spot, newStrike, T, 0, sigma, newType);
    const curMark = pos.mark_price_usd ?? 0;
    const closeValue = pos.net_qty * curMark;
    const openValue = newNetQty * newMark;
    const cost = closeValue - openValue;

    const strikeChanged = newStrike !== pos.strike;
    const qtyChanged = newAbsQty !== Math.abs(pos.net_qty);
    const typeChanged = newType !== pos.opt;
    let dteImpact = cost, strikeImpact = 0, qtyImpact = 0, typeImpact = 0;
    if (strikeChanged || qtyChanged || typeChanged) {
      const dteIv = pfLookupIv(newDte, pos.strike) ?? pos.iv_pct;
      const dteOnlyMark = bsPrice(spot, pos.strike, T, 0, dteIv / 100, pos.opt);
      dteImpact = pos.net_qty * curMark - pos.net_qty * dteOnlyMark;
      const strikeOnlyMark = bsPrice(spot, newStrike, T, 0, sigma, pos.opt);
      strikeImpact = strikeChanged ? (pos.net_qty * dteOnlyMark - pos.net_qty * strikeOnlyMark) : 0;
      typeImpact = typeChanged ? (pos.net_qty * strikeOnlyMark - pos.net_qty * newMark) : 0;
      qtyImpact = cost - dteImpact - strikeImpact - typeImpact;
    }

    totalCloseValue += closeValue;
    totalOpenValue += openValue;
    totalRollCost += cost;
    results.push({ pos, newExpCode, newDte, newStrike, newNetQty, newType, curMark, newMark, rollIvPct,
      cost, closeValue, openValue, strikeChanged, qtyChanged, typeChanged,
      dteImpact, strikeImpact, qtyImpact, typeImpact });
  }
  sbRenderRollResults(results, totalCloseValue, totalOpenValue, totalRollCost, globalExpiry);
}

function sbRenderRollResults(results, totalCloseValue, totalOpenValue, totalRollCost, globalExpiry) {
  sbLastResults = results;
  document.getElementById("sb-roll-results-section").style.display = "block";

  document.getElementById("sb-legs-count").textContent = results.length;
  const globalDte = rollDteForExpiry(globalExpiry);
  document.getElementById("sb-target-dte-display").textContent = `${globalExpiry} (${globalDte}d)`;

  const fmtUsd = v => "$" + Math.abs(Math.round(v)).toLocaleString();
  const fmtCost = v => { const r = Math.round(v); return `<span class="${r >= 0 ? "mtm-pos" : "mtm-neg"}" style="font-family:monospace;font-weight:600">${r >= 0 ? "Rcv" : "Pay"} $${Math.abs(r).toLocaleString()}</span>`; };

  const $close = document.getElementById("sb-total-close");
  $close.textContent = (totalCloseValue >= 0 ? "" : "-") + fmtUsd(totalCloseValue);
  $close.className = "risk-value " + (totalCloseValue >= 0 ? "mtm-pos" : "mtm-neg");
  const $open = document.getElementById("sb-total-open");
  $open.textContent = (totalOpenValue >= 0 ? "" : "-") + fmtUsd(totalOpenValue);
  $open.className = "risk-value " + (totalOpenValue >= 0 ? "mtm-pos" : "mtm-neg");
  const rounded = Math.round(totalRollCost);
  const $net = document.getElementById("sb-net-cost");
  $net.textContent = (rounded >= 0 ? "" : "-") + fmtUsd(totalRollCost);
  $net.className = "risk-value " + (rounded >= 0 ? "mtm-pos" : "mtm-neg");
  const $dir = document.getElementById("sb-direction");
  $dir.textContent = rounded >= 0 ? "Net Receive" : "Net Pay";
  $dir.className = "risk-value " + (rounded >= 0 ? "mtm-pos" : "mtm-neg");

  const hasStrikeChange = results.some(r => r.strikeChanged);
  const hasQtyChange = results.some(r => r.qtyChanged);
  const hasTypeChange = results.some(r => r.typeChanged);
  const hasBreakdown = hasStrikeChange || hasQtyChange || hasTypeChange;

  const $thead = document.getElementById("sb-results-thead");
  let hdr = `<tr><th style="text-align:left">Instrument</th><th style="text-align:left">Side</th><th>Strike</th><th>Type</th><th>Qty</th><th>Cur DTE</th><th>New Expiry</th><th>Cur Mark</th><th>New Mark</th><th>IV Used</th>`;
  if (hasBreakdown) {
    hdr += `<th>DTE Impact</th>`;
    if (hasStrikeChange) hdr += `<th>Strike Impact</th>`;
    if (hasTypeChange) hdr += `<th>Type Impact</th>`;
    if (hasQtyChange) hdr += `<th>Qty Impact</th>`;
  }
  hdr += `<th>Total Cost</th></tr>`;
  $thead.innerHTML = hdr;

  const fmtNum = (v, d=2) => v != null ? v.toLocaleString(undefined, { maximumFractionDigits: d }) : "---";
  const $tbody = document.getElementById("sb-results-body");
  $tbody.innerHTML = "";
  let totalDteImpact = 0, totalStrikeImpact = 0, totalQtyImpact = 0, totalTypeImpact = 0;

  for (const r of results) {
    totalDteImpact += r.dteImpact; totalStrikeImpact += r.strikeImpact;
    totalQtyImpact += r.qtyImpact; totalTypeImpact += r.typeImpact || 0;
    const sideClass = r.pos.side === "Long" ? "qty-long" : "qty-short";
    const strikeLabel = r.strikeChanged ? `${fmtNum(r.pos.strike, 0)} → ${fmtNum(r.newStrike, 0)}` : fmtNum(r.pos.strike, 0);
    const qtyLabel = r.qtyChanged ? `${fmtNum(r.pos.net_qty, 0)} → ${fmtNum(r.newNetQty, 0)}` : fmtNum(r.pos.net_qty, 0);
    const typeLabel = r.typeChanged ? `${r.pos.opt === "C" ? "Call" : "Put"} → ${r.newType === "C" ? "Call" : "Put"}` : (r.pos.opt === "C" ? "Call" : "Put");
    const typeColor = r.typeChanged ? "var(--accent)" : "inherit";
    let rowHtml = `<td style="text-align:left;font-size:.72rem">${r.pos.instrument}</td>` +
      `<td style="text-align:left" class="${sideClass}">${r.pos.side_raw}</td>` +
      `<td style="font-family:monospace">${strikeLabel}</td>` +
      `<td style="color:${typeColor}">${typeLabel}</td>` +
      `<td style="font-family:monospace">${qtyLabel}</td>` +
      `<td>${Math.round(r.pos.days_remaining)}d</td>` +
      `<td>${r.newExpCode} (${r.newDte}d)</td>` +
      `<td style="font-family:monospace">$${fmtNum(r.curMark)}</td>` +
      `<td style="font-family:monospace">$${fmtNum(r.newMark)}</td>` +
      `<td>${r.rollIvPct.toFixed(1)}%</td>`;
    if (hasBreakdown) {
      rowHtml += `<td>${fmtCost(r.dteImpact)}</td>`;
      if (hasStrikeChange) rowHtml += `<td>${r.strikeChanged ? fmtCost(r.strikeImpact) : '<span style="color:var(--muted)">—</span>'}</td>`;
      if (hasTypeChange) rowHtml += `<td>${r.typeChanged ? fmtCost(r.typeImpact) : '<span style="color:var(--muted)">—</span>'}</td>`;
      if (hasQtyChange) rowHtml += `<td>${r.qtyChanged ? fmtCost(r.qtyImpact) : '<span style="color:var(--muted)">—</span>'}</td>`;
    }
    rowHtml += `<td>${fmtCost(r.cost)}</td>`;
    const tr = document.createElement("tr"); tr.innerHTML = rowHtml; $tbody.appendChild(tr);
  }

  const $tfoot = document.getElementById("sb-results-foot");
  let footHtml = `<tr><td style="text-align:left;font-weight:700" colspan="5">TOTAL</td><td></td><td></td><td></td><td></td><td></td>`;
  if (hasBreakdown) {
    footHtml += `<td>${fmtCost(totalDteImpact)}</td>`;
    if (hasStrikeChange) footHtml += `<td>${fmtCost(totalStrikeImpact)}</td>`;
    if (hasTypeChange) footHtml += `<td>${fmtCost(totalTypeImpact)}</td>`;
    if (hasQtyChange) footHtml += `<td>${fmtCost(totalQtyImpact)}</td>`;
  }
  footHtml += `<td>${fmtCost(totalRollCost)}</td></tr>`;
  $tfoot.innerHTML = footHtml;

  sbDrawChart(results);
  sbRenderMtmGrid(results);
}

// ── Structure Builder ────────────────────────────────────

function sbAddLeg(side, type, strike, qty, expiry) {
  side = side || "buy";
  type = type || "C";
  strike = strike || Math.round(pfData.eth_spot / 50) * 50;
  qty = qty || 1000;
  expiry = expiry || sbGetTargetExpiry() || ((pfData.vol_surface || [])[0] || {}).expiry_code || "";
  sbLegs.push({ side, type, strike, qty, expiry });
  sbRenderLegs();
}

function sbRemoveLeg(idx) {
  sbLegs.splice(idx, 1);
  sbRenderLegs();
}

function sbRenderLegs() {
  const $tbody = document.getElementById("sb-legs-body");
  $tbody.innerHTML = "";
  const smiles = (pfData ? pfData.vol_surface || [] : []).filter(s => s.dte > 0).sort((a, b) => a.dte - b.dte);
  const expiryOpts = smiles.map(s => `<option value="${s.expiry_code}">${s.expiry_code} (${s.dte}d)</option>`).join("");

  sbLegs.forEach((leg, idx) => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td><select class="sb-leg-expiry" data-idx="${idx}" style="font-size:.75rem;width:100%;margin:0">${expiryOpts}</select></td>` +
      `<td style="white-space:nowrap">` +
        `<select class="sb-leg-side" data-idx="${idx}" style="font-size:.75rem;width:55px;margin:0">` +
          `<option value="buy" ${leg.side === "buy" ? "selected" : ""}>Buy</option>` +
          `<option value="sell" ${leg.side === "sell" ? "selected" : ""}>Sell</option></select> ` +
        `<select class="sb-leg-type" data-idx="${idx}" style="font-size:.75rem;width:55px;margin:0">` +
          `<option value="C" ${leg.type === "C" ? "selected" : ""}>Call</option>` +
          `<option value="P" ${leg.type === "P" ? "selected" : ""}>Put</option></select>` +
      `</td>` +
      `<td><input type="number" class="sb-leg-strike" data-idx="${idx}" value="${leg.strike}" step="any" style="width:80px;font-size:.75rem;padding:.2rem .3rem;text-align:center"></td>` +
      `<td><input type="number" class="sb-leg-qty" data-idx="${idx}" value="${leg.qty}" step="100" min="1" style="width:70px;font-size:.75rem;padding:.2rem .3rem;text-align:center"></td>` +
      `<td><button class="sb-leg-remove btn-secondary" data-idx="${idx}" style="padding:.15rem .4rem;font-size:.7rem;margin:0;width:auto">✕</button></td>`;
    $tbody.appendChild(tr);
    // Set expiry value after append
    const expSel = tr.querySelector(".sb-leg-expiry");
    if (expSel) expSel.value = leg.expiry;
  });

  // Wire changes
  $tbody.querySelectorAll(".sb-leg-expiry").forEach(el => el.addEventListener("change", () => { sbLegs[el.dataset.idx].expiry = el.value; }));
  $tbody.querySelectorAll(".sb-leg-side").forEach(el => el.addEventListener("change", () => { sbLegs[el.dataset.idx].side = el.value; }));
  $tbody.querySelectorAll(".sb-leg-type").forEach(el => el.addEventListener("change", () => { sbLegs[el.dataset.idx].type = el.value; }));
  $tbody.querySelectorAll(".sb-leg-strike").forEach(el => el.addEventListener("change", () => { sbLegs[el.dataset.idx].strike = parseFloat(el.value) || 0; }));
  $tbody.querySelectorAll(".sb-leg-qty").forEach(el => el.addEventListener("change", () => { sbLegs[el.dataset.idx].qty = parseFloat(el.value) || 1; }));
  $tbody.querySelectorAll(".sb-leg-remove").forEach(el => el.addEventListener("click", () => sbRemoveLeg(parseInt(el.dataset.idx))));
}

function sbApplyTemplate(name) {
  sbLegs = [];
  const spot = pfData ? pfData.eth_spot : 2500;
  const K = Math.round(spot / 50) * 50;
  const exp = sbGetTargetExpiry() || ((pfData.vol_surface || [])[0] || {}).expiry_code || "";
  switch (name) {
    case "long_call":     sbLegs.push({ side: "buy", type: "C", strike: K, qty: 1000, expiry: exp }); break;
    case "long_put":      sbLegs.push({ side: "buy", type: "P", strike: K, qty: 1000, expiry: exp }); break;
    case "bull_call_spread":
      sbLegs.push({ side: "buy", type: "C", strike: K, qty: 1000, expiry: exp });
      sbLegs.push({ side: "sell", type: "C", strike: K + 500, qty: 1000, expiry: exp }); break;
    case "bear_put_spread":
      sbLegs.push({ side: "buy", type: "P", strike: K, qty: 1000, expiry: exp });
      sbLegs.push({ side: "sell", type: "P", strike: K - 500, qty: 1000, expiry: exp }); break;
    case "straddle":
      sbLegs.push({ side: "buy", type: "C", strike: K, qty: 1000, expiry: exp });
      sbLegs.push({ side: "buy", type: "P", strike: K, qty: 1000, expiry: exp }); break;
    case "strangle":
      sbLegs.push({ side: "buy", type: "C", strike: K + 300, qty: 1000, expiry: exp });
      sbLegs.push({ side: "buy", type: "P", strike: K - 300, qty: 1000, expiry: exp }); break;
    case "iron_condor":
      sbLegs.push({ side: "buy", type: "P", strike: K - 500, qty: 1000, expiry: exp });
      sbLegs.push({ side: "sell", type: "P", strike: K - 200, qty: 1000, expiry: exp });
      sbLegs.push({ side: "sell", type: "C", strike: K + 200, qty: 1000, expiry: exp });
      sbLegs.push({ side: "buy", type: "C", strike: K + 500, qty: 1000, expiry: exp }); break;
  }
  document.querySelectorAll(".sb-template").forEach(b => b.classList.remove("active"));
  const btn = document.querySelector(`.sb-template[data-strategy="${name}"]`);
  if (btn) btn.classList.add("active");
  sbRenderLegs();
  document.getElementById("sb-pricing-results").style.display = "none";
  document.getElementById("btn-sb-add-to-portfolio").style.display = "none";
  sbLegsPriced = [];
  sbStructureCurves = null;
  sbDrawStructureChart();
}

// ── IV lookup by expiry code ─────────────────────────────

function sbLookupSmileIv(expiryCode, strike) {
  if (!pfData || !pfData.vol_surface) return null;
  const entry = pfData.vol_surface.find(s => s.expiry_code === expiryCode);
  if (!entry) return null;
  const { strikes, ivs } = entry;
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

// ── Price structure via vol surface ──────────────────────

function sbPriceStructure() {
  if (!pfData) { alert("Portfolio data not loaded."); return; }
  if (sbLegs.length === 0) { alert("Add at least one leg."); return; }

  const spot = pfData.eth_spot;
  const spots = pfData.spot_ladder;
  sbLegsPriced = [];
  let totalPay = 0, totalReceive = 0;

  for (const leg of sbLegs) {
    const entry = pfData.vol_surface.find(s => s.expiry_code === leg.expiry);
    if (!entry) { alert(`No vol surface data for expiry ${leg.expiry}`); return; }
    const dte = entry.dte;
    const T = Math.max(dte, 0) / 365.25;
    const ivPct = sbLookupSmileIv(leg.expiry, leg.strike);
    if (ivPct == null) { alert(`Cannot interpolate IV for strike ${leg.strike} at ${leg.expiry}`); return; }
    const sigma = ivPct / 100;
    const bsPremEth = bsPrice(spot, leg.strike, T, 0, sigma, leg.type);
    const bsPremUsd = bsPremEth;  // bsPrice already returns USD value per contract
    const dir = leg.side === "buy" ? 1 : -1;
    const legCost = dir * leg.qty * bsPremUsd;
    if (legCost > 0) totalPay += legCost; else totalReceive += Math.abs(legCost);

    sbLegsPriced.push({
      ...leg, dte, ivPct, sigma, bsPremEth: bsPremEth / spot, bsPremUsd, dir,
      totalCost: legCost,
    });
  }

  // Show pricing results
  const $results = document.getElementById("sb-pricing-results");
  $results.style.display = "block";
  const net = totalReceive - totalPay;
  document.getElementById("sb-pricing-summary").innerHTML =
    `Pay <span class="mtm-neg" style="font-weight:600">$${Math.round(totalPay).toLocaleString()}</span> &nbsp;|&nbsp; ` +
    `Receive <span class="mtm-pos" style="font-weight:600">$${Math.round(totalReceive).toLocaleString()}</span> &nbsp;|&nbsp; ` +
    `Net: <span class="${net >= 0 ? 'mtm-pos' : 'mtm-neg'}" style="font-weight:600">${net >= 0 ? "Rcv" : "Pay"} $${Math.abs(Math.round(net)).toLocaleString()}</span>`;

  const $tbody = document.getElementById("sb-premiums-body");
  $tbody.innerHTML = "";
  for (const lp of sbLegsPriced) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td style="text-align:left">${lp.expiry}</td>` +
      `<td style="text-align:left;color:${lp.side === "buy" ? "var(--green)" : "var(--red)"}">${lp.side}</td>` +
      `<td>${lp.type === "C" ? "Call" : "Put"}</td>` +
      `<td style="font-family:monospace">${lp.strike.toLocaleString()}</td>` +
      `<td style="font-family:monospace">${lp.qty.toLocaleString()}</td>` +
      `<td>${lp.dte}d</td>` +
      `<td>${lp.ivPct.toFixed(1)}%</td>` +
      `<td style="font-family:monospace">${lp.bsPremEth.toFixed(4)}</td>` +
      `<td style="font-family:monospace">$${lp.bsPremUsd.toLocaleString(undefined, { maximumFractionDigits: 2 })}</td>`;
    $tbody.appendChild(tr);
  }

  // Compute standalone structure payoff curves
  sbStructureCurves = {};
  const allHorizons = pfData.chart_horizons || [0, 30, 60];
  for (const h of allHorizons) {
    sbStructureCurves[h] = sbComputeStructureCurve(spots, h);
  }

  // Show Add to Portfolio button
  document.getElementById("btn-sb-add-to-portfolio").style.display = "block";

  // Draw standalone structure chart only — net payoff & matrix update on "Add to Portfolio"
  sbDrawStructureChart();
}

// ── Compute structure curve ──────────────────────────────

function sbComputeStructureCurve(spots, horizon) {
  const result = new Array(spots.length).fill(0);
  if (!sbLegsPriced || sbLegsPriced.length === 0) return result;
  for (const lp of sbLegsPriced) {
    const T = Math.max(lp.dte - horizon, 0) / 365.25;
    for (let i = 0; i < spots.length; i++) {
      const val = bsPrice(spots[i], lp.strike, T, 0, lp.sigma, lp.type);
      result[i] += lp.dir * lp.qty * val;
    }
  }
  // Subtract entry cost to show P&L not mark value
  const entryCost = sbLegsPriced.reduce((s, lp) => s + lp.totalCost, 0);
  for (let i = 0; i < result.length; i++) result[i] -= entryCost;
  return result;
}

// ── Add structure to portfolio ───────────────────────────

async function sbAddToPortfolio() {
  if (!sbLegsPriced || sbLegsPriced.length === 0) { alert("Price the structure first."); return; }

  const btn = document.getElementById("btn-sb-add-to-portfolio");
  btn.disabled = true;
  btn.textContent = "Adding...";

  try {
    for (const lp of sbLegsPriced) {
      const instrument = `ETH-${lp.expiry}-${lp.strike}-${lp.type}`;
      let tk;
      try {
        tk = await get(`/api/portfolio/ticker/${encodeURIComponent(instrument)}`);
      } catch (e) {
        // If ticker fetch fails, use our computed values
        tk = { mark_price_usd: lp.bsPremUsd, mark_iv: lp.ivPct, opt: lp.type, strike: lp.strike,
          days_remaining: lp.dte, expiry: "", delta: null, gamma: null, theta: null, vega: null };
      }

      const sign = lp.side === "sell" ? -1 : 1;
      const signedQty = sign * lp.qty;
      const markUsd = tk.mark_price_usd || lp.bsPremUsd;
      const sigma = (tk.mark_iv || lp.ivPct) / 100;
      const opt = tk.opt || lp.type;
      const strike = tk.strike || lp.strike;
      const dte = tk.days_remaining || lp.dte;
      const spots = pfData.spot_ladder;
      const allHorizons = pfData.all_horizons;
      const payoff = {};
      for (const h of allHorizons) {
        const T = Math.max(dte - h, 0) / 365.25;
        const vals = bsVec(spots, strike, T, 0, sigma, opt);
        payoff[String(h)] = vals.map(v => Math.round(signedQty * v * 100) / 100);
      }

      const newPos = {
        id: pfNextId++,
        counterparty: "Structure",
        trade_id: null,
        trade_date: new Date().toISOString().split("T")[0],
        side_raw: lp.side === "sell" ? "Sell" : "Buy",
        option_type: opt === "C" ? "Call" : "Put",
        instrument,
        expiry: tk.expiry || "",
        days_remaining: dte,
        strike,
        pct_otm_entry: 0,
        qty: lp.qty,
        notional_mm: 0,
        premium_per: lp.bsPremUsd,
        premium_usd: lp.totalCost,
        opt,
        side: sign > 0 ? "Long" : "Short",
        net_qty: signedQty,
        pct_otm_live: pfData.eth_spot > 0 ? Math.round((strike / pfData.eth_spot - 1) * 1000) / 10 : 0,
        iv_pct: tk.mark_iv || lp.ivPct,
        delta: tk.delta, gamma: tk.gamma, theta: tk.theta, vega: tk.vega,
        mark_price_usd: markUsd,
        current_mtm: Math.round(signedQty * markUsd * 100) / 100,
        notional_live: Math.round(lp.qty * pfData.eth_spot * 100) / 100,
        payoff_by_horizon: payoff,
      };

      pfData.positions.push(newPos);
      pfSelected.add(newPos.id);
      sbIncluded.add(newPos.id);
    }

    // Clear structure builder state (but keep sbOriginalPositionIds for before/after)
    const addedCount = sbLegsPriced.length;
    sbLegs = [];
    sbLegsPriced = [];
    sbStructureCurves = null;
    document.getElementById("sb-pricing-results").style.display = "none";
    btn.style.display = "none";
    document.querySelectorAll(".sb-template").forEach(b => b.classList.remove("active"));

    sbRenderTable();
    sbRenderLegs();
    sbDrawStructureChart();  // redraw as empty placeholder
    sbRefreshVisuals();
    alert(`Added ${addedCount} leg${addedCount !== 1 ? "s" : ""} to portfolio.`);
  } catch (e) {
    console.error("Failed to add structure:", e);
    alert("Failed to add structure to portfolio.\n" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Add Structure to Portfolio";
  }
}

// ── Standalone structure payoff chart ─────────────────────

function sbDrawStructureChart() {
  const spots = pfData ? pfData.spot_ladder : [];
  const spot = pfData ? pfData.eth_spot : 0;
  const horizons = pfData ? pfData.chart_horizons : [];
  const colors = ["#f85149", "#d29922", "#e3b341", "#3fb950", "#58a6ff", "#bc8cff"];
  const hasData = sbStructureCurves && Object.keys(sbStructureCurves).length > 0;
  const traces = [];

  if (hasData) {
    horizons.forEach((h, i) => {
      const curve = sbStructureCurves[h];
      if (curve) {
        traces.push({
          x: spots, y: curve, type: "scatter", mode: "lines",
          name: h === 0 ? "At Expiry" : `T+${h}d`,
          line: { color: colors[i % colors.length], width: 2.5 }
        });
      }
    });

    // Spot marker
    const allY = traces.flatMap(t => t.y);
    if (allY.length > 0) {
      traces.push({
        x: [spot, spot], y: [Math.min(...allY), Math.max(...allY)],
        type: "scatter", mode: "lines",
        name: `Spot $${spot.toFixed(0)}`,
        line: { color: "#3fb950", width: 1.5, dash: "dashdot" }
      });
    }

    // Break-even lines
    const expiryCurve = sbStructureCurves[0];
    if (expiryCurve && allY.length > 0) {
      const yMin = Math.min(...allY), yMax = Math.max(...allY);
      for (let i = 0; i < expiryCurve.length - 1; i++) {
        if ((expiryCurve[i] <= 0 && expiryCurve[i + 1] > 0) || (expiryCurve[i] >= 0 && expiryCurve[i + 1] < 0)) {
          const t = Math.abs(expiryCurve[i]) / (Math.abs(expiryCurve[i]) + Math.abs(expiryCurve[i + 1]));
          const beSpot = spots[i] + t * (spots[i + 1] - spots[i]);
          traces.push({
            x: [beSpot, beSpot], y: [yMin * 0.3, yMax * 0.3],
            type: "scatter", mode: "lines+text",
            name: `BE $${beSpot.toFixed(0)}`,
            text: [null, `BE $${beSpot.toFixed(0)}`],
            textposition: "top center",
            textfont: { color: "#d29922", size: 10 },
            line: { color: "#d29922", width: 1, dash: "dot" },
            showlegend: false,
          });
        }
      }
    }
  }

  const annotation = hasData ? [] : [{
    text: "Select a strategy and click<br><b>Price via Vol Surface</b>",
    xref: "paper", yref: "paper", x: 0.5, y: 0.5,
    showarrow: false,
    font: { size: 14, color: "#484f58" },
  }];

  const cc = chartColors();
  Plotly.react("sb-structure-chart", traces, {
    paper_bgcolor: cc.paper, plot_bgcolor: cc.plot,
    xaxis: {
      title: hasData ? "ETH Spot (USD)" : "",
      color: cc.muted, gridcolor: cc.grid, zerolinecolor: cc.zeroline,
      dtick: 500,
      showticklabels: hasData,
    },
    yaxis: {
      title: hasData ? "P&L (USD)" : "",
      color: cc.muted, gridcolor: cc.grid,
      zerolinecolor: hasData ? "#f85149" : cc.grid,
      zerolinewidth: hasData ? 2 : 1,
      showticklabels: hasData,
    },
    annotations: annotation,
    margin: { t: 15, r: 25, b: hasData ? 45 : 20, l: hasData ? 70 : 30 },
    showlegend: hasData,
    legend: { font: { color: cc.muted, size: 10 }, orientation: "h", x: 0.5, y: 1.02,
      xanchor: "center", yanchor: "bottom", bgcolor: cc.legendBg },
  }, { responsive: true });
}

// ── Portfolio payoff chart (mirrors Roll tab pattern) ─────

function sbDrawChart(results) {
  const $chart = document.getElementById("sb-payoff-chart");
  const $controls = document.getElementById("sb-chart-controls");
  $chart.style.display = "block";

  const spots = pfData.spot_ladder;
  const horizons = pfData.chart_horizons;
  const spot = pfData.eth_spot;
  const hasRolls = results && results.length > 0;
  const rollMap = hasRolls ? rollBuildRollMap(results) : new Map();
  const allPositions = pfData.positions;
  const includedPositions = allPositions.filter(p => sbIncluded.has(p.id));
  const hasExclusion = allPositions.some(p => !sbIncluded.has(p.id)) || sbRemovedPositions.length > 0;
  const hasNewPositions = sbOriginalPositionIds && allPositions.some(p => !sbOriginalPositionIds.has(p.id));
  const hasScenarioChange = hasRolls || hasExclusion || hasNewPositions;

  // Show toggle controls only when there's a scenario to compare
  $controls.style.display = hasScenarioChange ? "block" : "none";
  const showBaseline = hasScenarioChange ? document.getElementById("sb-show-baseline").checked : true;
  const showScenario = hasScenarioChange ? document.getElementById("sb-show-scenario").checked : false;

  // Baseline = original portfolio (including positions that were removed)
  const baselinePositions = [...allPositions, ...sbRemovedPositions]
    .filter(p => sbOriginalPositionIds.has(p.id));

  const baseColors = ["#f85149", "#d29922", "#e3b341", "#3fb950", "#58a6ff", "#bc8cff"];
  const scenColors = ["#ff7b72", "#e3b341", "#f0d060", "#56d364", "#79c0ff", "#d2a8ff"];
  const traces = [];

  // Baseline portfolio payoff curves (always shown when no scenario, toggled otherwise)
  if (!hasScenarioChange || showBaseline) {
    horizons.forEach((h, i) => {
      const baseCurve = rollBaselineCurve(spots, baselinePositions, h);
      const label = h === 0 ? "Expiry" : `T+${h}d`;
      traces.push({
        x: spots, y: baseCurve, type: "scatter", mode: "lines",
        name: hasScenarioChange ? `Base ${label}` : label,
        line: { color: baseColors[i % baseColors.length], width: 2.5 },
        legendgroup: `h${h}`,
      });
    });
  }

  // Overlay scenario curves when toggled on
  if (hasScenarioChange && showScenario) {
    horizons.forEach((h, i) => {
      const scenCurve = rollSumCurve(spots, includedPositions, h, rollMap);
      const label = h === 0 ? "Expiry" : `T+${h}d`;
      traces.push({
        x: spots, y: scenCurve, type: "scatter", mode: "lines",
        name: `Scenario ${label}`,
        line: { color: scenColors[i % scenColors.length], width: 2.5, dash: "dash" },
        legendgroup: `h${h}`,
      });
    });
  }

  // Spot marker
  const allY = traces.flatMap(t => t.y);
  if (allY.length > 0) {
    traces.push({
      x: [spot, spot], y: [Math.min(...allY), Math.max(...allY)],
      type: "scatter", mode: "lines", name: `Spot $${spot.toFixed(0)}`,
      line: { color: "#3fb950", width: 1.5, dash: "dashdot" }, legendgroup: "spot",
    });
  }

  // Dynamic title
  let titleText = "Portfolio Payoff Profile";
  if (hasScenarioChange) {
    const parts = [];
    if (hasNewPositions) parts.push("New Trades");
    if (hasRolls) parts.push("Rolls");
    if (hasExclusion) parts.push("Removals");
    titleText += ` — Base vs Scenario (${parts.join(" + ")})`;
  }

  const cc = chartColors();
  Plotly.react("sb-payoff-chart", traces, {
    title: { text: titleText, font: { color: cc.text, size: 16 } },
    paper_bgcolor: cc.paper, plot_bgcolor: cc.plot,
    xaxis: { title: "ETH Spot Price (USD)", color: cc.muted, gridcolor: cc.grid, zerolinecolor: cc.zeroline, dtick: 500 },
    yaxis: { title: "P&L (USD)", color: cc.muted, gridcolor: cc.grid, zerolinecolor: "#f85149", zerolinewidth: 2 },
    margin: { t: 50, r: 250, b: 50, l: 80 },
    showlegend: true,
    legend: { font: { color: cc.muted, size: 10 }, orientation: "v", x: 1.02, y: 1,
      xanchor: "left", yanchor: "top", bgcolor: cc.legendBg, bordercolor: cc.legendBorder, borderwidth: 1 },
  }, { responsive: true });
}

// ── MTM Grid (Base vs Scenario — mirrors Roll tab) ───────

function sbRenderMtmGrid(results) {
  const $section = document.getElementById("sb-mtm-section");
  $section.style.display = "block";
  const spots = pfData.spot_ladder;
  const horizons = pfData.matrix_horizons;
  const ethSpot = pfData.eth_spot;
  const allPositions = pfData.positions;
  const includedPositions = allPositions.filter(p => sbIncluded.has(p.id));
  const hasRolls = results && results.length > 0;
  const hasExclusion = allPositions.some(p => !sbIncluded.has(p.id)) || sbRemovedPositions.length > 0;
  const hasNewPositions = sbOriginalPositionIds && allPositions.some(p => !sbOriginalPositionIds.has(p.id));
  const hasScenarioChange = hasRolls || hasExclusion || hasNewPositions;
  const rollMap = hasRolls ? rollBuildRollMap(results) : new Map();

  // Baseline = original portfolio including removed positions
  const baselinePositions = [...allPositions, ...sbRemovedPositions]
    .filter(p => sbOriginalPositionIds.has(p.id));

  // Update section title based on whether there's a scenario
  const $title = $section.querySelector("h2");
  $title.innerHTML = hasScenarioChange
    ? "Portfolio MTM Matrix &mdash; Base vs Scenario"
    : "Portfolio MTM Matrix";

  const scenLabel = hasNewPositions
    ? (hasRolls ? "Rolled+New" : "With New")
    : (hasRolls ? "Rolled" : "Selected");

  // Header (same pattern as Roll tab: Base | Scenario)
  const thead = document.getElementById("sb-mtm-grid-thead");
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

  // Pick spots at $500 increments
  const displaySpots = [];
  for (let s = 500; s <= 7000; s += 500) {
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

  // Pre-compute curves: baseline = original portfolio, scenario = included + rolls
  const baseCurves = {}, scenCurves = {};
  for (const h of horizons) {
    baseCurves[h] = rollBaselineCurve(spots, baselinePositions, h);
    if (hasScenarioChange) {
      scenCurves[h] = rollSumCurve(spots, includedPositions, h, rollMap);
    }
  }

  const fmtVal = v => { const r = Math.round(v); return r >= 0 ? `$${r.toLocaleString()}` : `-$${Math.abs(r).toLocaleString()}`; };

  const tbody = document.getElementById("sb-mtm-grid-body");
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

function sbRefreshVisuals() {
  sbDrawChart(sbLastResults);
  sbRenderMtmGrid(sbLastResults);
}

// ── Event wiring ─────────────────────────────────────────

document.getElementById("btn-sb-compute").addEventListener("click", sbComputeRoll);

document.getElementById("btn-sb-delete-selected").addEventListener("click", () => {
  if (!pfData) return;
  const ids = [...sbSelected];
  if (ids.length === 0) { alert("Select at least one trade to delete."); return; }
  if (!confirm(`Delete ${ids.length} selected trade${ids.length !== 1 ? "s" : ""} from portfolio?`)) return;
  for (const id of ids) {
    const idx = pfData.positions.findIndex(p => p.id === id);
    if (idx !== -1) {
      sbRemovedPositions.push(pfData.positions[idx]);
      pfData.positions.splice(idx, 1);
    }
    sbIncluded.delete(id);
    sbSelected.delete(id);
    pfSelected.delete(id);
  }
  sbRenderTable();
  sbRefreshVisuals();
});

document.getElementById("btn-sb-select-all").addEventListener("click", () => {
  sbGetFilteredPositions().forEach(p => sbSelected.add(p.id));
  sbRenderTable();
});

document.getElementById("btn-sb-deselect-all").addEventListener("click", () => {
  sbSelected.clear();
  sbLastResults = null;
  sbRenderTable();
  document.getElementById("sb-roll-results-section").style.display = "none";
  sbRefreshVisuals();
});

document.getElementById("sb-check-all").addEventListener("change", (e) => {
  sbGetFilteredPositions().forEach(p => {
    if (e.target.checked) sbSelected.add(p.id); else sbSelected.delete(p.id);
  });
  sbRenderTable();
});

document.getElementById("sb-include-all").addEventListener("change", (e) => {
  sbGetFilteredPositions().forEach(p => {
    if (e.target.checked) sbIncluded.add(p.id); else sbIncluded.delete(p.id);
  });
  sbRenderTable();
  sbRefreshVisuals();
});

document.getElementById("btn-sb-expiring-10d").addEventListener("click", () => {
  if (!pfData) return;
  sbSelected.clear();
  pfData.positions.forEach(p => { if (p.days_remaining <= 10) sbSelected.add(p.id); });
  sbRenderTable();
});

["sb-filter-expiry", "sb-filter-type", "sb-filter-side"].forEach(id => {
  document.getElementById(id).addEventListener("change", () => { if (pfData) sbRenderTable(); });
});

document.getElementById("sb-target-expiry").addEventListener("change", () => { if (pfData) sbRenderTable(); });

// Sort headers
document.querySelectorAll("#sb-trades-table .sortable").forEach(th => {
  th.addEventListener("click", () => {
    const col = th.dataset.col;
    const existing = sbSortStack.findIndex(s => s.col === col);
    if (existing === 0) {
      sbSortStack[0].asc = !sbSortStack[0].asc;
    } else {
      if (existing > 0) sbSortStack.splice(existing, 1);
      sbSortStack.unshift({ col, asc: true });
      if (sbSortStack.length > 2) sbSortStack.length = 2;
    }
    sbRenderTable();
  });
});

// Chart toggles
["sb-show-baseline", "sb-show-scenario"].forEach(id => {
  document.getElementById(id).addEventListener("change", () => sbRefreshVisuals());
});

// Refresh
document.getElementById("btn-sb-refresh").addEventListener("click", async () => {
  sbLoaded = false;
  pfData = null;
  portfolioLoaded = false;
  sbSelected.clear();
  sbLastResults = null;
  sbStructureCurves = null;
  sbLegsPriced = [];
  document.getElementById("sb-roll-results-section").style.display = "none";
  document.getElementById("sb-pricing-results").style.display = "none";
  document.getElementById("btn-sb-add-to-portfolio").style.display = "none";
  await sbInit();
});

// Add Trade form
document.getElementById("btn-sb-add-trade").addEventListener("click", () => {
  const $form = document.getElementById("sb-add-trade-form");
  $form.style.display = $form.style.display === "none" ? "block" : "none";
  if ($form.style.display === "block") {
    if (pfData) {
      const $cp = document.getElementById("sb-add-counterparty");
      const existing = new Set();
      pfData.positions.forEach(p => { if (p.counterparty) existing.add(p.counterparty); });
      existing.add("What-If");
      const prev = $cp.value;
      $cp.innerHTML = [...existing].sort().map(c =>
        `<option value="${c}" ${c === prev ? "selected" : ""}>${c}</option>`
      ).join("");
    }
    document.getElementById("sb-add-instrument").focus();
  }
});

document.getElementById("btn-sb-add-cancel").addEventListener("click", () => {
  document.getElementById("sb-add-trade-form").style.display = "none";
});

document.getElementById("btn-sb-add-confirm").addEventListener("click", () => {
  const instrument = document.getElementById("sb-add-instrument").value.trim().toUpperCase();
  if (!instrument) { alert("Enter an instrument name."); return; }
  const counterparty = document.getElementById("sb-add-counterparty").value;
  const side = document.getElementById("sb-add-side").value;
  const qty = parseFloat(document.getElementById("sb-add-qty").value) || 0;
  const premUsd = parseFloat(document.getElementById("sb-add-premium").value) || 0;
  if (qty <= 0) { alert("Quantity must be positive."); return; }
  if (!pfData) { alert("Portfolio data not loaded yet."); return; }

  get(`/api/portfolio/ticker/${encodeURIComponent(instrument)}`)
    .then(tk => {
      const sign = side === "sell" ? -1 : 1;
      const signedQty = sign * qty;
      const markUsd = tk.mark_price_usd || 0;
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
      const newPos = {
        id: pfNextId++, counterparty, trade_id: null,
        trade_date: new Date().toISOString().split("T")[0],
        side_raw: side === "sell" ? "Sell" : "Buy",
        option_type: opt === "C" ? "Call" : "Put",
        instrument, expiry: tk.expiry || "", days_remaining: dte, strike,
        pct_otm_entry: 0, qty, notional_mm: 0, premium_per: premUsd / qty, premium_usd: premUsd,
        opt, side: sign > 0 ? "Long" : "Short", net_qty: signedQty,
        pct_otm_live: pfData.eth_spot > 0 ? Math.round((strike / pfData.eth_spot - 1) * 1000) / 10 : 0,
        iv_pct: tk.mark_iv || 80, delta: tk.delta, gamma: tk.gamma, theta: tk.theta, vega: tk.vega,
        mark_price_usd: markUsd, current_mtm: Math.round(signedQty * markUsd * 100) / 100,
        notional_live: Math.round(qty * pfData.eth_spot * 100) / 100,
        payoff_by_horizon: payoff,
      };
      pfData.positions.push(newPos);
      pfSelected.add(newPos.id);
      sbIncluded.add(newPos.id);
      sbRenderTable();
      sbRefreshVisuals();
      document.getElementById("sb-add-trade-form").style.display = "none";
      document.getElementById("sb-add-instrument").value = "";
    })
    .catch(e => { alert("Failed to fetch instrument data.\n" + e.message); });
});

// Structure builder
document.getElementById("btn-sb-add-leg").addEventListener("click", () => sbAddLeg());
document.getElementById("btn-sb-price-structure").addEventListener("click", sbPriceStructure);
document.getElementById("btn-sb-add-to-portfolio").addEventListener("click", sbAddToPortfolio);

document.querySelectorAll(".sb-template").forEach(btn => {
  btn.addEventListener("click", () => sbApplyTemplate(btn.dataset.strategy));
});

// Structure toggle
document.getElementById("sb-structure-toggle").addEventListener("click", () => {
  const $body = document.getElementById("sb-structure-body");
  const $icon = document.getElementById("sb-structure-toggle-icon");
  $body.classList.toggle("collapsed");
  $icon.textContent = $body.classList.contains("collapsed") ? "[ + ]" : "[ - ]";
});

// Export
document.getElementById("btn-sb-export-xlsx").addEventListener("click", () => {
  if (!pfData) return;
  const positions = pfData.positions.filter(p => sbIncluded.has(p.id));
  const globalExpiry = sbGetTargetExpiry();
  const rows = [];
  for (const pos of positions) {
    const isRolled = sbSelected.has(pos.id) && sbLastResults && sbLastResults.some(r => r.pos.id === pos.id);
    const result = isRolled ? sbLastResults.find(r => r.pos.id === pos.id) : null;
    rows.push({
      "Include": sbIncluded.has(pos.id) ? "Yes" : "",
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
      "New Expiry": result ? result.newExpCode : null,
      "New Strike": result ? result.newStrike : null,
      "New Qty": result ? result.newNetQty : null,
      "New Mark": result ? Math.round(result.newMark * 100) / 100 : null,
      "Roll Cost": result ? Math.round(result.cost) : null,
      "Direction": result ? (result.cost >= 0 ? "Receive" : "Pay") : "",
    });
  }
  const wb = XLSX.utils.book_new();
  const ws = XLSX.utils.json_to_sheet(rows);
  XLSX.utils.book_append_sheet(wb, ws, "Strategy Builder");
  const dateStr = new Date().toISOString().split("T")[0];
  XLSX.writeFile(wb, `PLGO_Strategy_Builder_${dateStr}.xlsx`);
});

// ─── Go! ────────────────────────────────────────────────────
init();

/* ================================================================
   OPTIMIZER V2 TAB
   ================================================================ */

let optv2Data = null;

document.getElementById("btn-load-optv2").addEventListener("click", async () => {
  const $btn = document.getElementById("btn-load-optv2");
  $btn.classList.add("loading");
  $btn.textContent = "Loading…";

  try {
    optv2Data = await get("/api/portfolio/pnl");
    optv2OptResult = null;  // reset so chart shows all horizons
    // Hide the "After" matrix panel
    document.getElementById("optv2-matrix-after-panel").style.display = "none";
    document.getElementById("optv2-matrix-grid").style.gridTemplateColumns = "1fr";
    optv2RenderAll();

    // Populate expiry dropdown from vol surface
    const $expiry = document.getElementById("optv2-target-expiry");
    $expiry.innerHTML = '<option value="">All Maturities</option>';
    if (optv2Data.vol_surface) {
      const smiles = optv2Data.vol_surface
        .filter(s => s.dte > 0)
        .sort((a, b) => a.dte - b.dte);
      smiles.forEach(s => {
        const opt = document.createElement("option");
        opt.value = s.expiry_code;
        opt.textContent = `${s.expiry_code} (${s.dte}d)`;
        $expiry.appendChild(opt);
      });
      // Default: "All Maturities" (empty value) is already selected
    }

    document.getElementById("btn-run-optv2").disabled = false;
  } catch (e) {
    console.error("Optimizer v2: failed to load portfolio:", e);
    alert("Failed to load portfolio data — check console.\n" + e.message);
  } finally {
    $btn.classList.remove("loading");
    $btn.textContent = "Load Risk Profile";
  }
});

const OPTV2_HORIZONS = [0, 16, 30, 60, 90];

function optv2RenderAll() {
  if (!optv2Data) return;

  document.getElementById("optv2-greeks-section").style.display = "";
  document.getElementById("optv2-payoff-section").style.display = "";
  document.getElementById("optv2-matrix-section").style.display = "";

  optv2RenderGreeks();
  optv2RenderPayoff();
  optv2RenderMatrix();
}

/* ── Greeks summary ─────────────────────────────────────────── */
function optv2RenderGreeks() {
  const t = optv2Data.totals;
  document.getElementById("optv2-total-delta").textContent  = optv2Fmt(t.portfolio_delta, 2);
  document.getElementById("optv2-total-gamma").textContent  = optv2Fmt(t.portfolio_gamma, 4);
  document.getElementById("optv2-total-theta").textContent  = optv2Fmt(t.portfolio_theta, 2);
  document.getElementById("optv2-total-vega").textContent   = optv2Fmt(t.portfolio_vega, 2);
  document.getElementById("optv2-total-mtm").textContent    = "$" + optv2Fmt(t.current_total_mtm, 2);
  document.getElementById("optv2-eth-spot").textContent     = "$" + optv2Fmt(optv2Data.eth_spot, 2);
}

let optv2OptResult = null;  // stores latest optimization result

/* ── Payoff profile chart (Plotly) — multi-horizon, log-moneyness x-axis ── */
function optv2RenderPayoff() {
  const spots = optv2Data.spot_ladder;
  const positions = optv2Data.positions;
  const S0 = optv2Data.eth_spot;

  // Compute log-moneyness: ln(K / S0)
  const logM = spots.map(s => Math.log(s / S0));

  // Only show ticks at valid data points: drop any generated in extrapolation regions
  const visibleIdx = [];
  for (let i = 0; i < spots.length; i++) {
    let hasData = false;
    for (const h of OPTV2_HORIZONS) {
      if (positions[0].payoff_by_horizon[h] && positions[0].payoff_by_horizon[h][i] != null) {
        hasData = true; break;
      }
    }
    if (hasData) visibleIdx.push(i);
  }

  // Build strike tick labels — spacing proportional to abs(log-moneyness)
  // Near ATM (small |lm|) → dense ticks;  in wings (large |lm|) → sparse ticks
  const tickVals = [];
  const tickTexts = [];
  const MIN_LM_GAP = 0.018;   // minimum log-moneyness gap between consecutive ticks
  const LM_GAP_SCALE = 0.12;  // how fast gaps grow with distance from ATM

  let lastTickLM = -Infinity;
  for (const i of visibleIdx) {
    const lm = logM[i];
    const absLM = Math.abs(lm);

    // Required gap grows linearly with distance from ATM:
    //   gap(lm) = MIN_LM_GAP + LM_GAP_SCALE * |lm|
    // This means at ATM (|lm|≈0) we get a tick every ~0.018 in lm-space (~1.8%)
    // and at |lm|=0.5 we need a gap of ~0.078 (~8%) between ticks
    const requiredGap = MIN_LM_GAP + LM_GAP_SCALE * absLM;

    if (lm - lastTickLM >= requiredGap) {
      // Snap to a "round" strike for clean labels
      const s = spots[i];
      const roundTo = absLM < 0.08 ? 100 : absLM < 0.20 ? 200 : 500;
      if (s % roundTo === 0) {
        tickVals.push(lm);
        tickTexts.push("$" + s.toLocaleString());
        lastTickLM = lm;
      }
    }
  }

  const traces = [];

  // If we have optimization results, show only 90d Before vs After
  if (optv2OptResult && optv2OptResult.status === "ok") {
    // Before (90d)
    const beforeCurve = optv2OptResult.before.payoff_by_horizon["90"];
    if (beforeCurve) {
      traces.push({
        x: logM, y: beforeCurve, mode: "lines",
        name: "Before (T+90d)",
        line: { color: "#e57373", width: 2, dash: "dash" },
      });
    }
    // After (90d)
    const afterCurve = optv2OptResult.after.payoff_by_horizon["90"];
    if (afterCurve) {
      traces.push({
        x: logM, y: afterCurve, mode: "lines",
        name: "After (T+90d)",
        line: { color: "#4fc3f7", width: 3 },
      });
    }
  } else {
    // Default: show all horizons
    const horizonColors = {
      0:  "#4fc3f7",  // cyan — Now
      16: "#ba68c8",  // purple
      30: "#ffb74d",  // orange
      60: "#81c784",  // green
      90: "#e57373",  // red
    };

    for (const h of OPTV2_HORIZONS) {
      const hKey = String(h);
      const totalPayoff = new Array(spots.length).fill(0);
      let hasData = false;

      positions.forEach(p => {
        const curve = p.payoff_by_horizon[hKey];
        if (curve) {
          hasData = true;
          for (let i = 0; i < curve.length; i++) totalPayoff[i] += curve[i];
        }
      });

      if (!hasData) continue;

      traces.push({
        x: logM, y: totalPayoff, mode: "lines",
        name: h === 0 ? "Now (0d)" : `T+${h}d`,
        line: { color: horizonColors[h] || "#8b949e", width: h === 0 ? 3 : 2 },
      });
    }
  }

  // Zero line
  traces.push({
    x: [logM[0], logM[logM.length - 1]],
    y: [0, 0],
    mode: "lines",
    name: "Break-even",
    line: { color: "rgba(255,255,255,0.25)", dash: "dot", width: 1 },
    showlegend: false,
  });

  // Current spot marker (log-moneyness = 0 by definition)
  const spotPayoff0 = (() => {
    const totalPayoff = new Array(spots.length).fill(0);
    positions.forEach(p => {
      const curve = p.payoff_by_horizon["0"];
      if (curve) for (let i = 0; i < curve.length; i++) totalPayoff[i] += curve[i];
    });
    return totalPayoff[optv2NearestIdx(spots, S0)];
  })();

  traces.push({
    x: [0],
    y: [spotPayoff0],
    mode: "markers",
    name: `Current Spot ($${S0.toLocaleString()})`,
    marker: { color: "#ffca28", size: 10, symbol: "diamond" },
  });

  const layout = {
    title: { text: optv2OptResult ? "Payoff Profile — Before vs After (T+90d)" : "Portfolio Payoff Profile — All Positions", font: { color: "#e6edf3", size: 16 } },
    xaxis: {
      title: "Log-Moneyness  ln(K / Spot)",
      tickvals: tickVals,
      ticktext: tickTexts,
      tickangle: -45,
      color: "#8b949e",
      gridcolor: "#21262d",
      zerolinecolor: "#ffca28",
      zerolinewidth: 1.5,
    },
    yaxis: {
      title: "MTM (USD)",
      tickformat: ",.0f",
      zeroline: true,
      zerolinecolor: "#f85149",
      zerolinewidth: 1,
      color: "#8b949e",
      gridcolor: "#21262d",
    },
    paper_bgcolor: "#161b22",
    plot_bgcolor: "#0d1117",
    font: { color: "#e0e0e0" },
    legend: {
      font: { color: "#8b949e", size: 11 },
      orientation: "h",
      y: -0.22,
    },
    margin: { t: 50, b: 80, l: 80, r: 30 },
  };

  Plotly.newPlot("optv2-payoff-chart", traces, layout, { responsive: true });
}

/* ── Scenario matrix: spot (rows) × horizon (columns) ──────── */
function optv2RenderMatrix() {
  const spots    = optv2Data.spot_ladder;
  const positions = optv2Data.positions;
  const ethSpot  = optv2Data.eth_spot;
  const step     = spots.length > 1 ? spots[1] - spots[0] : 100;

  // Header
  const $thead = document.getElementById("optv2-matrix-thead");
  $thead.innerHTML = "";
  const headRow = document.createElement("tr");
  headRow.innerHTML = "<th>ETH Spot</th>";
  OPTV2_HORIZONS.forEach(h => {
    const th = document.createElement("th");
    th.textContent = h === 0 ? "Now" : `${h}d`;
    headRow.appendChild(th);
  });
  $thead.appendChild(headRow);

  // Body
  const $tbody = document.getElementById("optv2-matrix-tbody");
  $tbody.innerHTML = "";

  spots.forEach((s, si) => {
    // Only show rows at $500 increments
    if (s % 500 !== 0) return;

    const tr = document.createElement("tr");
    if (Math.abs(s - ethSpot) < step / 2) tr.classList.add("row-highlight");

    const tdSpot = document.createElement("td");
    tdSpot.textContent = "$" + s.toLocaleString();
    tdSpot.style.fontWeight = "600";
    tr.appendChild(tdSpot);

    OPTV2_HORIZONS.forEach(h => {
      const hKey = String(h);
      let cellVal = 0;
      positions.forEach(p => {
        const curve = p.payoff_by_horizon[hKey];
        if (curve && curve[si] !== undefined) cellVal += curve[si];
      });

      const td = document.createElement("td");
      td.textContent = Math.round(cellVal).toLocaleString();
      td.style.textAlign = "right";
      if (cellVal > 0)  td.style.color = "#66bb6a";
      if (cellVal < 0)  td.style.color = "#ef5350";
      tr.appendChild(td);
    });

    $tbody.appendChild(tr);
  });
}

/* ── Helpers (namespaced to avoid collisions) ───────────────── */
function optv2Fmt(v, decimals) {
  if (v == null || isNaN(v)) return "—";
  return Number(v).toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function optv2NearestIdx(arr, val) {
  let best = 0, bestDist = Math.abs(arr[0] - val);
  for (let i = 1; i < arr.length; i++) {
    const d = Math.abs(arr[i] - val);
    if (d < bestDist) { best = i; bestDist = d; }
  }
  return best;
}

/* ── Run Optimizer ──────────────────────────────────────────── */
document.getElementById("btn-run-optv2").addEventListener("click", async () => {
  const $btn = document.getElementById("btn-run-optv2");
  $btn.classList.add("loading");
  $btn.textContent = "Running…";
  $btn.disabled = true;

  try {
    const params = {
      risk_aversion: parseFloat(document.getElementById("optv2-risk-aversion").value) || 1.0,
      lambda_delta: parseFloat(document.getElementById("optv2-lambda-delta").value) || 1.0,
      lambda_vega: parseFloat(document.getElementById("optv2-lambda-vega").value) || 100.0,
      txn_cost_pct: parseFloat(document.getElementById("optv2-txn-cost").value) || 5.0,
      max_collateral: parseFloat(document.getElementById("optv2-max-collateral").value) || 4000000,
      target_expiry: document.getElementById("optv2-target-expiry").value || null,
    };
    const res = await fetch("/api/optimization/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || res.statusText);
    }
    const data = await res.json();
    console.log("Optimizer v2 result:", data);
    optv2RenderResult(data);
  } catch (e) {
    console.error("Optimizer v2: run failed:", e);
    alert("Optimizer failed — check console.\n" + e.message);
  } finally {
    $btn.classList.remove("loading");
    $btn.textContent = "Run Optimizer";
    $btn.disabled = false;
  }
});

/* ── Render optimization results ───────────────────────────── */
function optv2RenderResult(data) {
  const $section = document.getElementById("optv2-result-section");

  if (data.status !== "ok") {
    $section.style.display = "none";
    alert(data.message || "Optimization returned no results.");
    return;
  }

  $section.style.display = "";

  // Store result and re-render main payoff chart with before/after overlay
  optv2OptResult = data;
  optv2RenderPayoff();

  // Status badge
  document.getElementById("optv2-result-status").textContent =
    data.optimizer_converged ? "Converged" : "Did not converge";

  // Before
  const b = data.before;
  document.getElementById("optv2-before-delta").textContent = optv2Fmt(b.delta, 2);
  document.getElementById("optv2-before-gamma").textContent = optv2Fmt(b.gamma, 4);
  document.getElementById("optv2-before-theta").textContent = optv2Fmt(b.theta, 2);
  document.getElementById("optv2-before-vega").textContent  = optv2Fmt(b.vega, 2);
  document.getElementById("optv2-before-risk").textContent  = "$" + optv2Fmt(b.daily_risk, 2);

  // After
  const a = data.after;
  document.getElementById("optv2-after-delta").textContent = optv2Fmt(a.delta, 2);
  document.getElementById("optv2-after-gamma").textContent = optv2Fmt(a.gamma, 4);
  document.getElementById("optv2-after-theta").textContent = optv2Fmt(a.theta, 2);
  document.getElementById("optv2-after-vega").textContent  = optv2Fmt(a.vega, 2);
  document.getElementById("optv2-after-risk").textContent  = "$" + optv2Fmt(a.daily_risk, 2);

  // Summary stats
  document.getElementById("optv2-trade-cost").textContent     = "$" + optv2Fmt(data.total_trade_cost, 2);
  document.getElementById("optv2-risk-reduction").textContent  = "$" + optv2Fmt(b.daily_risk - a.daily_risk, 2);
  document.getElementById("optv2-utility-gain").textContent    = optv2Fmt(data.utility_improvement, 2);
  document.getElementById("optv2-candidates").textContent      = data.candidates_evaluated;

  // Show "After" matrix next to the main P&L Matrix
  const $afterPanel = document.getElementById("optv2-matrix-after-panel");
  const $matrixGrid = document.getElementById("optv2-matrix-grid");
  $afterPanel.style.display = "";
  $matrixGrid.style.gridTemplateColumns = "1fr 1fr";
  optv2RenderCompareMatrix(data, "after", "optv2-matrix-after-main-thead", "optv2-matrix-after-main-tbody");

  // Trades table
  const $tbody = document.getElementById("optv2-trades-tbody");
  $tbody.innerHTML = "";

  if (data.trades.length === 0) {
    const tr = document.createElement("tr");
    tr.innerHTML = '<td colspan="12" style="text-align:center;opacity:.6">No trades proposed — portfolio is already optimal given cost constraints.</td>';
    $tbody.appendChild(tr);
    return;
  }

  data.trades.forEach(t => {
    const tr = document.createElement("tr");
    tr.innerHTML = [
      `<td>${t.instrument}</td>`,
      `<td style="color:${t.side === "Buy" ? "var(--green)" : "var(--red)"}">${t.side}</td>`,
      `<td>${Math.abs(t.qty)}</td>`,
      `<td>${t.strike.toLocaleString()}</td>`,
      `<td>${t.opt === "C" ? "Call" : "Put"}</td>`,
      `<td>${t.dte}</td>`,
      `<td>${optv2Fmt(t.iv_pct, 1)}</td>`,
      `<td>${optv2Fmt(t.bs_price_usd, 2)}</td>`,
      `<td>${optv2Fmt(t.notional, 2)}</td>`,
      `<td style="text-align:center"><span style="font-weight:700;font-size:.8rem;color:${t.is_unwind ? 'var(--green)' : 'var(--muted)'}">${t.is_unwind ? '✓ Yes' : '✗ No'}</span></td>`,
      `<td>${optv2Fmt(t.delta_contribution, 4)}</td>`,
      `<td>${optv2Fmt(t.gamma_contribution, 6)}</td>`,
      `<td>${optv2Fmt(t.vega_contribution, 4)}</td>`,
    ].join("");
    $tbody.appendChild(tr);
  });
}

/* ── Before/After payoff comparison charts ──────────────────── */
/* ── Before/After P&L matrix ───────────────────────────────── */
function optv2RenderCompareMatrix(data, side, theadId, tbodyId) {
  const spots = data.spot_ladder;
  const ethSpot = data.eth_spot;
  const horizons = data.chart_horizons || [0, 16, 30, 60, 90];
  const payoff = data[side].payoff_by_horizon;
  const step = spots.length > 1 ? spots[1] - spots[0] : 100;

  const $thead = document.getElementById(theadId);
  $thead.innerHTML = "";
  const headRow = document.createElement("tr");
  headRow.innerHTML = "<th>ETH Spot</th>";
  horizons.forEach(h => {
    const th = document.createElement("th");
    th.textContent = h === 0 ? "Now" : `${h}d`;
    headRow.appendChild(th);
  });
  $thead.appendChild(headRow);

  const $tbody = document.getElementById(tbodyId);
  $tbody.innerHTML = "";

  spots.forEach((s, si) => {
    if (s % 500 !== 0) return;

    const tr = document.createElement("tr");
    if (Math.abs(s - ethSpot) < step / 2) tr.classList.add("row-highlight");

    const tdSpot = document.createElement("td");
    tdSpot.textContent = "$" + s.toLocaleString();
    tdSpot.style.fontWeight = "600";
    tr.appendChild(tdSpot);

    horizons.forEach(h => {
      const curve = payoff[String(h)];
      const cellVal = (curve && curve[si] !== undefined) ? curve[si] : 0;
      const td = document.createElement("td");
      td.textContent = Math.round(cellVal).toLocaleString();
      td.style.textAlign = "right";
      if (cellVal > 0) td.style.color = "#66bb6a";
      if (cellVal < 0) td.style.color = "#ef5350";
      tr.appendChild(td);
    });

    $tbody.appendChild(tr);
  });
}