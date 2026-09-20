// The camera tuner's arithmetic, which is the part that decides what gets
// written to EEPROM. A sign error in the flap-to-step conversion would not
// look wrong on screen — it would quietly move every module twice as far in
// the wrong direction — so the conversion is pinned here rather than checked
// by eye against the display.
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(
  path.join(__dirname, '..', '..', 'server', 'static', 'camcal.js'), 'utf8');

const grab = name => {
  const i = src.indexOf(`function ${name}(`);
  if (i < 0) throw new Error(`${name} not found in camcal.js`);
  let depth = 0, started = false;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') { depth++; started = true; }
    else if (src[j] === '}') { depth--; if (started && depth === 0) return src.slice(i, j + 1); }
  }
  throw new Error(`${name} is unbalanced`);
};

const CHAR_MAP = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$&()-+=;q:%'.,/?*roygbpw";
const FLAPS = 64, CAL = 4096, STEPS_PER_FLAP = CAL / FLAPS;   // 64

const cc = {
  count: 45, cols: 15, rows: 3,
  results: {}, unread: {},
  settings: { calibrations: {}, tuned_chars: {} },
};

const controls = { ccMinConf: { value: '60' }, ccMaxFlaps: { value: '2' } };

const code = ['ccSolveH', 'ccGauss', 'ccApplyH', 'ccScaleH', 'ccCellQuad', 'ccScoreReads']
  .map(grab).join('\n');
const api = new Function(
  'cc', 'CC_CELL_INSET', 'document', 'getCharMap', 'getFlapCount',
  code + '; return {ccSolveH, ccApplyH, ccScaleH, ccCellQuad, ccScoreReads};'
)(
  cc, 0.14,
  { getElementById: id => controls[id] },
  () => CHAR_MAP,
  () => FLAPS
);

let failures = 0;
const check = (label, got, want) => {
  const ok = got === want;
  if (!ok) failures++;
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}  (got ${JSON.stringify(got)}, want ${JSON.stringify(want)})`);
};
const near = (label, got, want, tol = 1e-6) => {
  const ok = Math.abs(got - want) <= tol;
  if (!ok) failures++;
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}  (got ${got}, want ~${want})`);
};

// ── Homography ─────────────────────────────────────────────

const UNIT = [{x:0,y:0}, {x:1,y:0}, {x:1,y:1}, {x:0,y:1}];

// A handheld phone never squares up to the display, so the transform has to
// be projective, not just a scale — the four corners of a skewed view must
// land exactly where they were tapped.
const skew = [{x:100,y:50}, {x:900,y:80}, {x:920,y:260}, {x:80,y:240}];
const H = api.ccSolveH(UNIT, skew);
UNIT.forEach((u, i) => {
  const p = api.ccApplyH(H, u.x, u.y);
  near(`corner ${i} x round-trips`, p.x, skew[i].x, 1e-6);
  near(`corner ${i} y round-trips`, p.y, skew[i].y, 1e-6);
});

// Downscaling the frame must move the mapped point by exactly that factor,
// or motion detection would sample a different part of the module than OCR.
const half = api.ccApplyH(api.ccScaleH(H, 0.5), 0.5, 0.5);
const full = api.ccApplyH(H, 0.5, 0.5);
near('scaled transform halves x', half.x, full.x / 2);
near('scaled transform halves y', half.y, full.y / 2);

// ── Cell layout ────────────────────────────────────────────

// Straight-on view, 1500x300, so each module is a clean 100x100 box and the
// arithmetic is checkable by hand.
cc.H = api.ccSolveH(UNIT, [{x:0,y:0}, {x:1500,y:0}, {x:1500,y:300}, {x:0,y:300}]);

const q0 = api.ccCellQuad(0);
near('module 0 inset from the left edge', q0[0].x, 14);
near('module 0 inset from the top edge',  q0[0].y, 14);
near('module 0 stops short of its right edge', q0[1].x, 86);

// Row-major: module 16 is row 1, column 1 — the display is addressed as one
// flat string, and the grid position is the module id.
const q16 = api.ccCellQuad(16);
near('module 16 centre x', (q16[0].x + q16[1].x) / 2, 150);
near('module 16 centre y', (q16[0].y + q16[2].y) / 2, 150);

const qLast = api.ccCellQuad(44);
near('module 44 sits in the bottom-right cell x', (qLast[0].x + qLast[1].x) / 2, 1450);
near('module 44 sits in the bottom-right cell y', (qLast[0].y + qLast[2].y) / 2, 250);

