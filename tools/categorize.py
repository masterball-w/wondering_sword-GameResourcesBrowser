# -*- coding: utf-8 -*-
"""
资源分类器:
1. 嗅探每个 .uasset/.umap 的类名(名字表/导入表在文件头)
2. 资产成组 (同名的 .uasset/.uexp/.ubulk 归为一项)
3. 以硬链接组织到 categorized/<类别>/ 下(同卷不占额外空间)
4. 输出 分类的完整清单.json + 分类报告.md
"""
import os
import re
import sys
import json
import time

LINKS = '--links' in sys.argv   # 默认仅生成索引; 传 --links 才创建硬链接树
from collections import Counter, defaultdict

ROOT = r"D:\program\yijian_resource"
SRC = os.path.join(ROOT, "extracted")
DST = os.path.join(ROOT, "categorized")

# 类别判定: (标记列表, 类别) —— 顺序即优先级
MARKERS = [
    # 音频
    (b"SoundWave",                "03_音频"),
    (b"ReverbEffect",             "03_音频"),
    (b"SoundMix",                 "03_音频"),
    (b"SoundClass",               "03_音频"),
    (b"SoundCue",                 "03_音频"),
    (b"SoundAttenuation",         "03_音频"),
    (b"SoundConcurrency",         "03_音频"),
    # 2D 精灵 / 翻页动画
    (b"PaperFlipbook",            "01_角色精灵动画"),
    (b"PaperSprite",              "01_角色精灵动画"),
    (b"PaperSpriteSheet",         "01_角色精灵动画"),
    (b"TileMap",                  "01_角色精灵动画"),
    (b"TileSet",                  "01_角色精灵动画"),
    # 纹理
    (b"Texture2D",                "02_纹理贴图"),
    (b"TextureCube",              "02_纹理贴图"),
    (b"TextureRenderTarget2D",    "02_纹理贴图"),
    (b"TextureLightProfile",      "02_纹理贴图"),
    # 骨骼动画
    (b"AnimSequence",             "05_骨骼动画"),
    (b"AnimMontage",              "05_骨骼动画"),
    (b"BlendSpace",               "05_骨骼动画"),
    (b"AnimComposite",           "05_骨骼动画"),
    (b"AnimBlueprintGenerated",   "05_骨骼动画"),
    (b"Skeleton",                 "05_骨骼动画"),
    (b"PhysicsAsset",             "05_骨骼动画"),
    (b"PoseAsset",                "05_骨骼动画"),
    # 模型
    (b"SkeletalMesh",             "04_模型"),
    (b"StaticMesh",               "04_模型"),
    (b"LandscapeGrassType",       "04_模型"),
    # 特效
    (b"NiagaraSystem",            "09_特效"),
    (b"NiagaraEmitter",           "09_特效"),
    (b"ParticleSystem",           "09_特效"),
    (b"MaterialInstanceConstant", "07_材质"),
    (b"MaterialFunction",         "07_材质"),
    (b"Material",                 "07_材质"),
    (b"SubsurfaceProfile",        "07_材质"),
    # UI
    (b"WidgetBlueprint",          "08_UI界面"),
    (b"UserWidget",               "08_UI界面"),
    # 字体
    (b"FontFace",                 "13_字体"),
    (b"Font",                     "13_字体"),
    # 数据
    (b"DataTable",                "11_数据表格"),
    (b"CurveTable",               "11_数据表格"),
    (b"StringTable",              "11_数据表格"),
    (b"UserDefinedEnum",          "11_数据表格"),
    # 蓝图(放后面, 很多资产会引用 Blueprint 字样)
    (b"BlueprintGeneratedClass",  "06_蓝图逻辑"),
]

PATH_RULES = [
    ("/JH/Characters/",           "01_角色精灵动画"),
    ("/JH/UI",                     "08_UI界面"),
    ("/JH/JHNeoUI",                "08_UI界面"),
    ("/JH/JHGameUI",               "08_UI界面"),
    ("/JH/Audio/",                 "03_音频"),
    ("/JH/Skills/",                "06_蓝图逻辑"),
    ("/JH/Maps/",                  "10_地图场景"),
    ("/JH/MapEditor/",             "10_地图场景"),
    ("/JH/ParticleSystem/",        "09_特效"),
    ("/JH/Particles/",             "09_特效"),
    ("/JH/Tables/",                "11_数据表格"),
    ("/JH/Core/",                  "06_蓝图逻辑"),
    ("/Localization/",             "12_本地化文本"),
]

SNIFF_LIMIT = 512 * 1024   # 每个资产最多读 512KB 做嗅探

