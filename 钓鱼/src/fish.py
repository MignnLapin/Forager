# -*- coding: utf-8 -*-
"""
辅助钓鱼 - 屏幕识别"捕获提示"并自动点击左键两次
流程: 打开后按 F5 框选监控区域 → F8 开始识别 →
     区域内画面与模板图(Y/G)彩色滑窗匹配，最高相似度≥阈值 → 点击左键两次。

热键:
  F5   框选/重选监控区域(选中后自动生效)
  F8   开始/暂停识别
  F9   退出程序
"""
import ctypes
import json
import os
import threading
import time

import cv2
import numpy as np
import mss
from pynput import keyboard, mouse

from select_region import do_select

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(HERE)
TEMPLATE_DIR = os.path.join(HERE, "resources")

# 从 配置.py 导入所有配置
import sys
sys.path.insert(0, ROOT_DIR)
from 配置 import *


def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def imread_unicode(path):
    data = np.fromfile(path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if img is None:
        return None
    if img.ndim == 3 and img.shape[2] == 4:
        return img[:, :, :3]
    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return img


class Config:
    def __init__(self):
        pass  # 配置已从 配置.py 导入全局变量

    def get(self, key, dflt):
        # 直接返回全局配置变量
        global_vars = globals()
        return global_vars.get(key, dflt)

    def region(self):
        return region


class Fisher:
    def __init__(self, config):
        self.cfg = config
        self.threshold = float(config.get("threshold", 0.70))
        self.cooldown = float(config.get("cooldown_seconds", 2.0))
        self.click_interval = float(config.get("click_interval", 0.15))
        self.click_times = int(config.get("click_times", 2))
        self.match_ratios = [float(x) for x in
                             config.get("match_ratios",
                                        [0.4, 0.55, 0.7, 0.85, 1.0, 1.2])]

        # 加载模板 (两张：Y 和 G)
        self.templates = []
        for fname in config.get("resources",
                                ["捕获提示_Y.png", "捕获提示_G.png"]):
            p = os.path.join(TEMPLATE_DIR, fname)
            img = imread_unicode(p)
            if img is not None:
                self.templates.append((fname, img))
                print(f"[模板] 加载 {fname} -> {img.shape[1]}x{img.shape[0]}")
        if not self.templates:
            raise FileNotFoundError(f"没有可用模板，目录: {TEMPLATE_DIR}")

        # 鼠标
        self.mouse = mouse.Controller()
        self.accepting = False
        self.stopped = threading.Event()

        self._last_click_at = 0.0     # 冷却计时，防止同一次提示反复点击
        self.corner = config.get("region", {}) or {}
        self._select_lock = threading.Lock()

    # ---------- 框选区域 ----------
    def select_region(self):
        """弹出框选窗口(在独立线程运行，避免阻塞主循环)。选中后保存并即时生效。"""
        def _run_select():
            if not self._select_lock.acquire(blocking=False):
                return
            try:
                rg = do_select()
                if rg:
                    self.cfg.cfg["region"] = rg
                    self.cfg.save()
                    self.corner = rg
                    print(f"[框选] 区域已更新并保存: {rg}")
                else:
                    print("[框选] 已取消，沿用旧区域。")
            except Exception as e:
                print(f"[框选] 异常: {e!r}")
            finally:
                self._select_lock.release()

        threading.Thread(target=_run_select, daemon=True).start()

    # ---------- 匹配 ----------
    def match(self, frame):
        """返回区域内画面与所有模板最高相似度(0~1)。"""
        best = 0.0
        ih, iw = frame.shape[:2]
        if ih < 8 or iw < 8:
            return best
        for _name, tmpl in self.templates:
            th, tw = tmpl.shape[:2]
            if tw < 4 or th < 4:
                continue
            # 多档滑窗彩色匹配，取 B/G/R 三通道各自 TM_CCOEFF_NORMED 的峰值
            for k in self.match_ratios:
                ww = max(6, int(iw * k))
                if ww >= iw:
                    ww = iw - 2
                hh = max(6, int(round(th * ww / float(tw))))
                if hh >= ih:
                    hh = ih - 2
                    ww = max(6, int(round(tw * hh / float(th))))
                if ww >= iw or hh >= ih:
                    continue
                t2 = cv2.resize(tmpl, (ww, hh), interpolation=cv2.INTER_AREA)
                val = -1.0
                for ch in range(3):
                    r = cv2.matchTemplate(frame[:, :, ch], t2[:, :, ch],
                                          cv2.TM_CCOEFF_NORMED)
                    _, mv, _, _ = cv2.minMaxLoc(r)
                    if mv > val:
                        val = float(mv)
                if val > best:
                    best = val
        return best

    # ---------- 点击 ----------
    def click_twice(self):
        """在原地点击: 不移动鼠标，直接在当前光标位置连点 click_times 次。"""
        for i in range(self.click_times):
            self.mouse.press(mouse.Button.left)
            time.sleep(0.02)
            self.mouse.release(mouse.Button.left)
            if i < self.click_times - 1:
                time.sleep(self.click_interval)
        print(f"[点击] 已在当前位置点击 {self.click_times} 次")

    # ---------- 主循环 ----------
    def run(self):
        print("=" * 50)
        print("辅助钓鱼 - F5框选区域 | F8开始/暂停 | F9退出")
        print(f"阈值: {self.threshold*100:.0f}% | 点击{self.click_times}次 | "
              f"冷却{self.cooldown}秒")
        print("=" * 50)
        if not self.corner:
            print("[提示] 尚未选择区域，请按 F5 框选监控区域。")
        sct = mss.mss()
        mon = sct.monitors[1]
        start = time.time()
        frames = 0
        try:
            while not self.stopped.is_set():
                if not self.accepting:
                    time.sleep(0.15)
                    continue
                cg = self.corner
                if not cg:
                    time.sleep(0.5)
                    print("[错误] 尚未框选区域，请按 F5 框选。")
                    continue
                r = {
                    "left": mon["left"] + int(cg["left"]),
                    "top": mon["top"] + int(cg["top"]),
                    "width": int(cg["width"]),
                    "height": int(cg["height"]),
                }
                raw = sct.grab(r)
                frame = np.array(raw)[:, :, :3]
                frames += 1

                score = self.match(frame)
                now = time.time()
                if score >= self.threshold and now - self._last_click_at >= self.cooldown:
                    self._last_click_at = now
                    print(f"[命中] 相似度 {score*100:.1f}% >= "
                          f"{self.threshold*100:.0f}%，点击")
                    self.click_twice()

                if time.time() - start >= 2.0:
                    print(f"[状态] FPS≈{frames/2.0:.1f} | 当前相似度 {score*100:.1f}%")
                    start = time.time()
                    frames = 0
        finally:
            sct.close()


def main():
    enable_dpi_awareness()
    config = Config(CONFIG_PATH)
    fisher = Fisher(config)

    def on_press(key):
        try:
            name = key.char.lower() if key.char else None
        except Exception:
            name = None
        name = name or getattr(key, "name", None) or ""
        if name in ("f5",):
            print("[控制] 打开框选窗口…")
            fisher.select_region()
        elif name in ("f8",):
            if not fisher.corner:
                print("[提示] 请先按 F5 框选监控区域。")
            else:
                fisher.accepting = not fisher.accepting
                print("[控制] >>> 开始识别" if fisher.accepting
                      else "[控制] 已暂停。")
        elif name in ("f9",):
            print("[控制] 退出…")
            fisher.stopped.set()

    lst = keyboard.Listener(on_press=on_press, suppress=False)
    lst.daemon = True
    lst.start()

    try:
        fisher.run()
    except KeyboardInterrupt:
        pass
    finally:
        lst.stop()
    print("已退出。")


if __name__ == "__main__":
    main()