# -*- coding: utf-8 -*-
"""
辅助工具：框选识别区并更新 config.json 的 zones 坐标。
用法: python select_region.py [区号1-3]
默认更新区1。运行后全屏截图，按住左键框选该区，回车确认。
"""
import ctypes
import json
import os
import sys

import cv2
import numpy as np
import mss

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(HERE)

# 从 配置.py 导入所有配置
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


def main():
    zone_idx = int(sys.argv[1]) - 1 if len(sys.argv) > 1 else 0
    if zone_idx >= len(zones):
        print(f"配置里只有 {len(zones)} 个区，无法选第 {zone_idx+1} 个。")
        sys.exit(1)
    z = zones[zone_idx]

    dpi_aware()
    print(f"正在框选区{zone_idx+1}（{z.get('name','')}）…")
    with mss.mss() as sct:
        mon = sct.monitors[1]
        raw = sct.grab(mon)
    img = np.array(raw)[:, :, :3]
    print(f"屏幕: {img.shape[1]}x{img.shape[0]}，当前区坐标: {z}")

    r = cv2.selectROI("框选识别区 (回车确认, 按c取消)", img.copy(), showCrosshair=True)
    cv2.destroyAllWindows()
    x, y, w, h = r
    if w <= 0 or h <= 0:
        print("未选择有效区域，已取消。")
        return
    zones[zone_idx] = {"id": z["id"], "name": z["name"],
                       "left": x, "top": y, "width": w, "height": h}
    print(f"区{zone_idx+1} 已更新为 left={x} top={y} width={w} height={h}")
    print("注意：请手动更新 配置.py 中的 zones 配置")


if __name__ == "__main__":
    main()