def classify_uasset(path: str):
    """返回 (类别 or None)"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(min(size, SNIFF_LIMIT))
    except OSError:
        return None
    if len(head) < 8:
        return None
    for marker, cat in MARKERS:
        if marker in head:
            return cat
    return None

def classify(rel: str, sniff_result):
    low = "/" + rel.replace("\\", "/")
    ext = low.rsplit(".", 1)[-1] if "." in low else ""
    # 扩展名直判
    if ext == "umap":
        return "10_地图场景"
    if ext == "locres":
        return "12_本地化文本"
    if ext in ("ini", "cfg", "json", "txt", "xml", "uplugin", "upluginmanifest", "manifest", "icu", "res", "bin", "dat"):
        if "/Config/" in low or low.startswith("/Engine/Config"):
            return "14_配置文件"
        if "/Internationalization/" in low or ext == "icu":
            return "12_本地化文本"
        return "15_引擎与系统"
    if ext == "ufont":
        return "13_字体"
    if ext in ("uasset", "uexp", "ubulk", "uptnl"):
        if sniff_result:
            return sniff_result
        for rule, cat in PATH_RULES:
            if rule in low:
                return cat
        if low.startswith("/Engine/"):
            return "15_引擎与系统"
        return "16_未识别"
    return "15_引擎与系统"

def main():
    t0 = time.time()
    # 收集所有文件
    all_files = []
    for base, dirs, files in os.walk(SRC):
        for fn in files:
            if fn.startswith("_"):
                continue
            all_files.append(os.path.relpath(os.path.join(base, fn), SRC).replace("\\", "/"))
    all_files.sort()
    print(f"共 {len(all_files):,} 文件")

    # 成组: 同 stem 的 uasset/uexp/ubulk 一组, 以 uasset 的嗅探结果为准
    sniff_cache = {}
    groups = defaultdict(list)      # stem -> [rel, ...]
    group_root = {}                 # stem -> 所在目录

    def do_sniff(rel):
        if rel not in sniff_cache:
            sniff_cache[rel] = classify_uasset(os.path.join(SRC, rel))
        return sniff_cache[rel]

    for rel in all_files:
        d, fn = rel.rsplit("/", 1)
        stem, _, ext = fn.rpartition(".")
        if ext in ("uasset", "uexp", "ubulk", "uptnl") and stem:
            key = d + "/" + stem
            groups[key].append(rel)
            group_root.setdefault(key, d)
        else:
            groups[rel].append(rel)   # 独立文件自成一组
            group_root[rel] = d

    print(f"资产组: {len(groups):,}, 用时 {time.time()-t0:.0f}s")

    # 分类每组
    result = []        # (类别, 组key, [files])
    cat_counter = Counter()
    cat_bytes = Counter()
    for key, files in groups.items():
        head = next((f for f in files if f.endswith(".uasset") or f.endswith(".umap")), files[0])
        if head.endswith((".uasset", ".umap")):
            sniff = do_sniff(head)
            if sniff is None:
                for f in files:
                    if f.endswith((".uasset", ".ubulk")):
                        sniff = do_sniff(f)
                        if sniff:
                            break
            cat = classify(head, sniff)   # head 带扩展名, 保证扩展名分支正确
        else:
            cat = classify(files[0], None)
        result.append((cat, key, files))
        cat_counter[cat] += 1
        cat_bytes[cat] += sum(os.path.getsize(os.path.join(SRC, f)) for f in files)
        if len(result) % 20000 == 0:
            print(f"  已分类 {len(result):,}/{len(groups):,}", flush=True)

    # 硬链接组织 (可选)
    link_errors = 0
    for cat, key, files in (result if LINKS else []):
        for f in files:
            src = os.path.join(SRC, f)
            dst = os.path.join(DST, cat, f)
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if not os.path.exists(dst):
                    os.link(src, dst)
            except OSError:
                link_errors += 1
    print(f"硬链接完成, 失败 {link_errors}")

    # 清单
    manifest = {
        "总组数": len(result),
        "分类统计": {c: {"组数": n, "字节数": cat_bytes[c]} for c, n in cat_counter.most_common()},
    }
    with open(os.path.join(ROOT, "分类清单.json"), "w", encoding="utf-8") as f:
        json.dump({c: [{"资产": k, "文件": fs} for cat, k, fs in result if cat == c]
                   for c in cat_counter}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(ROOT, "分类报告.md"), "w", encoding="utf-8") as f:
        f.write("# 逸剑风云决 资源分类报告\n\n")
        f.write(f"- 来源: Wandering_Sword-WindowsNoEditor.pak\n")
        f.write(f"- 资产组总数: {len(result):,}\n")
        f.write(f"- 完整清单: 分类清单.json\n\n")
        f.write("| 类别 | 资产组数 | 体积 |\n|---|---|---|\n")
        for c, n in cat_counter.most_common():
            f.write(f"| {c} | {n:,} | {cat_bytes[c]/1e6:.1f} MB |\n")
    print("清单与报告已生成")
    print(f"总用时 {time.time()-t0:.0f}s")

if __name__ == "__main__":
    main()
