// Reading captured frames back.
//
// Two halves. The PNG reader is checked against images built here, because a
// decoder that only ever sees one filter type is a decoder with three
// untested branches — and a canvas picks its filter per scanline, so a real
// capture will hit all of them.
//
// The rest runs corner detection and flap matching over real captures from
// tests/fixtures/captures/. One is committed — a 3x15 display photographed
// handheld — and any session dumped from the calibration page and unzipped
// there is picked up as well.
const fs = require('fs');
const path = require('path');
const { decodePng, encodePng, toGray } = require(path.join(__dirname, 'png.js'));

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

const cc = { count: 45, cols: 15, rows: 3 };
const api = new Function(
  'cc',
  ['ccSolveH', 'ccGauss', 'ccApplyH', 'ccScaleH', 'ccCellQuad', 'ccOtsu', 'ccFindBlobs',
   'ccCornerModules', 'ccCornerCentres', 'ccCellAspect', 'ccGrayOf',
   'ccFeatures', 'ccNormalise', 'ccCorrelate', 'ccBuildTemplates', 'ccMatch'].map(grab).join('\n') +
  // Taken from the source rather than copied here, so retuning the matcher is
  // tested at its new values instead of silently drifting away from what this
  // file asserts.
  '\n' + (src.match(/^const CC_(?:TPL|CELL)_[A-Z_]+\s*=\s*[0-9.]+;/gm) || []).join('\n') +
  '; return {ccSolveH, ccApplyH, ccScaleH, ccCellQuad, ccOtsu, ccFindBlobs,' +
  ' ccCornerModules, ccCornerCentres, ccCellAspect, ccGrayOf,' +
  ' ccFeatures, ccNormalise, ccCorrelate, ccBuildTemplates, ccMatch};'
)(cc);

// ccFeatures takes what a canvas hands it, so wrap grey pixels back up as one.
function asImageData(grey, w, h) {
  const data = new Uint8ClampedArray(w * h * 4);
  for (let i = 0; i < grey.length; i++) {
    data[i*4] = data[i*4+1] = data[i*4+2] = grey[i];
    data[i*4+3] = 255;
  }
  return { data, width: w, height: h };
}

let failures = 0;
const check = (label, got, want) => {
  const ok = got === want;
  if (!ok) failures++;
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}  (got ${JSON.stringify(got)}, want ${JSON.stringify(want)})`);
};
const near = (label, got, want, tol) => {
  const ok = Math.abs(got - want) <= tol;
  if (!ok) failures++;
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}  (got ${got}, want ${want} ±${tol})`);
};

// ── The PNG reader ─────────────────────────────────────────

// A gradient with an edge in it: flat images decode correctly under every
// filter even when the filter is applied wrongly.
function pattern(w, h, channels) {
  const px = Buffer.alloc(w * h * channels);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      for (let c = 0; c < channels; c++) {
        px[(y * w + x) * channels + c] = (x * 7 + y * 13 + c * 61 + (x > w / 2 ? 120 : 0)) & 0xff;
      }
    }
  }
  return px;
}

for (const channels of [1, 3, 4]) {
  const w = 23, h = 11;                       // deliberately not round numbers
  const px = pattern(w, h, channels);
  for (let filter = 0; filter <= 4; filter++) {
    const img = decodePng(encodePng(w, h, channels, px, filter));
    check(`${channels}-channel PNG, filter ${filter}: size`,
          `${img.width}x${img.height}x${img.channels}`, `${w}x${h}x${channels}`);
    check(`${channels}-channel PNG, filter ${filter}: pixels`,
          Buffer.compare(img.data, px), 0);
  }
}

// Grey comes out of a grey PNG unchanged, and out of a colour one by the same
// luma weights the tuner uses — a fixture must not shift when it is saved in
// a different colour type.
const greyPx = pattern(9, 4, 1);
const asGrey = toGray(decodePng(encodePng(9, 4, 1, greyPx, 0)));
check('grey PNG round-trips through toGray', Buffer.compare(Buffer.from(asGrey), greyPx), 0);

const solid = Buffer.alloc(4 * 3);
for (let i = 0; i < 4; i++) { solid[i*3] = 200; solid[i*3+1] = 100; solid[i*3+2] = 50; }
const lum = toGray(decodePng(encodePng(4, 1, 3, solid, 0)));
check('colour PNG uses the same luma weights',
      lum[0], (200 * 0.299 + 100 * 0.587 + 50 * 0.114) | 0);

let badMagic = null;
try { decodePng(Buffer.alloc(32)); } catch (e) { badMagic = e.message; }
check('a non-PNG is rejected', badMagic, 'not a PNG');

// ── Real captures, when there are any ──────────────────────

const FIXTURES = path.join(__dirname, '..', 'fixtures', 'captures');
const sessions = fs.existsSync(FIXTURES)
  ? fs.readdirSync(FIXTURES).filter(d =>
      fs.existsSync(path.join(FIXTURES, d, 'manifest.json')))
  : [];

