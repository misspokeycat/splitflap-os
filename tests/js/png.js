// Minimal PNG reader, so captured frames can be used as test fixtures.
//
// Node has no image decoding and this project has no dependencies, but it
// does have zlib — and the only PNGs this needs to read are the ones a canvas
// produced: 8-bit, non-interlaced. That is a small enough corner of the format
// to implement here rather than take on a package for.
//
// An encoder is included because the decoder has to be tested too, and a
// camera is a poor unit test. It writes each filter type on demand so the
// decoder's filter handling is exercised deliberately rather than by luck.

const zlib = require('zlib');

const SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const CHANNELS = { 0: 1, 2: 3, 4: 2, 6: 4 };   // grey, rgb, grey+alpha, rgba

function paeth(a, b, c) {
  const p = a + b - c;
  const pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
  if (pa <= pb && pa <= pc) return a;
  return pb <= pc ? b : c;
}

function decodePng(buf) {
  if (!buf.subarray(0, 8).equals(SIGNATURE)) throw new Error('not a PNG');

  let pos = 8, width = 0, height = 0, depth = 0, colour = 0, interlace = 0;
  const idat = [];
  while (pos + 8 <= buf.length) {
    const len = buf.readUInt32BE(pos);
    const type = buf.toString('ascii', pos + 4, pos + 8);
    const data = buf.subarray(pos + 8, pos + 8 + len);
    if (type === 'IHDR') {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      depth = data[8]; colour = data[9]; interlace = data[12];
    } else if (type === 'IDAT') {
      idat.push(data);
    } else if (type === 'IEND') {
      break;
    }
    pos += 12 + len;
  }

  if (depth !== 8) throw new Error(`only 8-bit PNGs are supported (got ${depth})`);
  if (interlace) throw new Error('interlaced PNGs are not supported');
  const channels = CHANNELS[colour];
  if (!channels) throw new Error(`unsupported colour type ${colour}`);

  const raw = zlib.inflateSync(Buffer.concat(idat));
  const stride = width * channels;
  const out = Buffer.alloc(height * stride);

  let rp = 0;
  for (let y = 0; y < height; y++) {
    const filter = raw[rp++];
    const line = raw.subarray(rp, rp + stride);
    rp += stride;
    const cur = out.subarray(y * stride, (y + 1) * stride);
    const prev = y ? out.subarray((y - 1) * stride, y * stride) : null;
    for (let i = 0; i < stride; i++) {
      const a = i >= channels ? cur[i - channels] : 0;
      const b = prev ? prev[i] : 0;
      const c = (prev && i >= channels) ? prev[i - channels] : 0;
      let v = line[i];
      if (filter === 1) v += a;
      else if (filter === 2) v += b;
      else if (filter === 3) v += (a + b) >> 1;
      else if (filter === 4) v += paeth(a, b, c);
      else if (filter !== 0) throw new Error(`unknown filter ${filter}`);
      cur[i] = v & 0xff;
    }
  }
  return { width, height, channels, data: out };
}

// The same luma weights the tuner uses, so a fixture decoded here matches
// what the browser fed to OCR.
function toGray(img) {
  const n = img.width * img.height;
  const g = new Uint8Array(n);
  for (let i = 0; i < n; i++) {
    const o = i * img.channels;
    g[i] = img.channels >= 3
      ? (img.data[o] * 0.299 + img.data[o + 1] * 0.587 + img.data[o + 2] * 0.114) | 0
      : img.data[o];
  }
  return g;
}

// ── Encoder, for testing the decoder ───────────────────────

let CRC_TABLE = null;
function crc32(buf) {
  if (!CRC_TABLE) {
    CRC_TABLE = new Int32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = (c & 1) ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1);
      CRC_TABLE[n] = c;
    }
  }
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([len, body, crc]);
}

function encodePng(width, height, channels, pixels, filterType) {
  const colour = { 1: 0, 2: 4, 3: 2, 4: 6 }[channels];
  if (colour === undefined) throw new Error(`cannot encode ${channels} channels`);
  const stride = width * channels;
  const raw = Buffer.alloc(height * (stride + 1));

  for (let y = 0; y < height; y++) {
    raw[y * (stride + 1)] = filterType;
    for (let i = 0; i < stride; i++) {
      const v = pixels[y * stride + i];
      const a = i >= channels ? pixels[y * stride + i - channels] : 0;
      const b = y ? pixels[(y - 1) * stride + i] : 0;
      const c = (y && i >= channels) ? pixels[(y - 1) * stride + i - channels] : 0;
      let enc = v;
      if (filterType === 1) enc = v - a;
      else if (filterType === 2) enc = v - b;
      else if (filterType === 3) enc = v - ((a + b) >> 1);
      else if (filterType === 4) enc = v - paeth(a, b, c);
      raw[y * (stride + 1) + 1 + i] = enc & 0xff;
    }
  }

  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; ihdr[9] = colour; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;

  return Buffer.concat([
    SIGNATURE,
    chunk('IHDR', ihdr),
    chunk('IDAT', zlib.deflateSync(raw)),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

module.exports = { decodePng, encodePng, toGray };
