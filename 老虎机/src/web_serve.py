# -*- coding: utf-8 -*-
"""
Forager 三区抽奖辅助 - 本地 Web 版后端
浏览器前端通过这里控制识别引擎，右侧排序用 SortableJS 拖拽。
复用 main/engine 的全部识别与按键逻辑。
"""
import json
import os
import sys
import threading
import time
import webbrowser
from urllib.parse import unquote, quote
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from main import CONFIG_PATH, HERE, Loader, ctypes_enable_dpi_awareness
from engine import SlotEngine
from overlay import Overlay

WEB_DIR = os.path.join(HERE, "web")
RES_DIR = os.path.join(HERE, "resources")
HOST = "127.0.0.1"
PORT = 8765

_lock = threading.Lock()
_engine = None
_logs = []
_log_cursor = 0
_cfg = {}
_overlay = None
_hotkey_thread = None


def _load_cfg():
    global _cfg
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            _cfg = json.load(f)
    except Exception:
        _cfg = {}


def _write_cfg():
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(_cfg, f, ensure_ascii=False, indent=2)


def log_cb(*a):
    global _logs
    line = " ".join(str(x) for x in a)
    ts = time.strftime("%H:%M:%S")
    stamped = f"[{ts}] {line}"
    _logs.append(stamped)
    print(stamped)


def scan_items():
    """扫描 resources 目录。返回 (有序items, base目录)。"""
    base = os.path.join(HERE, _cfg.get("item_dir", "resources"))
    items = []
    if os.path.isdir(base):
        for root, _dirs, files in os.walk(base):
            for f in sorted(files):
                if not f.lower().endswith(Loader.IMG_EXTS):
                    continue
                full = os.path.join(root, f)
                rel = os.path.relpath(full, base).replace("\\", "/")
                name = os.path.splitext(f)[0]
                items.append({"name": name, "rel": rel, "icon": "/res/" + quote(rel)})
    return items, base


def ordered_names(items):
    """按 items_order 排布，缺失的（新加物品）按扫描顺序追加。"""
    order = list(_cfg.get("items_order") or [])
    have = set(order)
    order += [it["name"] for it in items if it["name"] not in have]
    return order


def folder_default_order():
    """按 考古→卷轴→加工→药剂→矿物→食物→其他 的分组顺序(组内按名称)，作为恢复默认。"""
    return _flat(_default_groups())


def _default_groups():
    """扫描 resources，按 考古→卷轴→加工→药剂→矿物→食物→种子→其他 分组(未知名文件夹归其他)。"""
    GORDER = ["考古", "卷轴", "加工", "药剂", "矿物", "食物", "种子", "其他"]
    EXTS = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif')
    base = os.path.join(HERE, _cfg.get("item_dir", "resources"))
    bucket = {g: [] for g in GORDER}
    if os.path.isdir(base):
        for root, _dirs, files in os.walk(base):
            for f in files:
                if os.path.splitext(f)[1].lower() not in EXTS:
                    continue
                rel = os.path.relpath(os.path.join(root, f), base)
                top = rel.split(os.sep)[0] if os.sep in rel else ""
                bucket.get(top, bucket["其他"]).append(os.path.splitext(f)[0])
    groups = []
    for g in GORDER:
        names = sorted(bucket[g])
        if names:
            groups.append({"name": g, "items": names})
    return groups


def _flat(groups):
    out = []
    for g in groups:
        out.extend(g.get("items") or [])
    return out


def icon_map():
    items, _base = scan_items()
    return {it["name"]: it for it in items}


def _dedup_other(groups):
    """合并分组中所有"其他/其它"组为一个，保留第一个的位置。修复历史累积的多"其他"。"""
    other_names = ("其他", "其它")
    out = []
    seen = False
    for x in groups:
        if x.get("name") in other_names:
            if not seen:
                out.append({"name": "其他", "items": list(x.get("items") or [])})
                seen = True
            else:
                out[-1]["items"] += (x.get("items") or [])
        else:
            out.append(x)
    return out


def load_groups():
    """读取分组结构。优先用 config.groups（含用户跨组调整），否则生成默认分组。
    用 config 快照时会把"配置里没有但 resources 中已存在"的新文件补进"其他"，
    并合并历史遗留的重复"其他"，避免新增图标消失/多个"其他"。返回内存副本。"""
    g = _cfg.get("groups")
    if g and isinstance(g, list):
        out = [dict(x) for x in g]
        out = _dedup_other(out)
        miss = [n for n in icon_map() if n not in _flat(out)]
        if miss:
            _merge_other(out, miss)
        return out
    return _default_groups()


