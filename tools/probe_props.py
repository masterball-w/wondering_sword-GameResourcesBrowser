# -*- coding: utf-8 -*-
"""属性流实时探测: 逐属性打印字段布局, 用于校准规则"""
import sys, struct
sys.path.insert(0, r"D:\program\yijian_resource\tools")
from uasset import Package

base = sys.argv[1] if len(sys.argv) > 1 else r"D:\program\yijian_resource\extracted\Wandering_Sword\Content\JH\Audio\Background\名剑山庄"
pkg = Package(open(base + ".uasset", "rb").read(), open(base + ".uexp", "rb").read())
d = pkg.export_reader(pkg.exports[0]).d
names = pkg.names
N = len(names)

def s(p, n=4):
    return struct.unpack_from("<i", d, p)[0] if 0 <= p and p + 4 <= len(d) else None

print(f"名字数 {N}, uexp {len(d)}")
p = 0
while p + 8 <= len(d):
    ni = s(p)
    if ni is None:
        print(f"@{p}: 越界"); break
    if ni == names.index("None") if "None" in names else ni == 11:
        print(f"@{p}: None (终止) hash={s(p+4)}")
        p += 8
        break
    if not (0 <= ni < N):
        print(f"@{p}: 非法名字索引 {ni} (0x{ni & 0xffffffff:x})")
        break
    # 打印接下来 12 个 i32 供人工判断
    row = [s(p + 4 * k) for k in range(12)]
    print(f"@{p}: '{names[ni]}' 后续 i32: {row}")
    # 自动尝试: [name8][type8][size4][arr4] + 按类型的标签区
    ti = s(p + 8)
    size = s(p + 16)
    if not (0 <= ti < N):
        print("   类型索引非法, 停止")
        break
    tname = names[ti]
    q = p + 24
    if tname == "BoolProperty":
        print(f"   type={tname} size={size} bool区={d[q:q+4].hex()}")
        q += 2
    elif tname == "StructProperty":
        sn = s(q)
        q += 8 + 16 + 1
        print(f"   type={tname}({names[sn] if 0<=sn<N else sn}) size={size}")
        q += size
    elif tname in ("FloatProperty", "IntProperty", "UInt32Property"):
        print(f"   type={tname} size={size}")
        q += size
    elif tname == "ObjectProperty":
        q += 4
        print(f"   type={tname}")
    elif tname == "ByteProperty" or tname == "EnumProperty":
        en = s(q)
        q += 8
        print(f"   type={tname} enum={names[en] if 0<=en<N else en} size={size}")
        q += max(size, 1)
    elif tname == "ArrayProperty":
        print(f"   type={tname} size={size}")
        q += 8 + size
    else:
        print(f"   type={tname} (未处理) size={size}")
        q += size
    p = q

print(f"\n结束 @{p} / {len(d)}")
if p < len(d):
    for i in range(p, min(p + 96, len(d)), 16):
        print(f"@{i}: {d[i:i+16].hex()}")
