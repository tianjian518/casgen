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

def _data_dir():
    """运行时数据目录：可用环境变量 CASGEN_DATA_DIR 指定（如 Docker 里挂 /data），默认仍是代码目录。

    之所以单独提出来：直接把宿主机目录挂到代码目录（/app）会把代码整个遮住、容器起不来，
    因此运行时数据要能放到独立目录，只挂那个目录即可持久化。
    """
    d = (os.environ.get("CASGEN_DATA_DIR") or "").strip()
    if d:
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except Exception:
            pass
    return os.path.dirname(os.path.abspath(__file__))


MON_FILE = os.path.join(_data_dir(), "casgen_monitors.json")
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
    """spec: {link_id, pwd, target, auto_cas, delete_source, interval_min, enabled}

    [6.1.9] 同一链接（link_id + pwd 均相同）重复添加时去重：不新建记录，
    改为把新配置合并进已有记录并返回 (已有记录, True)；全新添加返回 (新记录, False)。
    调用方需按返回的 existed 决定是否提示用户、是否触发首扫。
    """
    data = load()
    interval = int(spec.get("interval_min") or MIN_INTERVAL)
    if interval < MIN_INTERVAL:
        interval = MIN_INTERVAL
    link_id = spec.get("link_id", "")
    pwd = spec.get("pwd", "") or ""
    for m in data["monitors"]:
        if m.get("link_id") == link_id and (m.get("pwd") or "") == pwd:
            # 已存在同一链接：只更新配置字段，运行状态（last_scan/last_files/
            # pending_files/status/last_result）全部保留，绝不清基线。
            m["target"] = (spec.get("target") or "root") or "root"
            m["auto_cas"] = bool(spec.get("auto_cas", True))
            m["preserve_structure"] = bool(spec.get("preserve_structure", True))
            m["interval_min"] = interval
            m["enabled"] = bool(spec.get("enabled", True))
            m["delete_source"] = bool(spec.get("delete_source", False)) and m["auto_cas"]
            if spec.get("link_name"):
                m["link_name"] = spec.get("link_name")
            save(data)
            return m, True
    mon = {
        "id": uuid.uuid4().hex[:12],
        "link_id": link_id,
        "pwd": pwd,
        "link_name": spec.get("link_name", ""),
        "target": (spec.get("target") or "root") or "root",
        "auto_cas": bool(spec.get("auto_cas", True)),
        "delete_source": bool(spec.get("delete_source", False)) and bool(spec.get("auto_cas", True)),
        "preserve_structure": bool(spec.get("preserve_structure", True)),
        "interval_min": interval,
        "enabled": bool(spec.get("enabled", True)),
        "last_scan": "",
        "last_files": [],   # 上次扫到的视频 contentID 集合（用于差异检测）
        "pending_files": {},  # [6.1.8] 已提交转存但尚未确认落地的 {contentID: 提交时间戳}，宽限期内不重转
        "last_result": "",
        "status": "pending",  # pending|ok|invalid|paused|expired|net_error  [8.0.1 新增 net_error]
        "fail_count": 0,     # [8.0.1] 连续失败次数：网络瞬断下轮自动重试，连续 6 次才升级为 error
        "created": _now(),
    }
    data["monitors"].append(mon)
    save(data)
    return mon, False


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
