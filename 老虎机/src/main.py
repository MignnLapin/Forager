# -*- coding: utf-8 -*-
"""
Forager 抽奖辅助 - 屏幕识别 + 自动按 E
使用快速截屏(mss) + OpenCV 模板匹配，匹配度达到阈值时自动按键。
"""
import json
import os
import sys
import time
import threading
import signal

import numpy as np
import cv2
import mss
from pynput import keyboard

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(HERE)

# 从 配置.py 导入所有配置
sys.path.insert(0, ROOT_DIR)
from 配置 import *


def ctypes_enable_dpi_awareness():
    """启用 Windows 进程级 DPI 感知，避免截图坐标偏移。"""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class Loader:
    """从 resources 目录(可多级子目录)加载全部物品图标作为识别模板。"""
    IMG_EXTS = (".webp", ".png", ".jpg", ".jpeg", ".bmp")

    def __init__(self, config, log=None):
        self.cfg = config
        self.log = log or (lambda *a: print(*a))
        self.items = []              # [(物品名, 灰度数组)]
        self.by_name = {}            # 物品名 -> 灰度数组

    def load(self):
        base = os.path.join(HERE, self.cfg.get("item_dir", "resources"))
        if not os.path.isdir(base):
            raise FileNotFoundError(f"物品图片目录不存在: {base}")
        self.items = []
        for root, _dirs, files in os.walk(base):
            for f in sorted(files):
                if not f.lower().endswith(self.IMG_EXTS):
                    continue
                name = os.path.splitext(f)[0]
                img = self._imread_unicode(os.path.join(root, f))
                if img is None:
                    self.log(f"[警告] 无法读取 {os.path.join(root, f)}")
                    continue
                self.items.append((name, img))
                self.by_name[name] = img
        if not self.items:
            raise FileNotFoundError(f"物品图片目录为空: {base}")
        self.log(f"[模板] 已加载 {len(self.items)} 个物品图标，来自 {base}")
        return True

    @staticmethod
    def _imread_unicode(path):
        """支持含中文路径的图片读取；带透明背景的图标合成到中灰色背景后转BGR彩色，
        (保留颜色信息，供彩色模板匹配以降低误报)。"""
        try:
            data = np.fromfile(path, dtype=np.uint8)
            img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
            if img is None:
                return None
            if img.ndim == 3 and img.shape[2] == 4:
                a = img[:, :, 3].astype(np.float32) / 255.0
                fg = img[:, :, :3].astype(np.float32)
                comp = fg * a[:, :, None] + 132.0 * (1.0 - a[:, :, None])
                return comp.astype(np.uint8)
            if img.ndim == 3:
                return img
            return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        except Exception:
            return None


