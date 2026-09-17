# -*- coding: utf-8 -*-
"""
Forager 三区抽奖辅助 - 图形界面
流程: 观测奖品池 -> 按(物品权重)自动选目标 -> 逐区(1->2->3)识别并按E停止
运行引擎在独立线程，状态经队列回传界面。
"""
import base64
import json
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox

import cv2
import numpy as np

from main import ctypes_enable_dpi_awareness, CONFIG_PATH, HERE, Loader
from engine import SlotEngine

# ---------- 暗色主题 ----------
BG = "#1e1e1e"
BG2 = "#252526"
BG3 = "#2d2d30"
FG = "#e8e8e8"
FG_DIM = "#9d9d9d"
ACCENT = "#4a9eff"
OK = "#3fb950"
WARN = "#d29922"
ERR = "#f85149"
BORDER = "#3c3c3c"
FONT = ("Microsoft YaHei UI", 10)
FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 15, "bold")

ZONE_COLORS = ["#4a9eff", "#3fb950", "#d29922"]


class AppGUI:
    def __init__(self, root, config, config_path):
        self.root = root
        self.cfg = config
        self.config_path = config_path
        self.log_q = queue.Queue()
        self.engine = None
        self._order = []             # 当前优先级顺序 [(物品名, 路径)]
        self._thumbs = []            # 保持 PhotoImage 引用
        self._row_frames = []
        self._row_h = 40
        self._drag = None
        self._configure_theme()
        self._build()
        self._reload_items()
        self.root.after(80, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_exit)

    # ---------- 样式 ----------
    def _configure_theme(self):
        self.root.title("Forager 三区抽奖辅助")
        self.root.configure(bg=BG)
        self.root.geometry("860x720")
        self.root.minsize(720, 620)
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f"+{x}+{y}")

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background=BG2)
        style.configure("TLabel", background=BG2, foreground=FG, font=FONT)
        style.configure("TButton", background=BG3, foreground=FG, font=FONT,
                        borderwidth=1, padding=6)
        style.map("TButton", background=[("active", "#3e3e42")],
                  foreground=[("disabled", FG_DIM)])
        style.configure("Accent.TButton", background=ACCENT, foreground="#0b1220",
                        font=FONT_BOLD, padding=8)
        style.map("Accent.TButton", background=[("active", "#6cb0ff")])
        style.configure("Danger.TButton", background=ERR, foreground="#ffffff")
        style.map("Danger.TButton", background=[("active", "#ff7b6e")])
        style.configure("TLabelFrame", background=BG2, foreground=FG_DIM, borderwidth=1,
                        relief="solid")

    # ---------- 界面 ----------
    def _build(self):
        container = tk.Frame(self.root, bg=BG)
        container.pack(fill="both", expand=True, padx=12, pady=12)

        head = tk.Frame(container, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="🎰 Forager 三区抽奖辅助", bg=BG, fg=FG,
                 font=FONT_TITLE).pack(side="left")
        self.state_lbl = tk.Label(head, text="● 已就绪", bg=BG, fg=WARN, font=FONT_BOLD)
        self.state_lbl.pack(side="right")

        # 主体左右两栏：左侧=状态/控制/日志；右侧=排序区(独占整列高度)
        main_row = tk.Frame(container, bg=BG)
        main_row.pack(fill="both", expand=True)
        left = tk.Frame(main_row, bg=BG)
        left.pack(side="left", fill="both", expand=True)
        right = tk.Frame(main_row, bg=BG)
        right.pack(side="right", fill="y", padx=(8, 0))
        right.configure(width=485)
        right.pack_propagate(False)

        # 三区状态卡片
        self.zone_frames = []
        zones_row = tk.Frame(left, bg=BG)
        zones_row.pack(fill="x", pady=8)
        for i in range(3):
            card = self._build_zone_card(zones_row, i)
            self.zone_frames.append(card)

        # 中间信息行
        info = tk.Frame(left, bg=BG2, highlightbackground=BORDER,
                        highlightthickness=1)
        info.pack(fill="x", pady=8)
        self.target_lbl = tk.Label(info, text="本局目标：--", bg=BG2, fg=ACCENT,
                                   font=FONT_BOLD)
        self.target_lbl.pack(side="left", padx=10, pady=6)
        self.pool_lbl = tk.Label(info, text="观测池：--", bg=BG2, fg=FG, font=FONT)
        self.pool_lbl.pack(side="left", padx=10, pady=6)

        # 控制区
        ctrl = tk.Frame(left, bg=BG)
        ctrl.pack(fill="x", pady=6)
        self.start_btn = ttk.Button(ctrl, text="▶ 启动识别", style="Accent.TButton",
                                    command=self._on_start)
        self.start_btn.pack(side="left", padx=(0, 6))
        self.stop_btn = ttk.Button(ctrl, text="⏸ 暂停", command=self._on_pause,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(ctrl, text="🔁 重新加载", command=self._on_reload).pack(side="left", padx=6)
        self.exit_btn = ttk.Button(ctrl, text="✖ 退出", style="Danger.TButton",
                                   command=self._on_exit)
        self.exit_btn.pack(side="left", padx=6)

        # 排序区(右侧独占整列)
        self._build_weight_panel(right)

        # 日志
        logf = ttk.LabelFrame(left, text=" 运行日志 ")
        logf.pack(fill="both", expand=True)
        self.log_text = tk.Text(logf, bg="#141414", fg=FG, font=("Consolas", 9),
                                relief="flat", highlightthickness=0, wrap="word",
                                state="disabled", height=7)
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        self.log_text.config(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        sb.pack(side="right", fill="y", pady=8, padx=(0, 8))

    def _build_zone_card(self, parent, i):
        z = self.cfg["zones"][i]
        num = i + 1
        card = tk.Frame(parent, bg=BG2, highlightbackground=BORDER, highlightthickness=1)
        card.pack(side="left", fill="both", expand=True,
                  padx=(0 if i == 0 else 6, 0))
        tk.Label(card, text=f"抽奖区 {num}  ({z['name']})", bg=BG2, fg=ZONE_COLORS[i],
                 font=FONT_BOLD).pack(anchor="w", padx=10, pady=(8, 2))
        cur = tk.Label(card, text="当前物品：--", bg=BG2, fg=FG, font=FONT)
        cur.pack(anchor="w", padx=10)
        per = tk.Label(card, text="匹配度：--", bg=BG2, fg=FG_DIM, font=FONT)
        per.pack(anchor="w", padx=10)
        st = tk.Label(card, text="状态：未启动", bg=BG2, fg=FG_DIM, font=FONT)
        st.pack(anchor="w", padx=10, pady=(2, 8))
        return {"cur": cur, "per": per, "st": st, "id": z["id"]}

    def _build_weight_panel(self, parent):
        lf = tk.LabelFrame(parent, text="  物品优先级（上拖更优先）  ",
                           bg=BG2, fg=FG_DIM, relief="solid", bd=1)
        lf.pack(fill="both", expand=True, pady=6)

        # 顶部按钮行
        btns = tk.Frame(lf, bg=BG2)
        btns.pack(fill="x", padx=6, pady=(6, 4))
        self.wcount_lbl = tk.Label(btns, text="物品数：--", bg=BG2, fg=FG_DIM,
                                   font=FONT_BOLD)
        self.wcount_lbl.pack(side="right")
        ttk.Button(btns, text="💾 保存排序", style="Accent.TButton",
                   command=self._save_weights).pack(side="left")

        # 说明条
        hint = tk.Label(lf, text="👆 按住任意一行上下拖动：越靠上越想抽中",
                        bg=BG2, fg=ACCENT, font=FONT)
        hint.pack(anchor="w", padx=8)

        # 可滚动排序列表：每行 = 序号 + 图标 + 名称
        body = tk.Frame(lf, bg=BG2)
        body.pack(fill="both", expand=True, padx=8, pady=(2, 8))
        self.wcanvas = tk.Canvas(body, bg=BG2, highlightthickness=0)
        vsb = ttk.Scrollbar(body, orient="vertical", command=self.wcanvas.yview)
        self.winner = tk.Frame(self.wcanvas, bg=BG2)
        self.winner.bind("<Configure>",
                         lambda e: self.wcanvas.configure(scrollregion=self.wcanvas.bbox("all")))
        win_id = self.wcanvas.create_window((0, 0), window=self.winner, anchor="nw")
        self.wcanvas.configure(yscrollcommand=vsb.set)
        self.wcanvas.bind("<Configure>",
                          lambda e: self.wcanvas.itemconfig(win_id, width=e.width))
        self.wcanvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # 鼠标滚轮滚动（Windows）
        for w in (self.wcanvas, self.winner):
            w.bind("<MouseWheel>",
                   lambda e: self.wcanvas.yview_scroll(int(-e.delta / 120), "units"))
            w.bind("<Button-4>", lambda e: self.wcanvas.yview_scroll(-3, "units"))
            w.bind("<Button-5>", lambda e: self.wcanvas.yview_scroll(3, "units"))

        # 全局拖拽排序事件（子控件也能触发）
        self.root.bind_all("<Button-1>", self._on_reorder_press)
        self.root.bind_all("<B1-Motion>", self._on_reorder_motion)
        self.root.bind_all("<ButtonRelease-1>", self._on_reorder_release)
        self._drag = None

    # ---------- 权重列表 ----------
    def _iter_items(self):
        """按 resources 目录顺序返回 [(物品名, 图标文件路径)]。"""
        base = os.path.join(HERE, self.cfg.get("item_dir", "resources"))
        out = []
        if os.path.isdir(base):
            for root, _dirs, files in sorted(os.walk(base)):
                for f in sorted(files):
                    if f.lower().endswith(Loader.IMG_EXTS):
                        name = os.path.splitext(f)[0]
                        out.append((name, os.path.join(root, f)))
        return out

    def _thumb(self, path, show=36):
        """把图标合成到界面背景色生成 tk PhotoImage。"""
        try:
            data = np.fromfile(path, dtype=np.uint8)
            img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
            if img is None:
                return None
            if img.ndim == 3 and img.shape[2] == 4:
                a = img[:, :, 3].astype(np.float32) / 255.0
                fg = img[:, :, :3].astype(np.float32)
                bg = np.array([38, 37, 37], np.float32)   # ~BG2
                img = (fg * a[:, :, None] + bg * (1 - a[:, :, None])).astype(np.uint8)
            elif img.ndim == 2:
                img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
            if img.shape[0] != show or img.shape[1] != show:
                img = cv2.resize(img, (show, show), interpolation=cv2.INTER_LINEAR)
            ok, buf = cv2.imencode(".png", img)
            if not ok:
                return None
            from tkinter import PhotoImage
            photo = PhotoImage(data=base64.b64encode(buf.tobytes()).decode("ascii"))
            self._thumbs.append(photo)   # 防被垃圾回收
            return photo
        except Exception:
            return None

    def _reload_items(self):
        # 重读 config，恢复上次保存的排序（items_order）
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.cfg = json.load(f)
        except Exception:
            pass
        saved_order = self.cfg.get("items_order") or []
        all_items = self._iter_items()
        by_name = dict(all_items)
        # 以 saved_order 为准，缺失(新加物品)按默认顺序追加
        order = [(n, by_name[n]) for n in saved_order if n in by_name]
        have = {n for n, _p in order}
        order += [(n, p) for n, p in all_items if n not in have]
        self._set_order(order)

    def _set_order(self, order):
        """用给定的顺序重建并渲染列表。order: [(name, path), ...]"""
        self._order = list(order)
        self._thumbs = []
        for child in self.winner.winfo_children():
            child.destroy()
        self._row_frames = []
        for name, path in self._order:
            row = tk.Frame(self.winner, bg=BG2, highlightthickness=1,
                           highlightbackground=BORDER)
            row.grid(row=len(self._row_frames), column=0, sticky="ew", pady=2)
            row.columnconfigure(2, weight=1)
            img = self._thumb(path)
            tk.Label(row, image=img, bg=BG2, bd=0).grid(row=0, column=1, padx=(6, 10))
            name_lbl = tk.Label(row, text=name, bg=BG2, fg=FG, font=FONT, anchor="w")
            name_lbl.grid(row=0, column=2, sticky="w", pady=2)
            idx_lbl = tk.Label(row, text=str(len(self._row_frames) + 1), bg=BG2,
                               fg=FG_DIM, font=FONT, width=3)
            idx_lbl.grid(row=0, column=0, padx=(6, 2))
            self._row_frames.append({"fr": row, "idx_lbl": idx_lbl, "name": name})
        self._update_weight_count()
        self.root.update_idletasks()
        if self._row_frames:
            self._row_h = self._row_frames[0]["fr"].winfo_height() or 40

    def _row_of(self, widget):
        # 从命中的控件沿父级向上找，判断其属于哪一行（兼容不同 Tk 版本的层级）
        w = widget
        seen = set()
        while w is not None and id(w) not in seen:
            seen.add(id(w))
            for i, rd in enumerate(self._row_frames):
                if w is rd["fr"]:
                    return i
            w = w.master
        return None

    # ---------- 拖拽排序 ----------
    def _on_reorder_press(self, event):
        wid = self.winner.winfo_containing(event.x_root, event.y_root)
        if wid is None:
            return
        idx = self._row_of(wid)
        if idx is None:
            return
        self._drag = {"idx": idx, "y0": event.y_root}
        self._row_frames[idx]["fr"].config(bg=ACCENT, highlightbackground=ACCENT,
                                           highlightthickness=2)

    def _on_reorder_motion(self, event):
        if self._drag is None:
            return
        # 拖到可视区顶/底边时自动滚动
        ctop = self.wcanvas.winfo_rooty()
        cbot = ctop + self.wcanvas.winfo_height()
        if event.y_root < ctop + 30:
            self.wcanvas.yview_scroll(-1, "units")
        elif event.y_root > cbot - 30:
            self.wcanvas.yview_scroll(1, "units")

        dy = event.y_root - self._drag["y0"]
        target = self._drag["idx"] + int(round(dy / max(1, self._row_h)))
        target = max(0, min(len(self._order) - 1, target))
        if target != self._drag["idx"] and len(self._order) > 1:
            item = self._order.pop(self._drag["idx"])
            self._order.insert(target, item)
            self._drag["idx"] = target
            self._reposition_rows()

    def _reposition_rows(self):
        """把行 frame 与序号重排到与当前 self._order 一致（不重建内容）。"""
        by_name = {rd["name"]: rd for rd in self._row_frames}
        for pos, (name, _p) in enumerate(self._order):
            rd = by_name[name]
            rd["fr"].grid(row=pos, column=0, sticky="ew", pady=2)
            rd["idx_lbl"].config(text=str(pos + 1))
        self.winner.update_idletasks()
        self.wcanvas.configure(scrollregion=self.wcanvas.bbox("all"))

    def _on_reorder_release(self, _event):
        if self._drag is not None:
            idx = self._drag["idx"]
            if 0 <= idx < len(self._row_frames):
                self._row_frames[idx]["fr"].config(bg=BG2, highlightbackground=BORDER,
                                                   highlightthickness=1)
        self._drag = None

    def _update_weight_count(self):
        self.wcount_lbl.config(text=f"物品数：{len(self._order)}")

    def current_order(self):
        """返回当前显示顺序的 [物品名,...]（最靠前=最优先）。"""
        return [name for name, _p in self._order]

    def _save_weights(self):
        """保存排序：写 items_order(顺序) 和 weights(rank->数值, 供引擎决策)。"""
        order_names = self.current_order()
        self.cfg["items_order"] = order_names
        n = len(order_names)
        # 权重 = 最靠前最大(n)，最靠后=1，恒>0 → 引擎总是从池中选最靠前的
        weights = {name: float(n - rank) for rank, name in enumerate(order_names)}
        self.cfg["weights"] = weights
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        if self.engine:
            self.engine.machine.weights = weights
        self._log(f"[排序] 已保存 {n} 个物品的优先级顺序")

    # ---------- 控制 ----------
    def _on_start(self):
        try:
            # 启动前自动保存一次权重，保证引擎使用最新配置
            self._save_weights()
            if self.engine is None or not self.engine.thread or not self.engine.thread.is_alive():
                self.engine = SlotEngine(self.cfg, log=self._enqueue_log)
                self.engine.accepting.set()
                self.engine.start()
            # 若已有引擎但已暂停，重新开始
            elif not self.engine.stopped.is_set():
                self.engine.accepting.set()
            else:
                self.engine = SlotEngine(self.cfg, log=self._enqueue_log)
                self.engine.accepting.set()
                self.engine.start()
            self.state_lbl.config(text="● 识别中", fg=OK)
            self.start_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
        except Exception as e:
            messagebox.showerror("启动失败", str(e))

    def _on_pause(self):
        if self.engine:
            self.engine.accepting.clear()
            self.state_lbl.config(text="● 已暂停", fg=WARN)
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")

    def _on_exit(self):
        self._save_weights()
        if self.engine:
            self.engine.stop()
        self.root.destroy()

    def _on_reload(self):
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.cfg = json.load(f)
        except Exception:
            pass
        was_running = self.engine is not None and not self.engine.stopped.is_set()
        was_accepting = self.engine is not None and self.engine.accepting.is_set()
        if self.engine:
            self.engine.stop()
        self.engine = None
        if was_running or was_accepting:
            self._on_start()
        self._reload_items()
        self._log("[模板] 已重新加载物品与权重")

    # ---------- 轮询 ----------
    def _enqueue_log(self, *a):
        self.log_q.put(("log", " ".join(str(x) for x in a)))

    def _poll(self):
        try:
            while True:
                kind, payload = self.log_q.get_nowait()
                if kind == "log":
                    self._log(str(payload))
                elif kind == "status":
                    self._apply_status(payload)
        except queue.Empty:
            pass
        # 主动读取引擎状态
        if self.engine:
            try:
                self._apply_status(self.engine.status())
            except Exception:
                pass
        self.root.after(120, self._poll)

    def _apply_status(self, st):
        zmap = {}
        for i, f in enumerate(self.zone_frames):
            zmap[f["id"]] = f
        # 阶段状态
        phase = st.get("phase")
        if phase == "observe":
            self.state_lbl.config(text="● 观测中", fg=ACCENT)
        elif phase == "stop":
            self.state_lbl.config(text="● 停止中", fg=OK)
        elif phase == "idle":
            self.state_lbl.config(text="● 已暂停" if (self.engine and not self.engine.accepting.is_set())
                                  else "● 空闲", fg=WARN)
        # 每个区状态
        target = st.get("target")
        for f in self.zone_frames:
            zid = f["id"]
            if zid in st.get("done", []):
                f["st"].config(text="状态：已停住", fg=OK)
            elif st.get("stop_zone") == zid:
                f["st"].config(text="状态：正在识别", fg=ACCENT)
            elif phase == "observe":
                f["st"].config(text="状态：观测中", fg=FG_DIM)
            else:
                f["st"].config(text="状态：等待", fg=FG_DIM)
        self.target_lbl.config(text=f"本局目标：{target or '--'}")
        pool = st.get("pool", [])
        self.pool_lbl.config(text=("观测池：" + "、".join(pool)) if pool else "观测池：--")

    def _log(self, msg):
        self.log_text.config(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")


def main():
    ctypes_enable_dpi_awareness()
    if not os.path.exists(CONFIG_PATH):
        print(f"缺少配置文件: {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    root = tk.Tk()
    try:
        from pynput import keyboard as _kb
        _kb.Listener(
            on_press=lambda k: root.after(0, root.destroy)
            if getattr(k, "name", None) == str(config.get("exit_hotkey", "f9")).lower()
            else None).start()
    except Exception:
        pass
    AppGUI(root, config, CONFIG_PATH)
    root.mainloop()


if __name__ == "__main__":
    main()