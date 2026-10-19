# -*- coding: utf-8 -*-
"""回溯式属性流解析: 在每个属性边界尝试候选布局, 保证链条一致到 None 终止符"""
import sys, struct
sys.path.insert(0, r"D:\program\yijian_resource\tools")
from uasset import Package

def analyze(base):
    pkg = Package(open(base + ".uasset", "rb").read(), open(base + ".uexp", "rb").read())
    d = pkg.export_reader(pkg.exports[0]).d
    names = pkg.names
    N = len(names)
    none_idx = names.index("None") if "None" in names else None

    def fname_at(p):
        """FName 位置 p: 返回 (名字, 结束位置) 或 None"""
        if p + 8 > len(d):
            return None
        i = struct.unpack_from("<i", d, p)[0]
        if not (0 <= i < N):
            return None
        return names[i], p + 8

    # 候选: name@p 的前提下, header 变体 + 标签区变体 + 值 + 尾部字节数
    # 返回 (描述, 下一属性位置) 列表
    def variants(p):
        out = []
        nm = fname_at(p)
        if nm is None:
            return out
        name, _ = nm
        if none_idx is not None:
            i = struct.unpack_from("<i", d, p)[0]
            if i == none_idx:
                out.append(("None", p + 8))
                return out
        # type FName 位置候选: p+8 或 (若中间有填充) 其它
        for tpos in (p + 8,):
            t = fname_at(tpos)
            if t is None:
                continue
            tname, hend = t
            if tname not in ("BoolProperty", "StructProperty", "FloatProperty", "IntProperty",
                             "UInt32Property", "ObjectProperty", "NameProperty", "StrProperty",
                             "ByteProperty", "EnumProperty", "ArrayProperty", "MapProperty",
                             "Int64Property", "DoubleProperty"):
                continue
            if hend + 8 > len(d):
                continue
            size, arr = struct.unpack_from("<2i", d, hend)
            if size < 0 or size > 2_000_000:
                continue
            q = hend + 8
            tag_desc = ""
            if tname == "BoolProperty":
                # 值 = 1 字节; 尾部候选 0/1/2
                for trail in (1, 2, 0):
                    if q + 1 + trail <= len(d):
                        out.append((f"{name}(bool) 值={d[q]} 尾{trail}", q + 1 + trail))
                continue
            if tname == "StructProperty":
                sn = fname_at(q)
                if sn is None:
                    continue
                tag_desc = f"struct={sn[0]}"
                q += 8          # struct 名
                q += 16         # guid
            elif tname in ("ByteProperty", "EnumProperty"):
                en = fname_at(q)
                if en is None:
                    continue
                tag_desc = f"enum={en[0]}"
                q += 8
            elif tname == "ArrayProperty":
                inner = fname_at(q)
                if inner is None:
                    continue
                tag_desc = f"array<{inner[0]}>"
                q += 8
            elif tname == "MapProperty":
                q += 16
            # 值 + 尾部候选
            vstart = q
            q += size
            if q > len(d):
                continue
            for trail in (1, 0, 2, 4):
                if q + trail <= len(d):
                    out.append((f"{name}({tname}) {tag_desc} size={size} 值@{vstart} 尾{trail}", q + trail))
        return out

    # 贪心 + 单候选验证: 大多数属性只有一个合法变体
    p = 0
    history = []
    while p + 8 <= len(d):
        vs = variants(p)
        if not vs:
            print(f"!! @{p} 无合法变体, 已走 {len(history)} 属性")
            for h in history[-3:]:
                print("    ", h)
            return history, p
        if len(vs) == 1:
            desc, np_ = vs[0]
            if desc == "None" or desc.startswith("None"):
                history.append((p, desc))
                print(f"@{p}: None 终止 → 属性数据结束 @{np_}")
                return history, np_
            history.append((p, desc))
            p = np_
        else:
            # 多候选: 逐个试探后续链条
            print(f"?? @{p} 多候选:")
            for desc, np_ in vs:
                print(f"     {desc} → 下一@{np_}")
            # 选第一个能让后续至少再走 1 属性的
            chosen = None
            for desc, np_ in vs:
                nxt = variants(np_)
                if nxt:
                    chosen = (desc, np_)
                    break
            if not chosen:
                chosen = vs[0]
            history.append((p, chosen[0]))
            print(f"   选: {chosen[0]}")
            p = chosen[1]
    return history, p

if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else r"D:\program\yijian_resource\extracted\Wandering_Sword\Content\JH\Audio\Background\名剑山庄"
    hist, end = analyze(base)
    print(f"\n共 {len(hist)} 属性, 结束 @{end}")
