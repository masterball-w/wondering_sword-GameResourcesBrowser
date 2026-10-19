# -*- coding: utf-8 -*-
"""精确探索: 编码区边界 + 主索引尾 + 子索引1/2 结构"""
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

def walk_encoded(buf, p, end):
    """走 encoded 记录, 返回 (条目列表, 结束位置)"""
    ents = []
    while p < end:
        bits, = struct.unpack_from("<I", buf, p); p += 4
        is_off32 = (bits >> 31) & 1
        is_unc32 = (bits >> 30) & 1
        is_size32 = (bits >> 29) & 1
        encrypted = (bits >> 22) & 1
        method = (bits >> 23) & 0x3F
        nblocks = (bits >> 6) & 0xFFFF
        off = struct.unpack_from("<I" if is_off32 else "<Q", buf, p)[0]
        p += 4 if is_off32 else 8
        unc = struct.unpack_from("<I" if is_unc32 else "<Q", buf, p)[0]
        p += 4 if is_unc32 else 8
        size = struct.unpack_from("<I" if is_size32 else "<Q", buf, p)[0]
        p += 4 if is_size32 else 8
        bs = []
        if nblocks > 1:
            for _ in range(nblocks - 1):
                v, = struct.unpack_from("<I", buf, p); p += 4
                bs.append(v)
            v, = struct.unpack_from("<I", buf, p); p += 4
        if encrypted:
            p += 16
        ents.append((off, unc, size, method, nblocks, encrypted))
    return ents, p

size_enc, = struct.unpack_from("<I", idx, 106)
print(f"@106 u32 = {size_enc:,}; 主索引总长 = {len(idx):,}; @110+size = {110 + size_enc:,}")

ents, p_end = walk_encoded(idx, 110, 110 + size_enc)
print(f"编码条目数: {len(ents):,}, 结束位置: {p_end:,}")
print(f"主索引剩余尾部: {len(idx) - p_end} 字节: {idx[p_end:].hex()}")
print(f"尾部 ascii: {''.join(chr(b) if 32<=b<127 else '.' for b in idx[p_end:])}")

# 多块条目样例
multi = [e for e in ents if e[4] > 1][:3]
print("多块条目样例:", multi)
uncomp = [e for e in ents if e[3] == 0][:3]
print("未压缩条目样例:", uncomp)
print(f"偏移最大值: {max(e[0] for e in ents):,}  未压缩总大小: {sum(e[1] for e in ents):,}")
import collections
mc = collections.Counter(e[3] for e in ents)
print(f"压缩方法分布: {dict(mc)}")
bc = collections.Counter(e[4] for e in ents)
print(f"块数分布(前8): {bc.most_common(8)}")
enc_c = collections.Counter(e[5] for e in ents)
print(f"加密分布: {dict(enc_c)}")

# ---- 子索引1 (3.5MB): 猜 PHI = u32 count + (u64 hash, entry...)
f.seek(5_941_673_282)
s1 = f.read(3_515_862)
n1, = struct.unpack_from("<I", s1, 0)
print(f"\n子索引1 头 u32 = {n1:,}  前32字节: {s1[:32].hex()}")
# 若为 PHI: n1 应 ~ 文件数
if 0 < n1 < 1_000_000:
    q = 4
    print("按 PHI 解析前3条:")
    for i in range(3):
        h, = struct.unpack_from("<Q", s1, q); q += 8
        off, size, unc = struct.unpack_from("<QQQ", s1, q); q += 24
        method, = struct.unpack_from("<I", s1, q); q += 4
        hash20 = s1[q:q+20]; q += 20
        nb = 0; blocks = []
        if method != 0:
            nb, = struct.unpack_from("<i", s1, q); q += 4
            blocks = [struct.unpack_from("<QQ", s1, q + 16*j) for j in range(nb)]
            q += 16 * nb
        bs, = struct.unpack_from("<I", s1, q); q += 4
        print(f"  hash={h:#018x} off={off:,} size={size:,} unc={unc:,} m={method} nb={nb} bs={bs}")

# ---- 子索引2 (9.2MB): FDI = u32 ndir + [dir, u32 nfile, [file, u32 val]...]
f.seek(5_945_189_144)
s2 = f.read(9_199_136)
n2, = struct.unpack_from("<I", s2, 0)
print(f"\n子索引2 头 u32 = {n2:,} (目录数?) 前32字节: {s2[:32].hex()}")
q = 4
total = 0
vals = []
try:
    for i in range(n2 if n2 < 100000 else 3):
        d, q = rd_fstr(s2, q)
        nf, = struct.unpack_from("<i", s2, q); q += 4
        if i < 3:
            print(f"  目录[{i}] {d!r}  文件数 {nf}")
        for j in range(nf):
            fn, q = rd_fstr(s2, q)
            v, = struct.unpack_from("<I", s2, q); q += 4
            vals.append(v)
        total += nf
        if i >= 2 and n2 >= 100000:
            break
    print(f"  (仅前3目录) 文件累计 {total}")
    print(f"  值样本: {vals[:10]}")
    print(f"  值范围: {min(vals):,} .. {max(vals):,}, 高位标记: {sum(1 for v in vals if v & 0x80000000)}")
except Exception as ex:
    print("  解析失败:", ex)
