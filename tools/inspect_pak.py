# -*- coding: utf-8 -*-
"""检查 UE pak 文件尾部 FPakInfo，确认版本与加密状态"""
import struct, sys, io

PAK = r"D:\program\yijian_resource\Wandering_Sword-WindowsNoEditor.pak"

with open(PAK, "rb") as f:
    f.seek(0, io.SEEK_END)
    total = f.tell()
    f.seek(max(0, total - 1024))
    tail = f.read()

print(f"文件总大小: {total:,} bytes")

# 扫描 magic 0x52454B50 (文件字节序为 50 4B 45 52)
magic = b"\x50\x4b\x45\x52"
pos = tail.rfind(magic)
if pos < 0:
    print("未找到 pak magic！可能不是标准 UE pak")
    sys.exit(1)

off = pos
print(f"\nFPakInfo 位于文件尾偏移: {total - 1024 + off if total >= 1024 else off} (距 EOF {len(tail) - off} bytes)")
print(f"Pak 尾部大小: {len(tail) - pos} bytes")

ver, = struct.unpack_from("<I", tail, off + 4)
print(f"Version: {ver}")

# v9+: EncryptionKeyGuid(16) + bEncryptedIndex(1)
if ver >= 9:
    guid = tail[off + 8: off + 24]
    enc_idx = tail[off + 24]
    print(f"EncryptionKeyGuid: {guid.hex()}")
    print(f"索引是否加密: {bool(enc_idx)}")
    p = off + 25
else:
    p = off + 8

idx_off, idx_size = struct.unpack_from("<QQ", tail, p)
idx_hash = tail[p + 16: p + 36]
print(f"IndexOffset: {idx_off:,}")
print(f"IndexSize:   {idx_size:,}")
print(f"IndexHash:   {idx_hash.hex()}")

# v11: 压缩方法列表 (5 x 32 字节定长)
if ver >= 11:
    q = p + 36
    print("CompressionMethods:", end=" ")
    for i in range(5):
        name = tail[q + i * 32: q + (i + 1) * 32].split(b"\x00")[0].decode("ascii", "replace")
        if name:
            print(name, end=" ")
    print()

# 索引开头几个字节（未加密时可辨认 FString 长度）
with open(PAK, "rb") as f:
    f.seek(idx_off)
    head = f.read(min(200, idx_size))
print("\n索引头部原始字节(hex 前 64):", head[:64].hex())
print("索引头部预览:", head[:120].decode("utf-8", "replace"))