class Detector:
    """基于 OpenCV 的模板匹配检测器，识别一张帧里出现的物品名。
    支持两档工作模式：
      - 未校准 / 校准中：多尺度搜索（慢但可找出正确放大倍数）
      - 已校准：固定放大倍数单尺度极速匹配（快）
    """
    def __init__(self, config, items, log=None):
        self.cfg = config
        self.log = log or (lambda *a: print(*a))
        self.scale_min = float(self.cfg.get("scale_min", 1.0))
        self.scale_max = float(self.cfg.get("scale_max", 3.5))
        self.scale_step = float(self.cfg.get("scale_step", 0.15))
        self.threshold = float(self.cfg.get("threshold", 0.8))
        self.threshold_press = float(self.cfg.get("threshold_press", 0.8))
        self.fixed_scale = bool(self.cfg.get("fixed_scale", False))
        self.frames = 0
        # 滑窗匹配参数(符号显示约抽奖区宽的 ~0.55倍，在中央带内寻找)
        self.ratios = [float(x) for x in self.cfg.get("match_ratios", [0.52, 0.60, 0.68])]
        cb = self.cfg.get("center_band", [0.28, 0.72, 0.15, 0.85])
        self.cband = (float(cb[0]), float(cb[1]), float(cb[2]), float(cb[3]))
        self._coarse_n = int(self.cfg.get("coarse_n", 24))
        self._rank_n = int(self.cfg.get("rank_n", 12))   # 粗筛排序后只需精排前 n 个候选
        self._coarse_ratio = float(self.cfg.get("coarse_ratio", 0.74))
        self._coarse_w = int(self.cfg.get("coarse_width", 48))  # 粗筛缩帧宽度，越小越快，40~56 不失真
        # 摊平成 [(物品名, img)]
        self.flat = list(items)
        self.flat_map = {name: img for name, img in self.flat}
        self.effective_scale = None      # 校准得到的放大倍数
        self.prep = []                   # [(物品名, 已缩放的img)]
        self.prep_map = {}               # 物品名 -> 已缩放的img

    def set_scale(self, s):
        """锁定放大倍数并预缩放所有模板（单尺度极速模式）。"""
        self.effective_scale = float(s)
        self.prep = list(self.flat)         # 缩帧匹配模式不再预缩放放大模板
        self.prep_map = dict(self.prep)
        # 预计算每个模板"主体颜色"(中央核均色)，供停止阶段颜色校验使用——
        # TM_CCOEFF_NORMED 会弱化颜色差异导致不同颜色的相似形状互相误认，
        # 命中时额外比对一个颜色项来区分。
        self.tmp_center_color = {}
        for _n2, tmpl in self.prep:
            self.tmp_center_color[_n2] = self._center_color(self._to_bgr(tmpl))
        self.log(f"[校准] 已启用中央带滑窗匹配({len(self.prep)}模板)")

    @staticmethod
    def _center_color(img):
        """取图像中央核(占宽高 60%)的均色(BGR 0-255)。物品通常居中，核内以
        物品色为主，能代表"这个物品是什么颜色"，对背景相对不敏感。"""
        h, w = img.shape[:2]
        x0, x1 = int(w * 0.2), int(w * 0.8)
        y0, y1 = int(h * 0.2), int(h * 0.8)
        if x1 <= x0 or y1 <= y0:
            return [0.0, 0.0, 0.0]
        c = img[y0:y1, x0:x1].reshape(-1, 3).astype(np.float32)
        return c.mean(axis=0)

    def color_compat(self, frame_bgr, target_name):
        """返回画面上物品与目标模板的"主体颜色归一化距离"0~1；越大越不像。
        模板无主体色记录时返回 None(跳过颜色校验)。"""
        ref = self.tmp_center_color.get(target_name)
        if ref is None:
            return None
        frame = self._to_bgr(frame_bgr)
        c = self._center_color(frame)
        d = float(np.linalg.norm(np.asarray(c) - np.asarray(ref)))
        return d / (255.0 * 1.7320508)

    def match_name(self, img, target_name):
        """仅匹配单个目标物品模板，返回 (score, hit)。用于停止阶段极速命中。"""
        tmpl = (self.prep_map if self.effective_scale is not None
                else self.flat_map).get(target_name)
        if tmpl is None:
            return 0.0, False
        score = self._match_one(img, tmpl)
        return score, score >= self.threshold_press

    def _match_one(self, img, tmpl):
        """彩色滑窗匹配：把模板缩放到候选显示尺寸后，在抽奖区中央带内滑窗，
        取三通道 TM_CCOEFF_NORMED 的峰值。
        替代旧"整帧缩图L1"(真实画面上第一二名分差仅0.4%，无法区分任何物品)。
        滑窗在真实定格画面上分差可达~15%，配合相对分差判定可可靠识别。"""
        frame = self._to_bgr(img)
        t = self._to_bgr(tmpl)
        if t.shape[1] <= 3 or t.shape[0] <= 3:
            return 0.0
        ih, iw = frame.shape[:2]
        th2, tw2 = t.shape[:2]
        best = -1.0
        for k in self.ratios:
            ww = max(4, int(iw * k))
            if ww >= iw:
                ww = iw - 2
            hh = max(4, int(round(th2 * ww / float(tw2))))
            if hh >= ih:
                hh = ih - 2
                ww = max(4, int(round(tw2 * hh / float(th2))))
            if ww >= iw or hh >= ih:
                continue
            t2 = cv2.resize(t, (ww, hh), interpolation=cv2.INTER_AREA)
            bx0, bx1, by0, by1 = self.cband
            min_x = max(0, int(bx0 * iw - ww / 2))
            max_x = min(iw - ww, int(bx1 * iw - ww / 2))
            min_y = max(0, int(by0 * ih - hh / 2))
            max_y = min(ih - hh, int(by1 * ih - hh / 2))
            if max_x < min_x or max_y < min_y:
                continue
            val = -1.0
            for ch in range(3):
                r = cv2.matchTemplate(frame[:, :, ch], t2[:, :, ch],
                                      cv2.TM_CCOEFF_NORMED)
                sub = r[min_y:max_y + 1, min_x:max_x + 1]
                _, mv, _, _ = cv2.minMaxLoc(sub)
                if mv > val:
                    val = float(mv)
            if val > best:
                best = val
        return max(0.0, best)

    def _match_at(self, img, tmpl, k):
        """在单一显示比例下做中央带滑窗，返回彩色 TM_CCOEFF 峰值(单档，用于快速粗筛)。"""
        frame = self._to_bgr(img)
        t = self._to_bgr(tmpl)
        if t.shape[1] <= 3 or t.shape[0] <= 3:
            return 0.0
        ih, iw = frame.shape[:2]
        th2, tw2 = t.shape[:2]
        ww = max(4, int(iw * k))
        if ww >= iw:
            ww = iw - 2
        hh = max(4, int(round(th2 * ww / float(tw2))))
        if hh >= ih:
            hh = ih - 2
            ww = max(4, int(round(tw2 * hh / float(th2))))
        if ww >= iw or hh >= ih:
            return 0.0
        t2 = cv2.resize(t, (ww, hh), interpolation=cv2.INTER_AREA)
        bx0, bx1, by0, by1 = self.cband
        min_x = max(0, int(bx0 * iw - ww / 2))
        max_x = min(iw - ww, int(bx1 * iw - ww / 2))
        min_y = max(0, int(by0 * ih - hh / 2))
        max_y = min(ih - hh, int(by1 * ih - hh / 2))
        if max_x < min_x or max_y < min_y:
            return 0.0
        val = -1.0
        for ch in range(3):
            r = cv2.matchTemplate(frame[:, :, ch], t2[:, :, ch],
                                  cv2.TM_CCOEFF_NORMED)
            sub = r[min_y:max_y + 1, min_x:max_x + 1]
            _, mv, _, _ = cv2.minMaxLoc(sub)
            if mv > val:
                val = float(mv)
        return max(0.0, val)

    def _coarse(self, img):
        """快速彩色粗筛：用单一显示比例对整个区域滑窗挑候选(能定位符号)。
        必须保留颜色：宝石类(黄玉/翡翠/红宝石)靠颜色区分，灰度粗筛会把它们
        排到 20-53 名挤出候选导致漏检。档位贴近实际显示尺寸(coarse_ratio≈0.92)。
        缩帧粗筛：把帧缩小到 coarse_width 宽度再对所有模板匹配，计算量约降 k²，
        排序基本不变但对整帧像素做滑窗 —— 抽奖滚动需极快抓帧以避免按错/漏按。"""
        frame = self._to_bgr(img)
        cw = max(24, self._coarse_w)
        if frame.shape[1] > int(cw * 1.05):
            k = cw / float(frame.shape[1])
            frame = cv2.resize(frame, (int(frame.shape[1] * k),
                                       int(frame.shape[0] * k)),
                               interpolation=cv2.INTER_AREA)
        out = []
        for name, tmpl in self.prep:
            out.append((self._match_at(frame, tmpl, self._coarse_ratio), name))
        return out

    def topk(self, frame_bgr, k=3):
        """返回按匹配分降序的 [(分, 物品名), ...] 前 k 个。
        两阶段：灰度粗筛出候选排序 → 只对前 _rank_n 个候选跑精确彩色滑窗，
        兼顾速度与准确(实测 top12 已覆盖真实命中项)。"""
        img = self._to_bgr(frame_bgr)
        coarse = self._coarse(img)
        coarse.sort(reverse=True)
        res = []
        for _sc, n in coarse[: self._rank_n]:
            res.append((self._match_one(img, self.prep_map[n]), n))
        res.sort(reverse=True)
        return res[:k]

    @staticmethod
    def _to_bgr(x):
        if x.ndim == 2:
            return cv2.cvtColor(x, cv2.COLOR_GRAY2BGR)
        if x.ndim == 3 and x.shape[2] == 4:
            return x[:, :, :3]
        return x

    def _score_for_scale(self, img, tmpl):
        """缩帧匹配模式下，直接按原模板尺寸匹配(面积归一化)，不受倍数影响。"""
        return self._match_one(img, tmpl)

    def calibrate_scale(self, frame, idx):
        """缩帧匹配模式下校准：直接用原模板匹配整帧，返回 (score, 1.0, name)。
        倍数不再参与匹配，返回 1.0 仅保持调用方接口语义。"""
        img = self._to_bgr(frame)
        best_score, best_scale, best_name = 0.0, 1.0, None
        for name, tmpl in self.flat:
            sc = self._match_one(img, tmpl)
            if sc > best_score:
                best_score, best_scale, best_name = sc, 1.0, name
        return best_score, best_scale, best_name

    def detect(self, frame_bgr, _center=None):
        """
        返回 (name, score, is_hit)。
        name: 命中最高分的物品名；无命中为 None。
        """
        top = self.topk(frame_bgr, 2)
        if not top:
            self.frames += 1
            return None, 0.0, False
        name, score = top[0][1], float(top[0][0])
        is_hit = score >= self.threshold
        self.frames += 1
        return name, score, is_hit

