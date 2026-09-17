# -*- coding: utf-8 -*-
"""
抽奖机状态机（纯逻辑，不绑屏幕）。
流程：观测(observe观察池)→决策(选目标)→逐区停止(zone1→2→3)→完成/跳过→重新观测。

每个抽奖只有 1 个"目标的物品"。
目标从本局奖品池里按权重选：权重 >0 且最大的物品。
如果池里所有物品权重都是 0（即没有任何想要的）→ 本局跳过(不按键)。
"""
import time
import threading
from collections import Counter


class SlotMachine:
    PHASE_IDLE = "idle"
    PHASE_OBSERVE = "observe"
    PHASE_STOP = "stop"

    def __init__(self, config, weights, log=None, on_press=None):
        """
        weights: {物品名: 权重数值}
        on_press: callable(zone_id) 应执行的按键回调
        """
        self.cfg = config
        self.weights = weights or {}
        self.log = log or (lambda *a: print(*a))
        self.on_press = on_press or (lambda z: None)

        self.observe_seconds = float(config.get("observe_seconds", 10))
        self.reobserve_delay = float(config.get("reobserve_delay", 1.0))
        self.threshold_press = float(config.get("threshold_press", 0.8))
        self.press_interval = float(config.get("press_interval", 1.0))  # 全局按键最小间隔(秒)
        self.min_pool = int(config.get("observe_min_pool", 4))
        self.max_observe_seconds = float(config.get("observe_max_seconds", 300))

        self.zones = config.get("zones", [])
        zone_ids = [z["id"] for z in self.zones]

        # 状态
        self.phase = self.PHASE_IDLE
        self.observe_start = None
        self.pool = set()                # 观测期识别到的物品集（名）
        self.target = None               # 选定的目标物品名
        self.stop_index = 0              # 当前要停的区下标
        self.done = set()                # 已停住的区 id
        self.zone_ids = zone_ids
        self.zone_names = {z["id"]: z.get("name", f"区{z['id']}") for z in self.zones}
        self._lock = threading.Lock()
        self._last_press_time = 0.0      # 全局上次按键时间戳，三区共用同一秒间隔
        self._reset_until = 0.0          # IDLE 最短停留时间
        self.round_done = False          # 本局三区已全部停住，停止继续判断
        self.min_votes = int(config.get("observe_min_votes", 2))
        self._conf = Counter()           # 观测期各物品命中的次数(投票去噪)

    def _weight(self, name):
        try:
            w = self.weights.get(name, 0)
            return float(w)
        except (TypeError, ValueError):
            return 0.0

    # ---------- 外部喂入一区的检测结果 ----------
    def feed(self, zone_id, name, score, hit):
        """
        喂入一区最新识别结果。返回需要执行的动作字符串：
          None / "noop"  无需操作
          "press:{zone_id}" 需要按键停住该区
        """
        with self._lock:
            now = time.time()
            if self.phase == self.PHASE_IDLE:
                # 本局已完成：停止继续判断，直到再次按热键重置
                if self.round_done:
                    return None
                # 锁存延迟：让"跳过/完成"停顿一下再开新一轮观测
                if now < self._reset_until:
                    return None
                self.phase = self.PHASE_OBSERVE
                self.observe_start = now
                self.pool = set()
                self.target = None
                self.done = set()
                self.stop_index = 0
                self._conf.clear()
                self.log("[状态] 开始观测奖品池…")
                return None

            if self.phase == self.PHASE_OBSERVE:
                # 收集：同一个物品多次命中才可信(投票去噪)。pool 用于展示。
                if hit and name:
                    self._conf[name] += 1
                self.pool = set(self._conf.keys())
                elapsed = time.time() - self.observe_start
                # 判定是否看清本局奖品池：
                # 红宝石/翡翠/黄玉/硬币/紫水晶 是每局必出的奖励宝石，不能算"新增"；
                # 须另有 ≥observe_extra_required 种非宝石物品被确认才开始决策。
                must = set(self.cfg.get("guaranteed_items",
                                        ["红宝石", "翡翠", "黄玉", "硬币", "紫水晶"]))
                extra = sum(1 for n, c in self._conf.items()
                            if c >= self.min_votes and n not in must)
                enough = extra >= int(self.cfg.get("observe_extra_required", 1))
                if enough and elapsed >= self.observe_seconds:
                    return self._decide()
                # 5 个必出宝石都确认、又没有任何非宝石确认：说明本局可能只有最低档宝石、
                # 没有其它可停目标。多观察 observe_min_for_only_gems 秒(比普通观察更长，
                # 给低分/慢滚动物品留出确认机会)后才尽早结束交给决策；决策里没有
                # 想要物品会直接跳过本局重开，避免干等 max 超时。
                all_gems = all(g in self._conf and self._conf[g] >= self.min_votes
                               for g in must)
                only_secs = max(self.observe_seconds,
                                float(self.cfg.get("observe_min_for_only_gems", 25.0)))
                if all_gems and extra == 0 and elapsed >= only_secs:
                    self.log("[观测] 观察期间始终未确认到必出宝石以外的目标，尽早结束本局")
                    return self._decide()
                if elapsed >= self.max_observe_seconds:
                    return self._decide()
                return None

            if self.phase == self.PHASE_STOP:
                return self._stop_feed(zone_id, name, score, hit)
        return None

    def _decide(self):
        """观测结束，选出本局目标。返回动作或重新进入观测。"""
        # 目标只从"识别≥min_votes次"的可信物品里选；
        # 仅当整局没有任何物品达到票数时才退而求其次(兜底用全部出现过的)。
        cand = [n for n, c in self._conf.items() if c >= self.min_votes]
        if not cand:
            cand = list(self._conf.keys())
        # 从候选池里选权重最大(且>0)的物品
        best, best_w = None, -1.0
        for n in cand:
            w = self._weight(n)
            if w > 0 and w > best_w:
                best, best_w = n, w
        if best is None:
            self.log(f"[观测] 池={sorted(self.pool) or '(空)'}，没有想要(权重>0)的物品，本局跳过")
            self.phase = self.PHASE_IDLE
            self._reset_until = time.time() + self.reobserve_delay
            return None
        self.target = best
        self.done = set()
        self.stop_index = 0
        self.phase = self.PHASE_STOP
        pool_desc = ", ".join(sorted(self.pool)) if self.pool else "(空)"
        self.log(f"[决策] 本局奖品池: {pool_desc} → 权重最高目标={best} (权重{best_w})")
        self.log(f"[状态] 开始逐区停止 (从{self.zone_names[self.zone_ids[0]]}开始)")
        return None

    def _stop_feed(self, zone_id, name, score, hit):
        """停止阶段：zone_id 由引擎动态检测白色四角框得出(当前可停区)。
        三区同时滚动，白框高亮哪个区就只处理哪个区；命中目标才按键。"""
        # 不是有效区，或该区已停过，忽略
        if zone_id not in self.zone_ids or zone_id in self.done:
            return None
        # 命中目标 → 按键停住该区
        if hit and name and name == self.target:
            now = time.time()
            # 三区共用同一全局间隔：前一个区刚按过 E，本区必须等待 press_interval
            # 才能再按，避免区1/区2在同一瞬间连按。
            if now - self._last_press_time < self.press_interval:
                return None
            self._last_press_time = now
            self.done.add(zone_id)
            self.log(f"[命中] {self.zone_names[zone_id]} → {name} (匹配度{score*100:.1f}%)，按E")
            if len(self.done) >= len(self.zone_ids):
                self.log("[完成] 三区均已停住")
                # 全部停住后不再判断，等待再次按热键开始下一局
                self.phase = self.PHASE_IDLE
                self.round_done = True
            return f"press:{zone_id}"
        # 识别到其它物品：仅提示
        if hit and name:
            self.log(f"[{self.zone_names[zone_id]}] 滚到 {name}（{score*100:.0f}%），等待目标…")
        return None

    # 供 GUI 读取
    def status(self):
        with self._lock:
            return {
                "phase": self.phase,
                "pool": sorted(self.pool),
                "pool_conf": sorted(n for n, c in self._conf.items() if c >= self.min_votes),
                "target": self.target,
                "done": sorted(self.done),
                "stop_zone": self.zone_ids[self.stop_index] if self.stop_index < len(self.zone_ids) else None,
                "zone_names": dict(self.zone_names),
            }

    def force_decide(self):
        """测试辅助：立即结束观测并执行决策。"""
        with self._lock:
            return self._decide()

    def force_reobserve(self, delay=0.0):
        """强制重置到新一局观测（供再次按热键时重新开始）。"""
        with self._lock:
            self.phase = self.PHASE_IDLE
            self.pool = set()
            self.target = None
            self.done = set()
            self.stop_index = 0
            self.round_done = False
            self._conf.clear()
            self._reset_until = time.time() + delay
        self.log("[状态] 已重置，准备开始新一局观测…")
        return True