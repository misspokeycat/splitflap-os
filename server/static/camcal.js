// ============================================================
//  CAMERA CALIBRATION
// ============================================================
// Point a camera at the display, step every module through the flap
// positions, and read back what each one actually landed on. A module
// showing the wrong character is off by a whole number of flaps, and a
// flap is `calibration / flap_count` motor steps — so one photo yields an
// exact correction for all 45 modules at once.
//
// The sweep writes nothing. Corrections are staged in the browser,
// reviewed, and then written once in a single batch through
// /restore_settings. Per-nudge EEPROM writes land while the motors are
// drawing hardest, which is how modules lose their tuning.

const CC_MOTION_MAX_W   = 480;   // frame width used for settle detection
const CC_MOTION_W       = 12;    // per-cell crop for settle detection
const CC_MOTION_H       = 18;
const CC_OCR_W          = 96;    // per-cell crop handed to OCR
const CC_OCR_H          = 144;
const CC_FRAME_MS       = 90;    // settle sampling interval
const CC_STABLE_FRAMES  = 4;     // consecutive quiet frames before we believe it
const CC_SETTLE_TIMEOUT = 20000;
const CC_NOISE_FRAMES   = 24;    // frames used to learn the camera's noise floor
const CC_NOISE_GAIN     = 3.0;
const CC_NOISE_MIN      = 2.0;
const CC_CELL_INSET     = 0.14;  // trim each cell toward its centre, away from bezels
const CC_OCR_WORKERS    = 3;
const CC_TESSERACT_SRC  = 'https://cdn.jsdelivr.net/npm/tesseract.js@5.1.1/dist/tesseract.min.js';

// OCR-able flaps. The colour tiles (roygbpw), the blank and the symbols are
// either unreadable or easy to confuse, and 36 positions spread around the
// drum already characterise every module.
const CC_SWEEP_ALPHA = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';
const CC_SWEEP_DIGIT = '0123456789';

const cc = {
  screen:      'start',
  stream:      null,
  video:       null,
  corners:     [],     // {x,y} in natural video pixels: TL, TR, BR, BL
  H:           null,   // unit square -> image
  rows:        3,
  cols:        15,
  count:       45,
  threshold:   CC_NOISE_MIN,
  sweep:       [],     // char indices to visit
  results:     {},     // modId -> { charIndex: {read, conf, err, from, to} }
  unread:      {},     // modId -> count of positions we could not read
  settings:    null,
  ocr:         [],
  abort:       false,
  running:     false,
  frameCanvas: null,
  workCanvas:  null,
};

// ── Screens ────────────────────────────────────────────────

function openCamCal(){
  document.getElementById('camCalOverlay').style.display = 'flex';
  ccShow('start');
  ccResetRun();
}

function closeCamCal(){
  ccStop();
  document.getElementById('camCalOverlay').style.display = 'none';
  if(typeof loadSettingsData === 'function' && globalSettings) loadSettingsData();
}

function ccShow(name){
  cc.screen = name;
  for(const s of ['start','register','preview','homing','sweep','review']){
    const el = document.getElementById('cc_' + s);
    if(el) el.style.display = (s === name) ? 'block' : 'none';
  }
}

function ccResetRun(){
  cc.results = {};
  cc.unread  = {};
  cc.abort   = false;
  cc.running = false;
}

function ccStop(){
  cc.abort   = true;
  cc.running = false;
  if(cc.stream){
    cc.stream.getTracks().forEach(t => t.stop());
    cc.stream = null;
  }
  for(const slot of cc.ocr){
    try { slot.worker.terminate(); } catch(e) {}
  }
  cc.ocr = [];
}

// ── Camera ─────────────────────────────────────────────────

