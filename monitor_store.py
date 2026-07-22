"""3.0 分享链接监控清单持久化（JSON 文件，纯标准库，零依赖）。

每个监控项记录：
  - link_id / pwd        ：分享链接 ID 与提取码
  - target              ：转存到我自己云盘的目录（名称或路径，运行时解析为 catalogID）
  - auto_cas / delete_source ：转存后是否自动生成 CAS / 是否删除原视频
  - interval_min        ：扫描间隔（分钟），下限 60（用户决策：每小时至少一次）
  - enabled             ：是否启用
  - last_scan / last_files / last_result / status ：运行状态
"""

import json
import os
import time
import threading
import uuid

MON_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "casgen_monitors.json")
MIN_INTERVAL = 60  # 分钟，扫描间隔硬下限（用户决策：不低于 1 小时）
_LOCK = threading.Lock()


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def load():
    with _LOCK:
        try:
            if os.path.exists(MON_FILE):
                with open(MON_FILE, "r", encoding="utf-8") as f:
                    d = json.load(f)
                    if isinstance(d, dict) and "monitors" in d:
                        return d
        except Exception:
            pass
        return {"monitors": []}


def save(data):
    with _LOCK:
        try:
            with open(MON_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def list_all():
    return load()["monitors"]


def get(mid):
    for m in load()["monitors"]:
        if m.get("id") == mid:
            return m
    return None


def add(spec):
    """spec: {link_id, pwd, target, auto_cas, delete_source, interval_min, enabled}"""
    data = load()
    interval = int(spec.get("interval_min") or MIN_INTERVAL)
    if interval < MIN_INTERVAL:
        interval = MIN_INTERVAL
    mon = {
        "id": uuid.uuid4().hex[:12],
        "link_id": spec.get("link_id", ""),
        "pwd": spec.get("pwd", ""),
        "target": (spec.get("target") or "root") or "root",
        "auto_cas": bool(spec.get("auto_cas", True)),
        "delete_source": bool(spec.get("delete_source", False)) and bool(spec.get("auto_cas", True)),
        "interval_min": interval,
        "enabled": bool(spec.get("enabled", True)),
        "last_scan": "",
        "last_files": [],   # 上次扫到的视频 contentID 集合（用于差异检测）
        "last_result": "",
        "status": "pending",  # pending|ok|invalid|paused|expired
        "created": _now(),
    }
    data["monitors"].append(mon)
    save(data)
    return mon


def remove(mid):
    data = load()
    data["monitors"] = [m for m in data["monitors"] if m.get("id") != mid]
    save(data)


def update(mid, **fields):
    data = load()
    for m in data["monitors"]:
        if m.get("id") == mid:
            for k, v in fields.items():
                if k == "interval_min":
                    v = max(MIN_INTERVAL, int(v or MIN_INTERVAL))
                if k == "delete_source":
                    v = bool(v) and m.get("auto_cas", True)
                m[k] = v
            break
    save(data)