class KeyPresser:
    """自动按键控制。"""
    def __init__(self, config, log=None):
        self.cfg = config
        self.log = log or (lambda *a: print(*a))
        self.key_name = self._normalize(config.get("press_key", "e"))
        self.controller = keyboard.Controller()
        try:
            self.key_type = getattr(keyboard.Key, self.key_name, self.key_name)
        except Exception:
            self.key_type = self.key_name

    @staticmethod
    def _normalize(k):
        if not isinstance(k, str):
            k = str(k)
        return k.strip().lower()

    def press(self):
        try:
            self.controller.press(self.key_type)
            time.sleep(float(self.cfg.get("press_delay", 0.05)))
            self.controller.release(self.key_type)
            self.log(f"[按键] 已按下 {self.cfg.get('press_key')}")
            return True
        except Exception as e:
            self.log(f"[按键] 失败: {e}")
            return False


class App:
    def __init__(self, config):
        self.cfg = config
        self.print = print
        self.stop_flag = threading.Event()          # 退出程序
        self.accepting = threading.Event()          # 是否允许按 E
        from engine import SlotEngine
        self.engine = SlotEngine(config, log=self.print)
        self.engine.accepting = self.accepting      # 共享接受标志
        self.frames_total = 0
        self._last_stats = time.time()
        self._boot_time = time.time()

    # ---------- 热键 ----------
    def on_press(self, k):
        try:
            kk = k.char.lower() if hasattr(k, "char") and k.char else None
        except Exception:
            kk = None
        name = kk or getattr(k, "name", None) or str(k).lower().replace("'", "")
        if name == str(self.cfg.get("exit_hotkey", "f9")).lower():
            self.print("[控制] 收到退出热键，正在退出…")
            self.stop_flag.set()
            self.engine.stop()
        elif name == str(self.cfg.get("start_hotkey", "f8")).lower():
            if not self.accepting.is_set():
                self.accepting.set()
                self.engine.accepting.set()
                self.print("[控制] >>> 启动识别，命中目标将按 E <<<")
            else:
                self.accepting.clear()
                self.engine.accepting.clear()
                self.print("[控制] 已暂停识别。")

    def start_listener(self):
        lst = keyboard.Listener(on_press=self.on_press, suppress=False)
        lst.daemon = True
        lst.start()
        self.listener = lst

    # ---------- 主循环（引擎在后台线程跑，这里等待停止） ----------
    def run(self):
        self.running = threading.Event()
        self.running.set()
        self.print("=" * 50)
        self.print("Forager 抽奖辅助已启动（三区抽奖模式）")
        self.print(f"启动/暂停热键: {self.cfg['start_hotkey']}   退出热键: {self.cfg['exit_hotkey']}")
        self.print("流程: 观测奖品池 → 按(权重)自动选目标 → 逐区(1→2→3)识别并按E停止")
        self.print("=" * 50)
        self.engine.start()
        try:
            while not self.stop_flag.is_set():
                time.sleep(0.2)
        finally:
            self.engine.stop()


def main():
    ctypes_enable_dpi_awareness()
    config_path = CONFIG_PATH
    if not os.path.exists(config_path):
        print(f"缺少配置文件: {config_path}")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    app = App(config)
    app._boot_time = time.time()
    app.start_listener()
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    print("已退出。")


if __name__ == "__main__":
    main()