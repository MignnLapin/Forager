"""
右上角悬浮窗自动识别助手
全自动模式：通过 OCR 悬停识别奖品文字，在"终结"出现时点击祭坛。
"""
import sys
import json
import os
import time
import ctypes
import math
import difflib
import threading
import subprocess
from ctypes import wintypes

import cv2
import numpy as np
import mss
import tkinter as tk
import tkinter.font as tkfont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
RES_DIR = os.path.join(BASE_DIR, "resources")

# 从 配置.py 读取配置
import sys
sys.path.insert(0, ROOT_DIR)
from 配置 import Config

# 加载配置实例
_cfg_instance = Config()
_path_cfg = {
    "tesseract_exe": _cfg_instance.tesseract_exe,
    "forager_exe": _cfg_instance.forager_exe,
    "sound_effect": _cfg_instance.sound_effect,
    "targets": _cfg_instance.targets,
    "negatives": _cfg_instance.negatives,
    "rounds": _cfg_instance.rounds,
    "match_threshold": _cfg_instance.match_threshold,
    "negative_margin": _cfg_instance.negative_margin,
    "stop_heart": _cfg_instance.stop_heart,
    "regions": _cfg_instance.regions,
    "ocr_regions": _cfg_instance.ocr_regions
}

# 路径配置类（保持兼容性）
class _PathConfig:
    tesseract_exe = _cfg_instance.tesseract_exe
    forager_exe = _cfg_instance.forager_exe
    sound_effect = _cfg_instance.sound_effect

user32 = ctypes.windll.user32

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
VK_F8 = 0x77
VK_ESC = 0x1B
VK_E = 0x45
VK_CAPITAL = 0x14
AUTO_HOTKEY_ID = 2       # F8: 全自动模式

def _get_tesseract_exe():
    """返回 tesseract.exe 的路径，优先使用 lib/ 目录下的本地副本。"""
    local = os.path.join(BASE_DIR, "lib", "tesseract", "tesseract.exe")
    if os.path.isfile(local):
        return local
    return _path_cfg.get("tesseract_exe", local)

# --- 路径相关配置（从 配置.py 读取） ---
FORAGER_EXE = _path_cfg.get("forager_exe", r"C:\Games\Forager\Forager.exe")
_sfx_raw = _path_cfg.get("sound_effect", "音效.mp3")
SFX_FILE = _sfx_raw if os.path.isabs(_sfx_raw) else os.path.join(RES_DIR, _sfx_raw)

# 各奖品卡片的期望文字（名称/损失1点最大生命值/描述），用于 OCR 结果的模糊比对
PRIZE_TEXTS = {
    "终结": "终结/损失1点最大生命值/获得机器人和手雷",
    "炼药": "炼药/损失1点最大生命值/获取一些药水",
    "狂怒": "狂怒/损失1点最大生命值/永久增加1点伤害",
    "贪婪": "贪婪/损失1点最大生命值/获得一些金币和宝石",
    "暴食": "暴食/损失1点最大生命值/获得一些食物",
    "挑战": "挑战/损失1点最大生命值/召唤精英敌人",
    "毁灭": "毁灭/损失1点最大生命值/获取一些恶魔法术卷轴",
}
PRIZE_MATCH_THRESHOLD = 0.6   # 整段文字匹配度达到该值即视为该奖品


def clean_cn(s):
    """去掉字符串中所有空白（OCR 常在字/行间加空格）。"""
    return "".join(s.split())


def classify_text(text):
    """返回 OCR 文本最像的奖品名及其匹配度 (0~1)。"""
    clean = clean_cn(text)
    best, bestr = "", 0.0
    for name, std in PRIZE_TEXTS.items():
        r = difflib.SequenceMatcher(None, clean, clean_cn(std)).ratio()
        if r > bestr:
            bestr, best = r, name
    return best, bestr

HEART_BOX = (2, 2, 360, 170)          # 爱心计数区域，扩大为 (2,2 ~ 360,170)


def key_down(vk):
    user32.keybd_event(vk, 0, 0, 0)


