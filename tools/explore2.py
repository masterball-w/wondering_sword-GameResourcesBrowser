# -*- coding: utf-8 -*-
"""探索 v11 索引三个区域的确切布局"""
import struct

idx = open(r"D:\program\yijian_resource\tools\_primary_index.bin", "rb").read()
PAK = r"D:\program\yijian_resource\Wandering_Sword-WindowsNoEditor.pak"
PRIMARY_OFF = 5_935_743_672

f = open(PAK, "rb")

def rd_fstr(buf, p):
    n, = struct.unpack_from("<i", buf, p)
    p += 4
    if n == 0:
        return "", p
    if n < 0:
        s = buf[p:p + (-n) * 2].decode("utf-16-le", "replace").rstrip("\x00")
        return s, p + (-n) * 2
    s = buf[p:p + n].decode("utf-8", "replace").rstrip("\x00")
    return s, p + n

# ---- 主索引头部复核
p = 0
mount, p = rd_fstr(idx, p)
n289441, = struct.unpack_from("<i", idx, p); p += 4
seed, = struct.unpack_from("<Q", idx, p); p += 8
b1, = struct.unpack_from("<I", idx, p); p += 4
t1o, t1s = struct.unpack_from("<QQ", idx, p); p += 16
t1h = idx[p:p + 20]; p += 20
b2, = struct.unpack_from("<I", idx, p); p += 4
t2o, t2s = struct.unpack_from("<QQ", idx, p); p += 16
t2h = idx[p:p + 20]; p += 20
print(f"mount={mount!r} n={n289441} seed={seed:#x}")
print(f"子索引1: flag={b1} off={t1o:,} size={t1s:,}")
print(f"子索引2: flag={b2} off={t2o:,} size={t2s:,}")
print(f"主索引末尾 = {PRIMARY_OFF + len(idx):,}  子索引1起点一致: {PRIMARY_OFF + len(idx) == t1o}")
print(f"子索引1末尾 = {t1o + t1s:,}  子索引2起点一致: {t1o + t1s == t2o}")
print(f"子索引2末尾 = {t2o + t2s:,}  文件大小-204 = {5_954_388_501 - 204:,}")

# ---- @p 开始: encoded entries
n_enc, = struct.unpack_from("<I", idx, p); p += 4
print(f"\nNumEncodedEntries = {n_enc:,}")

def decode_encoded(buf, p):
    bits, = struct.unpack_from("<I", buf, p); p += 4
    is_off32 = (bits >> 31) & 1
    is_unc32 = (bits >> 30) & 1
    is_size32 = (bits >> 29) & 1
    encrypted = (bits >> 22) & 1
    method = (bits >> 23) & 0x3F
    nblocks = (bits >> 6) & 0xFFFF
    if is_off32:
        off, = struct.unpack_from("<I", buf, p); p += 4
    else:
        off, = struct.unpack_from("<Q", buf, p); p += 8
    if is_unc32:
        unc, = struct.unpack_from("<I", buf, p); p += 4
    else:
        unc, = struct.unpack_from("<Q", buf, p); p += 8
    if is_size32:
        size, = struct.unpack_from("<I", buf, p); p += 4
    else:
        size, = struct.unpack_from("<Q", buf, p); p += 8
    blocksizes = []
    if nblocks > 1:
        for _ in range(nblocks - 1):
            v, = struct.unpack_from("<I", buf, p); p += 4
            blocksizes.append(v)
        v, = struct.unpack_from("<I", buf, p); p += 4
    if encrypted:
        p += 16
    return dict(off=off, unc=unc, size=size, method=method, nblocks=nblocks,
                enc=encrypted, bs=blocksizes, bits=bits), p

# 解码前 5 个 + 统计
first5 = []
i = 0
while i < 5 and p < len(idx):
    e, p = decode_encoded(idx, p)
    first5.append(e)
    i += 1
for e in first5:
    print("  ", e)

# 跳过全部 encoded: 循环解码 n_enc 次
ok = True
for _ in range(n_enc - 5):
    try:
        _, p = decode_encoded(idx, p)
    except Exception as ex:
        ok = False
        print("解码失败 @", p, ex)
        break
print(f"encoded 区结束于 p={p:,} (主索引 {len(idx):,})" if ok else "中断")

# ---- 之后: fallback files?
n_fb, = struct.unpack_from("<I", idx, p); p += 4
print(f"\nNumFilesNeedingFallbacks = {n_fb:,}")
for i in range(5):
    s, p = rd_fstr(idx, p)
    print("  fallback:", s)
# 剩余
rest = len(idx) - p
print(f"剩余字节: {rest:,}")

# ---- 子索引1: 按 PHI 假设解析 (count + (u64, entry...))
f.seek(t1o)
s1 = f.read(t1s)
print(f"\n=== 子索引1 (size={t1s:,}) 前 64 字节: {s1[:64].hex()}")
n1, = struct.unpack_from("<I", s1, 0)
print(f"  作为 u32 count = {n1:,}")

# ---- 子索引2: 按 FDI 假设解析
f.seek(t2o)
s2 = f.read(t2s)
print(f"\n=== 子索引2 (size={t2s:,}) 前 64 字节: {s2[:64].hex()}")
n2, = struct.unpack_from("<I", s2, 0)
print(f"  作为 u32 count = {n2:,}")
q = 4
for i in range(3):
    try:
        d, q = rd_fstr(s2, q)
        nf, = struct.unpack_from("<i", s2, q); q += 4
        print(f"  目录 {d!r} 文件数 {nf}")
        for j in range(min(2, nf)):
            fn, q = rd_fstr(s2, q)
            v, = struct.unpack_from("<I", s2, q); q += 4
            print(f"     {fn} -> {v} ({v:#x})")
    except Exception as ex:
        print("  解析失败:", ex)
        break
