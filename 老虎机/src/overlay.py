# -*- coding: utf-8 -*-
"""
屏幕右上角半透明置顶悬浮窗：显示"当前可能的物品"与"目标物品"两个图标。
点击穿透(WS_EX_TRANSPARENT)，不抢游戏鼠标。Tk 在独立线程跑 mainloop。
"""
import os
import threading
import tkinter as tk

import numpy as np
import cv2

from main import ctypes_enable_dpi_awareness, HERE


class Overlay:
    def __init__(self, resource_dir=None, icon_px=72):
        self.res_dir = resource_dir or os.path.join(HERE, "resources")
        self.icon_px = icon_px
        self._icon_paths = {}
        self._scan_icons()
        self._photo_cache = {}          # name -> PhotoImage
        self._thread = None
        self.engine = None
        self._alive = False
        self.root = None
        self.cur_slots = []
        self.target_slots = []
        self._cur_shown = None
        self._target_shown = None

    def _scan_icons(self):
        EXTS = (".webp", ".png", ".jpg", ".jpeg", ".bmp")
        base = self.res_dir
        if os.path.isdir(base):
            for root, _dirs, files in os.walk(base):
                for f in files:
                    if not f.lower().endswith(EXTS):
                        continue
                    name = os.path.splitext(f)[0]
                    self._icon_paths.setdefault(name, os.path.join(root, f))

    # ---------- 控制 ----------
    def set_engine(self, eng):
        self.engine = eng

    def show(self, engine=None):
        if engine is not None:
            self.set_engine(engine)
        if self.root is not None:
            try:
                self.root.deiconify()
                self.root.lift()
                self.root.attributes("-topmost", True)
                self._alive = True
            except Exception:
                pass
            return
        if self._thread and self._thread.is_alive():
            return
        self._alive = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def hide(self):
        self._alive = False
        r = self.root
        self.root = None
        if r is not None:
            try:
                r.after(0, r.destroy)
            except Exception:
                try:
                    r.destroy()
                except Exception:
                    pass

    # ---------- 图标 ----------
    def _load_photo(self, name):
        if name is None:
            return None
        if name in self._photo_cache:
            return self._photo_cache[name]
        path = self._icon_paths.get(name)
        if not path:
            return None
        try:
            data = np.fromfile(path, dtype=np.uint8)
            img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
            if img is None:
                return None
            if img.ndim == 3 and img.shape[2] == 4:
                a = img[:, :, 3].astype(np.float32) / 255.0
                fg = img[:, :, :3].astype(np.float32)
                comp = fg * a[:, :, None] + 40.0 * (1.0 - a[:, :, None])
                img = comp.astype(np.uint8)
            else:
                img = img[:, :, :3]
            if img.shape[1] != self.icon_px:
                img = cv2.resize(img, (self.icon_px, self.icon_px),
                                 interpolation=cv2.INTER_LINEAR)
            ok, buf = cv2.imencode(".png", img)
            if not ok:
                return None
            from tkinter import PhotoImage
            ph = PhotoImage(data=buf.tobytes())
            self._photo_cache[name] = ph
            return ph
        except Exception:
            return None

    def _blank_photo(self, px=None):
        px = px or self.icon_px
        key = f"__blank__{px}"
        if key in self._photo_cache:
            return self._photo_cache[key]
        img = np.full((px, px, 3), 45, dtype=np.uint8)
        ok, buf = cv2.imencode(".png", img)
        if not ok:
            return None
        from tkinter import PhotoImage
        ph = PhotoImage(data=buf.tobytes())
        self._photo_cache[key] = ph
        return ph

    # ---------- 窗口 ----------
    def _run(self):
        ctypes_enable_dpi_awareness()
        import tkinter as tk

        root = tk.Tk()
        self.root = root
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.78)
        try:
            root.attributes("-toolwindow", True)
        except Exception:
            pass

        # 两行：当前可能(最多7个) / 目标
        outer = tk.Frame(root, bg="#202020", bd=1, relief="solid")
        outer.pack(fill="both", expand=True)
        self.cur_slots = self._build_row(outer, "当前可能", slots=7)
        self.target_slots = self._build_row(outer, "目  标", slots=1)

        # 目标下方：日志区(最多5行)
        logwrap = tk.Frame(outer, bg="#202020")
        logwrap.pack(fill="x", padx=6, pady=(0, 3))
        tk.Label(logwrap, text="日志", bg="#202020", fg="#9ecbff",
                 font=("Microsoft YaHei", 9), anchor="w").pack(fill="x")
        self.log_labels = []
        for _ in range(5):
            rowf = tk.Frame(logwrap, bg="#202020")
            rowf.pack(fill="x")
            lbl = tk.Label(rowf, text="", anchor="w", bg="#202020", fg="#bbbbbb",
                           font=("Consolas", 8), justify="left")
            lbl.pack(fill="x")
            self.log_labels.append(lbl)
        self._logs_shown = []

        # 右上角定位
        root.update_idletasks()
        w = root.winfo_reqwidth()
        h = root.winfo_reqheight()
        x = root.winfo_screenwidth() - w - 240   # 向左移开右边缘，避免遮挡右侧UI
        y = 14
        root.geometry(f"+{x}+{y}")

        # 点击穿透
        def click_through():
            try:
                import ctypes
                hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
                GWL_EXSTYLE = -20
                style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                style |= 0x20 | 0x80000    # WS_EX_TRANSPARENT | WS_EX_LAYERED
                ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            except Exception:
                pass
        root.after(350, click_through)

        root.after(150, self._tick)
        root.mainloop()
        self.root = None

    def _build_row(self, parent, title, slots=1):
        row = tk.Frame(parent, bg="#202020")
        row.pack(fill="x", padx=6, pady=3)
        t = tk.Label(row, text=title, bg="#202020", fg="#eeeeee",
                     font=("Microsoft YaHei", 10), anchor="w")
        t.pack(side="left", padx=(0, 8))
        cells = []
        for _ in range(max(1, slots)):
            cell = tk.Frame(row, bg="#202020", width=self.icon_px,
                            height=self.icon_px)
            cell.pack(side="left", padx=1)
            cell.pack_propagate(False)
            icon = tk.Label(cell, bg="#202020")
            icon.pack(fill="both", expand=True)
            cells.append(icon)
        return cells

    def _tick(self):
        if not self._alive or self.root is None:
            return
        self._refresh()
        self.root.after(150, self._tick)

    def _refresh(self):
        eng = self.engine
        # 第一行：本局观测池里"确实可能出现的物品"(按权重降序，最多7个)
        pool = []
        weights = {}
        if eng is not None:
            try:
                pool = eng.machine.status().get("pool_conf") or []
                weights = getattr(eng.machine, "weights", None) or {}
            except Exception:
                pass
        pool = sorted(pool, key=lambda n: -float(weights.get(n, 0)))[:7]
        if pool != self._cur_shown:
            self._cur_shown = pool
            for i, lbl in enumerate(self.cur_slots):
                ph = self._load_photo(pool[i]) if i < len(pool) else self._blank_photo()
                if ph is not None:
                    lbl.configure(image=ph)
                    lbl.image = ph
        # 第二行：目标
        target = None
        if eng is not None:
            try:
                target = eng.machine.status().get("target")
            except Exception:
                pass
        if target != self._target_shown:
            self._target_shown = target
            ph = self._load_photo(target) if target else self._blank_photo()
            if ph is not None:
                self.target_slots[0].configure(image=ph)
                self.target_slots[0].image = ph

        # 第三区：最近5行日志
        logs = []
        if eng is not None:
            try:
                logs = eng.status().get("logs") or []
            except Exception:
                pass
        if len(logs) < 5:
            shown = ([""] * (5 - len(logs))) + list(logs[-5:])
        else:
            shown = list(logs[-5:])
        if shown != self._logs_shown:
            self._logs_shown = shown
            for i, lbl in enumerate(self.log_labels):
                lbl.configure(text=shown[i])