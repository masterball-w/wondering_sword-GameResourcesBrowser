# -*- coding: utf-8 -*-
"""
服务端贴图解码 + PNG 编码 (纯标准库)
支持: PF_B8G8R8A8 / PF_R8G8B8A8 / PF_DXT1 / PF_DXT3 / PF_DXT5 / PF_BC5
"""
import struct
import zlib


def encode_png(w: int, h: int, rgba: bytearray) -> bytes:
    """RGBA8888 -> PNG"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    raw = bytearray()
    stride = w * 4
    for y in range(h):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def _c565(c):
    r, g, b = c >> 11 & 31, c >> 5 & 63, c & 31
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))


def decode_texture(data: bytes, w: int, h: int, fmt: str) -> bytearray:
    """解码为 RGBA 字节串"""
    px = bytearray(w * h * 4)
    if fmt in ("PF_B8G8R8A8", "PF_R8G8B8A8"):
        sw = fmt == "PF_B8G8R8A8"
        for i in range(0, w * h * 4, 4):
            if sw:
                px[i] = data[i + 2]; px[i + 1] = data[i + 1]; px[i + 2] = data[i]; px[i + 3] = data[i + 3]
            else:
                px[i] = data[i]; px[i + 1] = data[i + 1]; px[i + 2] = data[i + 2]; px[i + 3] = data[i + 3]
        return px
    if fmt == "PF_G8":
        for i in range(w * h):
            px[i * 4] = px[i * 4 + 1] = px[i * 4 + 2] = data[i]
            px[i * 4 + 3] = 255
        return px

    if fmt == "PF_DXT1":
        return _dxt(data, w, h, px, 0, 0)
    if fmt == "PF_DXT3":
        return _dxt(data, w, h, px, 1, 0)
    if fmt == "PF_DXT5":
        return _dxt(data, w, h, px, 0, 1)
    if fmt == "PF_BC5":
        return _bc5(data, w, h, px)
    if fmt == "PF_BC4":
        return _bc4(data, w, h, px)
    raise ValueError(f"不支持的像素格式: {fmt}")


def _dxt(data, w, h, px, dxt3, dxt5):
    bw, bh = (w + 3) // 4, (h + 3) // 4
    bs = 8 + (8 if dxt3 else 0) + (8 if dxt5 else 0)
    o = 0
    for by in range(bh):
        for bx in range(bw):
            al = [255] * 16
            if dxt3:
                for k in range(16):
                    al[k] = ((data[o + (k >> 1)] >> ((k & 1) * 4)) & 15) * 17
            elif dxt5:
                a0, a1 = data[o], data[o + 1]
                bits = []
                for k in range(6):
                    b = data[o + 2 + k]
                    for t in range(8):
                        bits.append((b >> t) & 1)
                if a0 > a1:
                    lut = [a0, a1, (a0 * 6 + a1) // 7, (a0 * 5 + a1 * 2) // 7,
                           (a0 * 4 + a1 * 3) // 7, (a0 * 3 + a1 * 4) // 7,
                           (a0 * 2 + a1 * 5) // 7, (a0 + a1 * 6) // 7]
                else:
                    lut = [a0, a1, (a0 * 4 + a1) // 5, (a0 * 3 + a1 * 2) // 5,
                           (a0 * 2 + a1 * 3) // 5, (a0 + a1 * 4) // 5, 0, 255]
                for k in range(16):
                    v = 0
                    for t in range(3):
                        v |= bits[k * 3 + t] << t
                    al[k] = lut[v]
            co = o + (8 if (dxt3 or dxt5) else 0)
            c0 = data[co] | data[co + 1] << 8
            c1 = data[co + 2] | data[co + 3] << 8
            p0, p1 = _c565(c0), _c565(c1)
            if c0 > c1:
                p2 = ((p0[0] * 2 + p1[0]) // 3, (p0[1] * 2 + p1[1]) // 3, (p0[2] * 2 + p1[2]) // 3)
                p3 = ((p0[0] + p1[0] * 2) // 3, (p0[1] + p1[1] * 2) // 3, (p0[2] + p1[2] * 2) // 3)
            else:
                p2 = ((p0[0] + p1[0]) // 2, (p0[1] + p1[1]) // 2, (p0[2] + p1[2]) // 2)
                p3 = (0, 0, 0)
            pal = (p0, p1, p2, p3)
            # 32 位索引字一次读入, 像素 k 的 2 位索引在 2k 处
            v32 = data[co + 4] | data[co + 5] << 8 | data[co + 6] << 16 | data[co + 7] << 24
            for py in range(4):
                for px2 in range(4):
                    k = py * 4 + px2
                    sel = (v32 >> (k * 2)) & 3
                    r, g, b = pal[sel]
                    x, y = bx * 4 + px2, by * 4 + py
                    if x < w and y < h:
                        j = (y * w + x) * 4
                        px[j] = r; px[j + 1] = g; px[j + 2] = b; px[j + 3] = al[k]
            o += bs
    return px


def _bc4(data, w, h, px):
    bw, bh = (w + 3) // 4, (h + 3) // 4
    o = 0
    for by in range(bh):
        for bx in range(bw):
            R = _dec8(data, o)
            for py in range(4):
                for px2 in range(4):
                    k = py * 4 + px2
                    x, y = bx * 4 + px2, by * 4 + py
                    if x < w and y < h:
                        j = (y * w + x) * 4
                        px[j] = px[j + 1] = px[j + 2] = R[k]
                        px[j + 3] = 255
            o += 8
    return px


def _dec8(data, o):
    a0, a1 = data[o], data[o + 1]
    bits = []
    for k in range(6):
        b = data[o + 2 + k]
        for t in range(8):
            bits.append((b >> t) & 1)
    if a0 > a1:
        lut = [a0, a1, (a0 * 6 + a1) // 7, (a0 * 5 + a1 * 2) // 7,
               (a0 * 4 + a1 * 3) // 7, (a0 * 3 + a1 * 4) // 7,
               (a0 * 2 + a1 * 5) // 7, (a0 + a1 * 6) // 7]
    else:
        lut = [a0, a1, (a0 * 4 + a1) // 5, (a0 * 3 + a1 * 2) // 5,
               (a0 * 2 + a1 * 3) // 5, (a0 + a1 * 4) // 5, 0, 255]
    out = []
    for k in range(16):
        v = 0
        for t in range(3):
            v |= bits[k * 3 + t] << t
        out.append(lut[v])
    return out


def _bc5(data, w, h, px):
    def dec(o):
        a0, a1 = data[o], data[o + 1]
        bits = []
        for k in range(6):
            b = data[o + 2 + k]
            for t in range(8):
                bits.append((b >> t) & 1)
        if a0 > a1:
            lut = [a0, a1, (a0 * 6 + a1) // 7, (a0 * 5 + a1 * 2) // 7,
                   (a0 * 4 + a1 * 3) // 7, (a0 * 3 + a1 * 4) // 7,
                   (a0 * 2 + a1 * 5) // 7, (a0 + a1 * 6) // 7]
        else:
            lut = [a0, a1, (a0 * 4 + a1) // 5, (a0 * 3 + a1 * 2) // 5,
                   (a0 * 2 + a1 * 3) // 5, (a0 + a1 * 4) // 5, 0, 255]
        out = []
        for k in range(16):
            v = 0
            for t in range(3):
                v |= bits[k * 3 + t] << t
            out.append(lut[v])
        return out

    bw, bh = (w + 3) // 4, (h + 3) // 4
    o = 0
    for by in range(bh):
        for bx in range(bw):
            R = dec(o)
            G = dec(o + 8)
            for py in range(4):
                for px2 in range(4):
                    k = py * 4 + px2
                    x, y = bx * 4 + px2, by * 4 + py
                    if x < w and y < h:
                        j = (y * w + x) * 4
                        px[j] = R[k]; px[j + 1] = G[k]; px[j + 2] = (R[k] + G[k]) // 2; px[j + 3] = 255
            o += 16
    return px


def crop_rgba(rgba: bytearray, w: int, x: int, y: int, cw: int, ch: int) -> tuple:
    """裁剪 RGBA (自动夹取边界)"""
    out = bytearray(cw * ch * 4)
    for row in range(ch):
        sy = y + row
        if sy < 0 or sy >= w and False:
            continue
        src = (sy * w + x) * 4
        dst = row * cw * 4
        n = min(cw * 4, len(rgba) - src) if 0 <= src < len(rgba) else 0
        if n > 0:
            out[dst:dst + n] = rgba[src:src + n]
    return out, cw, ch
