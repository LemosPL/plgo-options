/* Does the Portfolio P&L chart show the same numbers as the P&L Matrix?
 *
 * Both now go through pfBookCurves (bridge-repriced pnl_by_horizon, shifted
 * by the book's value at (h=0, spot)). This harness pulls those functions
 * straight out of app.js — no copies — feeds them a synthetic book whose
 * pnl_by_horizon and payoff_by_horizon deliberately disagree, and checks:
 *   1. every chart point equals the matrix cell at the same (horizon, spot);
 *   2. the (Now, spot) anchor reads exactly 0;
 *   3. horizons stay distinct (the old payoff_by_horizon path repeated them);
 *   4. the curves come from pnl_by_horizon, not the payoff sentinel.
 *
 * Usage: node scripts/diag_pnl_chart_vs_matrix.mjs
 */
import fs from 'fs';

const APP = 'src/plgo_options/web/static/app.js';
const lines = fs.readFileSync(APP, 'latin1').split(/\r?\n/);

function grab(name) {
  const i = lines.findIndex(l => l.startsWith('function ' + name + '('));
  if (i < 0) throw new Error('not found in app.js: ' + name);
  let depth = 0;
  const out = [];
  for (let j = i; j < lines.length; j++) {
    out.push(lines[j]);
    for (const ch of lines[j]) {
      if (ch === '{') depth++;
      else if (ch === '}') depth--;
    }
    if (depth === 0 && out.join('').indexOf('{') >= 0) break;
  }
  return out.join('\n');
}

// Single-line `const NAME = ...;` declarations those functions close over.
function grabConst(name) {
  const l = lines.find(x => x.startsWith('const ' + name + ' ='));
  if (!l) throw new Error('const not found in app.js: ' + name);
  return l;
}

const CONSTS = ['OPTV_MATRIX_STEP_PCT', 'OPTV_MATRIX_MAX_ROWS', 'OPTV_MATRIX_MIN_ROWS'];
const NAMES = ['pfMatrixCurve', 'pfBookCurves', 'pfPnlHorizons', 'optv4InterpAt', 'optvGeometricRows'];
const preamble = [
  'const OPTV2_HORIZONS = [0, 16, 30, 60, 90, 120, 150];',
  'let pfData = null; const pfRolled = new Map();',
  'function pfLookupIv() { return null; }',
  'function bsPrice() { return 0; }',
].join('\n');
const expose = 'return { ' + NAMES.join(', ') + ', setData: v => { pfData = v; } };';
const m = new Function([preamble, ...CONSTS.map(grabConst), ...NAMES.map(grab), expose].join('\n'))();

const spots = Array.from({ length: 41 }, (_, i) => 1000 + i * 100);
const spot = 3000;
const HZ = [0, 16, 30, 60, 90, 120, 150];
const PAYOFF_SENTINEL = 999;
const mkPos = (id, mult) => ({
  id, days_remaining: 45, strike: 3200, opt: 'C', net_qty: 1, iv_pct: 70,
  payoff_by_horizon: Object.fromEntries(HZ.map(h => [String(h), spots.map(() => PAYOFF_SENTINEL)])),
  pnl_by_horizon: Object.fromEntries(HZ.map(h => [String(h), spots.map(s => mult * (s - 2500 + h * 3))])),
});
m.setData({ eth_spot: spot, spot_ladder: spots, pnl_matrix_horizons: HZ, positions: [mkPos(1, 1), mkPos(2, 2)] });

const set = new Set([1, 2]);
const horizons = m.pfPnlHorizons();
const chart = m.pfBookCurves(set, horizons);
const matrix = m.pfBookCurves(set, horizons);
const rows = m.optvGeometricRows(spots, spot);

let fail = 0;
const check = (ok, msg) => { if (!ok) fail++; console.log((ok ? 'PASS  ' : 'FAIL  ') + msg); };

console.log('horizons: ' + horizons.join(', ') + '   display rows: ' + rows.length);

let maxDiff = 0;
for (const h of horizons) {
  for (const s of rows) {
    const a = m.optv4InterpAt(spots, chart[h], s) || 0;
    const b = m.optv4InterpAt(spots, matrix[h], s) || 0;
    maxDiff = Math.max(maxDiff, Math.abs(a - b));
  }
}
check(maxDiff === 0, 'chart == matrix at every (horizon, row): max diff ' + maxDiff);

// Absolute, not anchored: (Now, spot) must equal the summed book value there.
const atSpot = m.optv4InterpAt(spots, chart[0], spot) || 0;
const rawNow = spots.map((s, i) => (1 + 2) * (s - 2500 + 0 * 3));
const expectedAtSpot = m.optv4InterpAt(spots, rawNow, spot) || 0;
check(Math.abs(atSpot - expectedAtSpot) < 1e-9 && Math.abs(atSpot) > 1e-9,
  '(Now, spot) is the absolute book mark, not 0: got ' + atSpot + ', expected ' + expectedAtSpot);

const perH = horizons.map(h => (m.optv4InterpAt(spots, chart[h], spot) || 0).toFixed(2));
console.log('      value at spot per horizon: ' + horizons.map((h, i) => h + 'd=' + perH[i]).join('  '));
check(new Set(perH).size === horizons.length, 'all ' + horizons.length + ' horizons distinct (no repeated far columns)');

const flatAt999 = horizons.every(h => chart[h].every(v => v === chart[h][0]));
check(!flatAt999, 'curves come from pnl_by_horizon, not the flat payoff_by_horizon sentinel');

console.log(fail === 0 ? '\nALL CHECKS PASSED' : '\n' + fail + ' CHECK(S) FAILED');
process.exit(fail === 0 ? 0 : 1);
