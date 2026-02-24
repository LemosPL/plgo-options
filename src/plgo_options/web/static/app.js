"use strict";

// ─── State ──────────────────────────────────────────────────
let ethSpot = null;
let optionChain = [];   // OptionTicker[]
let legs = [];          // {side, type, strike, premium, quantity}

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

// ─── Bootstrap ──────────────────────────────────────────────
async function init() {
  // Fetch spot
  try {
    const data = await get("/api/market/spot");
    ethSpot = data.eth_spot;
    $spot.textContent = `$${ethSpot.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
    $spotMin.value = Math.round(ethSpot * 0.4);
    $spotMax.value = Math.round(ethSpot * 2.0);
  } catch (e) {
    $spot.textContent = "Error";
    console.error("Spot fetch failed:", e);
  }

  // Fetch expirations
  try {
    const expiries = await get("/api/market/expirations");
    $expSel.innerHTML = "";
    for (const exp of expiries) {
      const opt = document.createElement("option");
      opt.value = exp;
      opt.textContent = exp;
      $expSel.appendChild(opt);
    }
    $btnLoad.disabled = false;
  } catch (e) {
    $expSel.innerHTML = '<option value="">Failed to load</option>';
    console.error("Expiry fetch failed:", e);
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
function addLeg(side = "buy", type = "C", strike = "", premium = "0", quantity = "1") {
  legs.push({ side, type, strike: String(strike), premium: String(premium), quantity: String(quantity) });
  renderLegs();
}

function removeLeg(idx) {
  legs.splice(idx, 1);
  renderLegs();
}

function renderLegs() {
  $legsBody.innerHTML = "";
  legs.forEach((leg, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>
        <select data-i="${i}" data-field="side">
          <option value="buy"  ${leg.side === "buy"  ? "selected" : ""}>Buy</option>
          <option value="sell" ${leg.side === "sell" ? "selected" : ""}>Sell</option>
        </select>
      </td>
      <td>
        <select data-i="${i}" data-field="type">
          <option value="C" ${leg.type === "C" ? "selected" : ""}>Call</option>
          <option value="P" ${leg.type === "P" ? "selected" : ""}>Put</option>
        </select>
      </td>
      <td><input type="number" data-i="${i}" data-field="strike" value="${leg.strike}" step="10"></td>
      <td><input type="number" data-i="${i}" data-field="premium" value="${leg.premium}" step="0.001"></td>
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
  const strike = parts[2];
  const type = parts[3];
  const premiumEth = opt.mark_price || 0;
  const premiumUsd = ethSpot ? (premiumEth * ethSpot).toFixed(2) : premiumEth.toFixed(4);

  addLeg("buy", type, strike, premiumUsd, "1");
}

// ─── Deribit Replication Pricing ────────────────────────────
let lastReplPremiums = [];

async function replicateStrategy() {
  const expiry = $expSel.value;
  if (!expiry) {
    alert("Select an expiry first.");
    return;
  }
  if (legs.length === 0) {
    alert("Add at least one leg to the strategy.");
    return;
  }
  for (const l of legs) {
    const s = parseFloat(l.strike);
    if (isNaN(s) || s <= 0) {
      alert("Each leg must have a valid strike price.");
      return;
    }
  }

  $btnRepl.classList.add("loading");
  $btnRepl.textContent = "Fetching Deribit data…";

  const apiLegs = legs.map(l => ({
    strike:   parseFloat(l.strike),
    type:     l.type,
    premium:  0,
    quantity: parseFloat(l.quantity),
    is_long:  l.side === "buy",
  }));

  try {
    const data = await post("/api/pricing/replicate", {
      expiry:         expiry,
      legs:           apiLegs,
      spot_min:       parseFloat($spotMin.value),
      spot_max:       parseFloat($spotMax.value),
      num_points:     500,
    });

    lastReplPremiums = data.legs.map(l => l.bs_premium_usd);

    // Summary
    const $summary = document.getElementById("repl-summary");
    $summary.innerHTML =
      `Expiry: <strong>${data.expiry}</strong> &nbsp;|&nbsp; ` +
      `T = ${(data.time_to_expiry * 365.25).toFixed(0)}d &nbsp;|&nbsp; ` +
      `ETH = $${data.eth_spot.toLocaleString()} &nbsp;|&nbsp; ` +
      `Net cost: <strong>$${data.total_cost_usd.toLocaleString()}</strong> ` +
      `(${data.total_cost_eth.toFixed(4)} ETH)`;

    // Per-leg table
    const $body = document.getElementById("repl-body");
    $body.innerHTML = "";
    for (const d of data.legs) {
      const tr = document.createElement("tr");
      const sideColor = d.side === "buy" ? "var(--accent)" : "var(--orange)";
      const typeColor = d.type === "C" ? "var(--green)" : "var(--red)";
      tr.innerHTML = `
        <td style="color:${sideColor}">${d.side.toUpperCase()}</td>
        <td style="color:${typeColor}">${d.type === "C" ? "Call" : "Put"}</td>
        <td>${d.strike}</td>
        <td>${d.iv_pct.toFixed(1)}%</td>
        <td style="font-family:monospace">${d.bs_premium_eth.toFixed(6)}</td>
        <td style="font-family:monospace">$${d.bs_premium_usd.toFixed(2)}</td>
      `;
      $body.appendChild(tr);
    }
    document.getElementById("repl-results-section").style.display = "block";

    // Payoff chart
    const allPnl = [...data.pnl_expiry, ...data.pnl_now];
    const yMin = Math.min(...allPnl);
    const yMax = Math.max(...allPnl);

    const traces = [
      {
        x: data.spots, y: data.pnl_expiry,
        type: "scatter", mode: "lines",
        name: "P&L at Expiry",
        line: { color: "#58a6ff", width: 2.5 },
      },
      {
        x: data.spots, y: data.pnl_now,
        type: "scatter", mode: "lines",
        name: "P&L Now (BS)",
        line: { color: "#d29922", width: 2, dash: "dash" },
      },
    ];

    const uniqueStrikes = [...new Set(data.legs.map(l => l.strike))];
    for (const k of uniqueStrikes) {
      traces.push({
        x: [k, k], y: [yMin, yMax],
        type: "scatter", mode: "lines",
        name: `K=${k}`, showlegend: false,
        line: { color: "#8b949e", width: 1, dash: "dot" },
      });
    }

    traces.push({
      x: [data.eth_spot, data.eth_spot], y: [yMin, yMax],
      type: "scatter", mode: "lines",
      name: `Spot $${data.eth_spot.toFixed(0)}`,
      line: { color: "#3fb950", width: 1.5, dash: "dash" },
    });

    Plotly.react("payoff-chart", traces, chartLayout(), { responsive: true });

    // Vol smile chart
    drawSmileChart(data.smile, data.legs);

  } catch (e) {
    console.error("Replication failed:", e);
    alert("Replication pricing failed — check console.\n" + e.message);
  } finally {
    $btnRepl.classList.remove("loading");
    $btnRepl.textContent = "Price via Deribit Vol Surface";
  }
}

function drawSmileChart(smile, pricedLegs) {
  const $el = document.getElementById("smile-chart");
  $el.style.display = "block";

  const traces = [
    {
      x: smile.smile_strikes, y: smile.smile_ivs,
      type: "scatter", mode: "lines",
      name: "Vol Smile (interpolated)",
      line: { color: "#58a6ff", width: 2 },
    },
    {
      x: smile.observed_strikes, y: smile.observed_ivs,
      type: "scatter", mode: "markers",
      name: "Deribit Market IV",
      marker: { color: "#e6edf3", size: 6, symbol: "circle" },
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

  const layout = {
    title: { text: "Deribit Implied Volatility Smile", font: { color: "#e6edf3", size: 16 } },
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
const $controls = document.getElementById("controls");

document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    // Deactivate all
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-pane").forEach(p => p.classList.remove("active"));

    // Activate selected
    btn.classList.add("active");
    const pane = document.getElementById(`tab-${btn.dataset.tab}`);
    pane.classList.add("active");

    // Show left-panel controls only on the Pricing tab
    $controls.style.display = btn.dataset.tab === "pricing" ? "" : "none";

    // Lazy-load positions on first visit
    if (btn.dataset.tab === "positions" && !positionsLoaded) {
      loadPositions();
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

// ─── Go! ────────────────────────────────────────────────────
init();