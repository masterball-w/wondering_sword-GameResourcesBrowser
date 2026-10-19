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
