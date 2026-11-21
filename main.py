#!/usr/bin/env python3
"""Entrypoint for the Salmaris passport entry (make.fun program/v1).

Writes the entry image to /output/entry.png. The image is the rendered
passport data page shipped beside this file as passport.png (a render of
index.html). The shipped PNG already fits the gate caps (<=1024x1024 px,
<=1,048,576 bytes), so the normal path is a verbatim copy. If the source
ever exceeds the caps, it is downscaled with a pure-stdlib PNG codec --
the grading sandbox has no network and no third-party packages.
"""

import os
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "passport.png")
OUTPUT = os.path.join("/output", "entry.png")
MAX_W = 1024
MAX_H = 1024
MAX_BYTES = 1_048_576


def png_size(data):
    """(width, height) from a PNG byte string's IHDR."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("source is not a PNG")
    return struct.unpack(">II", data[16:24])


# ---- minimal pure-stdlib PNG codec (8-bit, non-interlaced) ----

def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def decode_png(data):
    """Return (width, height, rows) with rows as RGB bytearrays."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos = 8
    idat = bytearray()
    plte = None
    trns = None
    width = height = bit_depth = color_type = interlace = None
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, bit_depth, color_type, _comp, _filt, interlace = \
                struct.unpack(">IIBBBBB", chunk)
        elif ctype == b"PLTE":
            plte = chunk
        elif ctype == b"tRNS":
            trns = chunk
        elif ctype == b"IDAT":
            idat.extend(chunk)
        elif ctype == b"IEND":
            break
    if bit_depth != 8 or interlace != 0:
        raise ValueError("unsupported PNG encoding for fallback resize")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    rows = []
    prev = bytearray(stride)
    off = 0
    for _y in range(height):
        ftype = raw[off]
        line = bytearray(raw[off + 1:off + 1 + stride])
        off += 1 + stride
        if ftype == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(a, b, c)) & 0xFF
        rows.append(line)
        prev = line
    # normalise to RGB (alpha composited on white, palette expanded)
    rgb_rows = []
    for line in rows:
        out = bytearray(width * 3)
        for x in range(width):
            s = x * channels
            d = x * 3
            if color_type == 2:
                out[d:d + 3] = line[s:s + 3]
            elif color_type == 6:
                a = line[s + 3]
                for k in range(3):
                    out[d + k] = (line[s + k] * a + 255 * (255 - a)) // 255
            elif color_type == 0:
                out[d] = out[d + 1] = out[d + 2] = line[s]
            elif color_type == 4:
                g, a = line[s], line[s + 1]
                v = (g * a + 255 * (255 - a)) // 255
                out[d] = out[d + 1] = out[d + 2] = v
            elif color_type == 3:
                idx = line[s]
                out[d:d + 3] = plte[idx * 3:idx * 3 + 3]
                if trns and idx < len(trns):
                    a = trns[idx]
                    for k in range(3):
                        out[d + k] = (out[d + k] * a + 255 * (255 - a)) // 255
        rgb_rows.append(out)
    return width, height, rgb_rows


def encode_png(width, height, rgb_rows):
    def chunk(ctype, payload):
        return (struct.pack(">I", len(payload)) + ctype + payload +
                struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = bytearray()
    for row in rgb_rows:
        raw.append(0)
        raw.extend(row)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(bytes(raw), 9)) +
            chunk(b"IEND", b""))


def downscale(width, height, rows, new_w, new_h):
    """Box-average downscale of RGB rows."""
    out_rows = []
    for oy in range(new_h):
        y0 = oy * height // new_h
        y1 = max(y0 + 1, (oy + 1) * height // new_h)
        out = bytearray(new_w * 3)
        for ox in range(new_w):
            x0 = ox * width // new_w
            x1 = max(x0 + 1, (ox + 1) * width // new_w)
            n = (x1 - x0) * (y1 - y0)
            for k in range(3):
                total = 0
                for yy in range(y0, y1):
                    row = rows[yy]
                    for xx in range(x0, x1):
                        total += row[xx * 3 + k]
                out[ox * 3 + k] = total // n
        out_rows.append(out)
    return out_rows


def fit_within(data):
    """Return PNG bytes that satisfy the gate caps."""
    width, height = png_size(data)
    if width <= MAX_W and height <= MAX_H and len(data) <= MAX_BYTES:
        return data
    width, height, rows = decode_png(data)
    scale = min(MAX_W / width, MAX_H / height, 1.0)
    while True:
        new_w = max(1, int(width * scale))
        new_h = max(1, int(height * scale))
        result = encode_png(new_w, new_h, downscale(width, height, rows, new_w, new_h))
        if len(result) <= MAX_BYTES or scale <= 0.1:
            return result
        scale *= 0.85


def main():
    with open(SOURCE, "rb") as fh:
        data = fh.read()
    result = fit_within(data)
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "wb") as fh:
        fh.write(result)
    w, h = png_size(result)
    print(f"wrote {OUTPUT}: {w}x{h}, {len(result)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
