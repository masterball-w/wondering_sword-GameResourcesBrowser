# -*- coding: utf-8 -*-
"""
逸剑风云决 .uasset/.uexp 解析器 (自定义 cooked 包格式, 实测逆向)

包布局:
  uasset:
    @0  u32 Tag 0x9E2A83C1
    @4  i32 LegacyFileVersion (-7)
    @8  i32 FileVersionUE4 (0, 被剥离)
    @12 i32 LicenseeVersion (0)
    @16 i32 CustomVersions 数 (0)
    @20 i32 0
    @24 i32 TotalHeaderSize (=uasset 文件大小)
    @28 FString "None" (引擎版本占位)
    ... 摘要尾部(含各表偏移, 位置随 FString 变化, 用启发式定位)
    名字表: [i32 len][str\0][u32 hash] × N (排序)
    导入表: [FName cp(8)][FName cn(8)][i32 outer][FName name(8)] = 28B/条
    导出表: [i32 class][i32 super][i32 tmpl][i32 outer][FName(8)][u32 flags]
            [i64 SerialSize][i64 SerialOffset][尾部若干 i32] ≈ 104B/条
    FName = [i32 名字索引][i32 哈希(忽略)]
  uexp: 各导出对象的序列化数据 (SerialOffset - TotalHeaderSize)

Texture2D cooked 平台数据(紧跟 tagged properties "None" 之后):
  [i32? bCooked/序号][i32 SizeX][i32 SizeY][FString PixelFormat][i32? ...]
  mip: [u32 bulkFlags][i32 elemCount][i32 sizeOnDisk][i64 offsetInFile] + 数据/引用
"""
from __future__ import annotations
import struct

PKG_TAG = 0x9E2A83C1


class Reader:
    __slots__ = ("d", "p")

    def __init__(self, data: bytes, p: int = 0):
        self.d = data
        self.p = p

    def i8(self):
        v = struct.unpack_from("<b", self.d, self.p)[0]; self.p += 1; return v

    def u8(self):
        v = self.d[self.p]; self.p += 1; return v

    def i16(self):
        v = struct.unpack_from("<h", self.d, self.p)[0]; self.p += 2; return v

    def u16(self):
        v = struct.unpack_from("<H", self.d, self.p)[0]; self.p += 2; return v

    def i32(self):
        v = struct.unpack_from("<i", self.d, self.p)[0]; self.p += 4; return v

    def u32(self):
        v = struct.unpack_from("<I", self.d, self.p)[0]; self.p += 4; return v

    def i64(self):
        v = struct.unpack_from("<q", self.d, self.p)[0]; self.p += 8; return v

    def u64(self):
        v = struct.unpack_from("<Q", self.d, self.p)[0]; self.p += 8; return v

    def f32(self):
        v = struct.unpack_from("<f", self.d, self.p)[0]; self.p += 4; return v

    def raw(self, n):
        v = self.d[self.p:self.p + n]
        self.p += n
        return v

    def fstring(self) -> str:
        n = self.i32()
        if n == 0:
            return ""
        if n < 0:
            s = self.raw(-n * 2).decode("utf-16-le", "replace")
            return s.rstrip("\x00")
        return self.raw(n).decode("utf-8", "replace").rstrip("\x00")


class Export:
    __slots__ = ("class_index", "super_index", "outer_index", "name", "flags",
                 "serial_size", "serial_offset")

    def __init__(self):
        self.class_index = 0
        self.super_index = 0
        self.outer_index = 0
        self.name = ""
        self.flags = 0
        self.serial_size = 0
        self.serial_offset = 0


