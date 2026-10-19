# -*- coding: utf-8 -*-
"""解析 pak 索引并验证格式理解: 打印结构统计 + 验证 PathHash 公式"""
import sys, os, json
sys.path.insert(0, os.path.dirname(__file__))
from paklib import PakFile, city_hash64, city_hash64_with_seed

PAK = r"D:\program\yijian_resource\Wandering_Sword-WindowsNoEditor.pak"

pak = PakFile(PAK)
info = pak.info
print(f"版本: {info.version}")
print(f"MountPoint: {pak.mount_point!r}")
print(f"压缩方法: {info.compression_methods}")
print(f"PathHashSeeds: {pak.path_hash_seeds}")
print(f"主索引: offset={info.index_offset:,} size={info.index_size:,}")
print(f"PathHashIndex: offset={pak.phi_offset:,} size={pak.phi_size:,} 条目数={len(pak.path_hash_index)} 尾部剩余={pak.phi_tail}")
print(f"FullDirectoryIndex: offset={pak.fdi_offset:,} size={pak.fdi_size:,} 目录数={len(pak.full_directory_index)} 尾部剩余={pak.fdi_tail}")
print(f"主索引尾部: (encoded区起点={pak.primary_after_counts}, n_encoded={pak.encoded_count}, n_fallback={pak.fallback_count})")

n_files = sum(len(m) for m in pak.full_directory_index.values())
print(f"文件总数: {n_files:,}")

# FDI 值分布 → 判断 num 语义
vals = [v for m in pak.full_directory_index.values() for v in m.values()]
hi = [v for v in vals if v & 0x80000000]
print(f"FDI 值: max={max(vals)}, min={min(vals)}, 高位标记数={len(hi)}")

# 抽样看目录结构
dirs = sorted(pak.full_directory_index.keys())
print("\n目录数:", len(dirs))
for d in dirs[:15]:
    print("  ", d, f"({len(pak.full_directory_index[d])} 文件)")

# ---- 验证 PathHash 公式: 用前 N 个文件
seed_str = pak.path_hash_seeds[0] if pak.path_hash_seeds else ""
seed_h = city_hash64(seed_str.encode("utf-8"))
print(f"\nseed={seed_str!r} -> CityHash64(seed)={seed_h:#x}")

def full_path(d, fn):
    return (d + "/" + fn) if d else fn

pairs = []
for d, m in pak.full_directory_index.items():
    for fn in m:
        pairs.append(full_path(d, fn))
        if len(pairs) >= 50:
            break
    if len(pairs) >= 50:
        break

ph = pak.path_hash_index
def test(name, fn):
    ok = 0
    for p in pairs:
        if fn(p) in ph:
            ok += 1
    print(f"  {name}: {ok}/{len(pairs)}")
    return ok, len(pairs)

print("\n验证路径哈希公式:")
ok, total = test("CityHash64WithSeed(lower(path), CityHash64(seed))",
                 lambda p: city_hash64_with_seed(p.lower().encode("utf-8"), seed_h))
if ok < total:
    ok2, _ = test("CityHash64WithSeed(path原样, CityHash64(seed))",
                  lambda p: city_hash64_with_seed(p.encode("utf-8"), seed_h))
    ok3, _ = test("CityHash64(lower(path)+seed)",
                  lambda p: city_hash64((p.lower() + seed_str).encode("utf-8")))
    ok4, _ = test("CityHash64(seed+lower(path))",
                  lambda p: city_hash64((seed_str + p.lower()).encode("utf-8")))
    ok5, _ = test("CityHash64WithSeed(lower(path), CityHash64WithSeed(seed,0))",
                  lambda p: city_hash64_with_seed(p.lower().encode("utf-8"), city_hash64_with_seed(seed_str.encode("utf-8"), 0)))

# 条目统计
exts = {}
total_unc = total_cmp = 0
for d, m in pak.full_directory_index.items():
    for fn in m:
        e = fn.rsplit(".", 1)
        ext = ("." + e[1].lower()) if len(e) == 2 else "(无)"
        exts[ext] = exts.get(ext, 0) + 1
print("\n扩展名分布(前 25):")
for k, v in sorted(exts.items(), key=lambda x: -x[1])[:25]:
    print(f"  {k:12s} {v:,}")

pak.close()