// ── Reading a frame into corrections ───────────────────────

const reset = () => { cc.results = {}; cc.unread = {}; };

// One module per case, all at char index 10 ("J"), currently resting on the
// step the server reports as active.
const at = 10;
const FROM = 640;                       // 10 flaps * 64 steps
const positions = {};
for (let m = 0; m < cc.count; m++) {
  positions[String(m)] = { active: FROM, expected: FROM, tuned: null };
  cc.settings.calibrations[String(m)] = CAL;
}
const readsOf = spec => {
  const out = [];
  for (let m = 0; m < cc.count; m++) out.push(spec[m] || { char: '', conf: 0 });
  return out;
};

// Showing the character one ahead means the module overshot: fewer steps.
// This matches the manual flow, where "one ahead" applies a negative delta.
reset();
api.ccScoreReads(at, readsOf({ 0: { char: CHAR_MAP[at + 1], conf: 92 } }), positions);
check('overshoot recorded as +1 flap', cc.results[0][at].err, 1);
check('overshoot subtracts one flap of steps', cc.results[0][at].to, FROM - STEPS_PER_FLAP);

reset();
api.ccScoreReads(at, readsOf({ 0: { char: CHAR_MAP[at - 1], conf: 92 } }), positions);
check('undershoot recorded as -1 flap', cc.results[0][at].err, -1);
check('undershoot adds one flap of steps', cc.results[0][at].to, FROM + STEPS_PER_FLAP);

reset();
api.ccScoreReads(at, readsOf({ 0: { char: CHAR_MAP[at], conf: 95 } }), positions);
check('a correct read is still a read', cc.results[0][at].err, 0);
check('a correct read changes nothing', cc.results[0][at].to, FROM);

// Wrapping the drum: at index 1, reading index 63 is two flaps behind, not
// sixty-two ahead. Getting this backwards would drive the module most of a
// revolution the wrong way.
reset();
api.ccScoreReads(1, readsOf({ 0: { char: CHAR_MAP[63], conf: 90 } }),
                 { '0': { active: 64 } });
check('wrap reads as the short way round', cc.results[0][1].err, -2);
check('wrap corrects the short way round', cc.results[0][1].to, 64 + 2 * STEPS_PER_FLAP);

// OCR noise must never become a write. A read implying a nineteen-flap error
// is a misread of a huge clean capital, not a mechanism that slipped.
reset();
api.ccScoreReads(at, readsOf({ 0: { char: CHAR_MAP[at + 19], conf: 99 } }), positions);
check('implausible error is discarded', cc.results[0], undefined);
check('implausible error counted as unread', cc.unread[0], 1);

reset();
api.ccScoreReads(at, readsOf({ 0: { char: CHAR_MAP[at + 1], conf: 20 } }), positions);
check('low-confidence read is discarded', cc.results[0], undefined);
check('low-confidence read counted as unread', cc.unread[0], 1);

reset();
api.ccScoreReads(at, readsOf({}), positions);
check('a module that read as nothing is unread', cc.unread[0], 1);
check('every module unread when the frame is blank', Object.keys(cc.unread).length, cc.count);

// A step is a position within one revolution; the correction must stay inside
// it, because the server rejects anything else and the module cannot reach it.
reset();
api.ccScoreReads(1, readsOf({ 0: { char: CHAR_MAP[2], conf: 90 } }), { '0': { active: 10 } });
check('correction clamps at zero', cc.results[0][1].to, 0);

reset();
api.ccScoreReads(1, readsOf({ 0: { char: CHAR_MAP[0], conf: 90 } }), { '0': { active: CAL - 10 } });
check('correction clamps below the calibration', cc.results[0][1].to, CAL - 1);

// Every module is scored from one frame — that is the whole point of moving
// them in lockstep.
reset();
api.ccScoreReads(at, readsOf({
  0:  { char: CHAR_MAP[at + 1], conf: 90 },
  7:  { char: CHAR_MAP[at],     conf: 90 },
  44: { char: CHAR_MAP[at - 1], conf: 90 },
}), positions);
check('module 0 corrected from the shared frame',  cc.results[0][at].to,  FROM - STEPS_PER_FLAP);
check('module 7 left alone from the shared frame', cc.results[7][at].err, 0);
check('module 44 corrected from the shared frame', cc.results[44][at].to, FROM + STEPS_PER_FLAP);

console.log(failures ? `\n${failures} failure(s)` : '\nall checks passed');
process.exit(failures ? 1 : 0);