// getUserMedia is gated to secure contexts. The Pi serves plain HTTP, so on
// http://<pi-ip> the camera is unavailable no matter what the page does —
// say so precisely instead of showing an empty preview.
function ccCameraBlocked(){
  if(window.isSecureContext) return false;
  return !(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
}

function ccShowCamError(html){
  document.getElementById('ccCamError').style.display = 'block';
  document.getElementById('ccCamErrorText').innerHTML = html;
}

async function ccBeginCamera(){
  ccShow('register');
  document.getElementById('ccCamError').style.display = 'none';
  cc.video = document.getElementById('ccVideo');

  if(ccCameraBlocked()){
    ccShowCamError(
      'This page is served over plain HTTP (<code>' + location.origin + '</code>), and browsers ' +
      'only expose the camera to secure origins.<br><br>' +
      '<strong>To allow it for this Pi:</strong><br>' +
      'Chrome — open <code>chrome://flags/#unsafely-treat-insecure-origin-as-secure</code>, add ' +
      '<code>' + location.origin + '</code>, and relaunch.<br>' +
      'Firefox — set <code>media.devices.insecure.enabled</code> to true in <code>about:config</code>.'
    );
    return;
  }

  try {
    cc.stream = await navigator.mediaDevices.getUserMedia({
      video: {
        facingMode: { ideal: 'environment' },
        width:      { ideal: 3840 },
        height:     { ideal: 2160 },
      },
    });
  } catch(err){
    ccShowCamError('Camera could not be opened: ' + (err && err.message ? err.message : err));
    return;
  }

  cc.video.srcObject = cc.stream;
  await cc.video.play();

  cc.frameCanvas = document.createElement('canvas');
  cc.workCanvas  = document.createElement('canvas');

  cc.rows  = (globalSettings && parseInt(globalSettings.sim_rows)) || 3;
  cc.cols  = (globalSettings && parseInt(globalSettings.sim_cols)) || 15;
  cc.count = cc.rows * cc.cols;

  cc.corners = [];
  cc.H = null;
  ccRenderRegister();
}

// ── Corner registration ────────────────────────────────────

const CC_CORNER_NAMES = ['top-left', 'top-right', 'bottom-right', 'bottom-left'];
const CC_UNIT = [{x:0,y:0}, {x:1,y:0}, {x:1,y:1}, {x:0,y:1}];

function ccRenderRegister(){
  const n = cc.corners.length;
  const hint = document.getElementById('ccRegHint');
  if(n < 4){
    const corner = [0, cc.cols - 1, cc.count - 1, cc.count - cc.cols][n];
    hint.innerHTML = 'Tap the <strong>' + CC_CORNER_NAMES[n] + '</strong> corner of the display — ' +
                     'the outer corner of module ' + corner + '.';
  } else {
    hint.innerHTML = 'All four corners set. Check that the green cells line up with the modules.';
  }
  document.getElementById('ccRegNext').style.display = (n === 4) ? 'inline-block' : 'none';
  document.getElementById('ccRegUndo').style.display = (n > 0)  ? 'inline-block' : 'none';
  ccDrawOverlay();
}

function ccTapVideo(ev){
  if(cc.corners.length >= 4) return;
  ev.preventDefault();
  const v = cc.video;
  const r = v.getBoundingClientRect();
  const pt = ev.touches ? ev.touches[0] : ev;
  cc.corners.push({
    x: (pt.clientX - r.left) * (v.videoWidth  / r.width),
    y: (pt.clientY - r.top)  * (v.videoHeight / r.height),
  });
  if(cc.corners.length === 4) cc.H = ccSolveH(CC_UNIT, cc.corners);
  ccRenderRegister();
}

function ccUndoCorner(){
  cc.corners.pop();
  cc.H = null;
  ccRenderRegister();
}

function ccDrawOverlay(){
  const v = cc.video, cv = document.getElementById('ccOverlay');
  const r = v.getBoundingClientRect();
  cv.width  = r.width;
  cv.height = r.height;
  const sx = r.width  / (v.videoWidth  || 1);
  const sy = r.height / (v.videoHeight || 1);
  const ctx = cv.getContext('2d');
  ctx.clearRect(0, 0, cv.width, cv.height);

  ctx.fillStyle = '#ffb000';
  for(const c of cc.corners){
    ctx.beginPath();
    ctx.arc(c.x * sx, c.y * sy, 7, 0, Math.PI * 2);
    ctx.fill();
  }

  if(cc.corners.length !== 4 || !cc.H) return;
  ctx.strokeStyle = 'rgba(0,255,140,.85)';
  ctx.lineWidth = 1.5;
  for(let i = 0; i < cc.count; i++){
    const quad = ccCellQuad(i);
    ctx.beginPath();
    quad.forEach((p, k) => k ? ctx.lineTo(p.x * sx, p.y * sy) : ctx.moveTo(p.x * sx, p.y * sy));
    ctx.closePath();
    ctx.stroke();
  }
}

// ── Geometry ───────────────────────────────────────────────

// Solve the projective transform taking four source points to four
// destination points: eight unknowns, eight equations.
function ccSolveH(src, dst){
  const A = [], b = [];
  for(let i = 0; i < 4; i++){
    const s = src[i], d = dst[i];
    A.push([s.x, s.y, 1, 0, 0, 0, -d.x * s.x, -d.x * s.y]); b.push(d.x);
    A.push([0, 0, 0, s.x, s.y, 1, -d.y * s.x, -d.y * s.y]); b.push(d.y);
  }
  const h = ccGauss(A, b);
  return h ? [h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7], 1] : null;
}

