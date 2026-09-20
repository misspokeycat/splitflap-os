// ============================================================
//  CAMERA CALIBRATION
// ============================================================
// Point a camera at the display, step every module through the flap
// positions, and read back what each one actually landed on. A module
// showing the wrong character is off by a whole number of flaps, and a
// flap is `calibration / flap_count` motor steps — so one photo yields an
// exact correction for all 45 modules at once.
//
// A sweep writes nothing. Corrections are staged in the browser, reviewed,
// and then written in one batch — one command per correction, and none for
// the positions that were already right. Per-nudge EEPROM writes land while
// the motors are drawing hardest, which is how modules lose their tuning.
//
// Writing a correction is not evidence it worked, so a pass can be repeated:
// re-reading the display after a write is the only thing that proves the
// module moved where it was told.

const CC_MOTION_MAX_W   = 480;   // frame width used for settle detection
const CC_MOTION_W       = 12;    // per-cell crop for settle detection
const CC_MOTION_H       = 18;
const CC_CELL_W         = 96;    // per-cell crop handed to the matcher
const CC_CELL_H         = 144;   // only a fallback; the real height comes from the grid
const CC_CELL_H_MAX     = 400;
const CC_FRAME_MS       = 90;    // settle sampling interval
const CC_STABLE_FRAMES  = 4;     // consecutive quiet frames before we believe it
const CC_SETTLE_TIMEOUT = 20000;
const CC_NOISE_FRAMES   = 24;    // frames used to learn the camera's noise floor
const CC_NOISE_GAIN     = 3.0;
const CC_NOISE_MIN      = 2.0;
const CC_CELL_INSET     = 0.14;  // trim each cell toward its centre, away from bezels
const CC_PREVIEW_MS     = 50;    // live overlay redraw interval
const CC_MAX_PASSES     = 4;     // ceiling on write/re-read rounds
const CC_CONFIRM_WAIT_MS = 1500; // let the modules finish committing before reading back
const CC_CONFIRM_TRIES  = 3;     // how many times to wait for them
const CC_REGISTER_CHARS = 'wyog'; // solid colour flaps, brightest first

// Which flaps to visit. Matching compares images, so the colour tiles and
// the symbols are as usable as the letters — there is nothing to "read".
// Letters and digits stay the default only because 36 positions already
// characterise a module and every extra one costs a move and a settle.
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
  landscape:   true,
  threshold:   CC_NOISE_MIN,
  sweep:       [],     // char indices to visit
  results:     {},     // modId -> { charIndex: {read, conf, err, from, to} }
  unread:      {},     // modId -> count of positions we could not read
  live:        {},     // modId -> {char, err} from the frame just read
  history:     [],     // wrong-count after each completed pass
  pass:        0,
  outcome:     null,   // why the run stopped, see CC_OUTCOME
  settings:    null,
  templates:   null,   // reference image per flap, built from the display
  abort:       false,
  running:     false,
  drag:        null,   // index of the corner handle being dragged
  previewId:   null,
  cellW:       CC_CELL_W,
  cellH:       CC_CELL_H,
  frameCanvas: null,
  workCanvas:  null,
  dump:        null,   // {session, manifest} while a run is being recorded
};

// ── Capture dump ───────────────────────────────────────────

// Everything the run saw, kept so a misread can be looked at rather than
// guessed at, and so a real capture can become a test fixture.
//
// Collected in the browser and handed over as one zip at the end. Nothing is
// sent to the Pi: a sweep is a few thousand PNGs, and writing those to the SD
// card the display boots from — while the motors are drawing, which is when
// this hardware loses writes — is the thing the rest of this file is built to
// avoid.

const CC_DUMP_MAX_BYTES = 120 * 1024 * 1024;

function ccDumpOn(){
  const el = document.getElementById('ccDump');
  return !!(el && el.checked);
}

function ccDumpStart(){
  if(!ccDumpOn()){ cc.dump = null; return; }
  // Registration opens the session before the sweep does, and its two frames
  // are the ones corner detection is tested against — don't start over.
  if(cc.dump) return;
  const now = new Date();
  const pad = n => String(n).padStart(2, '0');
  cc.dump = {
    session: now.getFullYear() + pad(now.getMonth() + 1) + pad(now.getDate()) + '-' +
             pad(now.getHours()) + pad(now.getMinutes()) + pad(now.getSeconds()),
    bytes:    0,
    files:    [],
    manifest: {
      created:   now.toISOString(),
      grid:      { rows: cc.rows, cols: cc.cols, count: cc.count },
      motion:    { w: CC_MOTION_W, h: CC_MOTION_H, frameWidth: CC_MOTION_MAX_W },
      charMap:   getCharMap(0),
      positions: [],
    },
  };
}

function ccCellToCanvas(cell){
  const c = document.createElement('canvas');
  c.width = cell.width; c.height = cell.height;
  c.getContext('2d').putImageData(cell, 0, 0);
  return c;
}

