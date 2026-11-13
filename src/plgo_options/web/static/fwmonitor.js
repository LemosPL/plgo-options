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

  function positionCards() {
    if (!positions.length) {
      $("fm-positions").innerHTML = `<section class="card" style="text-align:center;color:var(--muted)">No open forwards or synthetic forwards.</section>`;
      return;
    }
    $("fm-positions").innerHTML = positions.map((p) => {
      const k = pk(p), s = short(p);
      const kind = p.kind === "synthetic"
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
              ${fmt(p.qty)} ${p.asset} @ ${px(p.asset, p.forward_price)}${allIn}</div>
            <div class="optv3-muted" style="font-size:.74rem">traded ${maturityText(p.trade_date)} · matures ${maturityText(p.maturity_date)} (${fmt(p.days_to_maturity)}d)${p.external_ref ? ` · <span title="${esc(p.external_ref)}">ref ${esc(String(p.external_ref).slice(0, 8))}</span>` : ""}</div>
            ${p.legs.length ? `<div class="optv3-muted" style="font-size:.7rem">${esc(p.legs.join(" · "))}</div>` : ""}
          </div>
          <div class="fm-mtm"><div class="optv3-muted" style="font-size:.68rem;text-transform:uppercase;letter-spacing:.05em">MTM</div>
            <div style="${sign(p.mtm)}">${usdK(p.mtm)}</div></div>
        </div>
        ${ladder(p)}
        <div class="fm-lvrow">${levelBox(p, p.levels.stop, "stop")}${levelBox(p, p.levels.target, "target")}</div>
        ${p.note && k !== editing ? `<div class="optv3-muted fm-note">“${esc(p.note)}”</div>` : ""}
        ${k === editing ? editor(p) : `<div class="fm-card-btns">
          <button class="btn-secondary" data-act="edit" style="width:auto">${p.stop_loss || p.take_profit ? "Edit levels" : "Set levels"}</button>
          <button class="btn-secondary" data-act="chart" style="width:auto">${k === selected ? "On chart ↓" : "Show on chart"}</button></div>`}
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
      margin: { t: 48, r: 20, b: 40, l: 70 }, showlegend: true,
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

  function renderAll() { summary(); positionCards(); priceChart(); }

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
    let embedded = 0;
    res.forEach((r, i) => {
      const a = ASSETS[i];
      if (r.status === "rejected") { books[a] = { error: r.reason?.message || "unavailable" }; return; }
      books[a] = r.value;
      embedded += r.value.embedded_synthetics || 0;
      for (const p of r.value.positions) positions.push({ ...p, asset: a, spot: r.value.spot });
    });
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
      note: clear ? "" : (card.querySelector('[data-f="note"]')?.value || "") };
    try {
      await api("PUT", "/api/forwards/levels", body);
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
      setBadge(res.flatMap((r) => (r.status === "fulfilled" ? r.value.positions : [])));
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
  $("fm-positions")?.addEventListener("keydown", (ev) => {
    if (!ev.target.closest(".fm-edit")) return;
    const k = ev.target.closest(".fm-card")?.dataset.key;
    if (ev.key === "Enter") save(k, false);
    else if (ev.key === "Escape") { editing = null; positionCards(); }
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && document.title.startsWith("⚠")) document.title = baseTitle; });
})();
