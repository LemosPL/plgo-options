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
  let selected = null, PROPS = [], DOC = null, POLICY = {};

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

  // A1-A5 and B1/B3 as the gate will actually apply them today. One table with
  // both books side by side rather than two stacks: the question this panel has
  // to answer at a glance is "what is still missing", and that only reads if
  // ETH and FIL sit on the same row.
  const MISSING = '<span style="color:#f59e0b">not set</span>';

  function renderStrategy(policies) {
    const assets = Object.keys(policies);
    const P = (a) => policies[a] || {};

    // Monday's rows are reference x (1 +/- step%), so they only exist once a
    // reference does. Showing the levels is the point of B1 - a percentage is
    // not something you can watch the tape against.
    const rowLevels = (a) => {
      const p = P(a), ref = p.reference_price, steps = p.row_steps_pct || [];
      if (!ref || !steps.length) {
        return steps.length ? '±' + steps.join("/") + "% <span style=\"color:#f59e0b\">(needs a reference)</span>"
                            : MISSING;
      }
      const down = steps.map((s) => px(a, ref * (1 - s / 100))).join("  ");
      const up = steps.map((s) => px(a, ref * (1 + s / 100))).join("  ");
      return '<span style="color:var(--muted)">↓</span> ' + down +
             '  <span style="color:var(--muted)">↑</span> ' + up;
    };

    const ROWS = [
      { sec: "A1 · the view" },
      { label: "View", v: (a) => esc(P(a).view || ""), miss: (a) => !P(a).view },
      { label: "Expected range, 90d",
        v: (a) => px(a, P(a).view_range_low) + " – " + px(a, P(a).view_range_high),
        miss: (a) => P(a).view_range_low == null || P(a).view_range_high == null },
      { label: "Right or wrong by", v: (a) => day(P(a).view_check_date),
        miss: (a) => !P(a).view_check_date },
      { sec: "A2 · the floor" },
      { label: "Floor (only ever rises)", v: (a) => px(a, P(a).floor_price),
        miss: (a) => P(a).floor_price == null },
      { sec: "A3 · the limits" },
      { label: "Stop", v: (a) => px(a, P(a).stop_price), miss: (a) => P(a).stop_price == null },
      { label: "Stop: further loss", v: (a) => money(P(a).stop_loss_usd),
        miss: (a) => !P(a).stop_loss_usd },
      { label: "Book size", v: (a) => money(P(a).book_notional_usd),
        miss: (a) => !P(a).book_notional_usd },
      { label: "Single trade, Lucas alone", v: (a) => money(P(a).max_single_trade_usd),
        miss: (a) => !P(a).max_single_trade_usd },
      { label: "Cost per trade", v: (a) => money(P(a).max_cost_per_trade_usd),
        miss: (a) => !P(a).max_cost_per_trade_usd },
      { label: "Cost per month", v: (a) => money(P(a).max_cost_per_month_usd),
        miss: (a) => !P(a).max_cost_per_month_usd },
      { label: "Perp / forward cap", v: (a) => money(P(a).max_perp_notional_usd) +
          (P(a).perp_cap_full_delta ? " or full delta" : "") +
          (P(a).perp_venue ? ' <span style="color:var(--muted)">@' + esc(P(a).perp_venue) + "</span>" : ""),
        miss: (a) => !P(a).max_perp_notional_usd },
      { label: "Rolls", v: (a) => P(a).rolls_need_chris === false ? "Lucas, inside limits" : "Chris",
        miss: () => false },
      { label: "Funding budget, monthly", v: (a) => money(P(a).perp_funding_budget_month_usd),
        miss: (a) => !P(a).perp_funding_budget_month_usd },
      { label: "Counterparty universe",
        v: (a) => (P(a).allowed_counterparties || []).length + " named",
        miss: (a) => !(P(a).allowed_counterparties || []).length },
      { sec: "B1 · the levels" },
      { label: "Monday reference", v: (a) => px(a, P(a).reference_price) +
          (P(a).reference_set_on ? ' <span style="color:var(--muted)">' + day(P(a).reference_set_on) + "</span>" : ""),
        miss: (a) => P(a).reference_price == null },
      { label: "Target exposure (delta)", v: (a) => P(a).reference_delta == null ? ""
          : Number(P(a).reference_delta).toLocaleString(), miss: (a) => P(a).reference_delta == null },
      { label: "Rows", v: rowLevels, miss: () => false, wide: true },
      { sec: "Optimizer sweep" },
      { label: "Target profiles", v: (a) => {
          const g = ((P(a).optimizer || {}).target_grid || []);
          return g.length ? g.map((x) => esc(x === "parametric" ? "Agreed V" : x.replace(/\.csv$/i, ""))).join(" · ")
                          : '<span style="color:var(--muted)">Auto (agreed V + saved profiles)</span>';
        }, miss: () => false, wide: true },
      { sec: "B3 · how we deal" },
      { label: "Quote tolerance vs model", v: (a) => money(P(a).cost_tolerance_usd),
        miss: (a) => !P(a).cost_tolerance_usd },
    ];

    // How much of the mandate is actually decided.
    const checks = ROWS.filter((r) => r.label && r.miss);
    const gaps = {};
    assets.forEach((a) => { gaps[a] = checks.filter((r) => r.miss(a)).length; });
    const summary = assets.map((a) => {
      const set = checks.length - gaps[a];
      const col = gaps[a] ? "#f59e0b" : "#10b981";
      return esc(a) + ' <span style="color:' + col + '">' + set + "/" + checks.length + " set</span>" +
        (P(a).is_example ? ' <span class="ag-ex">EXAMPLE</span>' : "");
    }).join('<span style="color:var(--muted)"> · </span>');

    $("agents-strategy").innerHTML =
      '<div style="font-size:.7rem;margin-bottom:.4rem">' + summary + "</div>" +
      '<table class="ag-strat"><thead><tr><th></th>' +
        assets.map((a) => "<th>" + esc(a) + "</th>").join("") + "</tr></thead><tbody>" +
      ROWS.map((r) => {
        if (r.sec) {
          return '<tr class="ag-strat-sec"><td colspan="' + (assets.length + 1) + '">' +
            esc(r.sec) + "</td></tr>";
        }
        return "<tr><td>" + esc(r.label) + "</td>" + assets.map((a) =>
          '<td' + (r.wide ? ' style="white-space:normal"' : "") + ">" +
          (r.miss && r.miss(a) ? MISSING : r.v(a)) + "</td>").join("") + "</tr>";
      }).join("") + "</tbody></table>";
  }

  // The Monday policy as a form rather than raw JSON, grouped the way the
  // manual is and captioned in its own words so the number being typed and the
  // rule it enforces sit next to each other.
  const POLICY_GROUPS = [
    { id: "A1", title: "A1 · The view",
      blurb: "One question decides everything else: do we believe this asset makes a big move in " +
             "the next three months? Written down on Monday with a date on it. It does not change " +
             "during the week because the price moved — only because the reasoning changed.",
      fields: [
        { k: "view", label: "The view", type: "select",
          opts: [["big_move", "Big move coming, direction unknown"], ["up", "Big move up"],
                 ["range", "Range-bound, nothing happens"]],
          help: "big_move keeps the current shape. up moves upside close to the money. " +
                "range means a smaller, closer, shorter-dated book — that is a reshape, not an adjustment." },
        { k: "view_range_low",  label: "Expected range low",  type: "num",
          help: "The price range we expect over 90 days." },
        { k: "view_range_high", label: "Expected range high", type: "num" },
        { k: "view_check_date", label: "Say we were right by", type: "date",
          help: "The date by which we call the view right or wrong." },
        { k: "view_note", label: "Reasoning", type: "text",
          help: "Why. This is the line that has to change before the view does." },
      ] },
    { id: "A2", title: "A2 · The floor",
      blurb: "The floor only goes up. Once we raise the level below which the book cannot lose more, " +
             "it never comes back down — and a roll that lowers it is a roll down wearing different clothes.",
      fields: [
        { k: "floor_price", label: "Floor", type: "num",
          help: "Every proposal is tested against this. The gate rejects anything that lowers it." },
      ] },
    { id: "A3", title: "A3 · The limits",
      blurb: "Set by Chris, reviewed monthly. These are the boundaries execution operates inside — " +
             "nobody at the screen changes them.",
      fields: [
        { k: "stop_price", label: "Stop price", type: "num",
          help: "Where we de-risk regardless of view. At the stop we close; we do not roll." },
        { k: "stop_loss_usd", label: "Stop: further loss ($)", type: "num",
          help: "Or this much more loss against Monday's mark, whichever comes first." },
        { k: "book_notional_usd", label: "Book size ($)", type: "num",
          help: "The underlying notional the options sit against." },
        { k: "max_single_trade_usd", label: "Max single trade, Lucas alone ($)", type: "num",
          help: "Biggest position he can put on or take off without calling. Anything larger goes to the handover." },
        { k: "max_cost_per_trade_usd", label: "Max cost per trade ($)", type: "num",
          help: "Premium we can pay net, after what we sell." },
        { k: "max_cost_per_month_usd", label: "Max cost per month ($)", type: "num",
          help: "Everything paid net across the month. The line that stops a bad month becoming a bad quarter." },
        { k: "max_perp_notional_usd", label: "Max perp / forward ($)", type: "num",
          help: "The biggest linear hedge allowed at any time." },
        { k: "perp_cap_full_delta", label: "Perps up to full delta", type: "bool",
          help: "Yes: the perp cap and Lucas's single perp trade grow to the options book's delta ($) when that is larger." },
        { k: "perp_venue", label: "Perp venue", type: "text",
          help: "Where row perps trade, and where the hedge's collateral sits." },
        { k: "perp_funding_budget_month_usd", label: "Monthly funding budget ($)", type: "num",
          help: "What the hedge is allowed to cost us to carry. Empty = no budget set." },
        { k: "rolls_need_chris", label: "Rolls need Chris", type: "bool",
          help: "No: Lucas rolls alone inside size and cost limits. Shortening expiry or moving the strike at the same expiry stays rejected." },
        { k: "allowed_counterparties", label: "Counterparty universe", type: "list",
          help: "Comma-separated. Nothing outside this list without a conversation." },
      ] },
    { id: "B1", title: "B1 · The levels",
      blurb: "Monday's reference price populates every row. Until it is set, rows are inactive and " +
             "the row watcher has nothing to measure against.",
      fields: [
        { k: "reference_price", label: "Monday reference price", type: "num",
          help: "Spot at the point we set the week's rows." },
        { k: "reference_set_on", label: "Reference set on", type: "date" },
        { k: "reference_delta", label: "Target exposure (delta at reference)", type: "num",
          help: "Book delta at the reference — what execution steers back toward." },
        { k: "row_steps_pct", label: "Row steps (%)", type: "list",
          help: "Comma-separated, e.g. 10,20,30 for ETH or 15,30,45 for FIL. Each fires once a week." },
      ] },
    { id: "B3", title: "B3 · How we deal",
      blurb: "Price it ourselves first, two quotes minimum, quote the whole package never the legs.",
      fields: [
        { k: "cost_tolerance_usd", label: "Quote tolerance vs our model ($)", type: "num",
          help: "If the quote is wider than our number plus this, we do not trade." },
      ] },
    { id: "OPT", title: "Optimizer · target profiles for the sweep",
      blurb: "Pick the three targets the 09:30 and 16:00 sweeps test, before 09:00. Each is ranked " +
             "on its own and its best run becomes a proposal. Left on Auto, the sweep uses the agreed " +
             "V and then the saved profiles in name order. Save new profiles on the v4 page.",
      fields: [
        { k: "target_1", label: "Target 1 (default)", type: "target" },
        { k: "target_2", label: "Target 2", type: "target" },
        { k: "target_3", label: "Target 3", type: "target" },
      ] },
  ];

  // Saved target profiles per asset, for the sweep's target dropdowns.
  const TARGETS = {};
  async function loadTargets(a) {
    if (TARGETS[a]) return;
    try {
      const r = await api("/api/optimization/target-profiles?asset=" + encodeURIComponent(a));
      TARGETS[a] = (r.profiles || []).map((p) => p.file);
    } catch (e) { TARGETS[a] = []; }
    if (a === asset() && !$("agents-policy-form").contains(document.activeElement)) renderPolicyForm(POLICY);
  }

  function renderPolicyForm(pol) {
    const val = (k) => {
      const v = pol[k];
      if (v == null) return "";
      return Array.isArray(v) ? v.join(", ") : String(v);
    };
    $("agents-policy-form").innerHTML = POLICY_GROUPS.map((g) =>
      '<fieldset>' +
      '<legend>' + esc(g.title) + '</legend>' +
      '<p class="ag-blurb">' + esc(g.blurb) + '</p>' +
      '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:.45rem .9rem">' +
      g.fields.map((f) => {
        const id = "pol-" + f.k;
        let input;
        if (f.type === "target") {
          const i = +f.k.slice(-1) - 1;
          const cur = ((pol.optimizer || {}).target_grid || [])[i] || "";
          const files = TARGETS[asset()] || [];
          const opts = [["", "Auto"], ["parametric", "Agreed V (parametric)"]]
            .concat(files.map((x) => [x, x.replace(/\.csv$/i, "")]));
          if (cur && !opts.some((o) => o[0] === cur)) opts.push([cur, cur + " (not found)"]);
          input = '<select id="' + id + '" data-pk="' + f.k + '" data-pt="target">' +
            opts.map((o) => '<option value="' + esc(o[0]) + '"' + (o[0] === cur ? " selected" : "") + ">" +
              esc(o[1]) + "</option>").join("") + "</select>";
        } else if (f.type === "select" || f.type === "bool") {
          const opts = f.type === "bool" ? [["true", "Yes"], ["false", "No"]] : f.opts;
          input = '<select id="' + id + '" data-pk="' + f.k + '" data-pt="' + f.type + '">' +
            opts.map((o) => '<option value="' + o[0] + '"' +
              (val(f.k) === o[0] ? " selected" : "") + ">" + esc(o[1]) + "</option>").join("") + "</select>";
        } else {
          const t = f.type === "num" ? "number" : f.type === "date" ? "date" : "text";
          input = '<input id="' + id + '" data-pk="' + f.k + '" data-pt="' + f.type + '" type="' + t + '"' +
            (f.type === "num" ? ' step="any"' : "") +
            ' value="' + esc(val(f.k)) + '">';
        }
        return "<label><span>" + esc(f.label) + "</span>" + input +
          (f.help ? '<span class="ag-help">' + esc(f.help) + "</span>" : "") + "</label>";
      }).join("") + "</div></fieldset>").join("");
  }

  function readPolicyForm(base) {
    const out = { ...base };
    const targets = [];
    document.querySelectorAll("#agents-policy-form [data-pk]").forEach((el) => {
      const k = el.dataset.pk, t = el.dataset.pt, raw = el.value.trim();
      if (t === "target") {
        if (raw && !targets.includes(raw)) targets.push(raw);
      } else if (t === "num") {
        out[k] = raw === "" ? null : Number(raw);
      } else if (t === "list") {
        const parts = raw ? raw.split(",").map((x) => x.trim()).filter(Boolean) : [];
        out[k] = k === "row_steps_pct" ? parts.map(Number).filter((n) => !isNaN(n)) : parts;
      } else if (t === "bool") {
        out[k] = raw === "true";
      } else if (t === "date") {
        out[k] = raw || null;
      } else {
        out[k] = raw;
      }
    });
    // An empty list means Auto (the agreed V, then the saved profiles).
    out.optimizer = { ...(base.optimizer || {}), target_grid: targets };
    return out;
  }

  // The manual is a converted Word document: mostly prose, but its real content
  // is 14 tables (the view matrix, the limits, instrument-for-job, the day).
  // Dumped into a <pre> those read as pipe soup, so render them as tables.
  // Split on newlines without writing an escape sequence into this file:
  // earlier tooling kept turning a literal backslash-n into a real newline
  // and breaking the regex across lines.
  const NEWLINE = String.fromCharCode(10);
  const RE_CR = new RegExp(String.fromCharCode(13), "g");

  function mdToHtml(md) {
    const inline = (t) => esc(t)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
    const lines = String(md).replace(RE_CR, "").split(NEWLINE);
    const out = [];
    let i = 0, list = null;
    const closeList = () => { if (list) { out.push("</" + list + ">"); list = null; } };

    while (i < lines.length) {
      const ln = lines[i];

      // table: a header row, a --- separator, then body rows
      if (/^\s*\|/.test(ln) && i + 1 < lines.length && /^\s*\|[\s|:-]+\|\s*$/.test(lines[i + 1])) {
        closeList();
        const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        const head = cells(ln);
        i += 2;
        const body = [];
        while (i < lines.length && /^\s*\|/.test(lines[i])) { body.push(cells(lines[i])); i++; }
        out.push('<table class="ag-md-t"><thead><tr>' +
          head.map((c) => "<th>" + inline(c) + "</th>").join("") + "</tr></thead><tbody>" +
          body.map((r) => "<tr>" + r.map((c) => "<td>" + inline(c) + "</td>").join("") + "</tr>").join("") +
          "</tbody></table>");
        continue;
      }

      const h = ln.match(/^(#{1,6})\s+(.*)$/);
      if (h) { closeList(); out.push("<h" + h[1].length + ">" + inline(h[2].replace(/\*\*/g, "")) +
                                     "</h" + h[1].length + ">"); i++; continue; }

      const li = ln.match(/^\s*[-*]\s+(.*)$/);
      if (li) {
        if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; }
        out.push("<li>" + inline(li[1]) + "</li>"); i++; continue;
      }

      if (!ln.trim()) { closeList(); i++; continue; }

      closeList();
      const buf = [];
      while (i < lines.length && lines[i].trim() && !/^\s*\|/.test(lines[i]) &&
             !/^#{1,6}\s/.test(lines[i]) && !/^\s*[-*]\s/.test(lines[i])) { buf.push(lines[i]); i++; }
      out.push("<p>" + inline(buf.join(" ")) + "</p>");
    }
    closeList();
    return out.join("");
  }

  function legLine(l) {
    const isOpt = (l.kind || "option") === "option";
    // Direction lives in `side`; show the quantity as a magnitude so a short
    // leg doesn't read as "Sell ... x-499". Expiries arrive both as a date and
    // as a full timestamp — show the date either way.
    const q = Math.abs(Number(l.qty));
    return esc(l.side || "") + " " + esc(l.opt || "") +
      (isOpt ? " " + esc(l.strike) : "") +
      (isOpt && l.expiry ? " " + esc(String(l.expiry).slice(0, 10)) : "") +
      " ×" + (isFinite(q) ? q.toLocaleString() : esc(l.qty)) +
      (l.counterparty ? ' <span style="color:var(--muted)">@' + esc(l.counterparty) + "</span>" : "") +
      (isOpt ? "" : ' <span style="color:var(--muted)">(perp — A4: direction only)</span>');
  }

  // The manual's rule codes in plain words, for "why was this rejected".
  const RULES = {
    "A1": "the view", "A2": "the floor", "A3": "the limits", "A4": "which instrument",
    "A5": "rolls", "B1": "the weekly rows", "B2": "kill switch", "B2a": "two-curve check",
    "B2b": "the handover", "B3": "how we deal",
  };
  const ruleWords = (txt) => {
    const m = String(txt).match(/\((A\d|B\d[ab]?)[^)]*\)\s*\.?$/);
    return m && RULES[m[1]] ? " <span style=\"color:var(--muted)\">— " + m[1] + " " + RULES[m[1]] + "</span>" : "";
  };
  let PFILTER = "all";

  function whyBlock(p) {
    const g = (p.proposal || {}).gate || {};
    let rej = g.rejected_by, chris = g.needs_chris;
    if (!rej && !chris) {                       // proposals made before the split was stored
      rej = p.route === "rejected" ? (p.reasons || []) : [];
      chris = p.route === "rejected" ? [] : (p.route === "lucas" ? [] : (p.reasons || []));
    }
    const failed = (g.checks || []).filter((c) => c.ok === false);
    let h = "";
    if (rej && rej.length) {
      h += '<div style="font-size:.68rem;line-height:1.4;margin:.2rem 0 .3rem;padding:.3rem .45rem;border-radius:4px;' +
        'background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.35)">' +
        '<b style="color:#ef4444">Why it was rejected</b><br>' +
        rej.map((r) => "✕ " + esc(r) + ruleWords(r)).join("<br>") +
        (failed.length ? '<div style="color:var(--muted);margin-top:.2rem">Checks that failed: ' +
          failed.map((c) => esc(c.check) + " (" + esc(c.note) + ")").join("; ") + "</div>" : "") +
        "</div>";
    }
    if (chris && chris.length) {
      h += '<div style="font-size:.65rem;line-height:1.35;color:#3b82f6;margin-bottom:.3rem">' +
        "<b>" + (rej && rej.length ? "Would also need Chris" : "Needs Chris") + "</b><br>" +
        chris.map((r) => "• " + esc(r) + ruleWords(r)).join("<br>") + "</div>";
    }
    return h;
  }

  function renderProposals() {
    const live = PROPS.filter((p) => p.status === "open");
    const n = (r) => live.filter((p) => r === "all" || p.route === r ||
      (r === "chris" && p.route === "lucas_tell_chris")).length;
    const chip = (r, label, col) => '<button class="btn-secondary" data-pfilter="' + r + '" style="width:auto;' +
      "font-size:.64rem;padding:.12rem .45rem;" + (PFILTER === r ? "border-color:" + col + ";color:" + col + ";font-weight:600" : "") +
      '">' + label + " " + n(r) + "</button>";
    const bar = '<div style="display:flex;gap:.3rem;flex-wrap:wrap;margin-bottom:.45rem">' +
      chip("all", "All", "#94a3b8") + chip("rejected", "Rejected", "#ef4444") +
      chip("chris", "Chris", "#3b82f6") + chip("lucas", "Lucas", "#10b981") + "</div>";
    const shown = live.filter((p) => PFILTER === "all" || p.route === PFILTER ||
      (PFILTER === "chris" && p.route === "lucas_tell_chris"));
    if (!shown.length) {
      $("agents-props").innerHTML = bar + '<p style="color:var(--muted);font-size:.8rem;margin:0">' +
        (live.length ? "Nothing in this filter." : "No open proposals.") + "</p>";
      return;
    }
    $("agents-props").innerHTML = bar + shown.map((p) => {
      const col = p.route === "rejected" ? "#ef4444" : p.route === "lucas" ? "#10b981" : "#3b82f6";
      const legs = (p.proposal || {}).legs || [];
      const nOpt = legs.filter((l) => (l.kind || "option") === "option").length;
      return '<div style="border-left:2px solid ' + col + ';background:rgba(148,163,184,.05);border-radius:5px;padding:.4rem .55rem;margin-bottom:.35rem">' +
        '<div style="display:flex;gap:.45rem;align-items:baseline;flex-wrap:wrap">' +
          '<span class="ag-chip" style="background:' + col + '22;color:' + col + '">' + esc(p.route.toUpperCase()) + '</span>' +
          '<b style="font-size:.74rem">#' + p.id + " " + esc(p.asset) + '</b>' +
          '<span style="color:var(--muted);font-size:.65rem">' + esc(p.agent) + " · " +
            esc(p.created_at.slice(5, 16).replace("T", " ")) + " · " + esc(p.kind || "") + '</span>' +
        '</div>' +
        '<div style="font-size:.72rem;margin:.2rem 0">' + esc(p.summary) + '</div>' +
        (legs.length ? '<div style="font-size:.66rem;line-height:1.4;margin:.15rem 0 .3rem">' +
          legs.map((l) => "• " + legLine(l)).join("<br>") + '</div>' : "") +
        whyBlock(p) +
        '<div style="display:flex;gap:.3rem;flex-wrap:wrap">' +
          '<button class="btn-secondary" style="width:auto;font-size:.64rem;padding:.15rem .4rem" data-price="' + p.id + '"' +
            (nOpt ? "" : " disabled") + '>Price it (B3)</button>' +
          '<button class="btn-secondary" style="width:auto;font-size:.64rem;padding:.15rem .4rem" data-v4="' + p.id + '">Validate in v4</button>' +
          '<button class="btn-secondary" style="width:auto;font-size:.64rem;padding:.15rem .4rem" data-decide="' + p.id + '" data-status="executed">Executed</button>' +
          '<button class="btn-secondary" style="width:auto;font-size:.64rem;padding:.15rem .4rem" data-decide="' + p.id + '" data-status="declined">Decline</button>' +
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
      POLICY = st.policies[asset()] || {};
      $("agents-policy-example").style.display = POLICY.is_example ? "" : "none";
      // Don't clobber half-typed edits: only repaint when nothing here has focus.
      if (!$("agents-policy-form").contains(document.activeElement) &&
          document.activeElement !== $("agents-policy-json")) {
        renderPolicyForm(POLICY);
        $("agents-policy-json").value = JSON.stringify(POLICY, null, 2);
      }
      loadTargets(asset());
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

  const MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
               "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];

  // The optimizer emits ISO expiries ("2026-12-25", sometimes with a time);
  // Pricing keys its vol smiles by Deribit code ("25DEC26") and looks them up
  // with an exact string match. Handing it the ISO form matches nothing and
  // cannot be bracketed either, which is the
  //   "Cannot price expiry 2026-12-25 - no vol smile data available"
  // alert. Convert, and prefer a code already on the loaded surface so the leg
  // gets a real smile rather than a synthetic one.
  function toDeribitExpiry(v) {
    if (!v) return null;
    const s = String(v).trim();
    if (/^\d{1,2}[A-Za-z]{3}\d{2}$/.test(s)) return s.toUpperCase();
    const m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (!m) return null;
    const y = +m[1], mo = +m[2], d = +m[3];

    const smiles = (typeof volSurface !== "undefined" && volSurface && volSurface.smiles) || [];
    for (const sm of smiles) {
      const c = (sm.expiry_code || "").match(/^(\d{1,2})([A-Z]{3})(\d{2})$/);
      if (c && +c[1] === d && MON.indexOf(c[2]) === mo - 1 && 2000 + +c[3] === y) {
        return sm.expiry_code;            // exact surface match
      }
    }
    // Deribit writes single-digit days without a leading zero (5DEC26).
    return `${d}${MON[mo - 1]}${String(y).slice(2)}`;
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
    opts.forEach((l) => {
      // Pricing takes direction from `side` and treats quantity as a magnitude
      // (app.js: dir = side === "buy" ? 1 : -1). The v4 trade carries the sign
      // in BOTH side and qty, so passing qty through would cancel the side out
      // and silently flip a sell into a buy.
      const q = Number(l.qty);
      const side = l.side ? String(l.side).toLowerCase() : (q < 0 ? "sell" : "buy");
      addLeg(side, String(l.opt || "C").toUpperCase(), l.strike, "0",
        Math.abs(q) || 1, toDeribitExpiry(l.expiry), l.counterparty || "");
    });
    // Point the global expiry selector at the legs' expiry too, so "Compute"
    // and the chain loader operate on the same maturity the proposal used.
    const es = document.getElementById("expiry-select");
    const code = toDeribitExpiry((opts.find((l) => l.expiry) || {}).expiry);
    if (es && code) {
      for (let i = 0; i < es.options.length; i++) {
        if (es.options[i].value === code) { es.value = code; break; }
      }
    }
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

  // Replay a proposal on the v4 page the way Lucas does it by hand: pick the
  // asset, Load Risk Profile, set the run's parameters, Run Optimizer. Then
  // lay the agent's own legs on top as what-if legs so both can be compared on
  // the same chart: an optimizer proposal shows the fresh run ticked and the
  // agent's legs off; a row/manual proposal shows the agent's legs on and the
  // optimizer's suggestions unticked.
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function v4Set(id, v) {
    const el = document.getElementById(id);
    if (!el || v == null) return;
    el.value = String(v);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function v4Check(id, on) {
    const el = document.getElementById(id);
    if (!el || on == null || el.checked === !!on) return;
    el.checked = !!on;
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function v4Status(html) {
    const el = document.getElementById("optv4-pricing-back-status");
    if (!el) return;
    el.innerHTML = html;
    el.style.display = "";
  }

  // An agent leg -> a v4 what-if leg (same shape optv4AddManualLeg builds).
  function v4ManualLeg(l, S0, on) {
    const code = toDeribitExpiry(l.expiry);
    const m = String(l.expiry || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
    let expDate = m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], 8)) : null;
    if (!expDate && code) {
      const c = code.match(/^(\d{1,2})([A-Z]{3})(\d{2})$/);
      if (c) expDate = new Date(Date.UTC(2000 + +c[3], MON.indexOf(c[2]), +c[1], 8));
    }
    if (!expDate) return null;
    const dte = Math.max((expDate - Date.now()) / 86400000, 0);
    const K = Number(l.strike), qAbs = Math.abs(Number(l.qty));
    if (!(K > 0) || !(qAbs > 0)) return null;
    const side = String(l.side || (Number(l.qty) < 0 ? "Sell" : "Buy")).toLowerCase() === "sell" ? "Sell" : "Buy";
    const opt = String(l.opt || "C").toUpperCase();
    const iv = optv4IvForExpiry(code);
    const T = dte / 365.25;
    const price = T > 0 ? bsPrice(S0, K, T, 0, iv / 100, opt)
      : (opt === "C" ? Math.max(S0 - K, 0) : Math.max(K - S0, 0));
    return { _mid: ++optv4LegSeq, _on: on, side, opt, strike: K,
             qty: side === "Buy" ? qAbs : -qAbs, dte, iv_pct: iv, expiry_code: code,
             bs_price_usd: price };
  }

  async function validateInV4(p) {
    if (typeof optv4Load !== "function") { alert("The v4 page isn't loaded."); return; }
    let d;
    try { d = await api("/api/agents/proposals/" + p.id + "/v4"); }
    catch (e) { alert("Could not load proposal #" + p.id + ": " + e.message); return; }
    const P = d.params || {};
    const fromOptimizer = d.agent === "optimizer";
    const notes = [];

    // 1. Asset, then the page, then its own first step: Load Risk Profile.
    if (currentAsset !== d.asset) {
      const ab = document.querySelector('.asset-btn[data-asset="' + d.asset + '"]');
      if (ab) ab.click();
    }
    const nav = document.querySelector('.nav-item[data-page="optv4"]');
    if (nav) nav.click();
    v4Status("Agents proposal #" + d.id + " (" + esc(d.asset) + "): loading the risk profile…");
    try { await optv4Load(); } catch (e) { v4Status("Load Risk Profile failed: " + esc(e.message)); return; }
    if (!optv4Data) { v4Status("Load Risk Profile returned no book."); return; }

    // 2. The run's settings, field by field as the page posts them.
    optv4ManualTarget = null;
    v4Set("optv4-target-select", P.target_profile_file || "");
    v4Set("optv4-target-trough-payoff", P.parametric_trough_payoff);
    v4Check("optv4-target-asymmetric-toggle", true);
    if (P.parametric_low_floor_ratio != null) v4Set("optv4-target-down-pct", +(P.parametric_low_floor_ratio * 100).toFixed(2));
    if (P.parametric_high_plateau_ratio != null) v4Set("optv4-target-up-pct", +(P.parametric_high_plateau_ratio * 100).toFixed(2));
    if (typeof optv4UpdateTargetShapeReadouts === "function") optv4UpdateTargetShapeReadouts();

    const te = document.getElementById("optv4-target-expiry");
    if (te && P.target_expiry) {
      if (![...te.options].some((o) => o.value === P.target_expiry)) {
        te.add(new Option(P.target_expiry + " (from agents)", P.target_expiry));
        notes.push(P.target_expiry + " is not on today's vol surface; added it to the list");
      }
      v4Set("optv4-target-expiry", P.target_expiry);
    }
    const cs = document.getElementById("optv4-counterparties");
    if (cs) {
      const want = (P.counterparties || []).map((c) => String(c).toLowerCase());
      [...cs.options].forEach((o) => { o.selected = want.length ? want.includes(o.value.toLowerCase()) : false; });
      cs.dispatchEvent(new Event("change", { bubbles: true }));
    }
    const nums = {
      "optv4-lam-factor": P.lam_factor, "optv4-downside-factor": P.downside_factor,
      "optv4-t90-weight": P.t90_weight, "optv4-atm-concentration": P.atm_concentration,
      "optv4-mu-factor": P.mu_factor, "optv4-cash-neutrality-factor": P.cash_neutrality_factor,
      "optv4-unwind-discount": P.unwind_discount, "optv4-new-position-penalty": P.new_position_penalty,
      "optv4-roll-dte-threshold": P.roll_dte_threshold, "optv4-collateral-budget-pct": P.collateral_budget_pct,
      "optv4-max-qty": P.max_qty, "optv4-max-trades": P.max_trades, "optv4-delta-band": P.delta_band_usd,
      "optv4-custom-spot": "",
    };
    Object.keys(nums).forEach((id) => v4Set(id, nums[id]));
    v4Check("optv4-roll-itm-only", P.roll_itm_only);
    v4Check("optv4-enable-box-neutralizer", P.enable_box_neutralizer);
    v4Check("optv4-enable-composite-unwind", P.enable_composite_unwind);
    v4Check("optv4-enable-delta-rehedge", P.enable_delta_rehedge);
    v4Check("optv4-roll-use-ticked", false);
    v4Check("optv4-exclude-collar-loans", true);
    if (typeof P.bid_ask_vol_pts === "number") {
      document.querySelectorAll("#optv4-volpts-list .opt-volpts-input").forEach((i) => { i.value = P.bid_ask_vol_pts; });
    }
    // The agents run on the whole book with no forced rolls; say so if the page
    // would scope it differently.
    if (tmSelected && tmSelected.size) notes.push(tmSelected.size + " Trade Management selection(s) will be sent as forced rolls - clear them for an exact replay");
    if (optv2BaseIds()) notes.push("a Deals scope is active, so the run covers only part of the book");

    // 3. Run Optimizer and wait for the result (the page's own handler).
    optv4SyncRunEnabled();
    const runBtn = document.getElementById("btn-run-optv4");
    if (!runBtn || runBtn.disabled) { v4Status("Run Optimizer is disabled - check the target expiry."); return; }
    v4Status("Agents proposal #" + d.id + ": running the optimizer with the agent's settings…");
    optv4OptResult = null;
    optv4HideError();                       // an old error must not end the wait
    runBtn.click();
    const errBox = document.getElementById("optv4-error");
    const t0 = Date.now();
    while (!optv4OptResult && !(errBox && errBox.offsetParent) && Date.now() - t0 < 15 * 60 * 1000) await sleep(500);
    if (!optv4OptResult) { v4Status("The run did not finish - see the error above."); return; }

    // 4. The agent's legs as what-if legs on the same chart.
    const S0 = optv4Data.eth_spot || 0;
    const legsIn = d.legs || [];
    const optLegs = legsIn.filter((l) => (l.kind || "option") === "option" && !/BOX/i.test(String(l.strategy || "")));
    const perps = legsIn.filter((l) => l.kind === "perp" || String(l.opt).toUpperCase() === "F");
    optv4ManualLegs = optv4ManualLegs.filter((l) => !l._agents);
    optLegs.forEach((l) => {
      const m = v4ManualLeg(l, S0, !fromOptimizer);
      if (m) { m._agents = d.id; optv4ManualLegs.push(m); }
    });
    if (!fromOptimizer && optLegs.length) {
      optv4ReplDeselected = new Set((optv4Replacements || []).map((t) => t._idx));
      document.querySelectorAll(".optv4-repl-cb").forEach((cb) => { cb.checked = false; });
      const all = document.getElementById("optv4-repl-all");
      if (all) all.checked = false;
    }
    optv4RenderManualLegs();
    optv4RefreshAfterWhatIf();
    if (perps.length) notes.push(perps.length + " perp leg(s) are not plotted: what-if legs are options only (A4: perps change direction, not shape)");

    v4Status("<b>Agents proposal #" + d.id + "</b> (" + esc(d.asset) + ", " + esc(d.kind) + ", routed " +
      esc(String(d.route).toUpperCase()) + ") replayed: risk profile loaded, settings " +
      (d.params_source === "stored" ? "as the agent ran them" : d.params_source === "summary"
        ? "read from the proposal summary" : "from today's policy preset") +
      " (λ " + P.lam_factor + ", κ " + P.downside_factor + ", T+90 " + P.t90_weight + ", max " + P.max_trades +
      " trades, " + esc(P.target_expiry || "") + ").<br>" +
      (fromOptimizer
        ? "The trade table is today's fresh run. The agent's " + optLegs.length +
          " leg(s) are in What-if legs, switched off - tick them to compare with what it proposed."
        : "The agent's " + optLegs.length + " designed leg(s) are on as What-if legs; the optimizer's own " +
          "suggestions are unticked for comparison.") +
      (notes.length ? '<br><span style="color:#f59e0b">' + notes.map(esc).join("; ") + "</span>" : ""));
  }

  document.addEventListener("click", async (ev) => {
    if (!(ev.target instanceof HTMLElement)) return;
    const pf = ev.target.closest("[data-pfilter]");
    if (pf) { PFILTER = pf.dataset.pfilter; renderProposals(); return; }
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
          DOC = r.ok ? mdToHtml(await r.text())
                     : "<p>Strategy document not found on the server.</p>";
        } catch (e) { DOC = "<p>Could not load the strategy document: " + esc(e.message) + "</p>"; }
        $("agents-doc-body").innerHTML = DOC;
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
    if (t.id === "btn-agents-policy-reload") { load(); return; }
    if (t.id === "btn-agents-policy-save") {
      try {
        // Start from the raw JSON so the optimizer preset survives, then let
        // the form fields win for everything they cover.
        let base;
        try { base = JSON.parse($("agents-policy-json").value); }
        catch (e) { base = { ...POLICY }; }
        const body = readPolicyForm(base);
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
