# -*- coding: utf-8 -*-
"""
核心识别引擎：多区域抓屏 + 模板识别 + 抽奖状态机 + 自动按键。
供 CLI(slot 模式)与 GUI 共用。运行在独立线程。
"""
import threading
import time
import os
from datetime import datetime
from collections import deque

import numpy as np
import cv2
import mss

from main import Detector, KeyPresser, Loader, HERE
from slot_machine import SlotMachine


class SlotEngine:
    def __init__(self, config, log=None, templates_override=None):
        self.cfg = config
        # 包装日志：既转发给外部(控制台/web)，又保留最近5条供悬浮窗展示，
        # 并自动追加写入日志文件(按天滚动)，方便离线复盘。
        raw_log = log or (lambda *a: print(*a))
        self._log_tail = deque(maxlen=5)
        date_str = datetime.now().strftime("%Y%m%d")
        try:
            self._log_file = config.get("log_file") or \
                os.path.join(HERE, f"engine_{date_str}.log")
        except Exception:
            self._log_file = ""
        self._log_lock = threading.Lock()

        def _wrap(*a):
            line = " ".join(str(x) for x in a)
            stamp = datetime.now().strftime("%H:%M:%S")
            self._log_tail.append(line)
            try:
                raw_log(line)
            except Exception:
                pass
            if self._log_file:
                try:
                    with self._log_lock:
                        with open(self._log_file, "a", encoding="utf-8") as f:
                            f.write(f"[{stamp}] {line}\n")
                except Exception:
                    pass

        self.log = _wrap
        self.loader = Loader(config, log=self.log)
        self.loader.load()
        if templates_override is None:
            items = self.loader.items
        else:
            items = templates_override
        self.detector = Detector(config, items, log=self.log)
        self.keypresser = KeyPresser(config, log=self.log)

        # 权重映射(优先级)。优先用 items_order(用户拖拽排序, rank 越小越优先)，
        # 否则回退到 config["weights"]，再缺省 0。
        ov = config.get("weight_overrides") or {}
        if ov:
            # 用户自定义优先级(weight_overrides)：数字越小越优先，≥99=不想要(最低)，
            # 未列出的物品按 99(不想要)处理。内部反转成"越大越优先"供状态机取 max。
            self.weights = {}
            for name, _img in items:
                v = float(ov.get(name, 99.0))
                self.weights[name] = max(0.0, 99.0 - v) if v < 99.0 else 0.0
        else:
            order = config.get("items_order") or []
            if order:
                n = len(order)
                self.weights = {name: float(n - rank) for rank, name in enumerate(order)}
            else:
                saved_weights = config.get("weights", {}) or {}
                self.weights = {
                    name: (float(saved_weights[name]) if name in saved_weights else 0.0)
                    for name, _img in items
                }
        # "99" 分组 = 不想要清单：权重置 0(决策取最大且需>0 即排除)。
        for _g in (config.get("groups") or []):
            if str(_g.get("name", "")).strip() == "99":
                for _it in (_g.get("items") or []):
                    nm = _it.get("name", _it) if isinstance(_it, dict) else _it
                    if nm in self.weights:
                        self.weights[nm] = 0.0
        self.machine = SlotMachine(
            config, self.weights, log=self.log,
            on_press=lambda z: self._press_zone(z))

        self.zones = config.get("zones", [])
        self.accepting = threading.Event()      # 是否允许自动按键
        self.armed = threading.Event()          # Ctrl+Alt+D 后才开始识别
        self.stopped = threading.Event()
        self.thread = None
        self.frames = 0
        self.boot = time.time()
        self.zone_last = {}                     # zone_id -> (物品名, 匹配度%, hit)
        self.share = {"cur": None, "target": None}   # 供悬浮窗读取的实时数据
        self._last_preview = 0.0                     # 未armed时"当前可能"图标刷新节流
        self._last_stop_log = 0.0                    # 停止阶段匹配分诊断日志节流
        self._last_white_log = 0.0                   # 白框分数诊断日志节流
        self._calibrated = False                     # 是否已在抽奖画面下完成一次放大倍数校准
        self._last_phase = None                      # 上一轮阶段，检测进入停止的时机
        self._stop_start = 0.0                       # 进入停止阶段的时刻
        # 停止阶段"画面是否还在滚动"检测：只有当前处理区画面持续静止
        # (抽奖机不再滚)才算真卡死，避免了"目标迟迟等不到就被固定时长重开"。
        self._thumbs = {}                            # zone_id -> 16x16 灰度缩略图
        self._static_start = {}                      # zone_id -> 该区连续静止段起点
        # 全自动模式(Ctrl+Alt+F)：本局三区抽完 → 等 auto_delay_press 后按E开始下一轮
        # → 再等 auto_delay_go → 自动重新观测，连续抽奖。
        self.auto_mode = False
        self._last_round_done = False                # round_done 上升沿检测
        self._auto_stage = "idle"                    # idle|wait_press|wait_go
        self._auto_press_at = 0.0
        self._auto_reobserve_at = 0.0

    def _press_zone(self, zone_id):
        if self.accepting.is_set():
            self.keypresser.press()

    def _consume_action(self, action):
        """消费状态机返回的动作；命中目标时触发按键，修复按键一直未生效的问题。"""
        if action and action.startswith("press:"):
            try:
                zid = int(action.split(":", 1)[1])
            except (ValueError, TypeError):
                zid = None
            if zid is not None:
                self._press_zone(zid)

    # ---------- 控制 ----------
    def start(self):
        self.stopped.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=3)

    # ---------- 抓取指定区 ----------
    def _grab_zone(self, sct, mon, zone):
        r = {
            "left": mon["left"] + int(zone["left"]),
            "top": mon["top"] + int(zone["top"]),
            "width": int(zone["width"]),
            "height": int(zone["height"]),
        }
        raw = sct.grab(r)
        return np.array(raw)[:, :, :3]

    def _zone_region(self, mon, zone_id):
        z = next((x for x in self.zones if x["id"] == zone_id), None)
        if z is None:
            return None
        return z["left"], z["top"], z["width"], z["height"]

    def _grab_combined(self, sct, mon):
        """一次抓屏覆盖所有抽奖区的外接矩形，返回 {zone_id: BGR 帧}。
        停止阶段需要定位白框区又要对该区匹配，单次抓屏比逐区 3 次 mss 抓取快。"""
        left = min(int(z["left"]) for z in self.zones)
        top = min(int(z["top"]) for z in self.zones)
        right = max(int(z["left"]) + int(z["width"]) for z in self.zones)
        bottom = max(int(z["top"]) + int(z["height"]) for z in self.zones)
        r = {
            "left": mon["left"] + left,
            "top": mon["top"] + top,
            "width": right - left,
            "height": bottom - top,
        }
        arr = np.array(sct.grab(r))[:, :, :3]
        out = {}
        for z in self.zones:
            x0 = int(z["left"]) - left
            y0 = int(z["top"]) - top
            # .copy() 保证切片内存连续，供 cv2.matchTemplate 使用
            out[z["id"]] = arr[y0:y0 + int(z["height"]),
                               x0:x0 + int(z["width"])].copy()
        return out

    # ---------- 白框检测(定位"当前可停区") ----------
    def _white_score(self, frame):
        """返回 (四角亮比均值, 四角中最小值)。白框=四角都明显高亮。"""
        size = int(self.cfg.get("white_corner_size", 26))
        h, w = frame.shape[:2]
        corners = [
            frame[:size, :size],
            frame[:size, w - size:],
            frame[h - size:, :size],
            frame[h - size:, w - size:],
        ]
        vals = []
        for c in corners:
            gray = cv2.cvtColor(c, cv2.COLOR_BGR2GRAY)
            vals.append(float((gray > 200).mean()))
        return sum(vals) / 4.0, min(vals)

    # ---------- 停止阶段画面静止判定 ----------
    def _thumb_moving(self, zone_id, frame):
        """判断指定区画面是否仍在滚动变化：当前与上一帧的 16x16 灰度缩略图
        平均差超过阈值即视为在滚动；无上一帧时按"在动"处理,避免启动即误判静止。"""
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        t = cv2.resize(g, (16, 16)).astype(np.float32)
        prev = self._thumbs.get(zone_id)
        self._thumbs[zone_id] = t
        if prev is None:
            return True
        diff = float(np.abs(t - prev).mean())
        return diff >= float(self.cfg.get("static_diff", 2.0))

    # ---------- 全自动抽奖推进 ----------
    def _reset_auto(self):
        """重置全自动内部状态机。"""
        self._auto_stage = "idle"
        self._auto_press_at = 0.0
        self._auto_reobserve_at = 0.0
        self._last_round_done = self.machine.round_done

    def _auto_advance(self):
        """全自动推进：本局三区抽完 → 等 auto_delay_press 按E → 等 auto_delay_go 重新观测。
        用 stage 状态自愈，不依赖 round_done 上升沿，避免偶发漏触发导致"卡住"。"""
        now = time.time()
        rd = self.machine.round_done
        # 本局刚完成且尚未开始推进 → 进入等待按E
        if rd and self._auto_stage not in ("wait_press", "wait_go"):
            self._auto_stage = "wait_press"
            self._auto_press_at = now + float(self.cfg.get("auto_delay_press", 2.0))
            self.log("[全自动] 本局抽完，"
                     f"{self.cfg.get('auto_delay_press', 2.0):.0f}秒后按 E 开始下一轮…")
        elif not rd:
            self._auto_stage = "idle"

        if self._auto_stage == "wait_press" and now >= self._auto_press_at:
            self.keypresser.press()
            self._auto_stage = "wait_go"
            self._auto_reobserve_at = now + float(self.cfg.get("auto_delay_go", 1.0))
            self.log(f"[全自动] 已按 E，"
                     f"{self.cfg.get('auto_delay_go', 1.0):.0f}秒后开始识别…")
        elif self._auto_stage == "wait_go" and now >= self._auto_reobserve_at:
            self.machine.force_reobserve(0.0)  # 强重新开始观测，连续抽下一局
            self._auto_stage = "idle"
            self.log("[全自动] 已重新开始识别，连续抽奖中…")

    # ---------- 校准放大倍数(启动时自动一次) ----------
    def _calibrate(self, sct, mon):
        calib_s = float(self.cfg.get("calibrate_seconds", 3.0))
        collect = float(self.cfg.get("calibrate_collect_threshold", 0.6))
        min_frames = int(self.cfg.get("calibrate_min_frames", 2))
        stats = {}      # scale -> [出现高分帧次数, 累计匹配分]
        end = time.time() + calib_s
        self.log("[校准] 正在统计各放大倍数命中频率，请保持三个抽奖区可见…")
        while time.time() < end and not self.stopped.is_set():
            for z in self.zones:
                if self.stopped.is_set():
                    break
                frame = self._grab_zone(sct, mon, z)
                score, scale, _ = self.detector.calibrate_scale(frame, z["id"])
                if scale is not None and score >= collect:
                    key = round(scale, 2)
                    st = stats.get(key, [0, 0.0])
                    st[0] += 1
                    st[1] += score
                    stats[key] = st
            time.sleep(0.02)
        # 只认"高置信"的放大倍数：多个高分帧 且 平均分达标。
        # 0.70 这类对滚动 UI 的普遍低配(65%)会被排除，避免顶掉真实的 2.5。
        min_avg = float(self.cfg.get("calibrate_min_avg", 0.70))
        valid = [
            (cnt, sums, sc)
            for sc, (cnt, sums) in stats.items()
            if cnt >= min_frames and sums / cnt >= min_avg
        ]
        dfl = float(self.cfg.get("default_scale", 2.0))
        if valid:
            valid.sort(key=lambda x: x[1] / x[0], reverse=True)
            cnt, sums, chosen = valid[0]
            self.detector.set_scale(chosen)
            self.log(f"[校准] 锁定放大倍数 {chosen:.2f}x"
                     f"({cnt} 个高分帧，平均 {sums/cnt*100:.0f}%)")
        else:
            self.detector.set_scale(dfl)
            self.log(f"[校准] 无高置信倍数(平均分需≥{min_avg*100:.0f}%)，"
                     f"使用默认 {dfl:.2f}x")
        self._calibrated = True

    # ---------- 主循环 ----------
    def _loop(self):
        sct = mss.mss()
        mon = sct.monitors[1]
        last_stats = time.time()
        try:
            if not self.zones:
                self.log("[错误] 配置中没有抽奖区(zones)，已停止引擎。请在 config.json 里配置 zones。")
                return
            while not self.stopped.is_set():
                # 未开始识别，或本局三区已全部停住 → 只低频预览，不判断不按键
                if (not self.armed.is_set()) or self.machine.round_done:
                    rd = self.machine.round_done
                    if self.auto_mode:
                        try:
                            self._auto_advance()
                        except Exception as e:
                            self.log(f"[全自动] 推进异常: {e!r}")
                    elif rd and not self._last_round_done:
                        self.log("[提示] 本局已抽完但未开启全自动，"
                                 "按 Ctrl+Alt+F 可自动连续抽奖。")
                    self._last_round_done = rd
                    if time.time() - self._last_preview >= 0.25:
                        self._last_preview = time.time()
                        try:
                            z = self.zones[0]
                            frame = self._grab_zone(sct, mon, z)
                            self.detector.detect(frame)
                        except Exception:
                            pass
                    time.sleep(0.05)
                    continue

                # 首次开始识别时，面对真实抽奖画面测定放大倍数
                if not self._calibrated:
                    if self.cfg.get("window_match", False):
                        self.detector.set_scale(1.0)
                        self.log("[校准] 滑窗匹配模式：无需倍数校准，直接开始")
                    else:
                        self._calibrate(sct, mon)
                    self._calibrated = True

                st = self.machine.status()
                phase = st["phase"]
                frames = 0
                # 跟踪阶段切换：进入停止阶段时重置"停止空转计时器"
                if self._last_phase != phase:
                    self._last_phase = phase
                    if phase == SlotMachine.PHASE_STOP:
                        self._stop_start = time.time()

                if phase in (SlotMachine.PHASE_OBSERVE,):
                    # 观测期：轮流抓取所有区收集池。
                    # 收集阈值放宽到 0.5(滚动模糊/分稍低的非宝石也能入池)，避免漏掉
                    # 5 个必出宝石之外的低分奖品；停止按键阶段仍用严格的阈值。
                    # 须"两次确认"(min_votes)才入候选。领先校验只在高置信场景生效：
                    # 仅当 top1 和 top2 分数都 ≥0.7(两者都很高=可能真混淆，如玻璃纤维/
                    # 皇家布料)才要求 top1 明显领先 top2，防止相似物互相攒票；否则
                    # 若画面本就是某物品、只是分偏低，不因"未领先第二名"而丢弃识别。
                    obs_collect = float(self.cfg.get("observe_collect_threshold", 0.5))
                    obs_margin = float(self.cfg.get("observe_lead_margin", 0.05))
                    for z in self.zones:
                        if self.stopped.is_set():
                            break
                        frame = self._grab_zone(sct, mon, z)
                        top = self.detector.topk(frame, 2)
                        if top:
                            name = top[0][1]
                            score = float(top[0][0])
                            lead = score - (float(top[1][0]) if len(top) > 1 else score)
                            both_high = (len(top) > 1
                                         and top[0][0] >= 0.7 and top[1][0] >= 0.7)
                            hit = score >= obs_collect and (
                                not both_high or lead >= obs_margin)
                        else:
                            name, score, hit = None, 0.0, False
                        self.zone_last[z["id"]] = (name, round(score * 100, 1), hit)
                        self.share["cur"] = name
                        self._consume_action(
                            self.machine.feed(z["id"], name, score, hit))
                        self.frames += 1
                elif phase == SlotMachine.PHASE_STOP:
                    # 停止期：以"当前带白色四角框"的区为定位，一次只处理一个区。
                    # 一次抓屏覆盖三区；白框标记当前可停区并在未停住的区间切换。
                    # 命中判定用"目标滑窗匹配 + 轻量粗筛确认本帧最优即目标"，
                    # 取代每帧触发的昂贵 topk 全量精排，避免识别滞后导致按晚。
                    done = set(st["done"])
                    zones_frames = self._grab_combined(sct, mon)
                    frame_ts = time.time()   # 本次抓帧时间戳，用于命中时效判断
                    all4_th = float(self.cfg.get("white_all4_th", 0.10))
                    min_avg = float(self.cfg.get("white_min_avg", 0.10))
                    # 诊断：节流打印各区白框分数(avg/四角最低min)，便于离线复盘
                    # 白框检测是否灵敏/是否因某区残留高亮而跟错区。
                    if time.time() - self._last_white_log >= 0.6:
                        self._last_white_log = time.time()
                        parts = []
                        for zid, f in zones_frames.items():
                            if zid in done:
                                continue
                            a, m = self._white_score(f)
                            parts.append(f"{zid}=Δt{m:.0%}/a{a:.0%}")
                        if parts:
                            self.log("[白框] " + " | ".join(parts))
                    best_id, best_score = None, 0.0
                    for zid, f in zones_frames.items():
                        if zid in done:
                            continue
                        avg, mn = self._white_score(f)
                        if mn >= all4_th and avg >= min_avg and avg > best_score:
                            best_id, best_score = zid, avg
                    zid = best_id
                    if zid is None:
                        # 白框未定位到未停区：立即按顺序回退到下一个未停区继续处理，
                        # 不做长时间空转等待，避免白框切换期间视觉上"卡在旧区"。
                        nxt = next((i for i in self.machine.zone_ids if i not in done), None)
                        if nxt is None or nxt not in zones_frames:
                            time.sleep(0.002)
                            continue
                        zid = nxt
                    frame = zones_frames[zid]
                    # ---- 停止阶段卡死兜底（全自动）：抽奖机只要还在滚动就不重开，
                    # 只有"当前处理区画面持续静止"(抽奖机不再滚)才算卡死，超时后自动
                    # 重开一局。修复之前"固定30秒强制重开"误杀仍在正常等待目标的局
                    # (目标迟迟没等到确认窗口就会整局重开，浪费已停好的区)。 ----
                    if self.auto_mode:
                        sto = float(self.cfg.get("stop_phase_timeout", 30.0))
                        if zid not in self._static_start:
                            self._static_start[zid] = time.time()
                        if self._thumb_moving(zid, frame):
                            self._static_start[zid] = time.time()
                        if time.time() - self._static_start[zid] > sto:
                            zname = self.machine.zone_names.get(zid, f"区{zid}")
                            self.log(f"[全自动] {zname}画面已静止超过{sto:.0f}秒"
                                     "(抽奖机不再滚)，判定卡死，自动重开一局…")
                            self._auto_stage = "idle"
                            self._thumbs.clear()
                            self._static_start.clear()
                            self.machine.force_reobserve(0.0)
                            time.sleep(0.05)
                            continue
                    tgt = st["target"]
                    if not tgt:
                        name, score, hit = self.detector.detect(frame)
                        self.zone_last[zid] = (name, round(score * 100, 1), hit)
                        self.share["cur"] = name
                        action = self.machine.feed(zid, name, score, hit)
                    else:
                        ts = self.detector.match_name(frame, tgt)[0]
                        if ts >= float(self.cfg.get("hit_trust_threshold", 0.78)):
                            # 主匹配(原分辨率多档彩色滑窗)高置信命中：缩帧粗筛在
                            # coarse_width 小尺寸下易把相似淡色原型(如 恐龙蛋/黄玉)搞混
                            # 而错误否决。高分时直接信任主匹配(仍走时效门限+颜色双保险)。
                            hit = True
                        elif ts >= self.detector.threshold_press:
                            # 目标滑窗分达标后，对粗筛 topK 候选用主匹配(match_name)复验：
                            # 缩帧粗筛(48px)会把相似低多边形物品搞混(如 玻璃纤维/皇家布料)，
                            # 粗筛第一名未必是画面上真实物品，故在少数候选上精确判定，
                            # 取主匹配真实最高分者决胜负，避免被错误的第一名带偏。
                            coarse = self.detector._coarse(frame)
                            coarse.sort(reverse=True)
                            K = int(self.cfg.get("hit_topk_reverify", 5))
                            hit = False
                            lead = 0.0
                            rev = []
                            for _, nm in coarse[:K]:
                                rev.append((self.detector.match_name(frame, nm)[0], nm))
                            rev.sort(reverse=True)
                            if rev:
                                lead = rev[0][0] - (rev[1][0] if len(rev) > 1 else 0.0)
                                hit = (rev[0][1] == tgt
                                       and lead >= float(self.cfg.get("hit_lead_margin", 0.05)))
                            if (not hit and (not rev or rev[0][1] != tgt)
                                    and time.time() - self._last_stop_log >= 0.5):
                                self._last_stop_log = time.time()
                                got = rev[0][1] if rev else "?"
                                topn = coarse[0][1] if coarse else "?"
                                self.log(f"[区{zid}] 目标[{tgt}] {ts*100:.1f}% "
                                         f"主匹配复验认为={got}(粗筛第一={topn})，未触发")
                        else:
                            hit = False
                            if time.time() - self._last_stop_log >= 0.5:
                                self._last_stop_log = time.time()
                                self.log(f"[区{zid}] 目标[{tgt}] "
                                         f"{ts*100:.1f}% 低于阈值，等待…")
                        self.zone_last[zid] = (tgt, round(ts * 100, 1), hit)
                        self.share["cur"] = tgt if hit else None
                        if hit:
                            # 命中时效门限：从抓帧到命中判定耗时超过阈值说明画面上
                            # 目标大概率已滚过，此时再按会停在错误位置，故跳过本次。
                            proc_ms = (time.time() - frame_ts) * 1000.0
                            max_ms = float(self.cfg.get("hit_max_delay_ms", 100.0))
                            if proc_ms > max_ms:
                                self.log(f"[区{zid}] 命中[{tgt}] 处理过慢 "
                                         f"{proc_ms:.0f}ms>{max_ms:.0f}ms，"
                                         "目标可能已滚过，本次跳过不按")
                                hit = False
                            else:
                                # 颜色校验：TM_CCOEFF_NORMED 弱化颜色，形状相似但
                                # 颜色迥异的物品(如 玻璃/塑料)可能互相误认，
                                # 命中后再比对主体颜色，差异过大则拒按。
                                cdiff = self.detector.color_compat(frame, tgt)
                                cth = float(self.cfg.get("color_threshold", 0.45))
                                if cdiff is not None and cdiff > cth:
                                    self.log(f"[区{zid}] 命中[{tgt}] "
                                             f"主体颜色不符(diff={cdiff*100:.0f}%"
                                             f">{cth*100:.0f}%)，拒按")
                                    hit = False
                        action = self.machine.feed(zid, tgt if hit else None, ts, hit)
                    self._consume_action(action)
                    self.frames += 1
                else:
                    # idle：喂一帧区1 触发状态机（可能因 reobserve_delay 不动作）
                    frame = self._grab_zone(sct, mon, self.zones[0])
                    name, score, hit = self.detector.detect(frame)
                    self.zone_last[self.zones[0]["id"]] = (name, round(score * 100, 1), hit)
                    self.share["cur"] = name
                    self._consume_action(
                        self.machine.feed(self.zones[0]["id"], name, score, hit))
                    self.frames += 1

                if time.time() - last_stats >= 1.0:
                    fps = self.frames / (time.time() - self.boot)
                    self.log(f"[引擎] FPS≈{fps:.0f} | 阶段={phase} | 目标={st['target']} | "
                             f"已停={st['done']} | 池={', '.join(st['pool'][:5]) or '(空)'}")
                    last_stats = time.time()
        except Exception as e:
            self.log(f"[引擎] 运行异常，已停止循环: {e!r}")
        finally:
            sct.close()

    def status(self):
        st = self.machine.status()
        st["running"] = self.thread is not None and self.thread.is_alive()
        st["fps"] = self.frames / max(1.0, time.time() - self.boot) if st["running"] else 0.0
        st["zone_last"] = dict(self.zone_last)
        st["logs"] = list(self._log_tail)
        return st