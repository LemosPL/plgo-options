// Agents page: status, proposals, latest output, Monday policy, kill switch.
// Self-contained; loads when the Agents nav item is clicked.
(function () {
  const SCHEDULE = {
    "row-watcher": "every 5 min",
    "morning-open": "09:00",
    "optimizer": "09:30 and 16:00",
    "handover": "15:30",
    "night-desk": "23:00",
    "close-check": "02:00",
    "monday-pack": "Mon 07:30",
    "monthly-review": "1st, 08:00",
  };
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const asset = () => (typeof currentAsset !== "undefined" ? currentAsset : "ETH");
  async function api(url, opts) {
    const r = await fetch(url, opts);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || r.statusText);
    return j;
  }

  async function load() {
    try {
      const st = await api("/api/agents/status");
      const kill = st.kill_switch === "on";
      $("agents-kill-state").textContent = kill ? "KILL SWITCH ON" : "Agents live (shadow)";
      $("btn-agents-kill").textContent = kill ? "Turn agents back on" : "Kill switch";
      const warn = [];
      Object.entries(st.policies).forEach(([a, p]) => {
        if (p.is_example) warn.push(`${a} is on the manual's example limits`);
        if (!p.reference_price) warn.push(`${a} has no Monday reference`);
      });
      Object.entries(st.curves_frozen).forEach(([a, v]) => { if (v === "on") warn.push(`${a} curves disagree: trading frozen (B2a)`); });
      $("agents-warn").style.display = warn.length ? "" : "none";
      $("agents-warn").textContent = warn.join(" · ");
      const badge = $("nav-badge-agents");
      if (badge) badge.textContent = st.open_proposals || "";

      $("agents-tbody").innerHTML = Object.entries(st.agents).map(([name, r]) =>
        `<tr><td>${name}</td><td>${SCHEDULE[name] || ""}</td>` +
        `<td>${r ? esc(r.started_at.replace("T", " ").slice(0, 16)) + " UTC" : "never"}</td>` +
        `<td>${r ? esc(r.status) + (r.delivered ? " · sent" : "") : ""}</td>` +
        `<td style="white-space:nowrap"><button class="btn-secondary" style="width:auto" data-show="${name}">Show</button> ` +
        `<button class="btn-secondary" style="width:auto" data-run="${name}">Run now</button></td></tr>`).join("");

      const props = (await api("/api/agents/proposals?limit=40")).proposals;
      $("agents-props-tbody").innerHTML = props.map((p) =>
        `<tr><td>${p.id}</td><td>${esc(p.created_at.slice(5, 16).replace("T", " "))}</td><td>${p.asset}</td>` +
        `<td>${esc(p.agent)}</td><td>${esc(p.summary)}</td>` +
        `<td><b>${esc(p.route.toUpperCase())}</b><br><span style="color:var(--muted)">${esc(p.status)}</span></td>` +
        `<td style="font-size:.72rem">${(p.reasons || []).map(esc).join("<br>")}</td>` +
        `<td style="white-space:nowrap">${p.status === "open" ?
          `<button class="btn-secondary" style="width:auto" data-decide="${p.id}" data-status="executed">Executed</button> ` +
          `<button class="btn-secondary" style="width:auto" data-decide="${p.id}" data-status="declined">Decline</button>` : ""}</td></tr>`).join("")
        || `<tr><td colspan="8" style="color:var(--muted)">No proposals yet.</td></tr>`;

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
    const r = (await api(`/api/agents/runs?agent=${encodeURIComponent(name)}&limit=1`)).runs[0];
    $("agents-output-name").textContent = `— ${name}`;
    $("agents-output").textContent = r ? r.text : "No runs yet.";
  }

  document.addEventListener("click", async (ev) => {
    const t = ev.target;
    if (!(t instanceof HTMLElement)) return;
    if (t.dataset.show) show(t.dataset.show);
    if (t.dataset.run) {
      t.textContent = "Running…"; t.disabled = true;
      try {
        const r = await api(`/api/agents/run/${t.dataset.run}`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ deliver: false, use_ai: true }),
        });
        $("agents-output-name").textContent = `— ${t.dataset.run}`;
        $("agents-output").textContent = r.text;
      } catch (e) { $("agents-output").textContent = "Failed: " + e.message; }
      t.textContent = "Run now"; t.disabled = false; load();
    }
    if (t.dataset.decide) {
      await api(`/api/agents/proposals/${t.dataset.decide}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: t.dataset.status, by: "desk" }),
      });
      load();
    }
    if (t.id === "btn-agents-refresh") load();
    if (t.id === "btn-agents-kill") {
      const on = $("agents-kill-state").textContent.startsWith("KILL");
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
        await api(`/api/agents/policy/${asset()}`, {
          method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
        });
        $("agents-policy-msg").textContent = "Saved.";
        load();
      } catch (e) { $("agents-policy-msg").textContent = "Not saved: " + e.message; }
    }
  });

  document.querySelector('.nav-item[data-page="agents"]')?.addEventListener("click", load);
})();