if (!sessions.length) {
  console.log('\n  (no capture fixtures in tests/fixtures/captures — ' +
              'dump a session from the calibration page to exercise the camera path)');
} else {
  for (const name of sessions) {
    const dir = path.join(FIXTURES, name);
    const manifest = JSON.parse(fs.readFileSync(path.join(dir, 'manifest.json'), 'utf8'));
    console.log(`\n  session ${name}:`);

    cc.rows  = manifest.grid.rows;
    cc.cols  = manifest.grid.cols;
    cc.count = manifest.grid.count;

    // Corner detection, re-run over the two frames that produced the grid.
    const reg = manifest.registration;
    const before = path.join(dir, 'reg_before.png');
    const after = path.join(dir, 'reg_after.png');
    if (reg && fs.existsSync(before) && fs.existsSync(after)) {
      const a = toGray(decodePng(fs.readFileSync(before)));
      const b = toGray(decodePng(fs.readFileSync(after)));
      const diff = new Uint8Array(a.length);
      for (let i = 0; i < a.length; i++) diff[i] = Math.abs(b[i] - a[i]);

      const blobs = api.ccFindBlobs(diff, reg.frameWidth, reg.frameHeight);
      check('    four lit corners still found', blobs.length >= 4, true);

      if (blobs.length >= 4) {
        const best = blobs.slice().sort((p, q) => q.area - p.area).slice(0, 4);
        const byY = best.slice().sort((p, q) => p.y - q.y);
        const top = byY.slice(0, 2).sort((p, q) => p.x - q.x);
        const bot = byY.slice(2, 4).sort((p, q) => p.x - q.x);
        const dst = [top[0], top[1], bot[1], bot[0]]
          .map(p => ({ x: p.x / reg.scale, y: p.y / reg.scale }));
        const H = api.ccSolveH(api.ccCornerCentres(), dst);
        check('    a transform is recovered', !!H, true);
        if (H && reg.homography) {
          // Compare where it puts things, not the matrix: two homographies
          // can differ numerically and mean the same mapping.
          for (const [u, v] of [[0, 0], [1, 0], [1, 1], [0, 1], [0.5, 0.5]]) {
            const got = api.ccApplyH(H, u, v);
            const want = api.ccApplyH(reg.homography, u, v);
            near(`    (${u},${v}) maps to the same place (x)`, got.x, want.x, 2);
            near(`    (${u},${v}) maps to the same place (y)`, got.y, want.y, 2);
          }
        }
      }
    }

    // A fixture ships the readings in full but only a few of the crops —
    // 45 per position at 27 KB each is not something to put in a repo. Each
    // one it does name must be there and be the size it claims.
    let missing = 0, wrongSize = 0, named = 0;
    for (const pos of manifest.positions || []) {
      for (const mod of pos.modules) {
        if (!mod.file) continue;
        named++;
        const file = path.join(dir, mod.file);
        if (!fs.existsSync(file)) { missing++; continue; }
        const img = decodePng(fs.readFileSync(file));
        if (img.width !== manifest.cell.w || img.height !== manifest.cell.h) wrongSize++;
      }
    }
    check(`    all ${named} named crops are present`, missing, 0);
    check('    crops are the size the manifest claims', wrongSize, 0);

    // What the run decided, recomputed from what it recorded. This is the
    // arithmetic running against field data rather than made-up numbers.
    let mismatched = 0, scored = 0;
    for (const pos of manifest.positions || []) {
      for (const mod of pos.modules) {
        if (mod.err === undefined || mod.from === undefined || !mod.cal || !mod.flaps) continue;
        scored++;
        const expect = Math.max(0, Math.min(mod.cal - 1,
          Math.round(mod.from - mod.err * (mod.cal / mod.flaps))));
        if (expect !== mod.to) mismatched++;
      }
    }
    if (scored) check(`    ${scored} recorded corrections match the arithmetic`, mismatched, 0);

    // A module that read wrong at every position it was seen at is an
    // offset problem; the fixture is the evidence for that claim.
    const perModule = {};
    for (const pos of manifest.positions || []) {
      for (const mod of pos.modules) {
        if (mod.err === undefined) continue;
        (perModule[mod.id] = perModule[mod.id] || []).push(mod.err);
      }
    }
    const systemic = Object.keys(perModule).filter(id =>
      perModule[id].length >= 4 && perModule[id].every(e => e === perModule[id][0] && e !== 0));
    if (systemic.length) {
      console.log(`    note: modules ${systemic.join(', ')} were off by a constant ` +
                  `(${systemic.map(id => perModule[id][0]).join(', ')} flaps) — a home offset`);
    }
  }
}

// ── Matching, on its own terms ─────────────────────────────