function ccPngOf(imageData){
  const b64 = ccCellToCanvas(imageData).toDataURL('image/png').split(',')[1];
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for(let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function ccImageFromGray(gray, w, h){
  const img = new ImageData(w, h);
  for(let i = 0; i < gray.length; i++){
    img.data[i*4] = img.data[i*4+1] = img.data[i*4+2] = gray[i];
    img.data[i*4+3] = 255;
  }
  return img;
}

function ccDumpAdd(name, bytes){
  if(!cc.dump) return false;
  if(cc.dump.bytes + bytes.length > CC_DUMP_MAX_BYTES){
    if(!cc.dump.full){
      showToast('Capture dump full at ' + ccBytes(CC_DUMP_MAX_BYTES) +
                ' — the rest of the run is not being saved', 'warn');
    }
    cc.dump.full = true;
    return false;
  }
  cc.dump.files.push({ name: name, bytes: bytes });
  cc.dump.bytes += bytes.length;
  return true;
}

// Finish the manifest and hand the whole session over as one file.
function ccDumpFinish(){
  if(!cc.dump || !cc.dump.files.length) return;
  cc.dump.manifest.cell       = { w: cc.cellW, h: cc.cellH, inset: CC_CELL_INSET };
  cc.dump.manifest.corners    = cc.corners;
  cc.dump.manifest.homography = cc.H;
  cc.dump.manifest.threshold  = cc.threshold;
  cc.dump.manifest.truncated  = !!cc.dump.full;
  cc.dump.manifest.history    = cc.history;
  cc.dump.manifest.outcome    = cc.outcome;
  cc.dump.manifest.video      = cc.video
    ? { width: cc.video.videoWidth, height: cc.video.videoHeight } : null;

  const json = new TextEncoder().encode(JSON.stringify(cc.dump.manifest, null, 2));
  cc.dump.files.push({ name: 'manifest.json', bytes: json });
  cc.dump.bytes += json.length;
  ccRenderDump();
}

function ccRenderDump(){
  const el = document.getElementById('ccDumpPanel');
  if(!el) return;
  if(!cc.dump || !cc.dump.files.length){ el.style.display = 'none'; return; }
  el.style.display = 'block';
  el.innerHTML =
    '<strong>Captured frames</strong> — ' + cc.dump.files.length + ' files, ' +
    ccBytes(cc.dump.bytes) +
    (cc.dump.full ? ' <span style="color:var(--orange)">(stopped at the size limit)</span>' : '') +
    '<div class="cc-capture-row"><span>' + cc.dump.session + '.zip</span>' +
    '<button class="btn btn-secondary btn-sm" onclick="ccDownloadDump()">Download</button></div>' +
    '<div style="color:#999;margin-top:6px">Unzip into <code>tests/fixtures/captures/</code> to ' +
    'turn this run into a regression test.</div>';
}

function ccDownloadDump(){
  if(!cc.dump || !cc.dump.files.length) return;
  const blob = new Blob([ccZipBytes(cc.dump.files)], { type: 'application/zip' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = cc.dump.session + '.zip';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

// ── Zip ────────────────────────────────────────────────────

// Stored, not deflated: PNGs are already compressed, so the only thing
// deflate would add here is time. Pure bytes in, bytes out — no Blob, no DOM —
// so the output can be checked against a real zip reader in the tests.

let CC_CRC_TABLE = null;

function ccCrc32(bytes){
  if(!CC_CRC_TABLE){
    CC_CRC_TABLE = new Int32Array(256);
    for(let n = 0; n < 256; n++){
      let c = n;
      for(let k = 0; k < 8; k++) c = (c & 1) ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1);
      CC_CRC_TABLE[n] = c;
    }
  }
  let c = -1;
  for(let i = 0; i < bytes.length; i++) c = CC_CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  return (c ^ -1) >>> 0;
}

function ccZipBytes(files){
  const enc = new TextEncoder();
  const entries = files.map(f => ({
    name:  enc.encode(f.name),
    bytes: f.bytes,
    crc:   ccCrc32(f.bytes),
  }));

  let size = 22;    // end-of-central-directory
  for(const e of entries) size += 30 + e.name.length + e.bytes.length + 46 + e.name.length;

  const out = new Uint8Array(size);
  const view = new DataView(out.buffer);
  let pos = 0;
  const u16 = v => { view.setUint16(pos, v, true); pos += 2; };
  const u32 = v => { view.setUint32(pos, v, true); pos += 4; };
  const raw = b => { out.set(b, pos); pos += b.length; };

  for(const e of entries){
    e.offset = pos;
    u32(0x04034b50); u16(20); u16(0); u16(0);     // signature, version, flags, stored
    u16(0); u16(0);                                // dos time and date, left at zero
    u32(e.crc); u32(e.bytes.length); u32(e.bytes.length);
    u16(e.name.length); u16(0);
    raw(e.name); raw(e.bytes);
  }

  const dirStart = pos;
  for(const e of entries){
    u32(0x02014b50); u16(20); u16(20); u16(0); u16(0);
    u16(0); u16(0);
    u32(e.crc); u32(e.bytes.length); u32(e.bytes.length);
    u16(e.name.length); u16(0); u16(0);
    u16(0); u16(0); u32(0);
    u32(e.offset);
    raw(e.name);
  }

  // Measured before the end record is written: the writers advance `pos`, so
  // reading it inside the call below would count this record's own bytes.
  const dirSize = pos - dirStart;
  u32(0x06054b50); u16(0); u16(0);
  u16(entries.length); u16(entries.length);
  u32(dirSize); u32(dirStart); u16(0);
  return out;
}

// ── Screens ────────────────────────────────────────────────

const CC_SCREENS = ['start', 'register', 'preview', 'homing', 'sweep', 'review'];
// Screens that want to see what the camera sees.
const CC_STAGE_SCREENS = ['register', 'preview', 'homing', 'sweep', 'review'];

function openCamCal(){
  document.getElementById('camCalOverlay').style.display = 'flex';
  ccShow('start');
  cc.dump = null;
  ccResetRun();
  ccRenderDump();
}

function closeCamCal(){
  ccStop();
  document.getElementById('camCalOverlay').style.display = 'none';
  if(typeof loadSettingsData === 'function' && globalSettings) loadSettingsData();
}

function ccShow(name){
  cc.screen = name;
  for(const s of CC_SCREENS){
    const el = document.getElementById('cc_' + s);
    if(el) el.style.display = (s === name) ? 'block' : 'none';
  }
  // The stage is shared rather than per-screen: losing sight of the display
  // mid-sweep is how you discover an hour later that the camera was nudged.
  const stage = document.getElementById('ccStage');
  stage.style.display = (CC_STAGE_SCREENS.indexOf(name) >= 0 && cc.stream) ? 'block' : 'none';
  document.getElementById('ccHandles').style.display = (name === 'register') ? 'block' : 'none';
}

function ccResetRun(){
  cc.results = {};
  cc.unread  = {};
  cc.live    = {};
  cc.history = [];
  cc.pass    = 0;
  cc.outcome = null;
  cc.abort   = false;
  cc.running = false;
}

function ccStop(){
  cc.abort   = true;
  cc.running = false;
  ccStopPreview();
  if(cc.stream){
    cc.stream.getTracks().forEach(t => t.stop());
    cc.stream = null;
  }
}

function ccStatus(text){
  const el = document.getElementById('ccStageStatus');
  if(el) el.textContent = text || '';
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
        facingMode:  { ideal: 'environment' },
        width:       { ideal: 3840 },
        height:      { ideal: 2160 },
        aspectRatio: { ideal: 16 / 9 },
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

  ccShow('register');
  ccCheckOrientation();
  ccDefaultCorners();
  ccStartPreview();
}

// The display is a 15-wide strip; in portrait it is either unreadably small
// or cropped. Everything downstream assumes the landscape framing, so this is
// checked rather than coped with.
function ccCheckOrientation(){
  const v = cc.video;
  if(!v || !v.videoWidth) return true;
  cc.landscape = v.videoWidth >= v.videoHeight;
  const warn = document.getElementById('ccRotate');
  warn.style.display = cc.landscape ? 'none' : 'block';
  for(const id of ['ccRegAuto', 'ccRegNext']){
    const el = document.getElementById(id);
    if(el) el.disabled = !cc.landscape;
  }
  return cc.landscape;
}

window.addEventListener('orientationchange', () => setTimeout(ccCheckOrientation, 400));
window.addEventListener('resize', () => { if(cc.stream) ccCheckOrientation(); });

// ── Registration ───────────────────────────────────────────

const CC_UNIT = [{x:0,y:0}, {x:1,y:0}, {x:1,y:1}, {x:0,y:1}];

// Start from a rectangle covering most of the frame. Dragging four handles
// that are already on screen, with the grid drawn live inside them, is a far
// steadier job than tapping four corners at nothing.
function ccDefaultCorners(){
  const w = cc.video.videoWidth, h = cc.video.videoHeight;
  const mx = w * 0.08, my = h * 0.30;
  cc.corners = [{x:mx, y:my}, {x:w-mx, y:my}, {x:w-mx, y:h-my}, {x:mx, y:h-my}];
  cc.H = ccSolveH(CC_UNIT, cc.corners);
  ccSetCellSize();
  ccRenderHandles();
}

function ccRegHint(text){
  document.getElementById('ccRegHint').innerHTML = text;
}

// Corner handles, positioned in CSS pixels over the video.
function ccRenderHandles(){
  const box = document.getElementById('ccHandles');
  const v = cc.video, r = v.getBoundingClientRect();
  const sx = r.width / (v.videoWidth || 1), sy = r.height / (v.videoHeight || 1);
  box.innerHTML = '';
  cc.corners.forEach((c, i) => {
    const h = document.createElement('div');
    h.className = 'cc-handle';
    h.style.left = (c.x * sx) + 'px';
    h.style.top  = (c.y * sy) + 'px';
    h.dataset.corner = String(i);
    h.addEventListener('pointerdown', ccHandleDown);
    box.appendChild(h);
  });
}

function ccHandleDown(ev){
  ev.preventDefault();
  cc.drag = parseInt(ev.currentTarget.dataset.corner);
  ev.currentTarget.setPointerCapture(ev.pointerId);
  ev.currentTarget.addEventListener('pointermove', ccHandleMove);
  ev.currentTarget.addEventListener('pointerup', ccHandleUp);
  ev.currentTarget.addEventListener('pointercancel', ccHandleUp);
}

function ccHandleMove(ev){
  if(cc.drag === null) return;
  ev.preventDefault();
  const v = cc.video, r = v.getBoundingClientRect();
  const x = (ev.clientX - r.left) * (v.videoWidth  / r.width);
  const y = (ev.clientY - r.top)  * (v.videoHeight / r.height);
  cc.corners[cc.drag] = {
    x: Math.max(0, Math.min(v.videoWidth,  x)),
    y: Math.max(0, Math.min(v.videoHeight, y)),
  };
  cc.H = ccSolveH(CC_UNIT, cc.corners);
  ccSetCellSize();
  ccRenderHandles();
}

function ccHandleUp(ev){
  cc.drag = null;
  ev.currentTarget.removeEventListener('pointermove', ccHandleMove);
  ev.currentTarget.removeEventListener('pointerup', ccHandleUp);
  ev.currentTarget.removeEventListener('pointercancel', ccHandleUp);
}

// The four corner modules, row-major.
function ccCornerModules(){
  return [0, cc.cols - 1, cc.count - 1, cc.count - cc.cols];
}

// Their centres in unit space. A module's window is a cell, so the lit patch
// is at the middle of one, not at the corner of the grid.
function ccCornerCentres(){
  const h = 0.5 / cc.cols, v = 0.5 / cc.rows;
  return [
    { x: h,     y: v     },
    { x: 1 - h, y: v     },
    { x: 1 - h, y: 1 - v },
    { x: h,     y: 1 - v },
  ];
}

function ccPage(chars){
  return fetch('/update_playlist', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pages: [chars], delay: 3600 }),
  });
}

// Find the grid by asking the display to point at its own corners: light the
// four corner modules and photograph the display with and without them lit.
// The difference is those four patches and nothing else, so it does not
// matter what colour the other flaps are or how evenly the room is lit —
// only that something changed exactly where we said it would.
async function ccAutoRegister(){
  if(!ccCheckOrientation()) return;
  const btn = document.getElementById('ccRegAuto');
  btn.disabled = true;

  if(cc.rows < 2 || cc.cols < 2){
    ccRegHint('Corner detection needs at least a 2x2 grid — drag the corners instead.');
    btn.disabled = false;
    return;
  }

  const map = getCharMap(0);
  let lit = '';
  for(const ch of CC_REGISTER_CHARS){
    if(map.indexOf(ch) > 0){ lit = ch; break; }
  }
  if(!lit){
    ccRegHint('No solid colour flap in the character map — drag the corners instead.');
    btn.disabled = false;
    return;
  }

  const blank   = map[0];
  const corners = ccCornerModules();
  const litPage = Array(cc.count).fill(blank);
  for(const m of corners) litPage[m] = lit;

  await fetch('/stop_app', { method: 'POST' });

  ccRegHint('Clearing the display…');
  await ccPage(Array(cc.count).fill(blank).join(''));
  await ccSleep(1200);              // let the display loop pick the page up
  await ccWaitForFrameQuiet();
  const before = ccGrayOf(ccFrame(CC_MOTION_MAX_W).img);

  ccRegHint('Lighting the four corner modules…');
  await ccPage(litPage.join(''));
  await ccSleep(1200);
  await ccWaitForFrameQuiet();

  // The corners may not have finished turning when the loop dispatched the
  // page, so look a few times before giving up.
  let found = null, lastAfter = null, frameW = CC_MOTION_MAX_W, frameH = 0;
  for(let attempt = 0; attempt < 4 && !found; attempt++){
    const frame = ccFrame(CC_MOTION_MAX_W);
    const after = ccGrayOf(frame.img);
    const diff  = new Uint8Array(after.length);
    for(let i = 0; i < after.length; i++) diff[i] = Math.abs(after[i] - before[i]);
    const blobs = ccFindBlobs(diff, frame.img.width, frame.img.height);
    lastAfter = after;
    frameW = frame.img.width;
    frameH = frame.img.height;
    if(blobs.length >= 4) found = { blobs: blobs, scale: frame.scale };
    else await ccSleep(600);
  }

  if(!found){
    ccRegHint('Could not pick out the four lit corners. Check they are visible and lit ' +
              'evenly, or drag the corners by hand.');
    btn.disabled = false;
    return;
  }

  // Four biggest changes, ordered top-left, top-right, bottom-right,
  // bottom-left — the order the corner modules are in.
  const best = found.blobs.slice().sort((a, b) => b.area - a.area).slice(0, 4);
  const byY  = best.slice().sort((a, b) => a.y - b.y);
  const top  = byY.slice(0, 2).sort((a, b) => a.x - b.x);
  const bot  = byY.slice(2, 4).sort((a, b) => a.x - b.x);
  const dst  = [top[0], top[1], bot[1], bot[0]]
    .map(b => ({ x: b.x / found.scale, y: b.y / found.scale }));

  const H = ccSolveH(ccCornerCentres(), dst);
  if(!H){
    ccRegHint('The four corners came out collinear — drag them by hand instead.');
    btn.disabled = false;
    return;
  }

  cc.H = H;
  cc.corners = CC_UNIT.map(u => ccApplyH(H, u.x, u.y));
  ccSetCellSize();
  ccRenderHandles();
  ccRegHint('Grid found from the four corner modules. Check the cells line up, and drag ' +
            'any corner to adjust.');
  btn.disabled = false;

  // The pair that produced the grid is the fixture corner detection is worth
  // testing against, so keep it even though the sweep has not started.
  if(ccDumpOn()){
    if(!cc.dump) ccDumpStart();
    cc.dump.manifest.registration = {
      frameWidth: frameW, frameHeight: frameH, scale: found.scale,
      blobs: found.blobs, chosen: dst, centres: ccCornerCentres(),
      modules: ccCornerModules(), homography: H,
    };
    ccDumpAdd('reg_before.png', ccPngOf(ccImageFromGray(before, frameW, frameH)));
    ccDumpAdd('reg_after.png',  ccPngOf(ccImageFromGray(lastAfter, frameW, frameH)));
  }
}

// ── Blob finding ───────────────────────────────────────────

// Otsu: split the histogram where it separates best, so the threshold comes
// from this room's light rather than a constant.
function ccOtsu(gray){
  const hist = new Array(256).fill(0);
  for(let i = 0; i < gray.length; i++) hist[gray[i]]++;
  const total = gray.length;
  let sum = 0;
  for(let t = 0; t < 256; t++) sum += t * hist[t];
  let sumB = 0, wB = 0, best = 0, cut = 127;
  for(let t = 0; t < 256; t++){
    wB += hist[t];
    if(!wB) continue;
    const wF = total - wB;
    if(!wF) break;
    sumB += t * hist[t];
    const mB = sumB / wB, mF = (sum - sumB) / wF;
    const between = wB * wF * (mB - mF) * (mB - mF);
    if(between > best){ best = between; cut = t; }
  }
  return cut;
}

// Connected bright regions of a grayscale image, with the specks and the
// whole-frame blobs dropped. Returns centroids with their areas.
function ccFindBlobs(gray, w, h){
  const n = w * h;
  const cut = ccOtsu(gray);
  const seen = new Uint8Array(n);
  const stack = new Int32Array(n);
  const blobs = [];

  for(let start = 0; start < n; start++){
    if(seen[start] || gray[start] <= cut) continue;
    let top = 0, area = 0, sx = 0, sy = 0;
    let minX = w, maxX = 0, minY = h, maxY = 0;
    stack[top++] = start;
    seen[start] = 1;
    while(top){
      const p = stack[--top];
      const x = p % w, y = (p / w) | 0;
      area++; sx += x; sy += y;
      if(x < minX) minX = x;
      if(x > maxX) maxX = x;
      if(y < minY) minY = y;
      if(y > maxY) maxY = y;
      if(x > 0     && !seen[p-1] && gray[p-1] > cut){ seen[p-1] = 1; stack[top++] = p-1; }
      if(x < w - 1 && !seen[p+1] && gray[p+1] > cut){ seen[p+1] = 1; stack[top++] = p+1; }
      if(y > 0     && !seen[p-w] && gray[p-w] > cut){ seen[p-w] = 1; stack[top++] = p-w; }
      if(y < h - 1 && !seen[p+w] && gray[p+w] > cut){ seen[p+w] = 1; stack[top++] = p+w; }
    }
    if(area < 40 || area > n * 0.2) continue;   // specks, and the wall behind it
    blobs.push({ x: sx / area, y: sy / area, area: area,
                 w: maxX - minX + 1, h: maxY - minY + 1 });
  }
  return blobs;
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
    if(Math.abs(M[piv][col]) < 1e-12) return null;
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

// A module window is far taller than it is wide — about 2.5:1 on a 3x15
// display. Warping it into a squarer buffer squashes the glyph to 60% of its
// height and throws away half the vertical detail, and OCR reads the result
// noticeably worse. Take the shape from the grid rather than assuming one.
function ccCellAspect(){
  if(!cc.H || cc.corners.length !== 4) return CC_CELL_H / CC_CELL_W;
  const quad = ccCellQuad(Math.floor(cc.count / 2));
  const d = (a, b) => Math.hypot(b.x - a.x, b.y - a.y);
  const w = (d(quad[0], quad[1]) + d(quad[3], quad[2])) / 2;
  const h = (d(quad[0], quad[3]) + d(quad[1], quad[2])) / 2;
  return (w > 0 && h > 0) ? h / w : CC_CELL_H / CC_CELL_W;
}

function ccSetCellSize(){
  cc.cellW = CC_CELL_W;
  cc.cellH = Math.max(CC_CELL_W,
                     Math.min(CC_CELL_H_MAX, Math.round(CC_CELL_W * ccCellAspect())));
}

// ── Live preview ───────────────────────────────────────────

// The grid stays drawn over the video for the whole run, carrying each
// module's latest reading. A camera that gets nudged is then obvious while
// it still matters, instead of showing up as a column of bad reads.
function ccStartPreview(){
  ccStopPreview();
  cc.previewId = setInterval(ccDrawOverlay, CC_PREVIEW_MS);
  ccDrawOverlay();
}

function ccStopPreview(){
  if(cc.previewId){ clearInterval(cc.previewId); cc.previewId = null; }
}

function ccDrawOverlay(){
  const v = cc.video, cv = document.getElementById('ccOverlay');
  if(!v || !cv || !cc.H) return;
  const r = v.getBoundingClientRect();
  if(!r.width) return;
  if(cv.width !== Math.round(r.width) || cv.height !== Math.round(r.height)){
    cv.width = Math.round(r.width);
    cv.height = Math.round(r.height);
  }
  const sx = r.width / (v.videoWidth || 1), sy = r.height / (v.videoHeight || 1);
  const ctx = cv.getContext('2d');
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.lineWidth = 1.5;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.font = Math.max(9, Math.round(cv.height / cc.rows / 3)) + 'px monospace';

  for(let i = 0; i < cc.count; i++){
    const quad = cc.corners.length === 4 ? ccCellQuad(i) : null;
    if(!quad) continue;
    const pts = quad.map(p => ({ x: p.x * sx, y: p.y * sy }));
    const live = cc.live[i];

    ctx.beginPath();
    pts.forEach((p, k) => k ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y));
    ctx.closePath();

    if(live && live.err === 0){
      ctx.strokeStyle = 'rgba(80,220,120,.95)';
      ctx.fillStyle   = 'rgba(40,160,90,.18)';
    } else if(live && live.err !== undefined){
      ctx.strokeStyle = 'rgba(255,90,90,.95)';
      ctx.fillStyle   = 'rgba(200,50,50,.22)';
    } else if(live){
      ctx.strokeStyle = 'rgba(255,190,60,.9)';
      ctx.fillStyle   = 'rgba(200,140,40,.16)';
    } else {
      ctx.strokeStyle = 'rgba(0,255,140,.7)';
      ctx.fillStyle   = 'rgba(0,0,0,0)';
    }
    ctx.fill();
    ctx.stroke();

    if(live && live.char){
      const cx = (pts[0].x + pts[2].x) / 2, cy = (pts[0].y + pts[2].y) / 2;
      ctx.fillStyle = '#fff';
      ctx.fillText(live.char, cx, cy);
    }
  }
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

function ccGrayOf(img){
  const n = img.width * img.height, g = new Uint8Array(n);
  for(let i = 0; i < n; i++){
    g[i] = (img.data[i*4] * 0.299 + img.data[i*4+1] * 0.587 + img.data[i*4+2] * 0.114) | 0;
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
// flap is passing. Learn how much the image moves when nothing is moving,
// then treat anything above that as motion.
async function ccLearnNoiseFloor(){
  let prev = null, worst = 0;
  for(let f = 0; f < CC_NOISE_FRAMES; f++){
    const cur = ccGrabCells(CC_MOTION_W, CC_MOTION_H, CC_MOTION_MAX_W).map(ccGrayOf);
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
    const cur = ccGrabCells(CC_MOTION_W, CC_MOTION_H, CC_MOTION_MAX_W).map(ccGrayOf);
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

// Settle without a grid, for the frame that is going to define the grid.
async function ccWaitForFrameQuiet(){
  const t0 = performance.now();
  let prev = null, stable = 0;
  while(performance.now() - t0 < CC_SETTLE_TIMEOUT){
    if(cc.abort) return false;
    const cur = ccGrayOf(ccFrame(CC_MOTION_MAX_W).img);
    if(prev){
      stable = ccMad(prev, cur) > CC_NOISE_MIN ? 0 : stable + 1;
      if(stable >= CC_STABLE_FRAMES) return true;
    }
    prev = cur;
    await ccSleep(CC_FRAME_MS);
  }
  return false;
}

// ── Matching against the display's own flaps ───────────────
//
// Reading the character is the wrong problem. This is closed-set
// classification over the flaps a module has, with a known expected answer —
// and the display can show us every one of them, so the reference images come
// from the display itself rather than from a model of what letters look like.
//
// Every module is commanded to the same flap at once, and most of them land
// on it, so the per-pixel median across modules at one position is that
// flap's reference image. Matching against those beats OCR on the same
// capture by a wide margin: 98% of cells classified against 71%, agreeing
// with OCR on 99.2% of the ones OCR could read, and resolving 95% of the ones
// it could not. P, B, R, I, O and 0 go from unreadable to confident, and the
// flap seam stops mattering because it is in the template too.

const CC_TPL_W    = 24;     // matching resolution; the glyphs are huge
const CC_TPL_H    = 60;
const CC_TPL_CONF = 1200;   // margin -> the 0-100 confidence the UI filters on
const CC_TPL_CHROMA = 2;    // how hard the colour planes pull against the shape
const CC_TPL_ALIKE = 0.93;  // two references this similar cannot be told apart
// One length for features and references alike. They are compared element by
// element, so the two drifting apart reads off the end of the shorter one and
// every correlation comes back NaN.
const CC_TPL_PLANES = 3;    // brightness, and two colour differences
const CC_TPL_LEN = CC_TPL_W * CC_TPL_H * CC_TPL_PLANES;

// Downsample to a small patch, then zero-mean and unit-norm it so a dot
// product between two of them is normalised cross-correlation — which is what
// makes the comparison indifferent to exposure and overall brightness.
// Shape from brightness, and the flap's colour alongside it.
//
// Brightness alone cannot tell the colour tiles apart: they are solid
// rectangles that differ only in hue, and subtracting the mean — which is
// what makes the comparison indifferent to exposure — throws away the only
// thing that distinguished them. Measured on a capture that swept all 63
// flaps, orange and yellow correlated at 0.994 and every pair of tiles above
// 0.94, so which one a module was showing was a coin toss.
//
// So the brightness plane is mean-subtracted as before, and two colour
// difference planes are carried beside it un-subtracted, because for a solid
// tile the mean is the signal. On the same capture that took identification
// from 81.2% to 89.4% and the worst pair of tiles from 0.994 to 0.814. For a
// letter, which is near-grey, the colour planes are close to zero and the
// result is what it always was.
function ccFeatures(cell){
  const w = cell.width, h = cell.height, px = cell.data;
  const n = CC_TPL_W * CC_TPL_H;
  const f = new Float64Array(CC_TPL_LEN);

  for(let y = 0; y < CC_TPL_H; y++){
    const y0 = Math.floor(y * h / CC_TPL_H);
    const y1 = Math.max(y0 + 1, Math.floor((y + 1) * h / CC_TPL_H));
    for(let x = 0; x < CC_TPL_W; x++){
      const x0 = Math.floor(x * w / CC_TPL_W);
      const x1 = Math.max(x0 + 1, Math.floor((x + 1) * w / CC_TPL_W));
      let r = 0, g = 0, b = 0, count = 0;
      for(let yy = y0; yy < y1; yy++){
        for(let xx = x0; xx < x1; xx++){
          const o = (yy * w + xx) * 4;
          r += px[o]; g += px[o + 1]; b += px[o + 2];
          count++;
        }
      }
      r /= count; g /= count; b /= count;
      const i = y * CC_TPL_W + x;
      f[i]         = r * 0.299 + g * 0.587 + b * 0.114;
      f[n + i]     = CC_TPL_CHROMA * (r - g);
      f[n * 2 + i] = CC_TPL_CHROMA * (g - b);
    }
  }

  // Only the brightness plane loses its mean. Removing it from the colour
  // planes too would undo the whole point of carrying them.
  let mean = 0;
  for(let i = 0; i < n; i++) mean += f[i];
  mean /= n;
  for(let i = 0; i < n; i++) f[i] -= mean;

  return ccUnitNorm(f);
}

// Scale to unit length, without touching the mean — ccFeatures has already
// taken the mean out of the plane it belongs in, and taking it out again
// across all three would undo the colour planes it deliberately kept.
function ccUnitNorm(f){
  let ss = 0;
  for(let i = 0; i < f.length; i++) ss += f[i] * f[i];
  const inv = ss > 0 ? 1 / Math.sqrt(ss) : 0;
  for(let i = 0; i < f.length; i++) f[i] *= inv;
  return f;
}

function ccCorrelate(a, b){
  let s = 0;
  for(let i = 0; i < a.length; i++) s += a[i] * b[i];
  return s;
}

// The median is what makes this work without knowing the answer first: a
// minority of modules are on the wrong flap, and a minority cannot move it.
function ccBuildTemplates(samples){
  const templates = {};
  for(const idx of Object.keys(samples)){
    const stack = Object.keys(samples[idx]).map(m => samples[idx][m]);
    if(stack.length < 3) continue;          // too few to outvote a bad one
    const v = new Float64Array(CC_TPL_LEN);
    const column = new Float64Array(stack.length);
    for(let px = 0; px < v.length; px++){
      for(let k = 0; k < stack.length; k++) column[k] = stack[k][px];
      const sorted = Array.prototype.slice.call(column).sort((a, b) => a - b);
      v[px] = sorted[sorted.length >> 1];
    }
    templates[idx] = ccUnitNorm(v);
  }
  return templates;
}

// Which flap this image is, and how much better that answer is than the
// runner-up. A thin margin means two flaps look alike here, or the card was
// caught mid-turn — either way it is not something to write to EEPROM.
function ccMatch(feature, templates){
  let best = -2, bestIdx = -1, second = -2, secondIdx = -1;
  for(const key of Object.keys(templates)){
    const s = ccCorrelate(feature, templates[key]);
    if(s > best){ second = best; secondIdx = bestIdx; best = s; bestIdx = parseInt(key); }
    else if(s > second){ second = s; secondIdx = parseInt(key); }
  }
  if(bestIdx < 0) return null;

  // Two flaps whose reference images are nearly identical cannot be told
  // apart by any margin between them — the margin is measuring noise. Say so
  // rather than pick, because the answer becomes an EEPROM write.
  if(secondIdx >= 0 &&
     ccCorrelate(templates[bestIdx], templates[secondIdx]) > CC_TPL_ALIKE){
    return { index: bestIdx, margin: 0, conf: 0, alike: secondIdx };
  }
  const margin = second > -2 ? best - second : 1;
  return {
    index: bestIdx,
    margin: margin,
    conf: Math.max(0, Math.min(100, Math.round(margin * CC_TPL_CONF))),
  };
}

// Present a match the way ccScoreReads already expects a reading, so the
// correction arithmetic downstream is unchanged.
function ccReadsFrom(samples, templates, idx){
  const reads = new Array(cc.count).fill(null);
  const at = samples[idx] || {};
  for(let m = 0; m < cc.count; m++){
    const f = at[m];
    if(!f) continue;
    const hit = ccMatch(f, templates);
    if(!hit) continue;
    reads[m] = { index: hit.index, char: getCharMap(m)[hit.index] || '',
                 conf: hit.conf, margin: hit.margin };
  }
  return reads;
}

// ── Passes ─────────────────────────────────────────────────

function ccBuildSweep(){
  const set = document.getElementById('ccSweepSet').value;
  const map = getCharMap(0);
  if(set === 'all'){
    const idx = [];
    for(let i = 1; i < map.length; i++) idx.push(i);
    return idx;
  }
  const chars = set === 'digit' ? CC_SWEEP_DIGIT
              : set === 'both'  ? CC_SWEEP_ALPHA + CC_SWEEP_DIGIT
              : CC_SWEEP_ALPHA;
  const idx = [];
  for(const ch of chars){
    const i = map.indexOf(ch);
    if(i > 0) idx.push(i);
  }
  return idx.sort((a, b) => a - b);
}

async function ccBegin(){
  if(!ccCheckOrientation()) return;
  cc.sweep = ccBuildSweep();
  if(!cc.sweep.length){ showToast('No readable flap positions in the char map', 'error'); return; }

  ccResetRun();
  ccDumpStart();
  ccShow('homing');
  const status = document.getElementById('ccHomingStatus');

  status.textContent = 'Learning the camera noise floor — hold still…';
  await ccLearnNoiseFloor();

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
  await ccRunUntilClean();
}

// What to do after a pass, given how many modules were still wrong after each
// pass so far. Kept separate from the loop that runs it so the decision can
// be checked without a camera.
function ccNextAction(history, maxPasses){
  const last = history[history.length - 1];
  if(last === 0) return 'done';
  if(history.length >= maxPasses) return 'exhausted';
  // Writes that do not reduce the error are not going to start working on
  // the next round, and each round is another EEPROM write per module.
  if(history.length >= 2 && last >= history[history.length - 2]) return 'stuck';
  return 'apply';
}

// Measure, write, measure again — repeating while it is still improving.
// Auto mode does the whole thing; otherwise it stops after the first pass and
// waits to be told.
async function ccRunUntilClean(){
  const auto = document.getElementById('ccAuto').checked;
  const maxPasses = auto
    ? Math.max(1, Math.min(CC_MAX_PASSES, parseInt(document.getElementById('ccMaxPasses').value) || 1))
    : 1;

  while(true){
    cc.pass++;
    ccShow('sweep');
    await ccSweepPass();
    if(cc.abort) break;

    const wrong = ccTotalWrong();
    cc.history.push(wrong);
    ccRenderHistory();

    const action = auto ? ccNextAction(cc.history, maxPasses)
                        : (wrong === 0 ? 'done' : 'exhausted');
    if(action !== 'apply'){
      cc.outcome = action;
      break;
    }

    ccShow('review');
    ccRenderReview();
    ccStatus('Pass ' + cc.pass + ': writing ' + wrong + ' correction(s)…');
    const written = await ccWriteCorrections();
    if(!written.confirmed){
      // Reading the display again now would measure a half-written state and
      // write corrections on top of corrections.
      cc.outcome = written.reason === 'unreadable' ? 'unverified' : 'write-pending';
      if(written.failed) cc.outcome = 'write-failed';
      break;
    }
    ccStatus('');
  }

  cc.running = false;
  ccDumpFinish();
  ccRenderReview();
  ccShow('review');
}

// One read of every position. Nothing is written here.
async function ccSweepPass(){
  cc.running = true;
  cc.results = {};
  cc.unread  = {};
  cc.live    = {};

  const bar   = document.getElementById('ccSweepBar');
  const label = document.getElementById('ccSweepLabel');
  const note  = document.getElementById('ccSweepNote');

  const samples = {}, positions = {}, seen = [];

  for(let n = 0; n < cc.sweep.length; n++){
    if(cc.abort) break;
    const idx   = cc.sweep[n];
    const shown = getCharMap(0)[idx];

    bar.style.width = Math.round((n / cc.sweep.length) * 100) + '%';
    label.textContent = 'Pass ' + cc.pass + ' · position ' + (n + 1) + ' of ' + cc.sweep.length +
                        ' — "' + shown + '" (index ' + idx + ')';

    note.textContent = 'Moving…';
    cc.live = {};
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
    const cells = ccGrabCells(cc.cellW, cc.cellH);
    samples[idx] = {};
    for(let m = 0; m < cc.count; m++) samples[idx][m] = ccFeatures(cells[m]);

    const res  = await fetch('/tuning_status?char_index=' + idx);
    positions[idx] = (await res.json()).positions || {};
    seen.push(idx);

    if(cc.dump){
      note.textContent = 'Saving frames…';
      ccDumpFrames(idx, cells);
    }
  }

  // Classification waits for the whole pass, because the reference images
  // are built out of it. Nothing to compare against until every module has
  // been seen on every flap.
  if(seen.length){
    note.textContent = 'Building reference images from the display…';
    const templates = ccBuildTemplates(samples);
    cc.templates = templates;
    for(const idx of seen){
      ccScoreReads(idx, ccReadsFrom(samples, templates, idx), positions[idx]);
    }
    ccDumpReadings();
    ccRenderSweepGrid();
  }

  bar.style.width = '100%';
  cc.running = false;
}

// One position's crops, each named for the pass, position and module that
// produced it, alongside what was expected of it and what was made of it.
// The frames go down as they are taken; what was made of them cannot, because
// nothing has been made of them yet. Matching needs the whole pass before it
// has anything to compare against, so the readings are filled in afterwards
// by ccDumpReadings. Recording them here wrote a manifest of nulls.
function ccDumpFrames(idx, cells){
  if(!cc.dump) return;
  const kept = [];
  for(let m = 0; m < cc.count; m++){
    const name = 'p' + cc.pass + '_i' + String(idx).padStart(2, '0') +
                 '_m' + String(m).padStart(2, '0') + '.png';
    if(!ccDumpAdd(name, ccPngOf(cells[m]))) break;   // out of room; stop here
    kept.push({ id: m, file: name });
  }
  if(kept.length){
    cc.dump.manifest.positions.push({
      pass: cc.pass, index: idx, char: getCharMap(0)[idx], modules: kept,
    });
  }
}

// Fill in what the pass concluded, against the frames it actually kept.
function ccDumpReadings(){
  if(!cc.dump) return;
  for(const pos of cc.dump.manifest.positions){
    if(pos.pass !== cc.pass) continue;
    for(const mod of pos.modules){
      const scored = (cc.results[mod.id] || {})[pos.index];
      const live   = cc.live[mod.id] || {};
      mod.expected = getCharMap(mod.id)[pos.index];
      mod.read     = scored ? scored.read : (live.char || null);
      mod.matched  = scored ? scored.matched : null;
      mod.conf     = scored ? scored.conf : null;
      mod.err      = scored ? scored.err  : null;
      mod.from     = scored ? scored.from : null;
      mod.to       = scored ? scored.to   : null;
      // Recorded per reading rather than looked up later: these are what the
      // correction was computed from, and settings.json can change after.
      mod.cal      = parseInt((cc.settings && cc.settings.calibrations &&
                               cc.settings.calibrations[String(mod.id)]) || 4096);
      mod.flaps    = getFlapCount(mod.id);
    }
  }
}

// Turn one frame's reads into staged corrections.
function ccScoreReads(idx, reads, positions){
  const minConf  = parseInt(document.getElementById('ccMinConf').value)  || 0;
  const maxFlaps = parseInt(document.getElementById('ccMaxFlaps').value) || 2;

  for(let m = 0; m < cc.count; m++){
    const r       = reads[m];
    const map     = getCharMap(m);
    const flaps   = getFlapCount(m);
    // The matcher names a flap outright; indexOf is the fallback for a
    // reading that only carries a character, and is wrong the moment a
    // character map lists the same character twice.
    const readIdx = !r ? -1
      : (r.index !== undefined ? r.index : (r.char ? map.indexOf(r.char) : -1));

    if(readIdx < 0 || !r || r.conf < minConf){
      cc.unread[m] = (cc.unread[m] || 0) + 1;
      cc.live[m] = { char: r && r.char ? r.char : '?' };
      continue;
    }

    // Signed distance in flaps, shortest way round the drum.
    let err = ((readIdx - idx) % flaps + flaps) % flaps;
    if(err > flaps / 2) err -= flaps;

    // A module is off by one flap, sometimes two. An apparent error of
    // seventeen is a misread, not a mechanism — drop it rather than write it.
    if(Math.abs(err) > maxFlaps){
      cc.unread[m] = (cc.unread[m] || 0) + 1;
      cc.live[m] = { char: r.char };
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
    cc.results[m][idx] = { read: r.char, matched: readIdx, conf: Math.round(r.conf),
                           err: err, from: from, to: to };
    cc.live[m] = { char: r.char, err: err };
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

function ccTotalWrong(){
  let total = 0;
  for(let m = 0; m < cc.count; m++) total += ccModStats(m).wrong;
  return total;
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

function ccRenderHistory(){
  const el = document.getElementById('ccHistory');
  if(!cc.history.length){ el.style.display = 'none'; return; }
  el.style.display = 'block';
  el.innerHTML = '<strong>Passes:</strong> ' + cc.history.map((n, i) =>
    'pass ' + (i + 1) + ' — ' + (n === 0 ? 'clean' : n + ' wrong')).join(' → ');
}

function ccAbortSweep(){
  cc.abort = true;
  showToast('Stopping after this position — nothing has been written', 'warn');
}

// ── Review ─────────────────────────────────────────────────

const CC_OUTCOME = {
  'done':         ['var(--green)',  'Every module read correctly at every position checked.'],
  'exhausted':    ['var(--orange)', 'Stopped at the pass limit with corrections still outstanding.'],
  'stuck':        ['var(--orange)', 'The last pass was no better than the one before it, so it stopped rather than write again. What is left is likely mechanical, or a module the camera cannot read.'],
  'write-failed': ['var(--red)',    'A write failed; nothing further was attempted.'],
  'write-pending': ['var(--orange)', 'The corrections were sent but some modules did not read them back. Reading the display again now would measure a half-written state, so it stopped instead.'],
  'unverified':   ['var(--orange)', 'The corrections were sent but could not be read back, so there is no evidence they landed. Nothing further was attempted.'],
};

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
    ' still reading wrong across ' + cc.count + ' modules' +
    (blind ? ' · <span style="color:var(--orange)">' + blind + ' module(s) never read</span>' : '');

  const outcome = document.getElementById('ccOutcome');
  const known = CC_OUTCOME[cc.outcome];
  if(known){
    outcome.style.display = 'block';
    outcome.innerHTML = '<strong style="color:' + known[0] + '">' +
      (cc.outcome === 'done' ? 'Calibrated.' : 'Not finished.') + '</strong> ' + known[1];
  } else {
    outcome.style.display = 'none';
  }

  ccRenderHistory();
  ccRenderSystemic();
  document.getElementById('ccApplyBtn').disabled = (totalFixes === 0);
  document.getElementById('ccRecheckBtn').disabled = false;
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

// Write once, at the end of a pass. restore_module_settings erases a module's
// corrections and nothing else. /restore_settings makes a module match our
// settings exactly, which means erasing its tuning and writing all of it
// back — sensible for a backup, and far too much bus for a few positions.
async function ccWriteCorrections(){
  const changed = {};
  let count = 0;
  for(let m = 0; m < cc.count; m++){
    const s = ccModStats(m);
    if(!s.wrong) continue;
    const pairs = {};
    for(const idx of Object.keys(s.byIdx)){
      if(s.byIdx[idx].err !== 0){ pairs[idx] = s.byIdx[idx].to; count++; }
    }
    changed[String(m)] = pairs;
  }
  if(!count) return { confirmed: true };

  try {
    const res  = await fetch('/apply_tuning', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tuned: changed }),
    });
    const data = await res.json();
    if(data.status !== 'success') throw new Error(data.error || data.message || 'Write rejected');
  } catch(err){
    showToast('Write failed: ' + err.message, 'error');
    return { confirmed: false, failed: true, reason: 'write' };
  }

  // What we just wrote is now what is stored, so the next pass measures
  // against it rather than against the tuning we started with.
  if(!cc.settings.tuned_chars) cc.settings.tuned_chars = {};
  for(const key of Object.keys(changed)){
    cc.settings.tuned_chars[key] =
      Object.assign({}, cc.settings.tuned_chars[key] || {}, changed[key]);
  }
  return await ccConfirmWrites(Object.keys(changed).map(Number));
}

// Storage on this hardware drops writes quietly. Read the modules back and
// say whether they actually kept what we just sent.
// Read the modules back and report which ones do not hold what we just
// sent. Returns the offending ids, or null if the audit could not be run.
async function ccAudit(ids){
  const audit = document.getElementById('ccAudit');
  audit.style.display = 'block';
  audit.textContent = 'Reading the modules back…';
  try {
    const res  = await fetch('/module_audit', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: ids }),
    });
    const data = await res.json();
    if(data.error) throw new Error(data.error);
    const bad = (data.modules || []).filter(x => x.status !== 'ok');
    audit.innerHTML = bad.length
      ? '<strong style="color:var(--orange)">' + bad.length + ' module(s) did not read back clean:</strong> ' +
        bad.map(x => x.id + ' (' + x.status + ')').join(', ') +
        '<br><span style="color:#999">A dropped EEPROM write during motor draw looks exactly like ' +
        'this.</span>'
      : '<strong style="color:var(--green)">All written modules read back clean.</strong>';
    return bad.map(x => x.id);
  } catch(err){
    audit.innerHTML = '<span style="color:var(--orange)">Could not verify: ' + err.message + '</span>';
    return null;
  }
}

// Do not start reading the display again until the modules actually hold the
// corrections. A write returns when the last command has been put on the bus,
// which is not the same as every module having committed it to EEPROM — and a
// pass that starts in between reads a display that is half corrected, counts
// the difference as fresh error, and writes again on top of it.
async function ccConfirmWrites(ids){
  const audit = document.getElementById('ccAudit');
  for(let attempt = 1; attempt <= CC_CONFIRM_TRIES; attempt++){
    await ccSleep(CC_CONFIRM_WAIT_MS);
    const bad = await ccAudit(ids);
    if(bad === null) return { confirmed: false, reason: 'unreadable' };
    if(!bad.length) return { confirmed: true };
    if(attempt < CC_CONFIRM_TRIES){
      audit.innerHTML += '<br><span style="color:#999">Waiting for ' + bad.length +
        ' module(s) to settle, attempt ' + (attempt + 1) + ' of ' + CC_CONFIRM_TRIES + '…</span>';
    } else {
      return { confirmed: false, reason: 'pending', pending: bad };
    }
  }
  return { confirmed: false, reason: 'pending' };
}

// Manual "Apply", for a single-pass run.
async function ccApply(){
  const btn = document.getElementById('ccApplyBtn');
  btn.disabled = true;
  btn.textContent = 'Writing…';
  const written = await ccWriteCorrections();
  btn.textContent = written.confirmed ? 'Applied' : 'Apply Corrections';
  if(!written.confirmed){
    btn.disabled = false;
    showToast(written.reason === 'pending'
      ? 'Some modules have not taken the write yet'
      : 'Write could not be confirmed', 'warn');
  } else {
    showToast('Corrections written and read back — re-check to confirm they took');
  }
}

// Read everything again without writing. The only thing that proves a
// correction worked.
async function ccRecheck(){
  const btn = document.getElementById('ccRecheckBtn');
  btn.disabled = true;
  cc.abort = false;
  cc.pass++;
  ccShow('sweep');
  await ccSweepPass();
  cc.history.push(ccTotalWrong());
  cc.outcome = ccTotalWrong() === 0 ? 'done' : 'exhausted';
  ccRenderReview();
  ccShow('review');
  btn.disabled = false;
}

function ccRestart(){
  cc.dump = null;
  ccResetRun();
  ccShow('preview');
}

// ── Sizes ──────────────────────────────────────────────────

function ccBytes(n){
  if(n < 1024) return n + ' B';
  if(n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}