def _merge_other(groups, names):
    """把 names 并入分组里已存在的"其他/其它"组；没有则追加一个。用于去重一个"其他"。"""
    target = next((x for x in groups if x["name"] in ("其他", "其它")), None)
    if target is not None:
        cur = set(target.get("items") or [])
        target["items"] = list(target.get("items") or []) + [n for n in names if n not in cur]
    else:
        groups.append({"name": "其他", "items": list(names)})


def apply_groups(groups):
    """保存分组结构 -> 展平为 items_order 并换算 weights，实时更新引擎。空列表=恢复文件夹默认。"""
    global _engine
    if not groups:
        groups = _default_groups()
    clean, seen = [], set()
    for g in groups:
        raw = g.get("items") or []
        items = []
        for it in raw:
            nm = it.get("name", it) if isinstance(it, dict) else it
            if nm and nm not in seen:
                seen.add(nm)
                items.append(nm)
        if items:
            clean.append({"name": g.get("name") or "其他", "items": items})
    miss = [n for n in icon_map() if n not in seen]
    if miss:
        # 并入已存在的"其他"分组，避免保存时反复追加导致出现多个"其他"
        _merge_other(clean, miss)
    clean = _dedup_other(clean)
    _cfg["groups"] = clean
    order = _flat(clean)
    n = len(order)
    _cfg["items_order"] = order
    _cfg["weights"] = {name: float(n - i) for i, name in enumerate(order)}
    _write_cfg()
    with _lock:
        if _engine is not None:
            _engine.machine.weights = _engine_weights()
    return clean


def _engine_weights():
    """返回供引擎使用的最终权重：config.weights 基础上，把"99"分组(不想要清单)
    的物品权重清零，确保决策时不会选到它们。"""
    w = dict(_cfg.get("weights") or {})
    for g in (_cfg.get("groups") or []):
        if str(g.get("name", "")).strip() == "99":
            for it in (g.get("items") or []):
                nm = it.get("name", it) if isinstance(it, dict) else it
                w[nm] = 0.0
    return w


def apply_order(order):
    """应用新排序：写 items_order 和 weights，并实时更新引擎。空列表=恢复文件夹默认顺序。"""
    global _engine
    if not order:
        order = folder_default_order()
    n = len(order)
    _cfg["items_order"] = list(order)
    _cfg["weights"] = {name: float(n - i) for i, name in enumerate(order)}
    _write_cfg()
    with _lock:
        if _engine is not None:
            _engine.machine.weights = _engine_weights()


def start_hotkey_listener(engine):
    """全局热键 Ctrl+Alt+D：首次按下后开始识别。"""
    global _hotkey_thread
    from pynput import keyboard as _kbd

    def on_activate():
        engine.armed.set()
        try:
            engine.machine.force_reobserve()
        except Exception:
            pass
        engine.log("[热键] Ctrl+Alt+D 已按下，重新开始当前局（或已开始识别）")

    def on_stop():
        engine.armed.clear()
        try:
            engine.machine.force_reobserve(0.0)
        except Exception:
            pass
        engine.log("[热键] Ctrl+Alt+A 已按下，停止识别")

    def on_auto():
        engine.auto_mode = not engine.auto_mode
        if engine.auto_mode:
            engine.armed.set()
            engine.accepting.set()
            try:
                engine._reset_auto()
                engine.machine.force_reobserve(0.0)
            except Exception:
                pass
            engine.log("[热键] >>> 已开启全自动模式：三区抽完后自动按E并连续抽奖 <<<")
        else:
            engine.auto_mode = False
            engine.armed.clear()
            engine.accepting.clear()
            engine.log("[热键] 已关闭全自动模式")

    hotkey = _kbd.GlobalHotKeys({
        '<ctrl>+<alt>+d': on_activate,
        '<ctrl>+<alt>+a': on_stop,
        '<ctrl>+<alt>+f': on_auto,
    })
    try:
        hotkey.start()
    except Exception as e:
        engine.log(f"[热键] 注册 Ctrl+Alt+D 失败: {e}")
        return None
    _hotkey_thread = hotkey
    return hotkey


def stop_hotkey_listener():
    global _hotkey_thread
    if _hotkey_thread is not None:
        try:
            _hotkey_thread.stop()
        except Exception:
            pass
        _hotkey_thread = None


def do_start():
    global _engine, _overlay
    _load_cfg()   # 每次启动前重读磁盘最新配置(含 zones / 权重)
    with _lock:
        if _engine is not None and not _engine.stopped.is_set() \
                and _engine.thread and _engine.thread.is_alive():
            _engine.armed.clear()   # 回到待命：等 Ctrl+Alt+D
            return {"ok": True, "msg": "已待命，请按 Ctrl+Alt+D 开始识别"}
        if _engine is not None:
            _engine.stop()
        _engine = SlotEngine(_cfg, log=log_cb)
        _engine.accepting.set()
        _engine.armed.clear()       # 默认不识别
        # 先显示右上角置顶悬浮窗，再启动引擎(避免低配机校准占满CPU时窗口起不来)
        if _overlay is None:
            _overlay = Overlay()
        _overlay.show(_engine)
        _engine.start()
        # 注册全局热键
        start_hotkey_listener(_engine)
        return {"ok": True, "msg": "已启动识别，请按 Ctrl+Alt+D 开始"}