function ccGauss(A, b){
  const n = b.length;
  const M = A.map((row, i) => row.concat([b[i]]));
  for(let col = 0; col < n; col++){
    let piv = col;
    for(let r = col + 1; r < n; r++){
      if(Math.abs(M[r][col]) > Math.abs(M[piv][col])) piv = r;
    }
    if(Math.abs(M[piv][col]) < 1e-9) return null;
    const tmp = M[col]; M[col] = M[piv]; M[piv] = tmp;
    for(let r = 0; r < n; r++){
      if(r === col) continue;
      const f = M[r][col] / M[col][col];
      for(let k = col; k <= n; k++) M[r][k] -= f * M[col][k];
    }
  }
  return M.map((row, i) => row[n] / M[i][i]);
}

function ccApplyH(H, x, y){
  const w = H[6] * x + H[7] * y + H[8];
  return { x: (H[0] * x + H[1] * y + H[2]) / w,
           y: (H[3] * x + H[4] * y + H[5]) / w };
}

// The same transform composed with a uniform scale on its output — one
// registration then serves both the full-resolution OCR grab and the
// downscaled frames used for motion.
function ccScaleH(H, s){
  return [H[0]*s, H[1]*s, H[2]*s, H[3]*s, H[4]*s, H[5]*s, H[6], H[7], H[8]];
}

// Module index is grid position: the display is addressed row-major.
function ccCellQuad(modId, H){
  H = H || cc.H;
  const row = Math.floor(modId / cc.cols), col = modId % cc.cols;
  const i = CC_CELL_INSET;
  const u0 = (col + i) / cc.cols, u1 = (col + 1 - i) / cc.cols;
  const v0 = (row + i) / cc.rows, v1 = (row + 1 - i) / cc.rows;
  return [ccApplyH(H, u0, v0), ccApplyH(H, u1, v0),
          ccApplyH(H, u1, v1), ccApplyH(H, u0, v1)];
}

// ── Frame capture ──────────────────────────────────────────

function ccFrame(maxWidth){
  const v = cc.video;
  const downscale = maxWidth && maxWidth < v.videoWidth;
  const w = downscale ? maxWidth : v.videoWidth;
  const s = w / v.videoWidth;
  const h = Math.round(v.videoHeight * s);
  const c = downscale ? cc.workCanvas : cc.frameCanvas;
  if(c.width !== w || c.height !== h){ c.width = w; c.height = h; }
  const ctx = c.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(v, 0, 0, w, h);
  return { img: ctx.getImageData(0, 0, w, h), scale: s };
}

// Rectify one module's window out of a frame, sampling bilinearly.
function ccWarpCell(frame, quad, w, h){
  const H = ccSolveH([{x:0,y:0}, {x:w,y:0}, {x:w,y:h}, {x:0,y:h}], quad);
  const out = new ImageData(w, h);
  if(!H) return out;
  const src = frame.data, sw = frame.width, sh = frame.height;
  for(let j = 0; j < h; j++){
    for(let i = 0; i < w; i++){
      const p  = ccApplyH(H, i + 0.5, j + 0.5);
      const x  = Math.max(0, Math.min(sw - 1.001, p.x));
      const y  = Math.max(0, Math.min(sh - 1.001, p.y));
      const x0 = x | 0, y0 = y | 0, fx = x - x0, fy = y - y0;
      const o  = (j * w + i) * 4;
      for(let ch = 0; ch < 3; ch++){
        const p00 = src[(y0 * sw + x0) * 4 + ch];
        const p10 = src[(y0 * sw + x0 + 1) * 4 + ch];
        const p01 = src[((y0 + 1) * sw + x0) * 4 + ch];
        const p11 = src[((y0 + 1) * sw + x0 + 1) * 4 + ch];
        out.data[o + ch] = (p00 * (1 - fx) + p10 * fx) * (1 - fy) +
                           (p01 * (1 - fx) + p11 * fx) * fy;
      }
      out.data[o + 3] = 255;
    }
  }
  return out;
}

