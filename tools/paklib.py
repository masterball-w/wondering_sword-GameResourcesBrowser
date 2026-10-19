# -*- coding: utf-8 -*-
"""
逸剑风云决 (Wandering Sword) UE pak v11 自定义格式 解析/写入库

文件布局 (实测逆向):
  [数据区: FPakEntry头+数据, 按条目偏移排列]
  [主索引]
  [PHI 区域: u32 count + count×(u64 哈希, u32 编码区偏移) + 冗余目录子表]
  [FDI 区域: u32 目录数 + 目录树(文件→编码区偏移)]
  [Footer 204B]

Footer: magic 0x5A6F12E1 | ver u32=11 | idxoff u64 | idxsize u64 | sha1 20B | methods 5×32B("Zlib")

主索引:
  FString 挂载点 ("../../../")
  u32 文件数
  u64 路径哈希种子
  u32 有PHI=1 + (u64 off, u64 size, 20B sha1)
  u32 有FDI=1 + (u64 off, u64 size, 20B sha1)
  u32 编码区字节数
  编码记录 × N:
    u32 bits: [31]off32 [30]unc32 [29]size32 [28..23]method [22]encrypted
              [21..6]块数 [5]有块数组 [4..0]元数据(unc/2048, 读取时忽略)
    off (u32/u64), unc (u32/u64),
    size (u32/u64, method=0 时省略),
    块数组 nb×u32 (bit5 且 nb>1 时),
    16B guid (encrypted 时)
  u32 兜底数 = 0

数据区 FPakEntry 头 (v11 自校验):
  u64 保留(0) | u64 size | u64 unc | u32 method | 20B sha1(压缩后数据)
  [method!=0: i32 nb + nb×(u64 start, u64 end)] | u32 blocksize(=unc*256 的怪值) | 1字节填充
  块偏移相对条目起点; 数据紧跟在头之后

FDI: u32 ndir + 每目录(FString 目录名(以/结尾), u32 nfile,
     每文件(FString 名(负长=UTF-16), u32 编码偏移))
"""
from __future__ import annotations
import hashlib
import os
import struct
import zlib
from dataclasses import dataclass, field

PAK_MAGIC = 0x5A6F12E1
FOOTER_SIZE = 204