def do_stop():
    global _engine, _overlay
    with _lock:
        if _overlay is not None:
            _overlay.hide()
            _overlay = None
        stop_hotkey_listener()
        if _engine is not None:
            _engine.armed.clear()
            _engine.accepting.clear()
            _engine.stop()
            _engine = None
        return {"ok": True, "msg": "已停止"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            try:
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            except TypeError:
                import numpy as np

                def _d(o):
                    if isinstance(o, (np.floating, np.float32, np.float64)):
                        return float(o)
                    if isinstance(o, np.integer):
                        return int(o)
                    if isinstance(o, np.ndarray):
                        return o.tolist()
                    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")

                body = json.dumps(body, ensure_ascii=False, default=_d).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            ln = int(self.headers.get("Content-Length", 0))
            if ln <= 0:
                return {}
            return json.loads(self.rfile.read(ln).decode("utf-8"))
        except Exception:
            return {}

    # ---------- 路由 ----------
    def do_GET(self):
        p = unquote(self.path).split("?", 1)[0]
        if p == "/":
            return self._send_file(os.path.join(WEB_DIR, "index.html"))
        if p == "/Sortable.min.js":
            return self._send_file(os.path.join(WEB_DIR, "Sortable.min.js"),
                                   "application/javascript; charset=utf-8")
        if p == "/api/status":
            with _lock:
                st = _engine.status() if _engine is not None else None
            base = dict(_cfg)
            base.setdefault("zones", [])
            return self._send(200, {
                "config": base,
                "status": st or {
                    "phase": "idle", "running": False, "pool": [], "target": None,
                    "done": [], "stop_zone": None, "zone_names": {},
                    "zone_last": {}, "fps": 0.0,
                },
            })
        if p == "/api/items":
            im = icon_map()
            groups = [
                {"name": g["name"],
                 "items": [im[n] for n in (g.get("items") or []) if n in im]}
                for g in load_groups()
            ]
            return self._send(200, {
                "groups": groups,
                "group_names": [g["name"] for g in groups],
            })
        if p == "/api/log":
            global _log_cursor
            with _lock:
                cur = _log_cursor
                lines = _logs[cur:]
                _log_cursor = len(_logs)
            return self._send(200, {"lines": lines})
        if p.startswith("/res/"):
            rel = p[len("/res/"):]
            fp = os.path.normpath(os.path.join(RES_DIR, rel))
            if os.path.abspath(fp).startswith(os.path.abspath(RES_DIR)) and os.path.isfile(fp):
                return self._send_file(fp, mime_for(fp))
            return self._send(404, "not found", "text/plain")
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        p = unquote(self.path).split("?", 1)[0]
        if p == "/api/start":
            return self._send(200, do_start())
        if p == "/api/stop":
            return self._send(200, do_stop())
        if p == "/api/items_order":
            body = self._read_json()
            if isinstance(body.get("groups"), list):
                apply_groups(body["groups"])
                log_cb("[排序] 已保存分组顺序")
            else:
                order = body.get("order") or []
                apply_order(order)
                log_cb(f"[排序] 已保存 {len(order)} 个物品的优先级顺序")
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})

    def _send_file(self, path, ctype=None):
        try:
            with open(path, "rb") as f:
                data = f.read()
            ctype = ctype or mime_for(path)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self._send(404, "file not found", "text/plain")


def mime_for(path):
    e = os.path.splitext(path)[1].lower()
    return {
        ".html": "text/html; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(e, "application/octet-stream")


def main():
    ctypes_enable_dpi_awareness()
    _load_cfg()
    # ---- 纯识别模式：不启动 web，直接用 config 里的排序启动引擎+悬浮窗+热键 ----
    if "--bare" in sys.argv:
        try:
            _load_cfg()
            r = do_start()
            print(f"[启动识别] {r.get('msg', '')}")
        except Exception as e:
            print(f"[启动识别] 失败: {e!r}")
            return
        e = _engine
        try:
            while e is not None and e.thread and e.thread.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            do_stop()
        return
    # ---- web 模式：只做"顺序调整"页面，不自动启动引擎 ----
    no_open = "--no-open" in sys.argv
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Forager 顺序调整 - Web 服务已启动: http://{HOST}:{PORT}/")
    if not no_open:
        threading.Timer(0.6, lambda: webbrowser.open(f"http://{HOST}:{PORT}/")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()