function ccGrabCells(w, h, maxWidth){
  const frame = ccFrame(maxWidth);
  const H = ccScaleH(cc.H, frame.scale);
  const cells = [];
  for(let i = 0; i < cc.count; i++) cells.push(ccWarpCell(frame.img, ccCellQuad(i, H), w, h));
  return cells;
}

function ccGray(cell){
  const n = cell.width * cell.height, g = new Uint8Array(n);
  for(let i = 0; i < n; i++){
    g[i] = (cell.data[i * 4] * 0.299 + cell.data[i * 4 + 1] * 0.587 + cell.data[i * 4 + 2] * 0.114) | 0;
  }
  return g;
}

function ccMad(a, b){
  let sum = 0;
  for(let i = 0; i < a.length; i++) sum += Math.abs(a[i] - b[i]);
  return sum / a.length;
}

const ccSleep = ms => new Promise(r => setTimeout(r, ms));

// ── Settle detection ───────────────────────────────────────

// A recheck photo taken while the reels are still turning reads whichever
// flap happens to be passing. Learn how much the image moves when nothing is
// moving, then treat anything above that as motion.
async function ccLearnNoiseFloor(){
  let prev = null, worst = 0;
  for(let f = 0; f < CC_NOISE_FRAMES; f++){
    const cur = ccGrabCells(CC_MOTION_W, CC_MOTION_H, CC_MOTION_MAX_W).map(ccGray);
    if(prev){
      for(let i = 0; i < cur.length; i++) worst = Math.max(worst, ccMad(prev[i], cur[i]));
    }
    prev = cur;
    await ccSleep(CC_FRAME_MS);
  }
  cc.threshold = Math.max(worst * CC_NOISE_GAIN, CC_NOISE_MIN);
  return cc.threshold;
}

async function ccWaitForSettle(onTick){
  const t0 = performance.now();
  let prev = null, stable = 0, moving = [];
  while(performance.now() - t0 < CC_SETTLE_TIMEOUT){
    if(cc.abort) return { settled: false, moving: [], aborted: true };
    const cur = ccGrabCells(CC_MOTION_W, CC_MOTION_H, CC_MOTION_MAX_W).map(ccGray);
    if(prev){
      moving = [];
      for(let i = 0; i < cur.length; i++){
        if(ccMad(prev[i], cur[i]) > cc.threshold) moving.push(i);
      }
      stable = moving.length ? 0 : stable + 1;
      if(onTick) onTick(moving, stable);
      if(stable >= CC_STABLE_FRAMES) return { settled: true, moving: [] };
    }
    prev = cur;
    await ccSleep(CC_FRAME_MS);
  }
  return { settled: false, moving: moving };
}

// ── OCR ────────────────────────────────────────────────────

function ccLoadTesseract(){
  if(window.Tesseract) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = CC_TESSERACT_SRC;
    s.onload = resolve;
    s.onerror = () => reject(new Error('Tesseract.js could not be loaded — this page needs internet access.'));
    document.head.appendChild(s);
  });
}

// Only the glyphs this sweep can actually land on, so a misread has to be a
// character that is genuinely in the alphabet.
function ccWhitelist(){
  const set = new Set();
  for(const idx of cc.sweep){
    for(let m = 0; m < cc.count; m++){
      const ch = getCharMap(m)[idx];
      if(ch) set.add(ch);
    }
  }
  return [...set].join('');
}

async function ccInitOcr(){
  await ccLoadTesseract();
  const whitelist = ccWhitelist();
  // Sweeping a second time rebuilds the pool; the previous workers are real
  // background threads and do not go away on their own.
  for(const slot of cc.ocr){
    try { slot.worker.terminate(); } catch(e) {}
  }
  cc.ocr = [];
  for(let i = 0; i < CC_OCR_WORKERS; i++){
    const worker = await Tesseract.createWorker('eng');
    await worker.setParameters({
      tessedit_char_whitelist: whitelist,
      tessedit_pageseg_mode:   '10',   // treat the image as a single character
    });
    cc.ocr.push({ worker: worker, busy: false });
  }
}