// Correlation after normalising is indifferent to exposure: the same flap
// photographed brighter is still the same flap, which is the whole reason the
// comparison is done this way rather than on raw pixels.
const shape = new Uint8Array(24 * 60);
for (let y = 10; y < 40; y++) for (let x = 4; x < 18; x++) shape[y * 24 + x] = 200;
const dim = shape.map(v => Math.round(v * 0.45 + 20));
const fA = api.ccFeatures(asImageData(shape, 24, 60));
const fB = api.ccFeatures(asImageData(dim, 24, 60));
near('the same shape at a different exposure still matches', api.ccCorrelate(fA, fB), 1, 1e-6);

const other = new Uint8Array(24 * 60);
for (let y = 10; y < 40; y++) for (let x = 4; x < 18; x++) if ((x + y) % 3) other[y * 24 + x] = 200;
const fC = api.ccFeatures(asImageData(other, 24, 60));
check('a different shape does not', api.ccCorrelate(fA, fC) < 0.95, true);

// A minority on the wrong flap must not drag the reference image with them.
const stack = {};
for (let m = 0; m < 12; m++) stack[m] = m < 9 ? api.ccFeatures(asImageData(shape, 24, 60))
                                              : api.ccFeatures(asImageData(other, 24, 60));
const built = api.ccBuildTemplates({ 7: stack });
near('the reference image follows the majority', api.ccCorrelate(built[7], fA), 1, 0.02);
check('too few samples to outvote anything builds nothing',
      Object.keys(api.ccBuildTemplates({ 7: { 0: fA, 1: fB } })).length, 0);

// ── Matching, on the real capture ──────────────────────────

if (sessions.length) {
  const dir = path.join(FIXTURES, sessions[0]);
  const manifest = JSON.parse(fs.readFileSync(path.join(dir, 'manifest.json'), 'utf8'));
  const st = manifest.strips;
  if (st) {
    cc.rows = manifest.grid.rows; cc.cols = manifest.grid.cols; cc.count = manifest.grid.count;
    console.log(`\n  session ${sessions[0]}, matching ${st.indices.length} positions:`);

    // Unpack the strips into one feature vector per module per position.
    const samples = {};
    for (const idx of st.indices) {
      const img = decodePng(fs.readFileSync(path.join(dir, `strip_i${String(idx).padStart(2,'0')}.png`)));
      const g = toGray(img);
      samples[idx] = {};
      for (let m = 0; m < st.modules; m++) {
        const cell = new Uint8Array(st.cellW * st.cellH);
        for (let y = 0; y < st.cellH; y++)
          for (let x = 0; x < st.cellW; x++) cell[y*st.cellW+x] = g[y*img.width + m*st.cellW + x];
        samples[idx][m] = api.ccFeatures(asImageData(cell, st.cellW, st.cellH));
      }
    }

    // Templates from one half of the modules, tested on the other, so a
    // module is never matched against a reference it helped build.
    const halves = [[0, 1], [1, 0]];
    let agree = 0, disagree = 0, scored = 0, resolved = 0, blind = 0, uncheckable = 0;
    const byIdx = new Map(manifest.positions.map(p => [p.index, p]));
    // Only a handful of positions are shipped, so a module that was one flap
    // out is often sitting on a flap this fixture has no reference for. The
    // matcher cannot name a flap it was never shown; those are skipped rather
    // than counted against it.
    const shipped = new Set(st.indices);

    for (const [build, test] of halves) {
      const group = {};
      for (const idx of st.indices) {
        group[idx] = {};
        for (let m = 0; m < st.modules; m++) if (m % 2 === build) group[idx][m] = samples[idx][m];
      }
      const templates = api.ccBuildTemplates(group);
      for (const idx of st.indices) {
        const recorded = new Map((byIdx.get(idx).modules || []).map(x => [x.id, x]));
        for (let m = 0; m < st.modules; m++) {
          if (m % 2 !== test) continue;
          const hit = api.ccMatch(samples[idx][m], templates);
          if (!hit) continue;
          const rec = recorded.get(m);
          if (rec && rec.err !== undefined) {
            if (!shipped.has(idx + rec.err)) { uncheckable++; continue; }
            scored++;
            if (hit.index === idx + rec.err) agree++; else disagree++;
          } else {
            blind++;
            if (hit.conf >= 60) resolved++;
          }
        }
      }
    }

    const pc = (a, b) => b ? (100 * a / b).toFixed(1) + '%' : 'n/a';
    console.log(`    where the run got a reading: ${scored} cells, matcher agrees ${pc(agree, scored)}` +
                (uncheckable ? `  (${uncheckable} skipped: flap not in this fixture)` : ''));
    console.log(`    where it got nothing:        ${blind} cells, matcher resolves ${pc(resolved, blind)}`);
    check('    the matcher agrees with the run it can be checked against',
          scored > 0 && agree / scored > 0.98, true);
    check('    and answers most of what the run could not read',
          blind === 0 || resolved / blind > 0.85, true);
  }
}

console.log(failures ? `\n${failures} failure(s)` : '\nall checks passed');
process.exit(failures ? 1 : 0);