def fstring_bytes(s: str) -> bytes:
    """序列化为 pak 内 FString; 含非 ASCII 时用 UTF-16(负长度)"""
    try:
        b = s.encode("ascii")
        return struct.pack("<i", len(b) + 1) + b + b"\x00"
    except UnicodeEncodeError:
        u = s.encode("utf-16-le") + b"\x00\x00"
        return struct.pack("<i", -(len(u) // 2)) + u


def read_fstring(buf: bytes, p: int):
    n, = struct.unpack_from("<i", buf, p)
    p += 4
    if n == 0:
        return "", p
    if n < 0:
        s = buf[p:p + (-n) * 2].decode("utf-16-le", "replace").rstrip("\x00")
        return s, p + (-n) * 2
    s = buf[p:p + n].decode("utf-8", "replace").rstrip("\x00")
    return s, p + n


@dataclass
class Entry:
    """一个文件条目"""
    offset: int = 0            # 数据区内条目头偏移
    size: int = 0              # 压缩大小
    uncompressed_size: int = 0
    compression: int = 0       # 0=none 1=zlib
    sha1: bytes = b""          # 未压缩内容 SHA1
    blocks: list = field(default_factory=list)   # [(start,end)] 相对条目起点
    block_size: int = 0
    path: str = ""             # pak 内相对路径 dir/name
    block_count: int = 0       # 压缩块数

    @property
    def header_size(self) -> int:
        n = 8 + 8 + 8 + 4 + 20
        if self.compression:
            n += 4 + 16 * max(self.block_count, 1)
        n += 4 + 1
        return n


class Pak:
    """读取/解析 pak"""

    def __init__(self, path: str):
        self.path = path
        self.f = open(path, "rb")
        self.size = os.fstat(self.f.fileno()).st_size
        self._footer()
        self._primary()
        self._phi()
        self._fdi()

    def close(self):
        self.f.close()

    # ---------- footer
    def _footer(self):
        self.f.seek(self.size - FOOTER_SIZE)
        t = self.f.read(FOOTER_SIZE)
        magic, ver = struct.unpack_from("<II", t, 0)
        if magic != PAK_MAGIC:
            raise ValueError(f"非本工具支持的 pak (magic={magic:#x})")
        self.version = ver
        self.index_offset, self.index_size = struct.unpack_from("<QQ", t, 8)
        self.index_hash = t[24:44]
        self.compression_methods = []
        for i in range(5):
            nm = t[44 + i * 32:44 + (i + 1) * 32].split(b"\x00")[0].decode("ascii", "replace")
            if nm:
                self.compression_methods.append(nm)

    # ---------- 主索引 + 编码记录
    @staticmethod
    def _read_var(buf, p, is32):
        if is32:
            return struct.unpack_from("<I", buf, p)[0], p + 4
        return struct.unpack_from("<Q", buf, p)[0], p + 8

    def _primary(self):
        self.f.seek(self.index_offset)
        buf = self.f.read(self.index_size)
        if hashlib.sha1(buf).digest() != self.index_hash:
            raise ValueError("主索引 SHA1 校验失败")
        p = 0
        self.mount_point, p = read_fstring(buf, p)
        self.num_files, = struct.unpack_from("<I", buf, p); p += 4
        self.path_hash_seed, = struct.unpack_from("<Q", buf, p); p += 8

        self.phi_offset = self.phi_size = 0
        if struct.unpack_from("<I", buf, p)[0]:
            p += 4
            self.phi_offset, self.phi_size = struct.unpack_from("<QQ", buf, p); p += 16
            self.phi_hash = buf[p:p + 20]; p += 20
        else:
            p += 4

        self.fdi_offset = self.fdi_size = 0
        if struct.unpack_from("<I", buf, p)[0]:
            p += 4
            self.fdi_offset, self.fdi_size = struct.unpack_from("<QQ", buf, p); p += 16
            self.fdi_hash = buf[p:p + 20]; p += 20
        else:
            p += 4

        enc_size, = struct.unpack_from("<I", buf, p); p += 4
        enc_start = p
        enc_end = enc_start + enc_size
        self.entries = []          # Entry 列表(按编码顺序)
        self.enc_offsets = []      # 每条记录在编码区内的字节偏移
        while p < enc_end:
            bits, = struct.unpack_from("<I", buf, p); p += 4
            off32 = (bits >> 31) & 1
            unc32 = (bits >> 30) & 1
            size32 = (bits >> 29) & 1
            method = (bits >> 23) & 0x3F
            encrypted = (bits >> 22) & 1
            nb = (bits >> 6) & 0xFFFF
            has_blocks = (bits >> 5) & 1
            e = Entry()
            e.compression = method
            self.enc_offsets.append(p - 4 - enc_start)
            e.offset, p = self._read_var(buf, p, off32)
            e.uncompressed_size, p = self._read_var(buf, p, unc32)
            if method:
                e.size, p = self._read_var(buf, p, size32)
                e.block_count = nb
                if has_blocks and nb > 1:
                    p += nb * 4
            else:
                e.size = e.uncompressed_size
            if encrypted:
                p += 16
            self.entries.append(e)
        if p != enc_end:
            raise ValueError(f"编码区走查错位 {p} != {enc_end}")
        nfallback, = struct.unpack_from("<I", buf, p); p += 4
        if nfallback:
            raise ValueError("存在兜底文件列表, 未预期")
        if len(self.entries) != self.num_files:
            raise ValueError(f"条目数 {len(self.entries)} != {self.num_files}")

    # ---------- PHI
    def _phi(self):
        self.f.seek(self.phi_offset)
        buf = self.f.read(self.phi_size)
        if hashlib.sha1(buf).digest() != self.phi_hash:
            raise ValueError("PHI SHA1 校验失败")
        n, = struct.unpack_from("<I", buf, 0)
        self.path_hash_index = {}
        for i in range(n):
            h, v = struct.unpack_from("<QI", buf, 4 + i * 12)
            self.path_hash_index[h] = v
        p = 4 + n * 12
        self.phi_tail = buf[p:]    # 冗余目录子表(原样保留, 不参与解析)

    # ---------- FDI
    def _fdi(self):
        self.f.seek(self.fdi_offset)
        buf = self.f.read(self.fdi_size)
        if hashlib.sha1(buf).digest() != self.fdi_hash:
            raise ValueError("FDI SHA1 校验失败")
        ndir, = struct.unpack_from("<I", buf, 0)
        p = 4
        self.directory_index = {}
        nfiles = 0
        for _ in range(ndir):
            d, p = read_fstring(buf, p)
            nf, = struct.unpack_from("<i", buf, p); p += 4
            m = {}
            for _ in range(nf):
                fn, p = read_fstring(buf, p)
                v, = struct.unpack_from("<I", buf, p); p += 4
                m[fn] = v
                nfiles += 1
            self.directory_index[d] = m
        if p != self.fdi_size:
            raise ValueError("FDI 解析错位")
        self.total_files = nfiles

    # ---------- 高层 API
    def iter_files(self):
        """迭代 (路径, Entry); FDI 值 = 编码区偏移 -> 条目"""
        off2entry = dict(zip(self.enc_offsets, self.entries))
        for d, m in self.directory_index.items():
            for fn, v in m.items():
                e = off2entry.get(v)
                if e is None:
                    raise ValueError(f"FDI 偏移 {v} 无对应条目: {d}{fn}")
                e.path = (d + fn) if d else fn
                yield e

    def get_entry_by_path(self, path: str):
        for e in self.iter_files():
            if e.path == path:
                return e
        return None

    def read_entry(self, e: Entry) -> bytes:
        """读取条目数据(解压), 校验 SHA1"""
        self.f.seek(e.offset)
        need = e.header_size + e.size + 16
        hdr = self.f.read(need)
        size, = struct.unpack_from("<Q", hdr, 8)
        unc, = struct.unpack_from("<Q", hdr, 16)
        method, = struct.unpack_from("<I", hdr, 24)
        sha = hdr[28:48]
        p = 48
        blocks = []
        if method:
            nb, = struct.unpack_from("<i", hdr, p); p += 4
            for _ in range(nb):
                s, t = struct.unpack_from("<QQ", hdr, p); p += 16
                blocks.append((s, t))
            p += 4 + 1
        else:
            p += 4 + 1
        if method == 0:
            data = hdr[p:p + e.size]
            stored_payload = data
        else:
            out = bytearray()
            for (s, t) in blocks:
                out += zlib.decompress(hdr[s:t])
            data = bytes(out)
            stored_payload = b"".join(hdr[s:t] for (s, t) in blocks)
        if len(data) != e.uncompressed_size:
            raise ValueError(f"{e.path}: 解压尺寸不符 {len(data)} != {e.uncompressed_size}")
        if sha != hashlib.sha1(stored_payload).digest():
            raise ValueError(f"{e.path}: SHA1 校验失败")
        return data


# ================================================================ 写入
class PakWriter:
    """生成与原 pak 同布局的 pak 文件"""

    def __init__(self, mount_point="../../../"):
        self.mount_point = mount_point
        self.items = []          # [(path, data, phi_hash)]
        self.block_threshold = 64 * 1024

    def add(self, path: str, data: bytes, phi_hash: int = 0):
        """以未压缩内容添加条目(打包时压缩)"""
        self.items.append(dict(path=path.replace("\\", "/"), data=data, phi_hash=phi_hash))

    def add_raw(self, path: str, payload: bytes, e_size: int, unc: int, method: int,
                nb: int, block_sizes, phi_hash: int = 0):
        """以原 pak 的原始 payload(条目头+压缩数据)直接搬运, 不重新压缩"""
        self.items.append(dict(path=path.replace("\\", "/"), payload=payload,
                               e_size=e_size, unc=unc, method=method, nb=nb,
                               block_sizes=list(block_sizes), phi_hash=phi_hash, data=None))

    @staticmethod
    def _encode_record(enc_off, e_size, unc, method, nb, block_sizes):
        b = bytearray()
        off32 = 0 < enc_off < 0xFFFFFFFF
        unc32 = unc < 0xFFFFFFFF
        size32 = e_size < 0xFFFFFFFF
        meta = min(0x1F, max(0, (unc + 2047) // 2048 - 1))
        has_blocks = 1 if nb > 1 else 0
        bits = ((1 if off32 else 0) << 31) | ((1 if unc32 else 0) << 30) | \
               ((1 if size32 else 0) << 29) | (method << 23) | \
               (nb << 6) | (has_blocks << 5) | meta
        b += struct.pack("<I", bits)
        b += struct.pack("<I" if off32 else "<Q", enc_off)
        b += struct.pack("<I" if unc32 else "<Q", unc)
        if method:
            b += struct.pack("<I" if size32 else "<Q", e_size)
            if has_blocks:
                for s in block_sizes:
                    b += struct.pack("<I", s)
        return bytes(b)

    def write(self, out_path: str, compress=True, progress=None):
        out = open(out_path, "wb")
        try:
            records = bytearray()
            phi_pairs = []
            fdi_dirs = {}
            total = len(self.items)
            for idx, item in enumerate(self.items):
                path = item["path"]
                if item.get("data") is None:
                    # 原样搬运
                    payload = item["payload"]
                    e_size = item["e_size"]
                    unc = item["unc"]
                    method = item["method"]
                    e_nb = item["nb"]
                    block_sizes = item["block_sizes"]
                else:
                    data = item["data"]
                    unc = len(data)
                    method = 0
                    comp_parts = None
                    if compress and unc > 0:
                        if unc > self.block_threshold:
                            nb = (unc + self.block_threshold - 1) // self.block_threshold
                            comp_parts = []
                            for i in range(nb):
                                seg = data[i * self.block_threshold:(i + 1) * self.block_threshold]
                                comp_parts.append(zlib.compress(seg, 6))
                            method = 1
                        else:
                            c = zlib.compress(data, 6)
                            if len(c) < unc:
                                comp_parts = [c]
                                method = 1
                    if method:
                        nb = len(comp_parts)   # 统一在此确定块数, 避免跨迭代泄漏
                        payload_sha1 = hashlib.sha1(b"".join(comp_parts)).digest()
                        block_sizes = [len(x) for x in comp_parts]
                        e_size = sum(block_sizes)
                        head_len = 8 + 8 + 8 + 4 + 20 + 4 + 16 * nb + 4 + 1
                        hdr = bytearray()
                        hdr += struct.pack("<Q", 0)
                        hdr += struct.pack("<Q", e_size)
                        hdr += struct.pack("<Q", unc)
                        hdr += struct.pack("<I", 1)
                        hdr += payload_sha1
                        hdr += struct.pack("<i", nb)
                        cur = head_len
                        for cs in block_sizes:
                            hdr += struct.pack("<QQ", cur, cur + cs)
                            cur += cs
                        hdr += struct.pack("<I", unc * 256 & 0xFFFFFFFF)
                        hdr += b"\x00"
                        payload = bytes(hdr) + b"".join(comp_parts)
                        e_nb = nb
                    else:
                        payload_sha1 = hashlib.sha1(data).digest()
                        e_size = unc
                        block_sizes = []
                        hdr = bytearray()
                        hdr += struct.pack("<Q", 0)
                        hdr += struct.pack("<Q", unc)
                        hdr += struct.pack("<Q", unc)
                        hdr += struct.pack("<I", 0)
                        hdr += payload_sha1
                        hdr += struct.pack("<I", unc * 256 & 0xFFFFFFFF)
                        hdr += b"\x00"
                        payload = bytes(hdr) + data
                        e_nb = 1

                enc_off = len(records)
                data_pos = out.tell()
                records += self._encode_record(data_pos, e_size, unc, method, e_nb, block_sizes)
                phi_pairs.append((item["phi_hash"] if item.get("phi_hash") else _fnv1a64(path.lower().encode("utf-8")), enc_off))
                d, _, fn = path.rpartition("/")
                d = d + "/" if d else ""
                fdi_dirs.setdefault(d, {})[fn] = enc_off
                out.write(payload)
                if progress and idx % 500 == 0:
                    progress(idx, total)

            # ---- 子索引
            phi_blob = bytearray()
            phi_blob += struct.pack("<I", len(phi_pairs))
            for h, v in phi_pairs:
                phi_blob += struct.pack("<QI", h, v)

            fdi_blob = bytearray()
            fdi_blob += struct.pack("<I", len(fdi_dirs))
            for d in sorted(fdi_dirs):
                fdi_blob += fstring_bytes(d)
                m = fdi_dirs[d]
                fdi_blob += struct.pack("<i", len(m))
                for fn in sorted(m):
                    fdi_blob += fstring_bytes(fn)
                    fdi_blob += struct.pack("<I", m[fn])

            data_end = out.tell()

            def body_with(phi_off, fdi_off):
                b = bytearray()
                b += fstring_bytes(self.mount_point)
                b += struct.pack("<I", len(self.items))
                b += struct.pack("<Q", 0x64AE11A2)
                b += struct.pack("<I", 1)
                b += struct.pack("<QQ", phi_off, len(phi_blob)) + hashlib.sha1(bytes(phi_blob)).digest()
                b += struct.pack("<I", 1)
                b += struct.pack("<QQ", fdi_off, len(fdi_blob)) + hashlib.sha1(bytes(fdi_blob)).digest()
                b += struct.pack("<I", len(records))
                b += records
                b += struct.pack("<I", 0)
                return b

            # 主索引长度与其内容无关(偏移字段定宽), 试算长度后回填绝对偏移
            probe = body_with(0, 0)
            phi_abs = data_end + len(probe)
            primary = body_with(phi_abs, phi_abs + len(phi_blob))
            assert len(primary) == len(probe)
            out.write(primary)
            out.write(phi_blob)
            out.write(fdi_blob)

            foot = bytearray()
            foot += struct.pack("<II", PAK_MAGIC, 11)
            foot += struct.pack("<QQ", data_end, len(primary))
            foot += hashlib.sha1(bytes(primary)).digest()
            foot += b"Zlib" + b"\x00" * 28
            foot += b"\x00" * 32 * 4
            assert len(foot) == FOOTER_SIZE
            out.write(foot)
            return dict(files=len(self.items), data_bytes=data_end)
        finally:
            out.close()


def _fnv1a64(b: bytes) -> int:
    h = 0xcbf29ce484222325
    for x in b:
        h = ((h ^ x) * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
    return h
