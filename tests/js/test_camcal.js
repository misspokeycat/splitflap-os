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
  // An async function is found by its `function` keyword, so take the
  // modifier with it — dropping it leaves the body's `await` inside a plain
  // function, which does not parse.
  const start = src.slice(0, i).endsWith('async ') ? i - 'async '.length : i;
  let depth = 0, started = false;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') { depth++; started = true; }
    else if (src[j] === '}') { depth--; if (started && depth === 0) return src.slice(start, j + 1); }
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
              'ccCornerModules', 'ccCornerCentres', 'ccOtsu', 'ccFindBlobs', 'ccStepSize',
              'ccWrapStep', 'ccCorrelate', 'ccMatch', 'ccReadsFrom', 'ccBehindSeen']
  .map(grab).join('\n');
const api = new Function(
  'cc', 'document', 'getCharMap', 'getFlapCount',
  code +
  // Taken from the source rather than copied here, so retuning any of them
  // is tested at its new value instead of drifting away from what this file
  // asserts.
  '\n' + (src.match(/^const CC_[A-Z_]+\s*=\s*[^;'"`]+;/gm) || []).join('\n') +
  '; return {ccSolveH, ccApplyH, ccScaleH, ccCellQuad, ccScoreReads,' +
         ' ccCornerModules, ccCornerCentres, ccOtsu, ccFindBlobs, ccStepSize, ccWrapStep,' +
         ' ccCorrelate, ccMatch, ccReadsFrom, ccBehindSeen};'
)(
  cc,
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

// A reel is a loop: step 0 and step cal-1 are neighbours. Clamping at the
// ends instead of wrapping turned a backward nudge near the start of the
// drum into no move at all, and only backward ones — forward never reaches
// that edge. It is the sort of asymmetry that shows up as "reverse
// corrections do not work".
reset();
api.ccScoreReads(1, readsOf({ 0: { char: CHAR_MAP[2], conf: 90 } }), { '0': { active: 10 } });
check('nudging back past zero wraps to the end of the reel',
      cc.results[0][1].to, CAL - 15);

reset();
api.ccScoreReads(1, readsOf({ 0: { char: CHAR_MAP[0], conf: 90 } }), { '0': { active: CAL - 10 } });
check('nudging forward past the end wraps to the start', cc.results[0][1].to, 15);

// Both directions move by the same amount from the same place.
reset();
api.ccScoreReads(at, readsOf({ 0: { char: CHAR_MAP[at + 1], conf: 90 } }), positions);
const back = cc.results[0][at].to;
reset();
api.ccScoreReads(at, readsOf({ 0: { char: CHAR_MAP[at - 1], conf: 90 } }), positions);
const forward = cc.results[0][at].to;
check('a backward nudge is the same size as a forward one',
      FROM - back, forward - FROM);
check('and every result is a step the reel actually has',
      [back, forward].every(v => v >= 0 && v < CAL), true);

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

// ── Noticing a module that is ahead ────────────────────────
//
// Corrections are made as the sweep goes, so only flaps already visited have
// a reference. A module one flap behind is showing one of those and is
// recognised. A module one flap ahead is showing a flap nothing has been
// learnt about yet and matches nothing — and used to be written off as
// unreadable, which is why a V showing W was never corrected while a V
// showing U always was.

// Real flap images are not unrelated: they share a module window, a frame
// and a background, so every reference correlates fairly well with every
// other. Built here to the separation actually measured on a capture of all
// 63 flaps — about 0.5 between different flaps, against 0.96 for a module
// sitting on the one it was sent to. Random vectors would be near
// orthogonal, every match would look confident, and the test would prove
// nothing about the situation this has to cope with.
const LEN = 512, SHARED = Math.sqrt(0.5);
const CC_ON_FLAP_VALUE = parseFloat(src.match(/^const CC_ON_FLAP\s*=\s*([\d.]+)/m)[1]);
function noise(seed) {
  const f = new Float64Array(LEN);
  let state = seed * 9301 + 49297;
  for (let i = 0; i < LEN; i++) {
    state = (state * 9301 + 49297) % 233280;
    f[i] = state / 233280 - 0.5;
  }
  return f;
}
const BACKGROUND = noise(1);
function vec(seed) {
  const own = noise(seed + 100), f = new Float64Array(LEN);
  for (let i = 0; i < LEN; i++) {
    f[i] = SHARED * BACKGROUND[i] + Math.sqrt(1 - SHARED * SHARED) * own[i];
  }
  let mean = 0; for (let i = 0; i < LEN; i++) mean += f[i];
  mean /= LEN;
  let ss = 0; for (let i = 0; i < LEN; i++) { f[i] -= mean; ss += f[i] * f[i]; }
  const inv = 1 / Math.sqrt(ss);
  for (let i = 0; i < LEN; i++) f[i] *= inv;
  return f;
}

// References for the flaps a forward sweep has reached: 8, 9 and 10.
const visited = { 8: vec(8), 9: vec(9), 10: vec(10) };
const unseen = vec(11);                       // flap 11, not visited yet

// The premise the rest of this rests on.
near('different flaps look alike, but not too alike',
     api.ccCorrelate(visited[9], visited[10]), 0.5, 0.12);

const behind = api.ccReadsFrom({ 10: { 0: visited[9] } }, visited, 10);
check('a module one flap behind is recognised', behind[0].index, 9);
check('and is not a guess', behind[0].assumed, false);

const onFlap = api.ccReadsFrom({ 10: { 0: visited[10] } }, visited, 10);
check('a module on the right flap reads as on it', onFlap[0].index, 10);
check('and is not a guess either', onFlap[0].assumed, false);

const ahead = api.ccReadsFrom({ 10: { 0: unseen } }, visited, 10);
check('a module matching nothing seen is taken to be ahead', ahead[0].index, 11);
check('and is marked as inferred', ahead[0].assumed, true);

check('and claims no confidence, because it has none', ahead[0].conf, 0);

// The inference has to turn into a nudge backwards, or it changes nothing —
// and it has to survive the confidence filter on its way there. It used to
// claim conf 100 to get through, which meant every module the camera could
// not read arrived at the same place wearing the same certainty.
reset();
api.ccScoreReads(10, readsOf({ 0: { index: 11, char: CHAR_MAP[11], conf: 0, assumed: true } }),
                 positions);
check('being ahead is scored as one flap ahead', cc.results[0][10].err, 1);
check('and nudges back, not forward', cc.results[0][10].to, FROM - NUDGE);
check('and is recorded as inferred', cc.results[0][10].assumed, true);

// The same reading without the inference flag is exactly what the filter is
// for, and is dropped.
reset();
api.ccScoreReads(10, readsOf({ 0: { index: 11, char: CHAR_MAP[11], conf: 0 } }), positions);
check('a matched read with no confidence is still discarded', cc.results[0], undefined);

// With no reference for the commanded flap there is nothing to say yet.
check('nothing is guessed before the flap has been seen',
      api.ccReadsFrom({ 12: { 0: unseen } }, visited, 12)[0], null);

// ── Where the inference does not hold ──────────────────────
//
// "It matches nothing seen, so it is on a flap still to come" needs the
// flaps it could be behind on to have been seen. Early in a sweep they have
// not been, and flap 0 — the blank — is never swept at all, so the opening
// positions of every pass called each wrong module ahead whatever it was
// really showing and nudged the ones that were behind further the wrong way.
check('the flaps behind are covered when they all have references',
      api.ccBehindSeen(10, FLAPS, visited, 2), true);
check('and are not when the sweep has not reached back that far',
      api.ccBehindSeen(9, FLAPS, visited, 2), false);

const early = { 9: vec(9), 10: vec(10) };      // flap 8 not yet visited
check('nothing is inferred while a flap it could be behind on is unseen',
      api.ccReadsFrom({ 10: { 0: unseen } }, early, 10)[0], null);

// A cell the camera cannot read looks like no flap at all. A module one flap
// ahead still looks like a flap — same window, same frame, same background.
// Only the second one is evidence of anything, and the difference matters
// because the inference ends in an EEPROM write.
const blind = (() => {
  const f = new Float64Array(LEN);
  for (let i = 0; i < LEN; i++) f[i] = ((i * 37) % 11) - 5;   // unrelated to any flap
  let mean = 0; for (let i = 0; i < LEN; i++) mean += f[i];
  mean /= LEN;
  let ss = 0; for (let i = 0; i < LEN; i++) { f[i] -= mean; ss += f[i] * f[i]; }
  const inv = 1 / Math.sqrt(ss);
  for (let i = 0; i < LEN; i++) f[i] *= inv;
  return f;
})();
check('an unreadable cell looks like nothing',
      Math.abs(api.ccCorrelate(blind, visited[10])) < 0.2, true);
check('and is left unread rather than nudged',
      api.ccReadsFrom({ 10: { 0: blind } }, visited, 10)[0], null);

// A weak best match to the flap it was sent to is a poor look at the right
// flap, not proof of being on the next one. Asserting "ahead" here wrote a
// correction to a module that was where it belonged.
const murky = (() => {
  const f = new Float64Array(LEN), other = vec(77);
  for (let i = 0; i < LEN; i++) f[i] = 0.25 * visited[10][i] + 0.75 * other[i];
  let mean = 0; for (let i = 0; i < LEN; i++) mean += f[i];
  mean /= LEN;
  let ss = 0; for (let i = 0; i < LEN; i++) { f[i] -= mean; ss += f[i] * f[i]; }
  const inv = 1 / Math.sqrt(ss);
  for (let i = 0; i < LEN; i++) f[i] *= inv;
  return f;
})();
const ownScore = api.ccCorrelate(murky, visited[10]);
check('the premise: too poor to call it on its flap', ownScore < CC_ON_FLAP_VALUE, true);
check('the premise: but still more like its own flap than any other',
      ownScore > Math.max(api.ccCorrelate(murky, visited[9]),
                          api.ccCorrelate(murky, visited[8])), true);
const weak = api.ccReadsFrom({ 10: { 0: murky } }, visited, 10);
check('a weak look at its own flap is not read as being ahead', weak[0].index, 10);
check('and nothing is inferred about it', weak[0].assumed, false);

// ── Nudging the same flap more than once ───────────────────
//
// A nudge is deliberately smaller than a flap, so a module past the boundary
// generally needs more than one. That only works if each attempt measures
// from where the module now is. `from` comes from cc.positions, which was
// read once before the first correction and never updated — so every attempt
// recomputed the same target from the same stale origin. The loop wrote the
// identical step six times, moved the module once, and called the flap
// stuck: the opposite of what nudging repeatedly was for, and invisible from
// outside, because every one of those writes succeeded.
//
// Driven through the real loop rather than the arithmetic alone. The bug was
// never in what one reading computes; it was in what the next reading is
// computed from.

const FIX_AT = 10;
const sim = { trueStep: 570, at: 640, writes: [], posts: 0, refuse: false };

async function ccPost(url, body) {
  if (url === '/apply_tuning') {
    sim.posts++;
    if (sim.refuse) return { ok: false, data: { error: 'flap 10 would sit 3 steps from flap 11' } };
    for (const m of Object.keys(body.tuned)) {
      for (const i of Object.keys(body.tuned[m])) sim.writes.push(body.tuned[m][i]);
    }
    return { ok: true, data: { status: 'success' } };
  }
  if (url === '/custom_tune') { sim.at = body.step; return { ok: true, data: {} }; }
  return { ok: true, data: {} };
}

// What the camera would see, given where the module is actually parked.
function ccRereadPosition(idx) {
  const shown = FIX_AT + Math.round((sim.at - sim.trueStep) / (CAL / FLAPS));
  loop.ccScoreReads(idx, [{ index: shown, char: CHAR_MAP[shown], conf: 95 }],
                    cc.positions[idx] || {});
  return Promise.resolve(true);
}

const ccStatus = () => {};

const loop = new Function(
  'cc', 'document', 'getCharMap', 'getFlapCount', 'ccPost', 'ccRereadPosition', 'ccStatus',
  ['ccFixPosition', 'ccStuckModules', 'ccWrongAt', 'ccCorrectionsAt', 'ccNoteWritten',
   'ccMoveTo', 'ccScoreReads', 'ccWrapStep', 'ccStepSize'].map(grab).join('\n') +
  '\n' + (src.match(/^const CC_[A-Z_]+\s*=\s*[^;'"`]+;/gm) || []).join('\n') +
  '; return {ccFixPosition, ccScoreReads};'
)(
  cc,
  { getElementById: id => controls[id] },
  () => CHAR_MAP,
  () => FLAPS,
  ccPost,
  ccRereadPosition,
  ccStatus
);

// One module, so the counts in the report read as themselves.
cc.count = 1;

const startFix = () => {
  reset();
  sim.at = 640; sim.writes = []; sim.posts = 0; sim.refuse = false;
  cc.positions = { [FIX_AT]: { '0': { active: 640, expected: 640, tuned: null } } };
  return ccRereadPosition(FIX_AT);
};

(async () => {
  const note = { textContent: '' };

  // Seventy steps out: one nudge of twenty-five is not enough, two are.
  await startFix();
  check('the module starts a flap ahead of where it was sent', cc.results[0][FIX_AT].err, 1);
  let out = await loop.ccFixPosition(FIX_AT, note);
  check('each nudge starts where the last one left off, so the flap comes right',
        out.fixed, true);
  check('in two nudges, not one', out.attempts, 2);
  check('and each write moves on from the one before',
        JSON.stringify(sim.writes), JSON.stringify([615, 590]));
  check('leaving nothing wrong at that position', out.left, 0);

  // What was written is what the next reading has to measure from.
  check('the stored position is carried forward as it is written',
        cc.positions[FIX_AT]['0'].active, 590);

  // A flap already right costs nothing — no write, no move, no wear.
  await startFix();
  sim.trueStep = 640;
  await ccRereadPosition(FIX_AT);
  out = await loop.ccFixPosition(FIX_AT, note);
  check('a flap already right is not written to', sim.writes.length, 0);
  check('and needs no attempts', out.attempts, 0);
  check('and is reported fixed', out.fixed, true);
  sim.trueStep = 570;

  // A refused write is the sequence check saying this is a misread rather
  // than a correction. Stop, and say which flap and why — naming it on the
  // spot is the point of correcting during the sweep, and the report was
  // being collected and then thrown away.
  await startFix();
  sim.refuse = true;
  out = await loop.ccFixPosition(FIX_AT, note);
  check('a refused write is not tried again', sim.posts, 1);
  check('and no nudge is counted, because none landed', out.attempts, 0);
  check('and it is not reported as fixed', out.fixed, false);
  check('and carries the reason it was refused', /would sit/.test(out.refused), true);
  check('and names the flap', out.char, CHAR_MAP[FIX_AT]);
  check('and the module still showing the wrong thing', out.modules.length, 1);
  check('by id', out.modules[0].id, 0);
  check('with what it showed instead', out.modules[0].read, CHAR_MAP[FIX_AT + 1]);

  console.log(failures ? `\n${failures} failure(s)` : '\nall checks passed');
  process.exit(failures ? 1 : 0);
})();