function ccCellToCanvas(cell){
  const c = document.createElement('canvas');
  c.width = cell.width; c.height = cell.height;
  c.getContext('2d').putImageData(cell, 0, 0);
  return c;
}

// Run every module's crop through the pool, keeping all workers busy.
async function ccRecognizeAll(cells){
  const out = new Array(cells.length).fill(null);
  let next = 0;
  await Promise.all(cc.ocr.map(async function(slot){
    while(true){
      const i = next++;
      if(i >= cells.length || cc.abort) return;
      try {
        const res  = await slot.worker.recognize(ccCellToCanvas(cells[i]));
        const text = (res.data.text || '').replace(/\s+/g, '');
        out[i] = { char: text.length === 1 ? text : '', conf: res.data.confidence || 0 };
      } catch(e){
        out[i] = { char: '', conf: 0 };
      }
    }
  }));
  return out;
}

// ── The sweep ──────────────────────────────────────────────

function ccBuildSweep(){
  const set = document.getElementById('ccSweepSet').value;
  const chars = set === 'digit' ? CC_SWEEP_DIGIT
              : set === 'both'  ? CC_SWEEP_ALPHA + CC_SWEEP_DIGIT
              : CC_SWEEP_ALPHA;
  const map = getCharMap(0);
  const idx = [];
  for(const ch of chars){
    const i = map.indexOf(ch);
    if(i > 0) idx.push(i);
  }
  return idx.sort((a, b) => a - b);
}

async function ccBegin(){
  cc.sweep = ccBuildSweep();
  if(!cc.sweep.length){ showToast('No readable flap positions in the char map', 'error'); return; }

  ccResetRun();
  ccShow('homing');
  const status = document.getElementById('ccHomingStatus');

  status.textContent = 'Learning the camera noise floor — hold still…';
  await ccLearnNoiseFloor();

  status.textContent = 'Loading OCR…';
  try {
    await ccInitOcr();
  } catch(err){
    showToast(err.message, 'error');
    ccShow('preview');
    return;
  }

  status.textContent = 'Reading current tuning…';
  cc.settings = await (await fetch('/settings')).json();

  status.textContent = 'Stopping the running app and homing all modules…';
  await fetch('/stop_app', { method: 'POST' });
  await fetch('/auto_tune', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'home' }),
  });

  // Homing is a full revolution at worst. Wait for the image to go quiet
  // rather than guessing at a countdown.
  status.textContent = 'Waiting for the reels to stop…';
  await ccWaitForSettle();

  ccShow('sweep');
  await ccRunSweep();
}

async function ccRunSweep(){
  cc.running = true;
  const bar   = document.getElementById('ccSweepBar');
  const label = document.getElementById('ccSweepLabel');
  const note  = document.getElementById('ccSweepNote');

  for(let n = 0; n < cc.sweep.length; n++){
    if(cc.abort) break;
    const idx   = cc.sweep[n];
    const shown = getCharMap(0)[idx];

    bar.style.width = Math.round((n / cc.sweep.length) * 100) + '%';
    label.textContent = 'Position ' + (n + 1) + ' of ' + cc.sweep.length +
                        ' — "' + shown + '" (index ' + idx + ')';

    note.textContent = 'Moving…';
    await fetch('/auto_tune', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'goto_char', char_index: idx }),
    });

    const settle = await ccWaitForSettle(function(moving){
      note.textContent = moving.length
        ? 'Waiting — ' + moving.length + ' module' + (moving.length === 1 ? '' : 's') + ' still turning'
        : 'Settling…';
    });
    if(settle.aborted) break;

    if(!settle.settled){
      // Never read a frame we know is smeared: a bad read here becomes a bad
      // tuning value later.
      note.textContent = 'Skipped index ' + idx + ' — ' + settle.moving.length +
                         ' module(s) never stopped';
      for(const m of settle.moving) cc.unread[m] = (cc.unread[m] || 0) + 1;
      await ccSleep(700);
      continue;
    }

    note.textContent = 'Reading…';
    const cells = ccGrabCells(CC_OCR_W, CC_OCR_H);
    const reads = await ccRecognizeAll(cells);
    const res   = await fetch('/tuning_status?char_index=' + idx);
    const data  = await res.json();
    ccScoreReads(idx, reads, data.positions || {});
    ccRenderSweepGrid();
  }

  bar.style.width = '100%';
  cc.running = false;
  ccRenderReview();
  ccShow('review');
}

