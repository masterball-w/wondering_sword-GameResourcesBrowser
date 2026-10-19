# -*- coding: utf-8 -*-
"""解压全部 pak 资源到 extracted/ 目录, 保留原始路径结构"""
import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paklib import Pak

PAK = r"D:\program\yijian_resource\Wandering_Sword-WindowsNoEditor.pak"
OUT = r"D:\program\yijian_resource\extracted"

def win_long(p: str) -> str:
    if os.name == "nt" and not p.startswith("\\\\?\\") and len(p) >= 248:
        return "\\\\?\\" + os.path.abspath(p)
    return p

def main():
    pak = Pak(PAK)
    os.makedirs(OUT, exist_ok=True)
    manifest = []
    n = 0
    total_unc = 0
    errors = []
    t0 = time.time()
    with open(os.path.join(OUT, "_manifest.jsonl"), "w", encoding="utf-8") as mf:
        for e in pak.iter_files():
            try:
                data = pak.read_entry(e)
            except Exception as ex:
                errors.append((e.path, str(ex)))
                continue
            dest = win_long(os.path.join(OUT, *e.path.split("/")))
            os.makedirs(win_long(os.path.dirname(dest)), exist_ok=True)
            with open(dest, "wb") as g:
                g.write(data)
            mf.write(json.dumps({
                "path": e.path, "size": e.uncompressed_size,
                "compression": e.compression, "nb": e.block_count,
            }, ensure_ascii=False) + "\n")
            manifest.append(e.path)
            total_unc += e.uncompressed_size
            n += 1
            if n % 5000 == 0:
                dt = time.time() - t0
                print(f"  {n:,} 文件, {total_unc/1e9:.2f} GB, {dt:.0f}s, {n/dt:.0f} 文件/秒", flush=True)
    pak.close()
    dt = time.time() - t0
    print(f"完成: {n:,} 文件, {total_unc/1e9:.2f} GB, 用时 {dt:.0f}s", flush=True)
    if errors:
        print(f"错误 {len(errors)} 个:", flush=True)
        for p, msg in errors[:10]:
            print("  ", p, msg, flush=True)
        with open(os.path.join(OUT, "_errors.json"), "w", encoding="utf-8") as g:
            json.dump(errors, g, ensure_ascii=False, indent=1)

if __name__ == "__main__":
    main()