def key_up(vk):
    user32.keybd_event(vk, 0, 0x0002, 0)


def tap(vk):
    key_down(vk)
    time.sleep(0.05)
    key_up(vk)


def caps_on():
    return bool(user32.GetKeyState(VK_CAPITAL) & 1)


def ensure_capslock():
    if not caps_on():
        tap(VK_CAPITAL)


def set_dpi_aware():
    """让进程感知系统 DPI，使坐标与鼠标/截图的物理像素一致，避免窗口定位错位。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_AWARE_V2
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", ctypes.c_ulong)]


def primary_work_rect():
    """返回主显示器工作区 (left, top, right, bottom)，单位物理像素。"""
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(_MONITORINFO)
    hm = user32.MonitorFromPoint(0, 0, 0x00000001)   # MONITOR_DEFAULTTOPRIMARY
    if hm and user32.GetMonitorInfoW(hm, ctypes.byref(mi)):
        return (mi.rcWork.left, mi.rcWork.top, mi.rcWork.right, mi.rcWork.bottom)
    return (0, 0, 0, 0)


def _send_mouse(flags):
    """用 SendInput 在当前光标位置发送一次鼠标事件（flags: 0x02 按下 / 0x04 抬起）。"""
    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]
    class INPUT(ctypes.Structure):
        class _I(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT)]
        _anonymous_ = ("i",)
        _fields_ = [("type", ctypes.c_ulong), ("i", _I)]
    S = ctypes.windll.user32.SendInput
    S.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
    S.restype = ctypes.c_uint
    inp = INPUT()
    inp.type = 0                     # INPUT_MOUSE
    inp.mi.dx = 0                    # 相对当前光标，0=不移动
    inp.mi.dy = 0
    inp.mi.mouseData = 0
    inp.mi.dwFlags = flags
    inp.mi.time = 0
    inp.mi.dwExtraInfo = None
    return S(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def mouse_move(x, y):
    user32.SetCursorPos(int(x), int(y))


def mouse_click(x, y, hold=0.05):
    """移动并左键点击。先移动定位，再按下、保持、抬起，模拟真实按压。"""
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.03)
    _send_mouse(0x0002)              # LEFTDOWN
    time.sleep(hold)                 # 保持按压一小段时间，游戏更易响应
    _send_mouse(0x0004)              # LEFTUP
    time.sleep(0.03)


def pct(x):
    """显示为百分比；无有效匹配(NaN/负值，通常该区域为纯色或无内容)时显示 0.0%。"""
    x = float(x)
    if math.isnan(x) or x < 0:
        x = 0.0
    return f"{x * 100.0:.1f}%"


def load_template(name):
    path = os.path.join(RES_DIR, name + ".png")
    # cv2.imread 不支持中文路径，改用 fromfile+imdecode（Unicode 安全）
    data = np.fromfile(path, dtype=np.uint8)
    bgr = cv2.imdecode(data, cv2.IMREAD_COLOR)   # 丢弃 alpha，仅用 RGB，与 auto_click 的 PIL 读取灰度一致
    if bgr is None:
        raise FileNotFoundError(path)
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)


class Matcher:
    def __init__(self, cfg):
        self.targets = cfg.get("targets", ["亮", "暗"])
        self.negatives = cfg.get("negatives", ["N亮", "N暗"])
        self.scales = np.arange(
            float(cfg.get("scale_from", 0.8)),
            float(cfg.get("scale_to", 1.2)) + 1e-9,
            float(cfg.get("scale_step", 0.05)),
        )
        # 自动识别所有图标模板（除 爱心 / 任何"开始游戏" 外都参与识别）
        icons = [f[:-4] for f in os.listdir(RES_DIR)
                 if f.lower().endswith(".png")
                 and f[:-4] != "爱心"
                 and not f[:-4].startswith("开始游戏")]
        self.templates = {n: load_template(n) for n in icons}

    def match_template(self, region_gray, template, scale):
        h, w = region_gray.shape
        th, tw = template.shape
        nw, nh = int(round(tw * scale)), int(round(th * scale))
        if nw <= 0 or nh <= 0 or nw > w or nh > h:
            return -1.0, (0, 0)
        t = cv2.resize(template, (nw, nh))
        res = cv2.matchTemplate(region_gray.astype(np.float32), t, cv2.TM_CCOEFF_NORMED)
        _mn, mx, _ml, _mxl = cv2.minMaxLoc(res)
        return float(mx), _mxl

    def best_for(self, region_gray, name):
        tmpl = self.templates[name]
        best = -1.0
        for s in self.scales:
            v, _pos = self.match_template(region_gray, tmpl, s)
            if v > best:
                best = v
        return best

    def evaluate(self, region_gray):
        """返回 {模板名: 相似度}，共 targets+negatives 全部模板。"""
        detail = {}
        for n in self.templates:
            detail[n] = self.best_for(region_gray, n)
        return detail


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        # OCR 悬停识别每区轮数（全自动用）
        self.rounds = int(cfg.get("rounds", 4))
        self.regions = [r for r in cfg.get("regions", [])]
        for r in self.regions:
            r["cx"] = (r["left"] + r["right"]) / 2.0
            r["cy"] = (r["top"] + r["bottom"]) / 2.0
        self.ocr_regions = [r for r in cfg.get("ocr_regions", self.regions)]
        self.matcher = Matcher(cfg)
        self.sct = mss.mss()
        # 悬浮窗尺寸（固定 450x360，内容自动收缩）
        self.WIN_W = int(cfg.get("window_w", 450))
        self.WIN_H = int(cfg.get("window_h", 360))
        self.result_threshold = float(cfg.get("result_threshold", 0.55))
        self._build_ui()
        self.auto_running = False
        self.auto_stop = threading.Event()
        self.logpath = os.path.join(BASE_DIR, "全自动.log")
        try:
            open(self.logpath, "w", encoding="utf-8").close()   # 每次启动清空日志
        except Exception:
            pass
        self._place_bottom_left()
        self._refresh("待机：按 F8 开始全自动")
        self._start_hotkey_thread()

    # ---------------- UI ----------------
    def _build_ui(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.win = tk.Toplevel(self.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.9)

        BG, FG = "#1e1f24", "#f0f0f0"
        self.win.configure(bg=BG)
        self.win.attributes("-alpha", 0.88)

        head = tk.Frame(self.win, bg=BG)
        head.pack(fill="x", padx=6, pady=(5, 0))
        tk.Label(head, text="抽奖助手  F8全自动",
                 bg=BG, fg="#9fd0ff", font=("Microsoft YaHei UI", 9, "bold")).pack(side="left")
        self.close_btn = tk.Label(head, text="  ✕  ", bg=BG, fg=FG,
                                  font=("Microsoft YaHei UI", 9))
        self.close_btn.pack(side="right")
        self.close_btn.bind("<Button-1>", lambda e: self._quit())

        self.lbl = tk.Label(self.win, text="待机：按 F8 开始全自动",
                            justify="left", anchor="nw",
                            bg=BG, fg=FG, font=("NSimSun", 10),
                            padx=8, pady=4)
        self.lbl.pack(fill="both", expand=True)

        # 精确收缩：右边减 1.5 个中文字符宽度，下面减 2.5 个中文字符行高
        f = tkfont.Font(font=self.lbl.cget("font"))
        self.WIN_W = int(self.WIN_W - 1.5 * f.measure("中"))
        self.WIN_H = int(self.WIN_H - 2.5 * f.metrics("linespace"))

    def _place_bottom_left(self):
        """固定位置：随内容自适应大小并贴主屏左下角，不做拖拽交互。"""
        self._autofit()

    def _quit(self):
        self.auto_stop.set()
        try:
            self.sct.close()
        except Exception:
            pass
        self.root.destroy()

    # ---------------- UI 辅助 ----------------
    def _refresh(self, text):
        if self._alive():
            self.lbl.configure(text=text)
            self._autofit()

    def _autofit(self):
        """悬浮窗尺寸随内容自动变化，并始终贴主屏左下角。"""
        if not self._alive():
            return
        self.win.update_idletasks()
        w = max(self.win.winfo_reqwidth(), 160)
        h = max(self.win.winfo_reqheight(), 40)
        l, t, r, b = primary_work_rect()
        if r - l <= 0 or b - t <= 0:
            l, t = 0, 0
            r = self.root.winfo_screenwidth()
            b = self.root.winfo_screenheight()
        self.win.geometry(f"{w}x{h}+{l}+{max(b - h, t)}")

    def _start_hotkey_thread(self):
        threading.Thread(target=self._hotkey_pump, daemon=True).start()

    def _hotkey_pump(self):
        # 消息专用隐藏窗口，用于接收全局热键 F8
        HWND_MESSAGE = -3
        hwnd = user32.CreateWindowExW(
            0, "STATIC", "", 0, 0, 0, 0, 0, ctypes.c_void_p(HWND_MESSAGE), None, None, None)
        user32.RegisterHotKey(hwnd, AUTO_HOTKEY_ID, 0, VK_F8)
        msg = wintypes.MSG()
        while not self.auto_stop.is_set():
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:
                break
            if msg.message == WM_HOTKEY and msg.wParam == AUTO_HOTKEY_ID:
                try:
                    self.root.after(0, self._on_auto_start)
                except Exception:
                    pass
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnregisterHotKey(hwnd, AUTO_HOTKEY_ID)
        user32.DestroyWindow(hwnd)

    # ---------------- 流程（手动识别已移除） ----------------
    def _alive(self):
        try:
            return self.root.winfo_exists()
        except Exception:
            return False

    def log(self, text):
        ts = time.strftime("%H:%M:%S")
        try:
            self.root.after(0, lambda: self._refresh(text))
        except Exception:
            pass
        try:
            with open(self.logpath, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {text}\n")
        except Exception:
            pass
        try:
            print(f"[{ts}] {text}", flush=True)   # 同时输出到控制台
        except Exception:
            pass

    def _wake(self, sec):
        """可分段的等待：按 F8 停止时能立即醒来退出流程。"""
        end = time.time() + sec
        while time.time() < end:
            if self.auto_stop.is_set() or not self._alive():
                return False
            time.sleep(0.1)
        return True

    def play_sfx(self, mp3=None):
        """非阻塞播放提示音 mp3（winmm mciSendStringW，与已验证可播的 ps1 相同写法）。"""
        try:
            if mp3 is None:
                mp3 = SFX_FILE
            mci = ctypes.windll.winmm.mciSendStringW
            mci(u"close sfx", None, 0, 0)
            if os.path.exists(mp3):
                cmd = u'open "{}" type mpegvideo alias sfx'.format(mp3)
                if mci(cmd, None, 0, 0) == 0:
                    mci(u"play sfx", None, 0, 0)
        except Exception:
            pass

    def _on_auto_start(self):
        if not self._alive():
            return
        if self.auto_running:
            self.auto_stop.set()
            self.auto_running = False
            self.log("全自动已停止")
            try:
                self.root.after(0, lambda: self._refresh("已停止：按 F8 重新开始"))
            except Exception:
                pass
            return
        self.auto_stop.clear()
        self.auto_running = True
        threading.Thread(target=self.auto_flow, daemon=True).start()

    def grab_box_gray(self, box):
        l, t, r, b = box
        shot = self.sct.grab({"left": l, "top": t, "width": r - l, "height": b - t})
        frame = np.asarray(shot, dtype=np.uint8)
        return cv2.cvtColor(frame[:, :, :3], cv2.COLOR_BGR2GRAY)

    def grab_box(self, box):
        """抓取区域，返回 RGB 彩色 float32。"""
        l, t, r, b = box
        shot = self.sct.grab({"left": l, "top": t, "width": r - l, "height": b - t})
        frame = np.asarray(shot, dtype=np.uint8)
        return cv2.cvtColor(frame[:, :, :3], cv2.COLOR_BGR2RGB).astype(np.float32)

    def ocr_box(self, box):
        """抓取区域放大后交给 tesseract 识别中文，返回文本（换行拼为 /）；失败返回空串。"""
        l, t, r, b = box
        try:
            shot = self.sct.grab({"left": l, "top": t, "width": r - l, "height": b - t})
            frame = np.asarray(shot, dtype=np.uint8)[:, :, :3]
            h, w = frame.shape[:2]
            big = cv2.resize(frame, (w * 3, h * 3), interpolation=cv2.INTER_CUBIC)
            outdir = os.path.join(BASE_DIR, "_ocr")
            os.makedirs(outdir, exist_ok=True)
            tmp = os.path.join(outdir, "_t.png")
            ok, buf = cv2.imencode(".png", big)
            np.array(buf).tofile(tmp)
            base = os.path.join(outdir, "_t")
            subprocess.run([_get_tesseract_exe(), tmp, base, "-l", "chi_sim", "--psm", "6"],
                           capture_output=True)
            txt = os.path.join(outdir, "_t.txt")
            with open(txt, "r", encoding="utf-8") as f:
                return f.read().strip().replace("\n", "/")
        except Exception:
            try:
                if os.path.exists(txt):
                    with open(txt, "r", encoding="utf-8") as f:
                        return f.read().strip().replace("\n", "/")
            except Exception:
                pass
            return ""

    def _load_color(self, name):
        """读取彩色模板转 RGB float32（load_template 仅返回灰度，彩色计数不能丢色）。"""
        data = np.fromfile(os.path.join(RES_DIR, name + ".png"), dtype=np.uint8)
        bgr = cv2.imdecode(data, cv2.IMREAD_COLOR)
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB).astype(np.float32)

    def count_hearts_box(self, box):
        """识别指定区域里有多少个爱心图标（固定用 resources/爱心.png 彩色模板，不再从当前画面自提取）。"""
        rgb = self.grab_box(box)
        tmpl = self._load_color("爱心")
        th_h, tw_w = tmpl.shape[:2]   # (高, 宽, 通道)
        peaks = []
        for s in self.matcher.scales:
            t = cv2.resize(tmpl, (max(1, int(tw_w * s)), max(1, int(th_h * s))))
            if t.shape[0] > rgb.shape[0] or t.shape[1] > rgb.shape[1]:
                continue
            res = cv2.matchTemplate(rgb, t, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(res >= 0.45)
            for y, x in zip(ys.tolist(), xs.tolist()):
                peaks.append((float(res[y, x]), int(x), int(y)))
        # 简单非极大抑制：同一爱心只算一次
        peaks.sort(reverse=True, key=lambda p: p[0])
        accepted = []
        gdist = max(12, min(tw_w, th_h) // 2)
        for score, x, y in peaks:
            if all((x - ax) ** 2 + (y - ay) ** 2 >= gdist * gdist for _, ax, ay in accepted):
                accepted.append((score, x, y))
        return len(accepted)

    def wait_until(self, tpl_name, box, timeout=25):
        """持续识别区域，直到目标模板相似度达标，或超时/被停止。用彩色帧匹配（与离线测试一致，避免灰度/彩色通道不符）。"""
        self.log(f"正在识别：{tpl_name}")
        tmpl = cv2.cvtColor(load_template(tpl_name), cv2.COLOR_BGR2RGB).astype(np.float32)  # 与 grab_box 同为 RGB float32
        th_h, tw_w = tmpl.shape[:2]   # (高, 宽, 通道)
        deadline = time.time() + timeout
        start = time.time()
        last_log = 0.0
        dbg = False
        while time.time() < deadline:
            if self.auto_stop.is_set() or not self._alive():
                return False
            rgb = self.grab_box(box)
            best = -1.0
            for s in self.matcher.scales:
                t = cv2.resize(tmpl, (max(1, int(tw_w * s)), max(1, int(th_h * s))))
                if t.shape[0] > rgb.shape[0] or t.shape[1] > rgb.shape[1]:
                    continue
                res = cv2.matchTemplate(rgb, t, cv2.TM_CCOEFF_NORMED)
                _m, mx, _l, _x = cv2.minMaxLoc(res)
                if mx > best:
                    best = float(mx)
            if not dbg:   # 首次进入时保存当前画面，便于排查停在什么界面
                dbg = True
                np.clip(rgb, 0, 255, out=rgb)
                dbg_path = os.path.join(BASE_DIR, f"debug_{tpl_name}.png")
                _ok, _png = cv2.imencode(".png", cv2.cvtColor(rgb.astype(np.uint8), cv2.COLOR_RGB2BGR))
                np.array(_png).tofile(dbg_path)   # 中文路径用 tofile 写，imwrite 会静默失败
                self.log(f"已保存当前画面到：{dbg_path}")
            if best >= self.result_threshold:
                self.log(f"识别到 {tpl_name}（相似度 {pct(best)}）")
                return True
            now = time.time()
            if now - last_log >= 1:
                last_log = now
                self.log(f"等待 {tpl_name} 中… 当前相似度 {pct(best)}，已用 {int(now - start)}s/{timeout}s")
            time.sleep(0.3)
        self.log(f"{tpl_name} 等待超时（{timeout}s）")
        return False

    def score_box(self, box, tpl_name):
        """返回区域(左,上,右,下)内目标模板的最佳相似度(0~1)；无有效匹配返回0。"""
        try:
            tmpl = cv2.cvtColor(load_template(tpl_name), cv2.COLOR_BGR2RGB).astype(np.float32)
        except Exception:
            return 0.0
        th_h, tw_w = tmpl.shape[:2]
        rgb = self.grab_box(box)
        best = 0.0
        for s in self.matcher.scales:
            t = cv2.resize(tmpl, (max(1, int(tw_w * s)), max(1, int(th_h * s))))
            if t.shape[0] > rgb.shape[0] or t.shape[1] > rgb.shape[1]:
                continue
            res = cv2.matchTemplate(rgb, t, cv2.TM_CCOEFF_NORMED)
            _m, mx, _l, _x = cv2.minMaxLoc(res)
            if float(mx) > best:
                best = float(mx)
        return max(0.0, best)

    def wait_forever(self, box, tpl_name):
        """一直等待目标模板出现，无超时；直到识别到或手动停止。"""
        last_log = 0.0
        while not self.auto_stop.is_set() and self._alive():
            sc = self.score_box(box, tpl_name)
            if sc >= self.result_threshold:
                self.log(f"识别到 {tpl_name}（相似度 {pct(sc)}）")
                return True
            now = time.time()
            if now - last_log >= 1:
                last_log = now
                self.log(f"等待 {tpl_name} 出现中… 当前相似度 {pct(sc)}")
            if not self._wake(0.3):
                return False
        return False

    def click_yellow_until_heart(self, pos):
        """在黄色区连点(0.1秒/次)，直到识别到爱心（已进入游戏）才停；被停止则返回False。"""
        last_check = 0.0
        while not self.auto_stop.is_set() and self._alive():
            now = time.time()
            if now - last_check >= 0.3:
                last_check = now
                hearts = self.count_hearts_box(HEART_BOX)
                self.log(f"连点黄色中… 识别到爱心 {hearts} 个")
                if hearts >= 4:   # 主菜单可能误判到相似图案，≥4 才确认真实进入游戏的血条
                    self.log("已识别到爱心（≥4），停止连点，进入游戏")
                    return True
            mouse_click(*pos)
            if not self._wake(0.1):
                return False
        return False

    @staticmethod
    def _end_score(detail):
        """提取 detail 中 终结-亮/暗 的最大相似度。"""
        tmp = 0.0
        for k, v in detail.items():
            if k.startswith("终结") and v > tmp:
                tmp = v
        return tmp

    def kill_forager(self):
        try:
            subprocess.run(["taskkill", "/IM", "Forager.exe", "/F"],
                           capture_output=True, timeout=20)
        except Exception:
            pass

    def run_forager(self):
        try:
            subprocess.Popen([FORAGER_EXE])
        except Exception as e:
            self.log(f"启动Forager失败:{e}")

    def auto_flow(self):
        try:
            hearts = self.count_hearts_box(HEART_BOX)
            self.stop_heart = max(1, int(self.cfg.get("stop_heart", 3)))   # 降到几颗就停，不得低于1
            self.log(f"检测到爱心={hearts}，实时心降到 {self.stop_heart} 颗即完全停止")
            self.skip_save = False   # True=上一轮强制结束进程重启，本轮跳过 设置/保存退出
            self.done_auto = False
            now_cycle = 0
            while ((not self.auto_stop.is_set()) and self._alive()
                   and (not self.done_auto)):
                now_cycle += 1
                self.log(f"[第{now_cycle}轮] 开始新一轮…")
                if self.skip_save:
                    restarted = True          # 重启后的这一轮：绿色开始游戏用连点
                    self.skip_save = False
                    self.log(f"[第{now_cycle}轮] 已强制结束重建Forager，本轮为重启轮：绿色开始游戏使用连点")
                    if not self._wake(0.5):
                        break
                else:
                    restarted = False
                    ensure_capslock()
                    tap(VK_ESC)
                    if not self._wake(0.5):
                        break
                    self.log("正在点击 设置…")
                    mouse_move(1233, 88)       # 移动到 设置
                    if not self._wake(0.5):
                        break
                    mouse_click(1233, 88)      # 左键点击 设置
                    if not self._wake(0.5):
                        break
                    self.log("正在点击 保存并退出…")
                    mouse_move(1394, 760)      # 移动到 保存并退出
                    if not self._wake(0.5):
                        break
                    mouse_click(1394, 760)     # 左键点击 保存并退出
                    if not self._wake(0.5):
                        break

                # 绿色开始游戏
                green_box = (44, 40, 680, 258)
                yellow_box = (1398, 156, 1600, 264)
                green_pos = (360, 148)
                yellow_pos = (1498, 209)
                if restarted:
                    # 重新打开游戏：在绿色区域连点(0.1秒/次)，识别到黄才停并点黄
                    yellow_hit = False
                    while not self.auto_stop.is_set() and self._alive():
                        if self.score_box(yellow_box, "开始游戏-黄") >= self.result_threshold:
                            yellow_hit = True
                            break
                        mouse_click(*green_pos)
                        if not self._wake(0.1):
                            break
                    if not yellow_hit:
                        self.log(f"[第{now_cycle}轮] 等待 开始游戏-黄（已被停止，退出本轮）")
                        break
                    self.log(f"[第{now_cycle}轮] 已识别到 开始游戏-黄，连点黄色区直到识别到爱心")
                    if not self.click_yellow_until_heart(yellow_pos):
                        break
                else:
                    # 正常轮：识别绿色出现（无超时），点一次即可
                    ok1 = self.wait_forever(green_box, "开始游戏-绿")
                    if not ok1:
                        break
                    self.log(f"[第{now_cycle}轮] 开始游戏-绿 √")
                    mouse_move(*green_pos)
                    if not self._wake(0.5):
                        break
                    mouse_click(*green_pos)          # 点一次即可
                    ok2 = self.wait_forever(yellow_box, "开始游戏-黄")   # 黄色：一直等，无超时
                    if not ok2:
                        break
                    self.log(f"[第{now_cycle}轮] 开始游戏-黄 √")
                    if not self.click_yellow_until_heart(yellow_pos):
                        break
                if not self._wake(2.0):   # 进入游戏后等 2 秒，等游戏交互就绪
                    break
                self.log(f"[第{now_cycle}轮] 已进入游戏")

                while not self.auto_stop.is_set() and self._alive():
                    self.live_count = self.count_hearts_box(HEART_BOX)   # 每次点击祭坛前记一次数
                    self.log(f"[第{now_cycle}轮] 点击祭坛前计数={self.live_count}")
                    mouse_move(960, 500)       # 移动鼠标到祭坛，按E打开
                    if not self._wake(0.5):
                        break
                    tap(VK_E)                  # 确认
                    if not self._wake(0.5):
                        break
                    # 按E后：悬停每区触发文字显示，OCR读文字判断是否终结（固定 rounds 次，逐区累计）
                    skip_out = False
                    quit_run = False
                    hits = []
                    zone_hit = set()                 # 记录识别到"终结"文字的区域
                    for ri in range(self.rounds):
                        if self.auto_stop.is_set() or not self._alive():
                            skip_out = True
                            break
                        for gi in range(len(self.regions)):
                            r = self.regions[gi]                     # 鼠标悬停位置（不变）
                            mouse_move(r["cx"], r["cy"])
                            if not self._wake(0.3):   # 悬停等待文字渲染完整
                                skip_out = True
                                break
                            or_ = self.ocr_regions[gi] if gi < len(self.ocr_regions) else r
                            text = self.ocr_box((or_["left"], or_["top"], or_["right"], or_["bottom"]))
                            clean = clean_cn(text)                    # 处理后的文字（去空白）
                            pname, pratio = classify_text(text)
                            self.log(f"[区域{gi + 1}] 第{ri + 1}次文字: {clean}（最像 {pname} {int(pratio * 100)}%）")
                            if pname == "终结" and pratio >= PRIZE_MATCH_THRESHOLD:
                                zone_hit.add(gi)
                        if skip_out:
                            break
                    if not skip_out:
                        # 任一识别到"终结"文字的区域视为命中
                        for gi in range(len(self.regions)):
                            if gi in zone_hit:
                                r = self.regions[gi]
                                hits.append((gi, r["cx"], r["cy"], "终结", 1.0))
                        if not hits:
                            self.log(f"[第{now_cycle}轮] 两区文字均未见终结，强制结束Forager")
                            quit_run = True
                    if skip_out:
                        break
                    if quit_run:
                        self.kill_forager()
                        self.skip_save = True   # 重启后直接在主菜单，下一轮跳过 设置/保存并退出
                        break
                    # 两区都识别到终结：依次点击
                    for _gi, cx, cy, name, sc in hits:
                        mouse_move(cx, cy)
                        if not self._wake(0.5):
                            break
                        mouse_click(cx, cy)
                        self.live_count -= 1   # 每次点击终结计数 -1
                        self.log(f"[第{now_cycle}轮] 终结 点击,剩余计数={self.live_count}")
                        if self.live_count <= self.stop_heart:
                            self.log(f"[第{now_cycle}轮] 心降到 {self.live_count}，全自动完成")
                            self.done_auto = True
                            break
                    if self.done_auto:
                        break
                    if not self._wake(0.5):
                        break
                    continue

                if self.auto_stop.is_set() or self.done_auto:
                    break
                if not self._wake(0.5):
                    break
                self.run_forager()
                if not self._wake(0.5):   # 打开游戏后等待0.5秒，再在绿色区域连点
                    break
            if not self.auto_stop.is_set() and not self.done_auto:
                self.log("全自动被打断结束")
            elif self.done_auto:
                self.log(f"全自动已完成：实时心降到 {self.live_count}")
                self.play_sfx()
        except Exception as e:
            self.log(f"全自动出错:{repr(e)}")
        finally:
            self.auto_running = False
            try:
                if self._alive():
                    self.root.after(0, lambda: self._refresh("已停止：按 F8 重新开始"))
            except Exception:
                pass


def main():
    set_dpi_aware()
    try:
        cfg = _load_cfg()
    except ValueError as e:
        print(e, flush=True)
        input("按回车退出...")
        return
    if not cfg.get("regions"):
        print("配置中没有任何识别区域，退出", flush=True)
        return
    print("正在启动悬浮窗（右上角）...", flush=True)
    app = App(cfg)
    app.root.mainloop()
    try:
        app.sct.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()