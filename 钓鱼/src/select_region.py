# -*- coding: utf-8 -*-
"""
辅助钓鱼 - 手动框选监控区域
功能:
  - 独立运行: python select_region.py  (框选并写入 config.json)
  - 或供 fish.py 调用: region = do_select()  (返回 dict 或 None，不写文件)
全屏截一张图，拖动鼠标框出"捕获提示"出现的区域，回车确认。
"""
import ctypes
import json
import os

import cv2
import numpy as np
import mss
import tkinter as tk
from tkinter import messagebox

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(HERE)

# 从 配置.py 导入所有配置
import sys
sys.path.insert(0, ROOT_DIR)
from 配置 import *


def dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _grab_screen():
    with mss.mss() as sct:
        mon = sct.monitors[1]
        raw = sct.grab(mon)
    return np.array(raw)[:, :, :3]


def do_select():
    """启动 Tk 框选窗口，返回 {"left","top","width","height"} 或 None(取消)。"""
    img = _grab_screen()
    h, w = img.shape[:2]
    print(f"全屏截图: {w}x{h}")

    root = tk.Tk()
    root.title("框选钓鱼监控区域(左键拖动-回车确认, Esc取消)")
    root.state("zoomed")

    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    scale = min(sw / w, sh / h) if w and h else 1.0
    disp_w, disp_h = int(w * scale), int(h * scale)

    canvas = tk.Canvas(root, bg="#404040", cursor="crosshair")
    canvas.pack(fill="both", expand=True)

    disp = cv2.resize(img, (disp_w, disp_h), interpolation=cv2.INTER_AREA)
    _ok, buf = cv2.imencode(".png", disp)
    bg = tk.PhotoImage(data=buf.tobytes())
    canvas.create_image(0, 0, image=bg, anchor="nw")

    result = {"ok": False}
    rect_id = None
    ox = oy = cx = cy = 0

    def screen_to_img(x, y):
        ix = max(0, min(w - 1, int(x / scale)))
        iy = max(0, min(h - 1, int(y / scale)))
        return ix, iy

    def on_press(e):
        nonlocal ox, oy, cx, cy, rect_id
        ox, oy = e.x, e.y
        cx, cy = e.x, e.y
        if rect_id:
            canvas.delete(rect_id)
        rect_id = canvas.create_rectangle(ox, oy, cx, cy,
                                          outline="#00ff66", width=2)

    def on_drag(e):
        nonlocal cx, cy
        cx, cy = e.x, e.y
        canvas.coords(rect_id, ox, oy, cx, cy)

    def confirm(_e=None):
        l, t = screen_to_img(min(ox, cx), min(oy, cy))
        r, b = screen_to_img(max(ox, cx), max(oy, cy))
        if r - l < 5 or b - t < 5:
            messagebox.showwarning("无效区域", "框选的区域太小，请重新框选。")
            return
        result["region"] = {"left": l, "top": t, "width": r - l, "height": b - t}
        result["ok"] = True
        root.destroy()

    def cancel(_e=None):
        root.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", lambda _e: None)
    root.bind("<Return>", confirm)
    root.bind("<Escape>", cancel)

    root.mainloop()
    return result.get("region") if result["ok"] else None


def main():
    dpi_aware()
    rg = do_select()
    if not rg:
        print("已取消框选。")
        return
    
    # 更新全局配置变量
    global region
    region = rg
    
    # 重新加载 配置.py 文件
    import importlib
    import 配置
    importlib.reload(配置)
    
    # 将更新后的配置写回 配置.py 文件
    with open(os.path.join(ROOT_DIR, "配置.py"), "r", encoding="utf-8") as f:
        lines = f.readlines()
    
    # 找到 region 的定义位置并替换
    start_idx = -1
    for i, line in enumerate(lines):
        if line.strip().startswith("region = {"):
            start_idx = i
            break
    
    if start_idx >= 0:
        # 构建新的 region 定义
        new_region_lines = [
            "region = {\n",
            f"    \"left\": {rg['left']},\n",
            f"    \"top\": {rg['top']},\n",
            f"    \"width\": {rg['width']},\n",
            f"    \"height\": {rg['height']}\n",
            "}\n"
        ]
        
        # 替换旧的 region 定义
        lines = lines[:start_idx] + new_region_lines + lines[start_idx+6:]
        
        # 写回文件
        with open(os.path.join(ROOT_DIR, "配置.py"), "w", encoding="utf-8") as f:
            f.writelines(lines)
        
        print(f"[框选] 区域已更新并保存到 配置.py: {rg}")
        return True
    
    print("[错误] 无法更新 配置.py 文件")
    return False


if __name__ == "__main__":
    main()