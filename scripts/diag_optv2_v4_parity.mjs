/* Are Optimizer v2 and v4 aligned on the inputs they send and the feedback they
 * give? v4 descends from v3, which forked before several v2 features existed,
 * so gaps here are silent: v4 accepts a setting, sends it, and shows nothing.
 *
 * Custom spot was exactly that — v4 had the input and sent custom_spot, but had
 * neither v2's live-spot placeholder nor v2's "Custom spot: $X" readout, so a
 * run on a hypothetical spot looked identical to a run on the live mark.
 *
 * Usage: node scripts/diag_optv2_v4_parity.mjs
 */
import fs from 'fs';

const js = fs.readFileSync('src/plgo_options/web/static/app.js', 'latin1');
const jsLines = js.split(/\r?\n/);
const html = fs.readFileSync('src/plgo_options/web/templates/index.html', 'utf8');

let fail = 0;
const check = (ok, msg) => { if (!ok) fail++; console.log((ok ? 'PASS  ' : 'FAIL  ') + msg); };

// ── 1. /api/optimization/run payload keys must match exactly ──────────────
function payloadKeys(fromLine) {
  const i = jsLines.findIndex((l, idx) => idx > fromLine && l.includes('post("/api/optimization/run"'));
  let depth = 0, started = false; const keys = [];
  for (let j = i; j < jsLines.length; j++) {
    for (const c of jsLines[j]) { if (c === '{') { depth++; started = true; } else if (c === '}') depth--; }
    if (started && depth >= 1) { const m = jsLines[j].match(/^\s{6}([a-z_0-9]+):/); if (m) keys.push(m[1]); }
    if (started && depth === 0) break;
  }
  return keys;
}
const v2Keys = payloadKeys(9000), v4Keys = payloadKeys(14500);
const s2 = new Set(v2Keys), s4 = new Set(v4Keys);
const onlyV2 = v2Keys.filter(k => !s4.has(k));
const onlyV4 = v4Keys.filter(k => !s2.has(k));
check(v2Keys.length > 30, `v2 payload parsed (${v2Keys.length} keys)`);
check(onlyV2.length === 0, 'no run param on v2 but missing from v4' + (onlyV2.length ? ': ' + onlyV2.join(', ') : ''));
check(onlyV4.length === 0, 'no run param on v4 but missing from v2' + (onlyV4.length ? ': ' + onlyV4.join(', ') : ''));

// ── 2. Per-feature parity: markup + wiring on BOTH screens ────────────────
// Each entry is something v2 does; v4 must do the equivalent.
const FEATURES = [
  { name: 'Custom spot: input',        html: id => `id="${id}-custom-spot"` },
  { name: 'Custom spot: placeholder',  js:   id => `${id}-custom-spot")` , needs: 'placeholder' },
  { name: 'Custom spot: summary wrap', html: id => `id="${id}-sum-custom-spot-wrap"` },
  { name: 'Custom spot: summary value',html: id => `id="${id}-sum-custom-spot"` },
  { name: 'Custom spot: readout wired',js:   id => `${id}-sum-custom-spot-wrap` },
];

for (const f of FEATURES) {
  for (const id of ['optv2', 'optv4']) {
    let ok;
    if (f.html) ok = html.includes(f.html(id));
    else if (f.needs === 'placeholder') {
      // the getElementById for the input must be followed by a .placeholder assignment
      const i = js.indexOf(f.js(id));
      ok = i >= 0 && /\.placeholder\s*=/.test(js.slice(i, i + 400));
    } else ok = js.includes(f.js(id));
    check(ok, `${f.name} — ${id}`);
  }
}

// custom_spot must be in each screen's OWN payload, not merely somewhere in the file.
check(s2.has('custom_spot'), 'Custom spot: in v2 run payload');
check(s4.has('custom_spot'), 'Custom spot: in v4 run payload');

console.log(fail === 0 ? '\nALL CHECKS PASSED' : '\n' + fail + ' CHECK(S) FAILED');
process.exit(fail === 0 ? 0 : 1);