// Turn one frame's reads into staged corrections.
function ccScoreReads(idx, reads, positions){
  const minConf  = parseInt(document.getElementById('ccMinConf').value)  || 0;
  const maxFlaps = parseInt(document.getElementById('ccMaxFlaps').value) || 2;

  for(let m = 0; m < cc.count; m++){
    const r       = reads[m];
    const map     = getCharMap(m);
    const flaps   = getFlapCount(m);
    const readIdx = (r && r.char) ? map.indexOf(r.char) : -1;

    if(readIdx < 0 || !r || r.conf < minConf){
      cc.unread[m] = (cc.unread[m] || 0) + 1;
      continue;
    }

    // Signed distance in flaps, shortest way round the drum.
    let err = ((readIdx - idx) % flaps + flaps) % flaps;
    if(err > flaps / 2) err -= flaps;

    // A module is off by one flap, sometimes two. An apparent error of
    // seventeen is a misread, not a mechanism — drop it rather than write it.
    if(Math.abs(err) > maxFlaps){
      cc.unread[m] = (cc.unread[m] || 0) + 1;
      continue;
    }

    const pos  = positions[String(m)] || {};
    const cal  = parseInt((cc.settings && cc.settings.calibrations &&
                           cc.settings.calibrations[String(m)]) || 4096);
    const from = (pos.active !== undefined && pos.active !== null)
      ? parseInt(pos.active) : Math.floor(idx * cal / flaps);

    // Reading the character ahead means the module overshot: fewer steps.
    let to = Math.round(from - err * (cal / flaps));
    to = Math.max(0, Math.min(cal - 1, to));

    if(!cc.results[m]) cc.results[m] = {};
    cc.results[m][idx] = { read: r.char, conf: Math.round(r.conf), err: err, from: from, to: to };
  }
}

function ccModStats(m){
  const byIdx = cc.results[m] || {};
  const keys  = Object.keys(byIdx);
  return {
    reads:  keys.length,
    wrong:  keys.filter(k => byIdx[k].err !== 0).length,
    unread: cc.unread[m] || 0,
    byIdx:  byIdx,
  };
}

function ccRenderSweepGrid(){
  const grid = document.getElementById('ccSweepGrid');
  grid.innerHTML = '';
  grid.style.gridTemplateColumns = 'repeat(' + cc.cols + ',1fr)';
  for(let m = 0; m < cc.count; m++){
    const s = ccModStats(m);
    const cell = document.createElement('div');
    cell.className = 'cc-cell' + (s.wrong ? ' bad' : s.reads ? ' ok' : '');
    cell.textContent = String(m).padStart(2, '0');
    cell.title = 'Module ' + m + ': ' + s.reads + ' read, ' + s.wrong + ' wrong, ' +
                 s.unread + ' unreadable';
    grid.appendChild(cell);
  }
}

function ccAbortSweep(){
  cc.abort = true;
  showToast('Stopping after this position — nothing has been written', 'warn');
}

// ── Review ─────────────────────────────────────────────────

function ccRenderReview(){
  const grid = document.getElementById('ccReviewGrid');
  grid.innerHTML = '';
  grid.style.gridTemplateColumns = 'repeat(' + cc.cols + ',1fr)';
  let totalFixes = 0, blind = 0;

  for(let m = 0; m < cc.count; m++){
    const s = ccModStats(m);
    totalFixes += s.wrong;
    if(!s.reads) blind++;
    const cell = document.createElement('div');
    cell.className = 'cc-cell' + (s.wrong ? ' bad' : s.reads ? ' ok' : ' blind');
    cell.textContent = s.wrong ? String(s.wrong) : String(m).padStart(2, '0');
    cell.title = 'Module ' + m + ': ' + s.reads + ' read, ' + s.wrong + ' to correct, ' +
                 s.unread + ' unreadable';
    grid.appendChild(cell);
  }

  document.getElementById('ccReviewSummary').innerHTML =
    '<strong>' + totalFixes + '</strong> position' + (totalFixes === 1 ? '' : 's') +
    ' to correct across ' + cc.count + ' modules' +
    (blind ? ' · <span style="color:var(--orange)">' + blind + ' module(s) never read</span>' : '') +
    '<br><span style="color:#777;font-size:.8rem">Nothing has been written yet.</span>';

  ccRenderSystemic();
  document.getElementById('ccApplyBtn').disabled = (totalFixes === 0);
}

