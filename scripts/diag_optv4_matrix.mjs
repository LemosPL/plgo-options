/* Does the Optimizer v4 P&L Matrix render as ONE table with Before|After pairs?
 *
 * Pulls optv4RenderMatrix and its row/interp helpers straight out of app.js —
 * no copies — drives them against a fake thead/tbody, and checks:
 *   1. columns are exactly 30/60/90/120d (no Now, no 16d, no 150d);
 *   2. after a run each horizon has a Before AND an After sub-column, in that
 *      order, and every body row has 2 + 2*4 cells;
 *   3. before a run it degrades to one column per horizon (2 + 4 cells);
 *   4. the numbers in the cells are the underlying curves, Before != After;
 *   5. a horizon missing from the run is dropped, not faked.
 *
 * Usage: node scripts/diag_optv4_matrix.mjs
 */
import fs from 'fs';

const lines = fs.readFileSync('src/plgo_options/web/static/app.js', 'latin1').split(/\r?\n/);
const grabFn = (n) => {
  const i = lines.findIndex(l => l.startsWith('function ' + n + '('));
  if (i < 0) throw new Error('not found in app.js: ' + n);
  let d = 0; const o = [];
  for (let j = i; j < lines.length; j++) {
    o.push(lines[j]);
    for (const c of lines[j]) { if (c === '{') d++; else if (c === '}') d--; }
    if (d === 0 && o.join('').indexOf('{') >= 0) break;
  }
  return o.join('\n');
};
const grabConst = (n) => {
  const l = lines.find(x => x.startsWith('const ' + n + ' ='));
  if (!l) throw new Error('const not found in app.js: ' + n);
  return l;
};

const els = {};
const preamble = `
const document = { getElementById: (id) => (els[id] = els[id] || { innerHTML: '' }) };
let optv4OptResult = null, optv4Data = null;
let currentAsset = 'ETH';
function optv4Dp() { return 0; }
function optv2Fmt(v, dp) { return Number(v).toFixed(dp); }
function optv4ChartColors() { return { before: '#e5737e', horizon: { 0: '#cde2fb' } }; }
function optv4ActivePositions() { return (optv4Data && optv4Data.positions) || []; }
function optv4AfterSelectedAtHorizon(hKey) { return AFTER[hKey] || null; }
let AFTER = {};
`;
const CONSTS = ['OPTV_MATRIX_STEP_PCT', 'OPTV_MATRIX_MAX_ROWS', 'OPTV_MATRIX_MIN_ROWS', 'OPTV4_MATRIX_HORIZONS'];
const FNS = ['optv4MatrixOpts', 'optv4InterpAt', 'optvGeometricRows', 'optv4RenderMatrix'];
const m = new Function('els', preamble
  + CONSTS.map(grabConst).join('\n') + '\n'
  + FNS.map(grabFn).join('\n\n')
  + `\nreturn { optv4RenderMatrix, OPTV4_MATRIX_HORIZONS,
       setRun: v => { optv4OptResult = v; }, setData: v => { optv4Data = v; },
       setAfter: v => { AFTER = v; } };`)(els);

let fail = 0;
const check = (ok, msg) => { if (!ok) fail++; console.log((ok ? 'PASS  ' : 'FAIL  ') + msg); };

const spots = Array.from({ length: 61 }, (_, i) => 1000 + i * 100);
const spot = 3000;
const ALL_H = [0, 16, 30, 60, 90, 120, 150];
const curve = (k) => spots.map(s => k * (s - 2500));
const byH = (k) => Object.fromEntries(ALL_H.map(h => [String(h), curve(k + h / 100)]));

m.setData({ eth_spot: spot, spot_ladder: spots, positions: [{ id: 1, payoff_by_horizon: byH(1) }] });

const thRows = () => els['optv4-matrix-thead'].innerHTML.split('<tr>').filter(Boolean);
const bodyRows = () => els['optv4-matrix-tbody'].innerHTML.split('<tr').filter(Boolean);
const cellsIn = (row) => (row.match(/<td/g) || []).length;

// --- 1 & 3: no run -> one column per horizon -----------------------------
m.setRun(null); m.setAfter({});
m.optv4RenderMatrix();
const preHead = els['optv4-matrix-thead'].innerHTML;
check(/>30d</.test(preHead) && />60d</.test(preHead) && />90d</.test(preHead) && />120d</.test(preHead),
  'pre-run header has 30/60/90/120d');
check(!/>Now</.test(preHead) && !/>16d</.test(preHead) && !/>150d</.test(preHead),
  'pre-run header excludes Now, 16d and 150d');
check(thRows().length === 1, 'pre-run header is a single row (no Before/After split)');
check(bodyRows().every(r => cellsIn(r) === 2 + 4), 'pre-run rows have 2 + 4 cells');

// --- 2 & 4: with a run -> paired Before|After ----------------------------
m.setRun({ status: 'ok', spot, spot_ladder: spots, before: { payoff_by_horizon: byH(1) },
           after: { payoff_by_horizon: byH(2) } });
m.setAfter({});
m.optv4RenderMatrix();
const head = els['optv4-matrix-thead'].innerHTML;
check(thRows().length === 2, 'post-run header is two rows (horizon over Before/After)');
check((head.match(/colspan="2"/g) || []).length === 4, 'each of the 4 horizons spans 2 columns');
const subs = [...head.matchAll(/>(Before|After)</g)].map(x => x[1]).join(',');
check(subs === 'Before,After,Before,After,Before,After,Before,After',
  'sub-headers alternate Before,After per horizon: ' + subs);
const rows = bodyRows();
check(rows.every(r => cellsIn(r) === 2 + 2 * 4), 'post-run rows have 2 + 2*4 = 10 cells');

const nums = (r) => [...r.matchAll(/>(-?[\d,]+)<\/td>/g)].map(x => Number(x[1].replace(/,/g, '')));
const spotRow = rows.find(r => r.includes('row-highlight'));
check(!!spotRow, 'the current-spot row is highlighted');
const v = nums(spotRow);            // [%-move excluded: it ends in %], so cells 0.. are the numbers
check(v.length === 8, 'spot row exposes 8 numeric cells (4 horizons x Before/After): got ' + v.length);
check(v[0] !== v[1] && v[2] !== v[3], 'Before and After differ within a horizon');
console.log('      spot row Before/After pairs: '
  + [0, 2, 4, 6].map(i => `${[30, 60, 90, 120][i / 2]}d ${v[i]}|${v[i + 1]}`).join('   '));

// --- 5: a horizon absent from the run is dropped, not faked --------------
const partial = byH(1); delete partial['90'];
m.setRun({ status: 'ok', spot, spot_ladder: spots, before: { payoff_by_horizon: partial },
           after: { payoff_by_horizon: byH(2) } });
m.optv4RenderMatrix();
const h2 = els['optv4-matrix-thead'].innerHTML;
check(!/>90d</.test(h2) && />120d</.test(h2), 'a horizon missing from the run is dropped, not faked');

console.log(fail === 0 ? '\nALL CHECKS PASSED' : '\n' + fail + ' CHECK(S) FAILED');
process.exit(fail === 0 ? 0 : 1);
