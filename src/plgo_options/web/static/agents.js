// Agents page: the B2 desk timetable as a task list, the strategy actually in
// force, the proposals with a route into Pricing, and the decision log.
// Self-contained; loads when the Agents nav item is clicked.
(function () {
  // Part B2's hour-by-hour table. Kept next to each agent so the screen reads
  // like the manual rather than like a job queue.
  const SCHEDULE = {
    "row-watcher":    { when: "every 5 min",   note: "spot vs Monday's rows" },
    "morning-open":   { when: "09:00",         note: "open: spot, MTM, rows, margin" },
    "optimizer":      { when: "09:30 · 16:00", note: "v4 sweep, two-curve check (B2a)" },
    "handover":       { when: "15:30",         note: "the six handover lines (B2b)" },
    "night-desk":     { when: "23:00",         note: "mark, cost, night orders" },
    "close-check":    { when: "02:00",         note: "orders live, margin ok" },
    "monday-pack":    { when: "Mon 07:30",     note: "the week, and the three decisions" },
    "monthly-review": { when: "1st, 08:00",    note: "dealing cost vs decay (A3)" },
  };
  const ORDER = ["morning-open", "optimizer", "row-watcher", "handover",
                 "night-desk", "close-check", "monday-pack", "monthly-review"];

  // Colour says what the agent DID, not merely that it ran.
  const C = { err: "#ef4444", prop: "#3b82f6", sent: "#8b5cf6",
              ok: "#10b981", idle: "#64748b" };

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const asset = () => (typeof currentAsset !== "undefined" ? currentAsset : "ETH");
  let selected = null, PROPS = [], DOC = null;

  async function api(url, opts) {
    const r = await fetch(url, opts);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || r.statusText);
    return j;
  }

  const money = (v) => (v == null || v === "" || !isFinite(v)) ? "—"
    : (Math.abs(v) >= 1e6 ? "$" + (v / 1e6).toFixed(2).replace(/\.00$/, "") + "M"
    : Math.abs(v) >= 1e3 ? "$" + Math.round(v / 1e3) + "k" : "$" + Number(v).toFixed(0));
  const px = (a, v) => v == null ? "—"
    : (a === "FIL" ? Number(v).toFixed(4) : Number(v).toLocaleString());
  const day = (s) => s ? String(s).slice(0, 10) : "—";

  function state(run, nProps) {
    if (!run) return { c: C.idle, label: "never run" };
    if (run.status === "error") return { c: C.err, label: "error" };
    if (nProps) return { c: C.prop, label: nProps + " proposal" + (nProps > 1 ? "s" : "") };
    if (run.delivered) return { c: C.sent, label: "sent to Slack" };
    return { c: C.ok, label: "ran clean" };
  }

  function renderTasks(st, counts) {
    $("agents-tasklist").innerHTML = ORDER.filter((n) => n in st.agents).map((n) => {
      const r = st.agents[n], s = state(r, counts[n] || 0);
      const meta = SCHEDULE[n] || { when: "", note: "" };
      const last = r ? esc(r.started_at.replace("T", " ").slice(5, 16)) : "";
      return '<div class="ag-task' + (selected === n ? " sel" : "") + '" style="--ag-c:' + s.c + '" data-show="' + n + '">' +
        '<span class="ag-dot"></span>' +
        '<span style="min-width:0;flex:1">' +
          '<span class="ag-task-n">' + esc(n) + '</span>' +
          '<span class="ag-task-w"> · ' + esc(meta.when) + '</span>' +
          '<div class="ag-task-w">' + esc(meta.note) + '</div>' +
          '<div class="ag-task-s" style="color:' + s.c + '">' + s.label +
            (last ? ' <span style="color:var(--muted)">· ' + last + '</span>' : "") + '</div>' +
        '</span>' +
        '<button class="btn-secondary ag-run" data-run="' + n + '">Run</button>' +
      '</div>';
    }).join("");
  }

  // A1-A5 and B1/B3 as the gate will actually apply them today.
  function renderStrategy(policies) {
    $("agents-strategy").innerHTML = Object.keys(policies).map((a) => {
      const p = policies[a] || {};
      const ex = p.is_example ? ' <span class="ag-ex">EXAMPLE</span>' : "";
      const range = (p.view_range_low != null && p.view_range_high != null)
        ? " " + px(a, p.view_range_low) + "–" + px(a, p.view_range_high) : "";
      return '<div>' +
        '<div style="font-weight:600;margin-bottom:.3rem">' + esc(a) + ex + '</div>' +
        '<div class="ag-sec">A1 view</div>' +
        '<dl class="ag-kv">' +
          '<dt>view</dt><dd>' + esc(p.view || "—") + range + '</dd>' +
          '<dt>checked</dt><dd>' + day(p.view_check_date) + '</dd>' +
        '</dl>' +
        '<div class="ag-sec">A2 · A3 floor, stop, limits</div>' +
        '<dl class="ag-kv">' +
          '<dt>floor</dt><dd>' + px(a, p.floor_price) + '</dd>' +
          '<dt>stop</dt><dd>' + px(a, p.stop_price) +
            (p.stop_loss_usd ? " · " + money(p.stop_loss_usd) : "") + '</dd>' +
          '<dt>book</dt><dd>' + money(p.book_notional_usd) + '</dd>' +
          '<dt>single trade</dt><dd>' + money(p.max_single_trade_usd) + '</dd>' +
          '<dt>cost/trade</dt><dd>' + money(p.max_cost_per_trade_usd) + '</dd>' +
          '<dt>cost/month</dt><dd>' + money(p.max_cost_per_month_usd) + '</dd>' +
          '<dt>perp cap</dt><dd>' + money(p.max_perp_notional_usd) + '</dd>' +
          '<dt>funding/mo</dt><dd>' + money(p.perp_funding_budget_month_usd) + '</dd>' +
        '</dl>' +
        '<div class="ag-sec">B1 rows · B3 tolerance</div>' +
        '<dl class="ag-kv">' +
          '<dt>rows</dt><dd>' + ((p.row_steps_pct || []).length
            ? "±" + p.row_steps_pct.join("/") + "%" : "—") + '</dd>' +
          '<dt>reference</dt><dd>' + px(a, p.reference_price) +
            (p.reference_set_on ? " · " + day(p.reference_set_on) : "") + '</dd>' +
          '<dt>tolerance</dt><dd>' + money(p.cost_tolerance_usd) + '</dd>' +
        '</dl>' +
      '</div>';
    }).join("");
  }

  function legLine(l) {
    const isOpt = (l.kind || "option") === "option";
    return esc(l.side || "") + " " + esc(l.opt || "") +
      (isOpt ? " " + esc(l.strike) : "") +
      (isOpt && l.expiry ? " " + esc(l.expiry) : "") +
      " ×" + esc(l.qty) +
      (l.counterparty ? ' <span style="color:var(--muted)">@' + esc(l.counterparty) + "</span>" : "") +
      (isOpt ? "" : ' <span style="color:var(--muted)">(perp — A4: direction only)</span>');
  }

  function renderProposals() {
    const live = PROPS.filter((p) => p.status === "open");
    if (!live.length) {
      $("agents-props").innerHTML = '<p style="color:var(--muted);font-size:.8rem;margin:0">No open proposals.</p>';
      return;
    }
    $("agents-props").innerHTML = live.map((p) => {
      const col = p.route === "rejected" ? "#f59e0b" : p.route === "lucas" ? "#10b981" : "#3b82f6";
      const legs = (p.proposal || {}).legs || [];
      const nOpt = legs.filter((l) => (l.kind || "option") === "option").length;
      return '<div style="border-left:3px solid ' + col + ';background:rgba(148,163,184,.06);border-radius:6px;padding:.6rem .75rem;margin-bottom:.5rem">' +
        '<div style="display:flex;gap:.6rem;align-items:baseline;flex-wrap:wrap">' +
          '<span class="ag-chip" style="background:' + col + '22;color:' + col + '">' + esc(p.route.toUpperCase()) + '</span>' +
          '<b style="font-size:.8rem">#' + p.id + " " + esc(p.asset) + '</b>' +
          '<span style="color:var(--muted);font-size:.72rem">' + esc(p.agent) + " · " +
            esc(p.created_at.slice(5, 16).replace("T", " ")) + " · " + esc(p.kind || "") + '</span>' +
        '</div>' +
        '<div style="font-size:.78rem;margin:.3rem 0">' + esc(p.summary) + '</div>' +
        (legs.length ? '<div style="font-size:.72rem;margin:.25rem 0 .4rem">' +
          legs.map((l) => "• " + legLine(l)).join("<br>") + '</div>' : "") +
        ((p.reasons || []).length ? '<div style="font-size:.71rem;color:#f59e0b;margin-bottom:.4rem">' +
          (p.reasons || []).map(esc).join("<br>") + '</div>' : "") +
        '<div style="display:flex;gap:.4rem;flex-wrap:wrap">' +
          '<button class="btn-secondary" style="width:auto;font-size:.7rem" data-price="' + p.id + '"' +
            (nOpt ? "" : " disabled") + '>Price it (B3)</button>' +
          '<button class="btn-secondary" style="width:auto;font-size:.7rem" data-v4="' + p.id + '">Validate in v4</button>' +
          '<button class="btn-secondary" style="width:auto;font-size:.7rem" data-decide="' + p.id + '" data-status="executed">Executed</button>' +
          '<button class="btn-secondary" style="width:auto;font-size:.7rem" data-decide="' + p.id + '" data-status="declined">Decline</button>' +
        '</div>' +
      '</div>';
    }).join("");
  }

  function renderDecisions() {
    const done = PROPS.filter((p) => p.status !== "open");
    if (!done.length) {
      $("agents-decisions").innerHTML = '<p style="color:var(--muted);font-size:.8rem;margin:0">Nothing decided yet.</p>';
      return;
    }
    $("agents-decisions").innerHTML =
      '<table class="data-table" style="width:100%"><thead><tr><th>When</th><th>#</th><th>Asset</th>' +
      '<th>What</th><th>Route</th><th>Outcome</th><th>By</th></tr></thead><tbody>' +
      done.map((p) => {
        const col = p.status === "rejected" ? "#f59e0b"
          : p.status === "executed" ? "#10b981" : "#94a3b8";
        return "<tr><td>" + esc(p.created_at.slice(5, 16).replace("T", " ")) + "</td><td>" + p.id +
          "</td><td>" + esc(p.asset) + '</td><td style="font-size:.74rem">' + esc(p.summary) +
          "</td><td>" + esc(p.route.toUpperCase()) + '</td><td style="color:' + col +
          ';font-weight:600">' + esc(p.status) + "</td><td>" + esc(p.decided_by || "gate") + "</td></tr>";
      }).join("") + "</tbody></table>";
  }

  async function load() {
    try {
      const st = await api("/api/agents/status");
      const kill = st.kill_switch === "on";
      $("agents-kill-state").textContent = kill ? "KILL SWITCH ON" : "Agents live (shadow)";
      $("btn-agents-kill").textContent = kill ? "Turn agents back on" : "Kill switch";

      const warn = [];
      Object.keys(st.policies).forEach((a) => {
        const p = st.policies[a];
        if (p.is_example) warn.push(a + " is on the manual's example limits");
        if (!p.reference_price) warn.push(a + " has no Monday reference");
      });
      Object.keys(st.curves_frozen).forEach((a) => {
        if (st.curves_frozen[a] === "on") warn.push(a + " curves disagree: trading frozen (B2a)");
      });
      $("agents-warn").style.display = warn.length ? "" : "none";
      $("agents-warn").textContent = warn.join(" · ");
      const badge = $("nav-badge-agents");
      if (badge) badge.textContent = st.open_proposals || "";

      PROPS = (await api("/api/agents/proposals?limit=60")).proposals || [];
      const counts = {};
      PROPS.filter((p) => p.status === "open").forEach((p) => {
        counts[p.agent] = (counts[p.agent] || 0) + 1;
      });

      renderTasks(st, counts);
      renderStrategy(st.policies);
      renderProposals();
      renderDecisions();

      $("agents-policy-asset").textContent = asset();
      if (document.activeElement !== $("agents-policy-json")) {
        $("agents-policy-json").value = JSON.stringify(st.policies[asset()] || {}, null, 2);
      }
    } catch (e) {
      $("agents-warn").style.display = "";
      $("agents-warn").textContent = "Agents API: " + e.message;
    }
  }

  async function show(name) {
    selected = name;
    const nodes = document.querySelectorAll(".ag-task");
    for (let i = 0; i < nodes.length; i++) {
      nodes[i].classList.toggle("sel", nodes[i].dataset.show === name);
    }
    $("agents-output-name").textContent = "— " + name;
    $("agents-output").textContent = "Loading…";
    try {
      const r = (await api("/api/agents/runs?agent=" + encodeURIComponent(name) + "&limit=1")).runs[0];
      $("agents-output").textContent = r ? r.text : "No runs yet.";
    } catch (e) {
      $("agents-output").textContent = "Could not load: " + e.message;
    }
  }

  // B3: price it ourselves first. Load the option legs into the Pricing page
  // (perp legs are A4 direction-only and have no vol to price) and jump there.
  function priceIt(p) {
    const all = (p.proposal || {}).legs || [];
    const opts = all.filter((l) => (l.kind || "option") === "option");
    if (!opts.length) { alert("This proposal has no option legs to price."); return; }
    if (typeof legs === "undefined" || typeof addLeg !== "function") {
      alert("Pricing page isn't loaded yet — open Pricing once, then try again.");
      return;
    }
    legs.length = 0;
    opts.forEach((l) => addLeg(
      String(l.side || "buy").toLowerCase(),
      String(l.opt || "C").toUpperCase(),
      l.strike, "0", l.qty, l.expiry || null, l.counterparty || ""));
    const cp = document.getElementById("cpty-pricing-select");
    const first = (opts.find((l) => l.counterparty) || {}).counterparty;
    if (cp && first) {
      for (let i = 0; i < cp.options.length; i++) {
        if (cp.options[i].value === first) { cp.value = first; break; }
      }
    }
    const nav = document.querySelector('.nav-item[data-page="pricing"]');
    if (nav) nav.click();
    const skipped = all.length - opts.length;
    console.log("agents → pricing: " + opts.length + " option leg(s) from proposal #" + p.id +
      (skipped ? "; " + skipped + " perp leg(s) skipped (A4)" : ""));
  }

  // The v4 sweep produced these numbers; send the operator back with the exact
  // preset so the run can be reproduced and the shape checked against the book.
  function validateInV4(p) {
    const v = (p.proposal || {}).variant || (p.proposal || {}).params || null;
    const nav = document.querySelector('.nav-item[data-page="optv4"]');
    if (nav) nav.click();
    const bits = v ? Object.keys(v).map((k) => k + "=" + v[k]).join("  ") : p.summary;
    console.log("agents → optv4: reproduce proposal #" + p.id + " with " + bits);
    const el = document.getElementById("optv4-pricing-back-status");
    if (el) {
      el.textContent = "From Agents proposal #" + p.id + " (" + p.asset + "): " + bits +
        ". Run the sweep with these settings to check the shape against the book.";
      el.style.display = "";
    }
  }

  document.addEventListener("click", async (ev) => {
    if (!(ev.target instanceof HTMLElement)) return;
    const t = ev.target.closest("[data-show],[data-run],[data-price],[data-v4],[data-decide],button");
    if (!t) return;

    if (t.dataset.run) {
      ev.stopPropagation();
      const n = t.dataset.run;
      t.textContent = "…"; t.disabled = true;
      try {
        const r = await api("/api/agents/run/" + n, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ deliver: false, use_ai: true }),
        });
        selected = n;
        $("agents-output-name").textContent = "— " + n;
        $("agents-output").textContent = r.text;
      } catch (e) { $("agents-output").textContent = "Failed: " + e.message; }
      t.textContent = "Run"; t.disabled = false; load();
      return;
    }
    if (t.dataset.price) {
      const p = PROPS.find((x) => String(x.id) === t.dataset.price);
      if (p) priceIt(p);
      return;
    }
    if (t.dataset.v4) {
      const p = PROPS.find((x) => String(x.id) === t.dataset.v4);
      if (p) validateInV4(p);
      return;
    }
    if (t.dataset.decide) {
      await api("/api/agents/proposals/" + t.dataset.decide, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: t.dataset.status, by: "desk" }),
      });
      load();
      return;
    }
    if (t.dataset.show) { show(t.dataset.show); return; }

    if (t.id === "btn-agents-refresh") load();
    if (t.id === "btn-agents-doc") {
      const box = $("agents-doc");
      const openNow = box.style.display === "none";
      box.style.display = openNow ? "" : "none";
      t.textContent = openNow ? "Hide the manual" : "Read the manual";
      if (openNow && DOC == null) {
        $("agents-doc-body").textContent = "Loading…";
        try {
          const r = await fetch("/static/strategy.md");
          DOC = r.ok ? await r.text() : "Strategy document not found on the server.";
        } catch (e) { DOC = "Could not load the strategy document: " + e.message; }
        $("agents-doc-body").textContent = DOC;
      }
    }
    if (t.id === "btn-agents-kill") {
      const on = $("agents-kill-state").textContent.indexOf("KILL") === 0;
      await api("/api/agents/flags", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: "kill_switch", value: on ? "off" : "on", by: "desk" }),
      });
      load();
    }
    if (t.id === "btn-agents-policy-save") {
      try {
        const body = JSON.parse($("agents-policy-json").value);
        body.is_example = false;
        await api("/api/agents/policy/" + asset(), {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        $("agents-policy-msg").textContent = "Saved.";
        load();
      } catch (e) { $("agents-policy-msg").textContent = "Not saved: " + e.message; }
    }
  });

  const navItem = document.querySelector('.nav-item[data-page="agents"]');
  if (navItem) navItem.addEventListener("click", load);
})();
