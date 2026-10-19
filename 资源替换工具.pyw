# -*- coding: utf-8 -*-
"""
逸剑风云决 资源替换工具
- 浏览 pak 内资源 (目录树 / 搜索)
- 导出资源 (uasset+uexp+ubulk 成组导出)
- 打包补丁 pak (推荐: 仅含修改文件, 放入游戏 Paks 目录覆盖原资源)
- 重建完整 pak (可选, 较慢)
- 校验 pak 完整性

依赖: 无 (纯标准库); 需与本文件同目录的 paklib.py
"""
import os
import sys
import json
import struct
import threading
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_here, "tools"))
sys.path.insert(0, _here)
from paklib import Pak, PakWriter, read_fstring

TITLE = "逸剑风云决 · 资源替换工具"


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}GB"


def read_raw_payload(pak, e) -> bytes:
    """读取条目的原始 payload(条目头+压缩数据), 用于原样搬运"""
    pak.f.seek(e.offset)
    return pak.f.read(e.header_size + e.size)


def block_sizes_of(pak, e):
    if not e.compression:
        return []
    pak.f.seek(e.offset)
    hdr = pak.f.read(e.header_size)
    out = []
    for i in range(e.block_count):
        s, t = struct.unpack_from("<QQ", hdr, 52 + 16 * i)
        out.append(t - s)
    return out


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(TITLE)
        root.geometry("1200x760")
        self.pak = None
        self.entries = {}        # path -> Entry
        self.group_members = {}  # stem -> [path,...]
        self.path_to_group = {}
        self.mod_items = {}      # pak路径 -> 本地文件路径 (待打包修改)
        self._build_ui()

    # ------------------------------------------------ UI
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=6)
        top.pack(fill="x")
        ttk.Button(top, text="打开 pak…", command=self.open_pak).pack(side="left")
        self.lbl_pak = ttk.Label(top, text="未打开", foreground="#555")
        self.lbl_pak.pack(side="left", padx=10)
        ttk.Button(top, text="校验 pak…", command=self.verify_pak).pack(side="right")

        main = ttk.PanedWindow(self.root, orient="horizontal")
        main.pack(fill="both", expand=True, padx=6, pady=(0, 4))

        # 左: 目录树
        lf = ttk.LabelFrame(main, text=" 资源目录 ")
        self.tree = ttk.Treeview(lf, show="tree")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)
        main.add(lf, weight=1)

        # 右: 文件列表 + 搜索
        rf = ttk.Frame(main)
        sf = ttk.Frame(rf)
        sf.pack(fill="x", pady=(0, 3))
        ttk.Label(sf, text="搜索:").pack(side="left")
        self.search_var = tk.StringVar()
        ent = ttk.Entry(sf, textvariable=self.search_var)
        ent.pack(side="left", fill="x", expand=True, padx=4)
        ent.bind("<Return>", lambda e: self.do_search())
        ttk.Button(sf, text="查找", command=self.do_search).pack(side="left")

        cols = ("name", "size", "comp", "type")
        self.list = ttk.Treeview(rf, columns=cols, show="headings", selectmode="extended")
        for c, w, t in (("name", 420, "资源名"), ("size", 110, "大小"),
                        ("comp", 90, "压缩"), ("type", 90, "扩展名")):
            self.list.heading(c, text=t)
            self.list.column(c, width=w, anchor="w")
        sb2 = ttk.Scrollbar(rf, orient="vertical", command=self.list.yview)
        self.list.configure(yscrollcommand=sb2.set)
        self.list.pack(fill="both", expand=True)
        sb2.pack(side="right", fill="y")
        rf.pack(fill="both", expand=True)
        main.add(rf, weight=2)

        # 修改清单
        mf = ttk.LabelFrame(self.root, text=" 修改清单 (双击条目可移除) ", height=140)
        mf.pack(fill="x", padx=6, pady=2)
        self.modlist = ttk.Treeview(mf, columns=("pakpath", "local"), show="headings", height=5)
        self.modlist.heading("pakpath", text="pak 内路径")
        self.modlist.column("pakpath", width=520)
        self.modlist.heading("local", text="本地文件")
        self.modlist.column("local", width=380)
        self.modlist.pack(fill="both", expand=True)
        self.modlist.bind("<Double-1>", lambda e: self.remove_mod_selected())

        btns = ttk.Frame(self.root, padding=6)
        btns.pack(fill="x")
        ttk.Button(btns, text="导出选中", command=self.export_selected).pack(side="left")
        ttk.Button(btns, text="导出全部(整个pak)", command=self.export_all).pack(side="left")
        ttk.Button(btns, text="添加修改文件…", command=self.add_mod_files).pack(side="left", padx=(20, 0))
        ttk.Button(btns, text="添加修改目录…", command=self.add_mod_dir).pack(side="left")
        ttk.Button(btns, text="清空修改", command=self.clear_mods).pack(side="left")
        ttk.Button(btns, text="打包为补丁 pak (推荐)", command=self.build_patch).pack(side="right")
        ttk.Button(btns, text="重建完整 pak", command=self.build_full).pack(side="right")

        self.status = ttk.Label(self.root, text="就绪", anchor="w", relief="sunken", padding=4)
        self.status.pack(fill="x", side="bottom")

    def set_status(self, msg):
        self.root.after(0, lambda: self.status.config(text=msg))

    # ------------------------------------------------ pak 打开与浏览
    def open_pak(self):
        p = filedialog.askopenfilename(title="选择 pak 文件",
                                       filetypes=[("UE pak", "*.pak"), ("所有文件", "*.*")])
        if not p:
            return
        self.load_pak(p)

    def load_pak(self, path):
        try:
            self.status.config(text="解析索引中…")
            self.root.update()
            pak = Pak(path)
        except Exception as ex:
            messagebox.showerror("错误", f"解析失败:\n{ex}\n\n{traceback.format_exc()[-800:]}")
            self.status.config(text="解析失败")
            return
        self.pak_path = path
        self.pak = pak
        self.entries.clear()
        self.group_members.clear()
        self.path_to_group.clear()
        for e in pak.iter_files():
            self.entries[e.path] = e
            stem = e.path.rsplit(".", 1)[0]
            self.group_members.setdefault(stem, []).append(e.path)
            self.path_to_group[e.path] = stem
        self.lbl_pak.config(text=f"{os.path.basename(path)}  |  {len(self.entries):,} 个文件  |  "
                                 f"挂载点 {pak.mount_point}")
        # 构建目录树
        self.tree.delete(*self.tree.get_children())
        dirs = {}
        for d in pak.directory_index:
            dirs[d] = True
        tree_nodes = {"": ""}
        def get_node(dirpath):
            if dirpath in tree_nodes:
                return tree_nodes[dirpath]
            parent, _, name = dirpath.rstrip("/").rpartition("/")
            parent_key = get_node(parent + "/" if parent else "")
            nid = self.tree.insert(parent_key, "end", text=name, values=(dirpath,), open=False)
            tree_nodes[dirpath] = nid
            return nid
        for d in sorted(dirs):
            get_node(d)
        self.status.config(text=f"已加载 {os.path.basename(path)}: {len(self.entries):,} 文件, "
                                f"{len(dirs):,} 目录")
        self.list.delete(*self.list.get_children())

    def on_tree_select(self, _ev):
        sel = self.tree.selection()
        if not sel:
            return
        dirpath = self.tree.item(sel[0], "values")[0]
        m = self.pak.directory_index.get(dirpath, {})
        self.show_files([f"{dirpath}{fn}" for fn in sorted(m)])

    def show_files(self, paths):
        self.list.delete(*self.list.get_children())
        shown = 0
        for p in paths:
            e = self.entries.get(p)
            if not e:
                continue
            stem = self.path_to_group[p]
            members = self.group_members[stem]
            ext = p.rsplit(".", 1)[-1]
            if ext in ("uexp", "ubulk", "uptnl"):
                continue   # 从属于 uasset, 不单独列出
            total = sum(self.entries[m].uncompressed_size for m in members)
            self.list.insert("", "end", iid=p, values=(
                p.rsplit("/", 1)[-1], human(total), "压缩" if e.compression else "原始", "." + ext))
            shown += 1
            if shown >= 3000:
                break
        self.set_status(f"列出 {shown} 个资源")

    def do_search(self):
        q = self.search_var.get().strip().lower()
        if not q or not self.pak:
            return
        hits = [p for p in self.entries if q in p.lower()]
        hits.sort()
        self.show_files(hits[:3000])
        self.set_status(f"搜索 '{q}': {len(hits):,} 个匹配")

    # ------------------------------------------------ 导出
    def _selected_groups(self):
        sels = self.list.selection()
        if not sels:
            messagebox.showinfo("提示", "请先在列表中选择资源")
            return []
        out = []
        for p in sels:
            stem = self.path_to_group.get(p)
            if stem:
                out.append(stem)
        return out

    def export_selected(self):
        if not self.pak:
            return
        groups = self._selected_groups()
        if not groups:
            return
        d = filedialog.askdirectory(title="选择导出目录")
        if not d:
            return
        n = 0
        try:
            for stem in groups:
                for path in self.group_members[stem]:
                    e = self.entries[path]
                    data = self.pak.read_entry(e)
                    dest = os.path.join(d, *path.split("/"))
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with open(dest, "wb") as g:
                        g.write(data)
                    n += 1
            messagebox.showinfo("完成", f"已导出 {len(groups)} 个资源 ({n} 个文件)\n到 {d}")
        except Exception as ex:
            messagebox.showerror("错误", str(ex))

    def export_all(self):
        if not self.pak:
            return
        d = filedialog.askdirectory(title="导出全部资源到(将创建 extracted 子目录)")
        if not d:
            return
        out = os.path.join(d, "extracted")
        os.makedirs(out, exist_ok=True)
        th = threading.Thread(target=self._export_all_worker, args=(out,), daemon=True)
        th.start()

    def _export_all_worker(self, out):
        n = 0
        try:
            for path, e in self.entries.items():
                data = self.pak.read_entry(e)
                dest = os.path.join(out, *path.split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as g:
                    g.write(data)
                n += 1
                if n % 2000 == 0:
                    self.set_status(f"导出中… {n:,}/{len(self.entries):,}")
            self.set_status(f"导出完成: {n:,} 文件 → {out}")
        except Exception as ex:
            self.set_status(f"导出失败: {ex}")

    # ------------------------------------------------ 修改清单
    def add_mod_files(self):
        fs = filedialog.askopenfilenames(title="选择修改后的文件 (保持 pak 内相对路径结构)")
        if not fs:
            return
        added = skipped = 0
        for f in fs:
            rp = self._map_local_to_pak(f)
            if rp:
                self.mod_items[rp] = f
                added += 1
            else:
                skipped += 1
        self.refresh_modlist()
        msg = f"已添加 {added} 个文件"
        if skipped:
            msg += f"\n{skipped} 个无法定位 pak 路径(见下述规则)"
            msg += "\n路径需包含 Wandering_Sword/ 或 Engine/ 段"
        messagebox.showinfo("添加修改", msg)

    def add_mod_dir(self):
        d = filedialog.askdirectory(title="选择修改目录 (目录内需含 Wandering_Sword/ 或 Engine/ 结构)")
        if not d:
            return
        added = 0
        for base, _dirs, files in os.walk(d):
            for fn in files:
                f = os.path.join(base, fn)
                rp = self._map_local_to_pak(f)
                if rp:
                    self.mod_items[rp] = f
                    added += 1
        self.refresh_modlist()
        messagebox.showinfo("添加修改目录", f"共添加 {added} 个修改文件")

    def _map_local_to_pak(self, local: str):
        p = local.replace("\\", "/")
        for anchor in ("Wandering_Sword/", "Engine/", "Plugins/"):
            i = p.find(anchor)
            if i >= 0:
                return p[i:]
        return None

    def remove_mod_selected(self):
        for s in self.modlist.selection():
            rp = self.modlist.item(s, "values")[0]
            self.mod_items.pop(rp, None)
        self.refresh_modlist()

    def clear_mods(self):
        self.mod_items.clear()
        self.refresh_modlist()

    def refresh_modlist(self):
        self.modlist.delete(*self.modlist.get_children())
        for rp, f in sorted(self.mod_items.items()):
            self.modlist.insert("", "end", values=(rp, f))

    # ------------------------------------------------ 打包
    def _phi_hash_map(self):
        """FDI值->PHI哈希键 (用于原文件复用原键)"""
        if not hasattr(self, "_phi_map"):
            m = {}
            off2hash = {v: h for h, v in self.pak.path_hash_index.items()}
            for d, mm in self.pak.directory_index.items():
                for fn, v in mm.items():
                    if v in off2hash:
                        m[(d + fn) if d else fn] = off2hash[v]
            self._phi_map = m
        return self._phi_map

    def build_patch(self):
        if not self.pak:
            messagebox.showinfo("提示", "请先打开 pak")
            return
        if not self.mod_items:
            messagebox.showinfo("提示", "修改清单为空, 请先添加修改文件")
            return
        out = filedialog.asksaveasfilename(title="保存补丁 pak",
                                           defaultextension=".pak",
                                           initialfile="ZZ_MyMod_P.pak",
                                           filetypes=[("UE pak", "*.pak")])
        if not out:
            return
        th = threading.Thread(target=self._build_patch_worker, args=(out,), daemon=True)
        th.start()

    def _build_patch_worker(self, out):
        try:
            self.set_status("打包补丁中…")
            w = PakWriter(self.pak.mount_point)
            phis = self._phi_hash_map()
            for rp, local in sorted(self.mod_items.items()):
                with open(local, "rb") as g:
                    data = g.read()
                w.add(rp, data, phis.get(rp, 0))
            st = w.write(out)
            # 回读校验
            chk = Pak(out)
            bad = 0
            for e in chk.iter_files():
                chk.read_entry(e)
                if e.path not in self.mod_items:
                    bad += 1
            chk.close()
            self.set_status(f"补丁完成: {st['files']} 文件 → {out}")
            self.root.after(0, lambda: messagebox.showinfo(
                "完成",
                f"补丁 pak 已生成:\n{out}\n\n"
                f"包含 {st['files']} 个文件\n"
                f"使用方法: 复制到游戏目录\nWandering_Sword/Content/Paks/ 下即可生效\n"
                f"(名称以 ZZ 开头保证优先级高于原包)\n\n回读校验{'通过' if bad == 0 else '发现异常:' + str(bad)}"))
        except Exception as ex:
            err = traceback.format_exc()[-1200:]
            self.root.after(0, lambda: messagebox.showerror("打包失败", err))
            self.set_status("打包失败")

    def build_full(self):
        if not self.pak:
            messagebox.showinfo("提示", "请先打开 pak")
            return
        if not self.mod_items:
            if not messagebox.askyesno("确认", "修改清单为空, 仍要重建原样 pak?(用于完整性验证)"):
                return
        out = filedialog.asksaveasfilename(title="保存重建的完整 pak",
                                           defaultextension=".pak",
                                           filetypes=[("UE pak", "*.pak")])
        if not out:
            return
        th = threading.Thread(target=self._build_full_worker, args=(out,), daemon=True)
        th.start()

    def _build_full_worker(self, out):
        try:
            w = PakWriter(self.pak.mount_point)
            phis = self._phi_hash_map()
            total = len(self.entries)
            n = 0
            for path, e in self.entries.items():
                if path in self.mod_items:
                    with open(self.mod_items[path], "rb") as g:
                        w.add(path, g.read(), phis.get(path, 0))
                else:
                    payload = read_raw_payload(self.pak, e)
                    w.add_raw(path, payload, e.size, e.uncompressed_size, e.compression,
                              max(e.block_count, 1), block_sizes_of(self.pak, e),
                              phis.get(path, 0))
                n += 1
                if n % 2000 == 0:
                    self.set_status(f"重建中… {n:,}/{total:,}")
            self.set_status("写出索引…")
            st = w.write(out)
            self.set_status(f"重建完成: {st['files']} 文件 → {out}")
            self.root.after(0, lambda: messagebox.showinfo(
                "完成", f"完整 pak 已重建:\n{out}\n\n{st['files']} 个文件\n"
                        f"可直接替换游戏目录同名 pak 使用。"))
        except Exception as ex:
            err = traceback.format_exc()[-1200:]
            self.root.after(0, lambda: messagebox.showerror("重建失败", err))
            self.set_status("重建失败")

    # ------------------------------------------------ 校验
    def verify_pak(self):
        p = filedialog.askopenfilename(title="选择要校验的 pak", filetypes=[("UE pak", "*.pak")])
        if not p:
            return
        th = threading.Thread(target=self._verify_worker, args=(p,), daemon=True)
        th.start()

    def _verify_worker(self, p):
        try:
            pak = Pak(p)
            n = bad = 0
            for e in pak.iter_files():
                try:
                    pak.read_entry(e)
                except Exception:
                    bad += 1
                n += 1
                if n % 5000 == 0:
                    self.set_status(f"校验中… {n:,}/{pak.num_files:,} (异常 {bad})")
            pak.close()
            self.set_status(f"校验完成: {n:,} 文件, 异常 {bad}")
            self.root.after(0, lambda: messagebox.showinfo(
                "校验完成", f"{p}\n\n共 {n:,} 个文件\nSHA1 校验失败: {bad} 个"))
        except Exception as ex:
            self.root.after(0, lambda: messagebox.showerror("校验失败", str(ex)))


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    app = App(root)
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        root.after(200, lambda: app.load_pak(sys.argv[1]))
    root.mainloop()


if __name__ == "__main__":
    main()
