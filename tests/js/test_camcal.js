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
const FLAPS = 64, CAL = 4096, NUDGE = 25;   // a flap is 64 steps; a nudge is not

const cc = {
  count: 45, cols: 15, rows: 3,
  results: {}, unread: {}, live: {},
  settings: { calibrations: {}, tuned_chars: {} },
};

const controls = { ccMinConf: { value: '60' }, ccMaxFlaps: { value: '2' },
                   ccStepSize: { value: String(NUDGE) } };

const code = ['ccSolveH', 'ccGauss', 'ccApplyH', 'ccScaleH', 'ccCellQuad', 'ccScoreReads',
              'ccCornerModules', 'ccCornerCentres', 'ccOtsu', 'ccFindBlobs', 'ccStepSize']
  .map(grab).join('\n');
const api = new Function(
  'cc', 'CC_CELL_INSET', 'document', 'getCharMap', 'getFlapCount',
  code + '; return {ccSolveH, ccApplyH, ccScaleH, ccCellQuad, ccScoreReads,' +
         ' ccCornerModules, ccCornerCentres, ccOtsu, ccFindBlobs, ccStepSize};'
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

const reset = () => { cc.results = {}; cc.unread = {}; cc.live = {}; };

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

// The flap that is showing says which way, not how far. A module past the
// boundary might be five steps over or forty, so it is nudged and looked at
// again — the same 25 steps Auto Fine-Tune applies by hand. Moving it a
// whole flap would overshoot almost every time and land it a flap out the
// other way, which is what a run that went from 171 wrong to 252 was doing.
reset();
api.ccScoreReads(at, readsOf({ 0: { char: CHAR_MAP[at + 1], conf: 92 } }), positions);
check('overshoot recorded as +1 flap', cc.results[0][at].err, 1);
check('overshoot nudges back, it does not jump a flap', cc.results[0][at].to, FROM - NUDGE);

reset();
api.ccScoreReads(at, readsOf({ 0: { char: CHAR_MAP[at - 1], conf: 92 } }), positions);
check('undershoot recorded as -1 flap', cc.results[0][at].err, -1);
check('undershoot nudges forward', cc.results[0][at].to, FROM + NUDGE);

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
check('wrap corrects the short way round', cc.results[0][1].to, 64 + NUDGE);

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
check('module 0 corrected from the shared frame',  cc.results[0][at].to,  FROM - NUDGE);
check('module 7 left alone from the shared frame', cc.results[7][at].err, 0);
check('module 44 corrected from the shared frame', cc.results[44][at].to, FROM + NUDGE);

// ── Registering from the four corner modules ───────────────

// The lit patch is a module's window, so its centre is the middle of a cell,
// not the corner of the grid. Treating one as the other would shift the whole
// grid by half a module and read every column off by one.
check('corner modules are the grid corners',
      JSON.stringify(api.ccCornerModules()), JSON.stringify([0, 14, 44, 30]));
const centres = api.ccCornerCentres();
near('first corner centre sits half a cell in (x)', centres[0].x, 0.5 / 15);
near('first corner centre sits half a cell in (y)', centres[0].y, 0.5 / 3);
near('third corner centre sits half a cell in (x)', centres[2].x, 1 - 0.5 / 15);
near('third corner centre sits half a cell in (y)', centres[2].y, 1 - 0.5 / 3);

// Photograph four lit corners through a skewed view, recover the transform
// from their centres alone, and the rest of the grid must land where the
// original put it.
const truth = api.ccSolveH(UNIT, [{x:120,y:70}, {x:1180,y:40}, {x:1210,y:400}, {x:90,y:360}]);
const seen  = centres.map(c => api.ccApplyH(truth, c.x, c.y));
const recovered = api.ccSolveH(centres, seen);
for (const m of [0, 7, 22, 44]) {
  cc.H = truth;     const want = api.ccCellQuad(m);
  cc.H = recovered; const got  = api.ccCellQuad(m);
  near(`module ${m} lands in the same place (x)`, got[0].x, want[0].x, 1e-6);
  near(`module ${m} lands in the same place (y)`, got[0].y, want[0].y, 1e-6);
}
cc.H = null;

// ── Picking the lit corners out of a difference image ──────

// Four bright squares on black, as the before/after difference produces.
const DW = 200, DH = 60;
const diff = new Uint8Array(DW * DH);
const squares = [[20, 12], [170, 12], [170, 46], [20, 46]];
for (const [cx, cy] of squares) {
  for (let y = cy - 5; y <= cy + 5; y++) {
    for (let x = cx - 5; x <= cx + 5; x++) diff[y * DW + x] = 240;
  }
}
const blobs = api.ccFindBlobs(diff, DW, DH);
check('four lit corners found', blobs.length, 4);

// Ordered the way the corner modules are: top-left, top-right, bottom-right,
// bottom-left. Getting this wrong mirrors or rotates the entire grid.
const byY = blobs.slice().sort((a, b) => a.y - b.y);
const ordered = byY.slice(0, 2).sort((a, b) => a.x - b.x)
  .concat(byY.slice(2, 4).sort((a, b) => a.x - b.x).reverse());
check('ordered top-left first',    `${Math.round(ordered[0].x)},${Math.round(ordered[0].y)}`, '20,12');
check('then top-right',            `${Math.round(ordered[1].x)},${Math.round(ordered[1].y)}`, '170,12');
check('then bottom-right',         `${Math.round(ordered[2].x)},${Math.round(ordered[2].y)}`, '170,46');
check('then bottom-left',          `${Math.round(ordered[3].x)},${Math.round(ordered[3].y)}`, '20,46');

// An unchanged frame differences to nothing, which must not be read as a grid.
check('an empty difference finds nothing', api.ccFindBlobs(new Uint8Array(DW * DH), DW, DH).length, 0);

// The threshold comes from the image, not a constant, so a dim room and a
// bright one both split in the right place. ccFindBlobs takes pixels above
// the cut, so the cut is the last background level, not a midpoint.
const split = (dark, light) => {
  const px = new Uint8Array(1000).fill(dark);
  for (let i = 0; i < 200; i++) px[i] = light;
  const cut = api.ccOtsu(px);
  return dark <= cut && light > cut;
};
check('a dim scene splits correctly',   split(20, 90), true);
check('a bright scene splits correctly', split(140, 230), true);
check('a faint difference still splits', split(6, 30), true);

console.log(failures ? `\n${failures} failure(s)` : '\nall checks passed');
process.exit(failures ? 1 : 0);