// A module wrong by the same amount everywhere has a home-offset problem, not
// thirty-six separate per-character problems. Say so — `offsets` is one value
// and one EEPROM write; per-character tuning is dozens.
function ccRenderSystemic(){
  const systemic = [];
  for(let m = 0; m < cc.count; m++){
    const s = ccModStats(m);
    if(s.reads < 4 || !s.wrong) continue;
    const errs = Object.keys(s.byIdx).map(k => s.byIdx[k].err);
    if(errs.length === s.reads && errs.every(e => e === errs[0])){
      const cal = parseInt((cc.settings.calibrations || {})[String(m)] || 4096);
      systemic.push({
        m: m,
        err: errs[0],
        steps: Math.round(-errs[0] * cal / getFlapCount(m)),
      });
    }
  }

  const el = document.getElementById('ccSystemic');
  if(!systemic.length){ el.style.display = 'none'; return; }
  el.style.display = 'block';
  el.innerHTML =
    '<strong>Off by the same amount at every position:</strong> ' +
    systemic.map(s => 'module ' + s.m + ' (' + (s.err > 0 ? '+' : '') + s.err + ' flap, ' +
                      (s.steps > 0 ? '+' : '') + s.steps + ' steps)').join(', ') +
    '<br><span style="color:#999">That is a home offset, not per-character drift. Adjusting the ' +
    'offset in the Hardware Inspector fixes all 64 positions with one value — applying below would ' +
    'instead write a correction for every position measured.</span>';
}

// Write once, at the end. restore_module_settings erases a module's tuning and
// rewrites it, so each module must be sent its complete map — corrections
// merged over what is already stored, not the corrections alone.
async function ccApply(){
  const btn = document.getElementById('ccApplyBtn');
  btn.disabled = true;
  btn.textContent = 'Writing…';

  const stored = (cc.settings && cc.settings.tuned_chars) || {};
  const merged = {};
  for(let m = 0; m < cc.count; m++){
    const s = ccModStats(m);
    if(!s.wrong) continue;
    const key = String(m);
    const map = Object.assign({}, stored[key] || {});
    for(const idx of Object.keys(s.byIdx)){
      if(s.byIdx[idx].err !== 0) map[idx] = s.byIdx[idx].to;
    }
    merged[key] = map;
  }

  try {
    const res  = await fetch('/restore_settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tuned_chars: merged }),
    });
    const data = await res.json();
    if(data.status !== 'success') throw new Error(data.message || 'Write rejected');
  } catch(err){
    btn.disabled = false;
    btn.textContent = 'Apply Corrections';
    showToast('Write failed: ' + err.message, 'error');
    return;
  }

  // Storage on this hardware drops writes quietly. Read the modules back and
  // say whether they actually kept what we just sent.
  btn.textContent = 'Verifying…';
  const audit = document.getElementById('ccAudit');
  audit.style.display = 'block';
  audit.textContent = 'Reading the modules back…';
  try {
    const res  = await fetch('/module_audit', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: Object.keys(merged).map(Number) }),
    });
    const data = await res.json();
    if(data.error) throw new Error(data.error);
    const bad = (data.modules || []).filter(x => x.status !== 'ok');
    audit.innerHTML = bad.length
      ? '<strong style="color:var(--orange)">' + bad.length + ' module(s) did not read back clean:</strong> ' +
        bad.map(x => x.id + ' (' + x.status + ')').join(', ') +
        '<br><span style="color:#999">Write them again, or check them in the Hardware Inspector — a ' +
        'dropped EEPROM write during motor draw looks exactly like this.</span>'
      : '<strong style="color:var(--green)">All written modules read back clean.</strong>';
  } catch(err){
    audit.innerHTML = '<span style="color:var(--orange)">Could not verify: ' + err.message + '</span>';
  }

  btn.textContent = 'Applied';
  showToast('Corrections written');
}

function ccRestart(){
  ccResetRun();
  ccShow('preview');
}
