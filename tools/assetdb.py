# -*- coding: utf-8 -*-
"""
资源分析层: 从 pak 直读资产, 生成预览数据
支持: Texture2D / PaperSprite / PaperFlipbook / SoundWave / locres / 通用属性
"""
from __future__ import annotations
import io
import os
import re
import struct
import zlib
from functools import lru_cache

from paklib import Pak
from uasset import Package, Reader


class AssetDB:
    def __init__(self, pak_path: str, index_json: str | None = None):
        self.pak_path = pak_path
        self.pak = Pak(pak_path)
        self.entries = {}
        for e in self.pak.iter_files():
            self.entries[e.path] = e
        # 组: stem -> [文件]
        self.groups = {}
        for p, e in self.entries.items():
            stem, _, ext = p.rpartition(".")
            self.groups.setdefault(stem, []).append(p)
        # 分类索引
        self.stem_cat = {}
        self.cat_stems = {}
        if index_json and os.path.exists(index_json):
            import json
            data = json.load(open(index_json, encoding="utf-8"))
            for cat, items in data.items():
                self.cat_stems[cat] = []
                for it in items:
                    stem = it["资产"]
                    self.stem_cat[stem] = cat
                    self.cat_stems[cat].append(stem)
        self._pkg_cache = {}

    # ---------------------------------------------- 基础
    def read_file(self, path: str) -> bytes:
        e = self.entries.get(path)
        if e is None:
            raise KeyError(path)
        return self.pak.read_entry(e)

    def package_of(self, stem: str) -> Package | None:
        pkg = self._pkg_cache.get(stem)
        if pkg is not None:
            return pkg
        ua = self.read_file(stem + ".uasset")
        ue = self.read_file(stem + ".uexp") if (stem + ".uexp") in self.entries else b""
        ub = self.read_file(stem + ".ubulk") if (stem + ".ubulk") in self.entries else b""
        pkg = Package(ua, ue, ub)
        self._pkg_cache[stem] = pkg
        if len(self._pkg_cache) > 400:
            for k in list(self._pkg_cache)[:100]:
                del self._pkg_cache[k]
        return pkg

    def export_class(self, pkg: Package) -> str:
        """主导出对象的类名 (跳过 BodySetup 等附属对象)"""
        def cname_of(e):
            ci = e.class_index
            if ci < 0 and -ci <= len(pkg.imports):
                return pkg.imports[-ci - 1][3]
            return None
        for e in pkg.exports:
            if e.serial_size == 0:
                continue
            cn = cname_of(e)
            if cn and cn not in ("BodySetup", "Class", "Package"):
                return cn
        for e in pkg.exports:
            if e.serial_size > 0:
                cn = cname_of(e)
                if cn:
                    return cn
        return ""

    def game_path_to_pak(self, gp: str) -> str:
        """/Game/JH/... -> Wandering_Sword/Content/JH/..."""
        if gp.startswith("/Game/"):
            return "Wandering_Sword/Content/" + gp[len("/Game/"):]
        return gp.strip("/")

    def import_path(self, pkg: Package, ref: int) -> str | None:
        """对象引用(负=导入)解析为 /Game/ 路径或名字"""
        if ref >= 0:
            if 0 < ref <= len(pkg.exports):
                return pkg.exports[ref - 1].name
            return None
        i = -ref - 1
        if 0 <= i < len(pkg.imports):
            imp = pkg.imports[i]
            name = imp[3]
            num = imp[4] if len(imp) > 4 else 0
            return name if num == 0 else f"{name}_{num - 1}"
        return None

    # ---------------------------------------------- 贴图
    def texture_info(self, stem: str) -> dict | None:
        """解析 Texture2D: 尺寸/格式/mip0 数据"""
        pkg = self.package_of(stem)
        d = pkg.export_reader(pkg.exports[0]).d if pkg.exports else b""
        m = re.search(rb"PF_[A-Z0-9]+", d)
        if not m:
            return None
        fmt = d[m.start():m.end()].decode()
        # 实测(视觉验证): i-16 = SizeX(宽), i-12 = SizeY(高)
        sx, sy = struct.unpack_from("<2i", d, m.start() - 16)
        # 字符串结束后: [NumMips][b?][flags][count][size][offset i64] + 数据
        after = m.end()
        # 找 bulk: 从 after 起扫描 [i32 count][i32 size] 后跟 i64, 数据
        mip = None
        for delta in range(0, 96):
            q = after + delta
            if q + 24 > len(d):
                break
            flags, cnt, size = struct.unpack_from("<3i", d, q)
            off, = struct.unpack_from("<q", d, q + 12)   # bulk头: [flags][cnt][size][offset i64] 共20B
            expect = self._expected_bytes(sx, sy, fmt)
            if cnt > 0 and expect and cnt == expect:
                data_start = q + 20
                inline = len(d) - data_start >= cnt
                if inline:
                    mip = dict(data=d[data_start:data_start + cnt], source="inline")
                else:
                    ub = self.read_file(stem + ".ubulk") if (stem + ".ubulk") in self.entries else b""
                    if ub:
                        if len(ub) >= off + cnt:
                            mip = dict(data=ub[off:off + cnt], source="ubulk")
                        else:
                            mip = dict(data=ub[:cnt], source="ubulk-head")
                break
        return dict(w=sx, h=sy, fmt=fmt, mip=mip)

    @staticmethod
    def _expected_bytes(w: int, h: int, fmt: str):
        bw, bh = (max(1, (w + 3) // 4), max(1, (h + 3) // 4))
        if fmt in ("PF_DXT1", "PF_BC4"):
            return bw * bh * 8
        if fmt in ("PF_DXT3", "PF_DXT5", "PF_BC3", "PF_BC5", "PF_BC6H", "PF_BC7"):
            return bw * bh * 16
        if fmt in ("PF_B8G8R8A8", "PF_R8G8B8A8", "PF_A8R8G8B8"):
            return w * h * 4
        if fmt == "PF_G8":
            return w * h
        return None

    # ---------------------------------------------- 精灵
    def sprite_info(self, stem: str) -> dict | None:
        pkg = self.package_of(stem)
        for e in pkg.exports:
            if e.serial_size == 0 or e.name == "BodySetup":
                continue
            r = pkg.export_reader(e)
            props, ok = pkg.read_properties(r)
            if "BakedSourceTexture" not in props:
                continue
            uv = props.get("BakedSourceUV")
            dim = props.get("BakedSourceDimension")
            tex_ref = props["BakedSourceTexture"][1]
            tex = self.import_path(pkg, tex_ref) if isinstance(tex_ref, int) else None
            tex_stem = None
            if tex and tex.startswith("/Game/"):
                tex_stem = self.game_path_to_pak(tex)
            else:
                # 回退: 去掉 _Sprite_N 后缀
                base = re.sub(r"_Sprite(_\d+)?$", "", stem)
                tex_stem = base
            uvv = uv[1] if uv else None
            dimv = dim[1] if dim else None
            return dict(
                uv=(uvv[1], uvv[2]) if uvv else (0.0, 0.0),
                dim=(dimv[1], dimv[2]) if dimv else (0.0, 0.0),
                tex=tex_stem,
                has_uv=uv is not None,
            )
        return None

    # ---------------------------------------------- 翻页动画
    def flipbook_info(self, stem: str) -> dict | None:
        pkg = self.package_of(stem)
        for e in pkg.exports:
            if e.serial_size == 0:
                continue
            r = pkg.export_reader(e)
            props, ok = pkg.read_properties(r)
            if "KeyFrames" not in props:
                continue
            fps = props.get("FramesPerSecond", (None, 10.0))[1] or 10.0
            d = r.d
            n = pkg.names
            try:
                sprite_i = n.index("Sprite")
                obj_i = n.index("ObjectProperty")
                none_i = n.index("None")
            except ValueError:
                return None
            # 帧引用模式: [Sprite idx][0][ObjectProperty idx][0][4][0][1字节前导][i32 引用]
            frames = []
            runs = []
            for q in range(0, len(d) - 40):
                if (struct.unpack_from("<i", d, q)[0] == sprite_i
                        and struct.unpack_from("<i", d, q + 8)[0] == obj_i
                        and struct.unpack_from("<i", d, q + 16)[0] == 4):
                    ref, = struct.unpack_from("<i", d, q + 25)
                    frames.append(ref)
                    runs.append(q)
            names = []
            for ref in frames:
                nm = self.import_path(pkg, ref) if isinstance(ref, int) else None
                names.append(nm)
            return dict(fps=fps, frames=names, n=len(frames))
        return None

    # ---------------------------------------------- 音频
    def sound_info(self, stem: str) -> dict | None:
        pkg = self.package_of(stem)
        for e in pkg.exports:
            if e.serial_size == 0:
                continue
            r = pkg.export_reader(e)
            props, ok = pkg.read_properties(r)
            if "Duration" not in props and "SampleRate" not in props:
                continue
            d = r.d
            p = r.p
            meta = {k: v[1] for k, v in props.items()}
            # 找内联 OGG
            ogg = None
            pos = d.find(b"OggS", p)
            if pos >= 0 and pos - p < 4096:
                cnt, = struct.unpack_from("<i", d, pos - 16)
                if 0 < cnt <= len(d) - pos:
                    ogg = d[pos:pos + cnt]
                else:
                    ogg = d[pos:]
            else:
                # ubulk 回退
                if (stem + ".ubulk") in self.entries:
                    ub = self.read_file(stem + ".ubulk")
                    pos = ub.find(b"OggS")
                    if pos >= 0:
                        ogg = ub[pos:]
            return dict(meta=meta, ogg=ogg, fmt="OGG")
        return None

    # ---------------------------------------------- locres
    def locres_entries(self, path: str) -> list:
        d = self.read_file(path)
        items = []
        pos = 33
        skipped = 0
        while pos + 8 <= len(d):
            h, ln = struct.unpack_from("<Ii", d, pos)
            okp = False
            if 0 < ln < 32768 and pos + 8 + ln <= len(d):
                raw = d[pos + 8:pos + 8 + ln]
                if raw.endswith(b"\x00"):
                    try:
                        s = raw[:-1].decode("utf-8")
                        if s and all(c.isprintable() or c in "\n\t\r" for c in s):
                            items.append(s)
                            pos += 8 + ln
                            okp = True
                    except UnicodeDecodeError:
                        pass
            if not okp and -32768 < ln < 0 and pos + 8 + (-ln) * 2 <= len(d):
                raw = d[pos + 8:pos + 8 + (-ln) * 2]
                if raw.endswith(b"\x00\x00"):
                    try:
                        s = raw.decode("utf-16-le").rstrip("\x00")
                        if s and all(c.isprintable() or c in "\n\t\r" for c in s):
                            items.append(s)
                            pos += 8 + (-ln) * 2
                            okp = True
                    except Exception:
                        pass
            if not okp:
                pos += 1
                skipped += 1
                if skipped > 64:
                    break
            else:
                skipped = 0
        return items

    # ---------------------------------------------- 通用
    def asset_type(self, stem: str) -> str:
        if stem.endswith(".locres") or stem in self.entries and stem.endswith(".locres"):
            return "locres"
        if stem in self.entries:
            ext = stem.rsplit(".", 1)[-1]
            if ext != "uasset":
                return "file"
        pkg = self.package_of(stem)
        cls = self.export_class(pkg)
        return cls or "unknown"


    # ---------------------------------------------- PNG 导出
    def _png_cache(self, key):
        import hashlib
        os.makedirs(os.path.join(os.path.dirname(self.pak_path), "tools", "_pngcache"), exist_ok=True)
        h = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
        return os.path.join(os.path.dirname(self.pak_path), "tools", "_pngcache", h + ".png")

    def texture_png(self, stem: str, rect=None) -> bytes:
        """纹理 → PNG; rect=(x,y,w,h) 为 UV 坐标(左下原点), 服务端翻转到左上"""
        from texdec import decode_texture, encode_png
        cache_key = stem + (f"|{rect}" if rect else "")
        cf = self._png_cache(cache_key)
        if os.path.exists(cf):
            return open(cf, "rb").read()
        t = self.texture_info(stem)
        if not t or not t.get("mip"):
            raise ValueError("无纹理数据")
        rgba = decode_texture(t["mip"]["data"], t["w"], t["h"], t["fmt"])
        if rect:
            x, y, w, h = rect
            cy = t["h"] - y - h      # 左下原点 → 左上原点
            from texdec import crop_rgba
            rgba, w, h = crop_rgba(rgba, t["w"], x, cy, w, h)
            png = encode_png(w, h, rgba)
        else:
            png = encode_png(t["w"], t["h"], rgba)
        with open(cf, "wb") as f:
            f.write(png)
        return png

    def sprite_png(self, stem: str) -> bytes:
        sp = self.sprite_info(stem)
        if not sp or not sp.get("tex"):
            raise ValueError("精灵无图集")
        t = self.texture_info(sp["tex"])
        if not t or not t.get("mip"):
            raise ValueError("图集无数据")
        x, y = int(sp["uv"][0]), int(sp["uv"][1])
        w, h = int(sp["dim"][0]), int(sp["dim"][1])
        return self.texture_png(sp["tex"], rect=(x, y, w, h))

    def flipbook_frames(self, stem: str):
        """返回 (fps, [(帧名, 精灵stem)], 图集stem) 或 None"""
        fb = self.flipbook_info(stem)
        if not fb or not fb["frames"]:
            return None
        dirp = stem.rsplit("/", 1)[0]
        atlas = None
        frames = []
        for fname in fb["frames"]:
            sp = None
            for cand in (f"{dirp}/{fname}", f"{dirp}/Textures/{fname}"):
                if (cand + ".uasset") in self.entries:
                    sp = self.sprite_info(cand)
                    if sp:
                        break
            if not sp:
                continue
            if atlas is None and sp.get("tex"):
                at = self.texture_info(sp["tex"])
                if at and at.get("mip"):
                    atlas = sp["tex"]
                    aw, ah = at["w"], at["h"]
            frames.append((fname, sp, ))
        if not atlas:
            return None
        return fb["fps"], frames, atlas

    # ---------------------------------------------- 数据表
    def datatable_info(self, stem: str, max_rows=3000, time_budget=15.0):
        """DataTable → (columns, rows[(行名, {列: 值})])"""
        pkg = self.package_of(stem)
        none_i = pkg.names.index("None") if "None" in pkg.names else -1
        cols = []
        rows = []
        for e in sorted(pkg.exports, key=lambda x: -x.serial_size):
            if e.serial_size == 0:
                continue
            try:
                r = pkg.export_reader(e)
                head, ok = pkg.read_properties(r)
            except Exception:
                continue
            if "RowStruct" not in head:
                continue
            d = r.d
            r.p += 8      # 表头后 8 字节 (计数/引用)
            import time as _time
            _t0 = _time.time()
            while r.p + 8 <= len(d) and len(rows) < max_rows:
                if _time.time() - _t0 > time_budget:
                    break
                try:
                    idx, num = struct.unpack_from("<2i", d, r.p)
                except struct.error:
                    break
                if none_i >= 0 and idx == none_i:
                    break
                nm = pkg.names[idx] if 0 <= idx < len(pkg.names) else None
                if nm is None:
                    break
                r.p += 8
                try:
                    props, ok = pkg.read_properties(r)
                except Exception:
                    break
                if not ok:
                    break
                vals = {}
                for k, v in props.items():
                    if k not in cols:
                        cols.append(k)
                    vals[k] = v[1] if isinstance(v, tuple) else v
                rows.append((nm, vals))
            return cols, rows
        return None

    # ---------------------------------------------- 模型(点云)
    def mesh_info(self, stem: str, max_points=60000):
        """StaticMesh/SkeletalMesh → 顶点位置点云 (扫描 cooked 顶点流)"""
        pkg = self.package_of(stem)
        e = max((x for x in pkg.exports if x.serial_size > 0), key=lambda x: x.serial_size, default=None)
        if e is None:
            return None
        d = pkg.export_reader(e).d
        r = Reader(d)
        try:
            pkg.read_properties(r)
        except Exception:
            pass
        start_scan = r.p
        streams = []

        def f32_ok(p, n):
            for i in range(n):
                x, y, z = struct.unpack_from("<3f", d, p + i * 12)
                for v in (x, y, z):
                    if not (-1e5 < v < 1e5) or v != v:
                        return False
            return True

        def half_ok(p, n):
            # FP16 xyz + 2B 填充 (stride 8)
            for i in range(n):
                a, b, c = struct.unpack_from("<3H", d, p + i * 8)
                for h in (a, b, c):
                    if h >= 0x7C00:
                        return False
            return True

        p = start_scan
        while p + 8 < len(d) - 12 and len(streams) < 6:
            stride, cnt = struct.unpack_from("<2I", d, p)
            if stride == 12 and 4 <= cnt <= 200000 and p + 8 + cnt * 12 <= len(d) and f32_ok(p + 8, cnt):
                pts = struct.unpack_from(f"<{cnt*3}f", d, p + 8)
                streams.append((cnt, pts))
                p = p + 8 + cnt * 12
                continue
            if stride == 8 and 8 <= cnt <= 200000 and p + 8 + cnt * 8 <= len(d) and half_ok(p + 8, cnt):
                pts = []
                ok = True
                for i in range(cnt):
                    a, b, c = struct.unpack_from("<3H", d, p + 8 + i * 8)
                    for h, sh in ((a, 0), (b, 1), (c, 2)):
                        s = -(h >> 15) & 1
                        e5 = (h >> 10) & 0x1F
                        m = h & 0x3FF
                        if e5 == 0:
                            v = m * 2.0 ** -24
                        else:
                            v = (1 + m / 1024.0) * 2.0 ** (e5 - 15)
                        pts.append(-v if s else v)
                streams.append((cnt, tuple(pts)))
                p = p + 8 + cnt * 8
                continue
            p += 1
        if not streams:
            return None
        streams.sort(key=lambda s: -s[0])
        total = 0
        pts_out = []
        for cnt, pts in streams:
            take = min(cnt, max_points - total)
            if take <= 0:
                break
            pts_out.extend(pts[:take * 3])
            total += take
        xs = pts_out[0::3]; ys = pts_out[1::3]; zs = pts_out[2::3]
        return dict(
            n=total,
            lods=[c for c, _ in streams],
            bbox=[min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)],
            pts=[round(v, 3) for v in pts_out],
        )

    # ---------------------------------------------- 特效
    def _resolve_import(self, pkg, idx):
        """导入表项 → /Game/ 完整路径 (outer 链)"""
        if idx >= 0 or -idx > len(pkg.imports):
            return None
        imp = pkg.imports[-idx - 1]
        cp, cn, outer, nm, num = imp
        if cn == "Package":
            return nm  # 包导入本身就是路径
        if outer < 0 and -outer <= len(pkg.imports):
            base = self._resolve_import(pkg, outer)
            if base:
                return base.rstrip("/") + "/" + nm
        return nm

    def _textures_from_package_hints(self, pkg):
        """材质包的 Package 导入常指向纹理路径 (商用特效包模式), 尝试加载"""
        out = []
        for i, (cp, cn, outer, nm, num) in enumerate(pkg.imports, 1):
            if cn != "Package":
                continue
            full = nm
            if not full or not full.startswith("/Game/"):
                continue
            pp = self.game_path_to_pak(full)
            if (pp + ".uasset") in self.entries:
                try:
                    tp = self.package_of(pp)
                    if self.export_class(tp) == "Texture2D":
                        ti = self.texture_info(pp)
                        if ti and ti.get("mip"):
                            out.append(pp)
                except Exception:
                    pass
        return out

    def _textures_of_material(self, path, depth=0, seen=None):
        """MIC/Material → 其引用的 Texture2D pak 路径"""
        if seen is None:
            seen = set()
        if depth > 2 or path in seen:
            return []
        seen.add(path)
        pak_path = self.game_path_to_pak(path)
        if (pak_path + ".uasset") not in self.entries:
            # 对象路径 → 包路径 (去掉末尾对象名)
            pak_path = pak_path.rsplit("/", 1)[0]
            if (pak_path + ".uasset") not in self.entries:
                return []
        try:
            pkg = self.package_of(pak_path)
        except Exception:
            return []
        out = []
        for i, (cp, cn, outer, nm, num) in enumerate(pkg.imports, 1):
            full = self._resolve_import(pkg, -i)
            if cn == "Texture2D" and full and full.startswith("/Game/"):
                out.append(self.game_path_to_pak(full))
            elif cn in ("MaterialInstanceConstant", "Material") and full and full.startswith("/Game/"):
                out.extend(self._textures_of_material(full, depth + 1, seen))
        # 商用特效包: 材质的 Package 导入即纹理引用
        if not out:
            out.extend(self._textures_from_package_hints(pkg))
        return out

    def particle_info(self, stem: str):
        """Cascade ParticleSystem → 发射器/模块/纹理"""
        pkg = self.package_of(stem)
        emitters = []
        modules = set()
        for e in pkg.exports:
            if e.name.startswith("ParticleSpriteEmitter"):
                emitters.append(e.name)
            elif e.name.startswith("ParticleModule"):
                modules.add(e.name.rsplit("_", 1)[0])
        # 收集材质 → 纹理
        mats = []
        for i, (cp, cn, outer, nm, num) in enumerate(pkg.imports, 1):
            full = self._resolve_import(pkg, -i)
            if cn in ("MaterialInstanceConstant", "Material") and full and full.startswith("/Game/"):
                mats.append(full)
        texs = []
        seen = set()
        for m in mats[:6]:
            for t in self._textures_of_material(m):
                if t not in seen:
                    seen.add(t)
                    texs.append(t)
        # PS 直接导入的纹理
        for i, (cp, cn, outer, nm, num) in enumerate(pkg.imports, 1):
            full = self._resolve_import(pkg, -i)
            if cn == "Texture2D" and full and full.startswith("/Game/"):
                t = self.game_path_to_pak(full)
                if t not in seen:
                    seen.add(t)
                    texs.append(t)
        # 回退: 材质链无纹理时, 沿目录树上溯找纹理目录 (商用特效包 2_Material/3_Texture 结构)
        if not texs:
            dirp = stem.rsplit("/", 1)[0]
            dirs = dirp.split("/")
            found_base = None
            for up in range(1, min(5, len(dirs))):
                base = "/".join(dirs[:len(dirs) - up])
                if not base:
                    break
                for texdir in ("3_Texture", "Texture", "Textures", "texture"):
                    tp = base + "/" + texdir
                    if any(s.startswith(tp + "/") for s in self.groups):
                        found_base = tp
                        break
                if found_base:
                    break
            if found_base:
                for cand in sorted(self.groups):
                    if cand.startswith(found_base + "/") and (cand + ".uasset") in self.entries:
                        try:
                            ti = self.texture_info(cand)
                            if ti and ti.get("mip") and ti["w"] >= 64:
                                texs.append(cand)
                        except Exception:
                            pass
                        if len(texs) >= 4:
                            break

        # 路径规范化 (对象路径 → 包路径) + 过滤有效纹理
        valid = []
        vseen = set()
        for t in texs:
            if (t + ".uasset") not in self.entries:
                t = t.rsplit("/", 1)[0]
            if t in vseen or (t + ".uasset") not in self.entries:
                continue
            try:
                ti = self.texture_info(t)
                if ti and ti.get("mip"):
                    vseen.add(t)
                    valid.append(t)
            except Exception:
                pass
            if len(valid) >= 8:
                break
        return dict(emitters=len(emitters), modules=sorted(modules), textures=valid)

    # ---------------------------------------------- 精灵序列动画
    def sprite_sequence(self, stem: str):
        """PG_Sprite_14 → 全序列 [(帧号, 精灵stem)]; 无序列返回 None"""
        import re
        dirp = stem.rsplit("/", 1)[0]
        name = stem.rsplit("/", 1)[-1]
        m = re.match(r"^(.*?)(?:_(\d+))$", name)
        if not m:
            return None
        base = m.group(1)
        first = int(m.group(2))
        siblings = []
        for s in self.groups:
            nm = s.rsplit("/", 1)[-1]
            if not s.startswith(dirp + "/"):
                continue
            mm = re.match(rf"^{re.escape(base)}_(\d+)$", nm)
            if mm and (s + ".uasset") in self.entries:
                siblings.append((int(mm.group(1)), s))
        if len(siblings) < 2:
            return None
        siblings.sort()
        return base, siblings

    def sprite_seq_frames(self, stem: str, max_frames=60):
        """精灵序列 → [(帧号, uv, dim, tex_stem)]"""
        seq = self.sprite_sequence(stem)
        if not seq:
            return None
        base, siblings = seq
        frames = []
        for num, s in siblings[:max_frames]:
            sp = self.sprite_info(s)
            if sp and sp.get("tex"):
                frames.append(dict(n=num, x=int(sp["uv"][0]), y=int(sp["uv"][1]),
                                   w=int(sp["dim"][0]), h=int(sp["dim"][1]),
                                   tex=sp["tex"], name=s.rsplit("/", 1)[-1]))
        return frames if frames else None

    def gif_from_frames(self, frame_rects, fps=10, max_size=512):
        """帧矩形列表 → GIF bytes (Pillow)"""
        from PIL import Image
        from texdec import decode_texture
        images = []
        # 同一图集只解码一次
        atlas_cache = {}
        max_w = max_h = 0
        for fr in frame_rects:
            tex_stem = fr["tex"]
            if tex_stem not in atlas_cache:
                t = self.texture_info(tex_stem)
                if not t or not t.get("mip"):
                    continue
                rgba = decode_texture(t["mip"]["data"], t["w"], t["h"], t["fmt"])
                atlas_cache[tex_stem] = (t["w"], t["h"], rgba)
            aw, ah, rgba = atlas_cache[tex_stem]
            x, y, w, h = fr["x"], fr["y"], fr["w"], fr["h"]
            # UV 坐标系: 左下原点 → 裁剪时翻转 Y
            cy = ah - y - h
            cy = max(0, min(cy, ah - 1))
            h2 = min(h, ah - cy)
            w2 = min(w, aw - x)
            img = Image.new("RGBA", (w2, h2), (0, 0, 0, 0))
            for row in range(h2):
                src_off = ((cy + row) * aw + x) * 4
                dst_off = row * w2 * 4
                img.frombytes  # noop
            # 用 frombytes 更快
            crop_data = bytearray()
            for row in range(h2):
                src_off = ((cy + row) * aw + x) * 4
                crop_data += rgba[src_off:src_off + w2 * 4]
            img = Image.frombytes("RGBA", (w2, h2), bytes(crop_data))
            images.append(img)
            max_w = max(max_w, w2)
            max_h = max(max_h, h2)
        if not images:
            return None
        # 缩放限制
        scale = min(1.0, max_size / max(max_w, max_h))
        if scale < 1.0:
            images = [im.resize((int(im.width * scale), int(im.height * scale)), Image.NEAREST) for im in images]
            max_w = images[0].width
            max_h = images[0].height
        # 统一画布 (底部对齐)
        canvas_list = []
        for im in images:
            c = Image.new("RGBA", (max_w, max_h), (0, 0, 0, 0))
            c.paste(im, ((max_w - im.width) // 2, max_h - im.height))
            canvas_list.append(c)
        # 编码 GIF
        buf = io.BytesIO()
        duration_ms = int(1000 / fps)
        canvas_list[0].save(buf, format="GIF", save_all=True,
                           append_images=canvas_list[1:], duration=duration_ms,
                           loop=0, transparency=0, disposal=2)
        return buf.getvalue()
