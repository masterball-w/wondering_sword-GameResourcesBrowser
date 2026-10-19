# -*- coding: utf-8 -*-
"""最终索引解析: FDI(含UTF-16文件名) + PHI + CityHash 验证"""
import struct, sys
sys.path.insert(0, r"D:\program\yijian_resource\tools")
from paklib import city_hash64_with_seed

PAK = r"D:\program\yijian_resource\Wandering_Sword-WindowsNoEditor.pak"
f = open(PAK, "rb")

def rd_str(buf, p):
    n, = struct.unpack_from("<i", buf, p); p += 4
    if n == 0:
        return "", p
    if n < 0:
        s = buf[p:p + (-n) * 2].decode("utf-16-le", "replace").rstrip("\x00")
        return s, p + (-n) * 2
    s = buf[p:p + n].decode("utf-8", "replace").rstrip("\x00")
    return s, p + n

# ---- FDI 全量
f.seek(5_945_189_144)
s2 = f.read(9_199_136)
ndir, = struct.unpack_from("<I", s2, 0)
p = 4
files = []   # (dir, name, enc_offset)
for i in range(ndir):
    d, p = rd_str(s2, p)
    nf, = struct.unpack_from("<i", s2, p); p += 4
    for j in range(nf):
        fn, p = rd_str(s2, p)
        v, = struct.unpack_from("<I", s2, p); p += 4
        files.append((d, fn, v))
print(f"FDI: 目录 {ndir:,}, 文件 {len(files):,}, 结束 {p:,}/{len(s2):,}")
uni = sum(1 for _, fn, _ in files if any(ord(c) > 127 for c in fn))
print(f"非 ASCII 文件名: {uni:,}")

# ---- PHI
f.seek(5_941_673_282)
s1 = f.read(3_515_862)
n1, = struct.unpack_from("<I", s1, 0)
phi = {}
for i in range(n1):
    h, v = struct.unpack_from("<QI", s1, 4 + i * 12)
    phi[h] = v

# ---- CityHash 验证
seed = 0x64AE11A2
tests = files[:200] + files[-50:]
ok = fail = 0
fail_samples = []
for d, fn, v in tests:
    path = (d + fn) if d else fn
    h = city_hash64_with_seed(path.lower().encode("utf-8"), seed)
    if h in phi:
        ok += 1
    else:
        fail += 1
        if len(fail_samples) < 5:
            fail_samples.append(path)
print(f"\nCityHash64WithSeed(lower(path), 0x64AE11A2): 匹配 {ok}/{len(tests)}")
if fail:
    print("失败样例:", fail_samples)
    # 试其它变体
    for name, fn in [
        ("原大小写", lambda p: p.encode("utf-8")),
        ("不带目录", None),
    ]:
        if fn is None:
            continue
        ok2 = sum(1 for d, f2, v in tests[:50] if city_hash64_with_seed(fn((d + f2) if d else f2), seed) in phi)
        print(f"  变体 {name}: {ok2}/50")