class Package:
    """一个 .uasset(+.uexp+.ubulk) 资产包"""

    def __init__(self, uasset: bytes, uexp: bytes = b"", ubulk: bytes = b""):
        self.uasset = uasset
        self.uexp = uexp
        self.ubulk = ubulk
        self.names = []
        self.imports = []     # (class_package, class_name, outer, object_name)
        self.exports = []
        self.total_header_size = 0
        self._parse()

    # ------------------------------------------------ 摘要/表
    def _parse(self):
        d = self.uasset
        if len(d) < 64 or struct.unpack_from("<I", d, 0)[0] != PKG_TAG:
            raise ValueError("非 uasset 包")
        self.total_header_size = struct.unpack_from("<i", d, 24)[0]
        # 名字表: 逐字节起点扫描得到候选运行(允许含空格名字), 按长度优先逐个尝试
        runs = self._find_name_table(d)
        if not runs:
            raise ValueError("名字表定位失败")
        exp_pos = None
        chosen = None
        for n_start, names, n_end in runs:
            ep = self._find_export_table(d, n_end, names)
            if ep is not None:
                exp_pos = ep
                chosen = (n_start, names, n_end)
                break
        if exp_pos is None:
            for n_start, names, n_end in runs:
                ep = self._find_export_table(d, n_end)
                if ep is not None:
                    exp_pos = ep
                    chosen = (n_start, names, n_end)
                    break
        if exp_pos is None:
            n_start, names, n_end = runs[0]
            self.names = names
            self._name_start = n_start
            self._name_end = n_end
            raise ValueError("导出表定位失败")
        n_start, names, n_end = chosen
        self.names = names
        self._name_start = n_start
        self._name_end = n_end
        self._export_start = exp_pos

        # 导入表: 名字表尾 .. 导出表头
        imp_len = exp_pos - n_end
        stride = None
        for s in (28, 32, 36, 40):
            if imp_len > 0 and imp_len % s == 0:
                stride = s
                break
        if stride is None:
            stride = 28
        p = n_end
        while p + stride <= exp_pos:
            r = Reader(d, p)
            cp = self._fname_n(r)
            cn = self._fname_n(r)
            outer = r.i32()
            nm = self._fname_n(r)
            self.imports.append((cp[0], cn[0], outer, nm[0], nm[1]))
            p += stride

        # 导出表: 从已验证起点 exp_pos 开始扫描签名链 (避免把导入区误链进来)
        cands = []
        p = exp_pos
        ths = self.total_header_size
        limit = min(len(d), ths)
        while p + 52 <= limit:
            ci = struct.unpack_from("<i", d, p)[0]
            ni = struct.unpack_from("<i", d, p + 16)[0]
            ss, so = struct.unpack_from("<qq", d, p + 28)
            ok_off = (ths <= so <= ths + len(self.uexp) + 64
                      or (0 < so < ths and 8 <= ss and so + ss <= ths))
            if (-2000 <= ci <= 2000 and 0 <= ni < len(names) and names[ni] != "None"
                    and 0 < ss <= len(self.uexp) + len(d) and ok_off):
                cands.append(p)
            p += 4
        if not cands:
            raise ValueError("导出表定位失败")
        # 等步长最长链
        best_run = [cands[0]]
        for i in range(len(cands)):
            for j in range(i + 1, min(i + 6, len(cands))):
                stride = cands[j] - cands[i]
                if stride < 44 or stride > 256:
                    continue
                run = [cands[i]]
                q = cands[j]
                while q in cands_set if False else False:
                    pass
                # 用集合加速
                cs = set(cands)
                run = [cands[i]]
                q = cands[i] + stride
                while q in cs:
                    run.append(q)
                    q += stride
                if len(run) > len(best_run):
                    best_run = run
        for p in best_run:
            r = Reader(d, p)
            e = Export()
            e.class_index = r.i32()
            e.super_index = r.i32()
            r.i32()   # template
            e.outer_index = r.i32()
            e.name = self._fname(r)
            e.flags = r.u32()
            e.serial_size = r.i64()
            e.serial_offset = r.i64()
            self.exports.append(e)
        self._export_start = best_run[0]

    def _find_name_table(self, d):
        """返回候选运行列表 [(起点, 名字列表, 结束点)] 按长度降序; 名字允许空格"""
        n = len(d)
        limit = min(n, 16384)

        def entry_at(p):
            ln = struct.unpack_from("<i", d, p)[0]
            if ln > 0:
                if ln > 300:
                    return None
                s = d[p + 4:p + 4 + ln - 1]
                if not s or b"\x00" in s:
                    return None
                try:
                    t = s.decode("utf-8")
                except UnicodeDecodeError:
                    return None
                if not t or any(ord(c) < 0x20 or ord(c) == 0x7f for c in t):
                    return None
                return t, 4 + ln + 4
            if ln < 0:
                cnt = -ln
                if cnt > 300:
                    return None
                raw = d[p + 4:p + 4 + cnt * 2]
                if len(raw) < cnt * 2 or raw[-2:] != b"\x00\x00":
                    return None
                t = raw.decode("utf-16-le", "replace").rstrip("\x00")
                if not t or any(ord(c) < 0x20 or ord(c) == 0x7f for c in t):
                    return None
                return t, 4 + cnt * 2 + 4
            return None

        runs = []
        for start in range(28, limit):
            run = []
            p = start
            while p + 8 <= n:
                r = entry_at(p)
                if r is None:
                    break
                run.append(r[0])
                p += r[1]
            if len(run) >= 4:
                runs.append((start, run, p))
        runs.sort(key=lambda x: -(x[2] - x[0]))
        return runs[:6] or None

    def _imports_valid(self, d, names, n_end, exp_pos) -> bool:
        """导入表区域 (n_end..exp_pos) 按 28~40B 步长切分后, 所有 FName 索引必须合法"""
        imp_len = exp_pos - n_end
        if imp_len <= 0:
            return True
        N = len(names)
        for stride in (28, 32, 36, 40):
            if imp_len % stride != 0:
                continue
            ok = 0
            total = imp_len // stride
            for k in range(total):
                p = n_end + k * stride
                good = True
                for fo in (0, 8, 20):
                    idx = struct.unpack_from("<i", d, p + fo)[0]
                    if not (0 <= idx < N):
                        good = False
                        break
                if good:
                    ok += 1
            if total > 0 and ok == total:
                return True
        return False

    def _find_export_table(self, d, from_pos, names=None):
        """找导出链起点: ≥3 条等距(44..256B)签名, 且前方区域可解析为合法导入"""
        ths = self.total_header_size
        limit = min(len(d), ths)
        cands = []
        p = from_pos
        while p + 52 <= limit:
            ss, so = struct.unpack_from("<qq", d, p + 28)
            ci = struct.unpack_from("<i", d, p)[0]
            ni = struct.unpack_from("<i", d, p + 16)[0]
            ok_off = (ths <= so <= ths + len(self.uexp) + 16
                      or (0 < so < ths and 8 <= ss and so + ss <= ths))
            if (0 < ss and ok_off and -2000 <= ci <= 2000
                    and names is not None and 0 <= ni < len(names) and names[ni] != "None"):
                cands.append(p)
            p += 4
        if not cands:
            return None
        cs = set(cands)
        chains = []
        for i in range(len(cands)):
            for j in range(i + 1, min(i + 8, len(cands))):
                stride = cands[j] - cands[i]
                if stride < 44 or stride > 256:
                    continue
                run = 2
                q = cands[j] + stride
                while q in cs:
                    run += 1
                    q += stride
                chains.append((run, cands[i]))
        chains.sort(key=lambda x: -x[0])
        for run, ep in chains:
            imp_len = ep - from_pos
            if imp_len == 0 or any(imp_len % s == 0 for s in (28, 32, 36, 40)):
                if names is None or self._imports_valid(d, names, from_pos, ep):
                    return ep
        # 无链时回退: 逐候选尝试 (单导出包等)
        for ep in cands:
            imp_len = ep - from_pos
            if imp_len == 0 or any(imp_len % s == 0 for s in (28, 32, 36, 40)):
                if names is None or self._imports_valid(d, names, from_pos, ep):
                    return ep
        return None

    def _fname(self, r: Reader) -> str:
        idx = r.i32()
        num = r.i32()
        base = self.names[idx] if 0 <= idx < len(self.names) else f"~{idx}"
        return base if num == 0 else f"{base}_{num - 1}"

    def _fname_n(self, r: Reader):
        idx = r.i32()
        num = r.i32()
        base = self.names[idx] if 0 <= idx < len(self.names) else f"~{idx}"
        return base, num

    # ------------------------------------------------ 属性
    def export_reader(self, e: Export) -> Reader:
        """导出对象的属性数据 Reader (跨 uasset/uexp 的包坐标系)"""
        off = e.serial_offset
        size = e.serial_size
        if off >= self.total_header_size:
            # 在 uexp 内
            p = off - self.total_header_size
            blob = self.uexp[p:p + size] if p + size <= len(self.uexp) else self.uexp[p:]
            return Reader(blob)
        blob = self.uasset[off:off + size]
        return Reader(blob)

    def read_properties(self, r: Reader, stop_on_none=True, max_iter=256):
        """解析 tagged properties 到 "None"; 返回 (dict, 到达None?) —— 实测规则:
        头 = FName名(8)+FName类型(8)+i32 size+i32 arr
        标签区: Struct=FName结构名(8)+16B guid; Byte/Enum/Array=FName(8); Map=2×FName(16)
        值 = size 字节 (Bool 为 1 字节); 之后恒定 1 字节尾部(HasGuid标志, 忽略)
        """
        props = {}
        reached_none = False
        for _ in range(max_iter):
            idx = r.i32(); r.i32()   # 名字 FName
            if 0 <= idx < len(self.names):
                name = self.names[idx]
            else:
                name = f"~{idx}"
                return props, False
            if name == "None":
                reached_none = True
                if stop_on_none:
                    return props, True
                continue
            tidx = r.i32(); r.i32()
            if not (0 <= tidx < len(self.names)):
                return props, False
            ptype = self.names[tidx]
            size = r.i32()
            r.i32()   # array index
            # 标签区
            tagdata = {}
            if ptype == "StructProperty":
                si = r.i32(); r.i32()
                tagdata["struct"] = self.names[si] if 0 <= si < len(self.names) else str(si)
                r.raw(16)   # guid
            elif ptype in ("ByteProperty", "EnumProperty"):
                ei = r.i32(); r.i32()
                tagdata["enum"] = self.names[ei] if 0 <= ei < len(self.names) else str(ei)
            elif ptype == "ArrayProperty":
                ii = r.i32(); r.i32()
                tagdata["inner"] = self.names[ii] if 0 <= ii < len(self.names) else str(ii)
            elif ptype == "MapProperty":
                ki = r.i32(); r.i32()
                vi = r.i32(); r.i32()
                tagdata["key"] = self.names[ki] if 0 <= ki < len(self.names) else str(ki)
                tagdata["val"] = self.names[vi] if 0 <= vi < len(self.names) else str(vi)
            # 值 (实测: 非 bool 属性值前恒有 1 个前导字节; bool 值在 header 末尾, 后跟 1 字节)
            try:
                if ptype == "BoolProperty":
                    value = r.u8() != 0
                    r.raw(1)
                else:
                    r.raw(1)
                    value = self._read_value(r, ptype, size, tagdata)
            except Exception:
                r.p = value_end_backup = r.p
                value = None
            props[name] = (ptype, value, tagdata)
        return props, reached_none

    def _read_value(self, r, ptype, size, tagdata):
        if ptype == "BoolProperty":
            return r.u8() != 0
        if ptype == "IntProperty":
            return r.i32()
        if ptype == "UInt32Property":
            return r.u32()
        if ptype == "Int64Property":
            return r.i64()
        if ptype == "FloatProperty":
            return r.f32()
        if ptype == "DoubleProperty":
            return struct.unpack("<d", r.raw(8))[0]
        if ptype == "NameProperty":
            i = r.i32(); r.i32()
            return self.names[i] if 0 <= i < len(self.names) else f"~{i}"
        if ptype == "StrProperty":
            return r.fstring()
        if ptype in ("ObjectProperty", "WeakObjectProperty", "LazyObjectProperty", "InterfaceProperty"):
            return r.i32()
        if ptype == "SoftObjectProperty":
            self._fname(r)          # 引用 FName
            return r.fstring()      # 资产路径
        if ptype == "ByteProperty":
            v = r.raw(size)
            n = int.from_bytes(v, "little") if len(v) <= 8 else None
            return (tagdata.get("enum"), n)
        if ptype == "EnumProperty":
            i = r.i32(); r.i32()
            return self.names[i] if 0 <= i < len(self.names) else f"~{i}"
        if ptype == "StructProperty":
            return self._read_struct(r, tagdata.get("struct", ""), size)
        if ptype == "ArrayProperty":
            return self._read_array(r, tagdata.get("inner", ""), size)
        if ptype == "MapProperty":
            count = r.i32()
            return ("map", count, r.p + size - r.p)
        return r.raw(size)

    def _read_prop_value(self, r, ptype, size):
        if ptype == "BoolProperty":
            return r.i32() != 0
        if ptype == "IntProperty":
            return r.i32()
        if ptype == "FloatProperty":
            return r.f32()
        if ptype == "UInt32Property":
            return r.u32()
        if ptype == "Int64Property":
            return r.i64()
        if ptype == "DoubleProperty":
            return struct.unpack("<d", r.raw(8))[0]
        if ptype == "NameProperty" or ptype == "EnumProperty":
            if ptype == "EnumProperty":
                return self._fname(r)   # enum type 名被跳过? 见调用处
            return self._fname(r)
        if ptype == "StrProperty":
            return r.fstring()
        if ptype == "ObjectProperty" or ptype == "WeakObjectProperty" or ptype == "LazyObjectProperty":
            return r.i32()
        if ptype == "InterfaceProperty":
            r.i32()
            return r.i32()
        if ptype == "ByteProperty":
            # enum 名 FName + u8 值 (若 size==8 → 枚举名引用)
            if size >= 8:
                enum_name = self._fname(r)
                val = r.raw(size - 8)
                return (enum_name, int.from_bytes(val, "little") if len(val) <= 8 else None)
            return int.from_bytes(r.raw(size), "little")
        if ptype == "StructProperty":
            r.raw(16)   # struct guid
            return self._read_struct(r, size - 16)
        if ptype == "ArrayProperty":
            inner = self._fname(r)
            count = r.i32()
            return self._read_array(r, inner, count, size)
        if ptype == "MapProperty":
            key_t = self._fname(r)
            val_t = self._fname(r)
            count = r.i32()
            return ("map", key_t, val_t, count)
        if ptype == "SetProperty":
            r.i32()   # element count 零时省略
            return ("set",)
        return r.raw(size)

    def _read_struct(self, r, sname, size):
        end = r.p + size
        try:
            if sname == "IntPoint":
                a = r.i32(); b = r.i32()
                return ("IntPoint", a, b)
            if sname == "Vector2D":
                return ("Vec2", r.f32(), r.f32())
            if sname == "Guid":
                return ("Guid", r.raw(16).hex())
            if sname in ("Color", "LinearColor"):
                if size == 16:
                    return ("LinearColor", r.f32(), r.f32(), r.f32(), r.f32())
                return ("Color", r.raw(4).hex())
            if sname == "Box2D" or sname == "Box":
                return (sname, r.raw(size).hex())
        except Exception:
            pass
        r.p = end
        return ("raw", size)

    def _read_array(self, r, inner, size):
        end = r.p + size
        count = r.i32()
        out = None
        try:
            if inner == "StructProperty":
                items = []
                for _ in range(count):
                    sub, ok = self.read_properties(r, stop_on_none=True, max_iter=64)
                    items.append(sub)
                out = items
            elif inner == "IntProperty":
                out = [r.i32() for _ in range(count)]
            elif inner == "FloatProperty":
                out = [r.f32() for _ in range(count)]
            elif inner == "ByteProperty":
                out = r.raw(count)
            elif inner == "ObjectProperty":
                out = [r.i32() for _ in range(count)]
            elif inner == "NameProperty":
                out = []
                for _ in range(count):
                    i = r.i32(); r.i32()
                    out.append(self.names[i] if 0 <= i < len(self.names) else f"~{i}")
            elif inner == "StrProperty":
                out = [r.fstring() for _ in range(count)]
        except Exception:
            out = None
        r.p = end
        return out if out is not None else ("array", inner, count)
