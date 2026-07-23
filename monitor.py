"""3.0 分享链接定时监控（纯标准库，零依赖）。

调度线程按每个监控项自己的间隔（下限 60 分钟）扫描：
  1. 列举分享链接当前所有视频文件
  2. 与上次记录的 contentID 集合取差集 → 新增剧集
  3. 仅转存新增文件到目标目录
  4. 转存后等待云盘索引（INDEX_WAIT），再对目标目录生成 CAS（generate 幂等，全目录重算无害）
  5. 回写新的文件集合作为基线

失效处理：
  - 任一 139 接口抛 TokenExpired → 标记全局登录失效、暂停该监控（等待用户重登）
  - 分享链接失效（ShareError.fatal，如过期/被取消）→ 标记该监控项 status=invalid，不再扫描
  - 网络等非致命错误 → 标记 error，下个周期重试
"""

import time
import threading

from share139 import (list_all_share_files, save_share_files,
                       ShareError, parse_share_input)
from yidong import TokenExpired
import monitor_store
from monitor_store import MIN_INTERVAL

# 转存后等待云盘索引的秒数（139 转存后文件不会立即可见，需延迟再 generate）
INDEX_WAIT = 30
# 生成 CAS 失败（文件尚未索引到）时的重试次数与间隔
GEN_RETRY = 3
GEN_RETRY_WAIT = 30


# ---- 与 app.py 解耦：通过 getter 读取实时登录态，避免循环 import ----
_CLIENT_GETTER = None
_EXPIRED_GETTER = None
_PHONE = ""  # 手机号(msisdn)：139 转存接口必填，登录时由 app.py 注入
_LOGGED_IN = False  # 是否已登录：调度器在首次登录成功前不扫描，避免后台 139 流量与登录抢资源


def set_phone(phone):
    """app.py 登录时把解码出的手机号(msisdn)注入，供 scan_one 转存使用。"""
    global _PHONE
    _PHONE = phone or ""


def mark_logged_in():
    """app.py 登录/恢复登录成功后调用：解除调度器扫描闸门。"""
    global _LOGGED_IN
    _LOGGED_IN = True


def reset_login():
    """登录失效/退出时调用：再次暂停后台扫描。"""
    global _LOGGED_IN
    _LOGGED_IN = False


def bind(app_module):
    """app.py 启动时调用，传入自身模块，使本模块能读取实时 CLIENT / AUTH_EXPIRED。"""
    global _CLIENT_GETTER, _EXPIRED_GETTER
    _CLIENT_GETTER = lambda: getattr(app_module, "CLIENT", None)
    _EXPIRED_GETTER = lambda: getattr(app_module, "AUTH_EXPIRED", False)


def _client():
    return _CLIENT_GETTER() if _CLIENT_GETTER else None


def _expired():
    return bool(_EXPIRED_GETTER()) if _EXPIRED_GETTER else False


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _summary(added, total):
    if added:
        return "%s 扫描：新增 %d 个视频（共 %d），已转存并生成 CAS" % (_now(), added, total)
    return "%s 扫描：无新增（共 %d）" % (_now(), total)


def scan_one(monitor):
    """扫描单个监控项一次，返回结果 dict。"""
    client = _client()
    if client is None:
        return {"status": "no_client", "msg": "尚未登录"}
    if _expired():
        monitor_store.update(monitor["id"], status="paused")
        return {"status": "paused", "msg": "登录已失效，暂停监控"}

    # 1) 解析目标目录
    try:
        target_catalog = client.resolve_folder(monitor["target"])
    except TokenExpired:
        monitor_store.update(monitor["id"], status="paused")
        return {"status": "paused", "msg": "登录已失效，暂停监控"}

    # 2) 列举分享当前文件
    try:
        files = list_all_share_files(monitor["link_id"], monitor["pwd"], token=client.token)
    except ShareError as e:
        if e.fatal:
            monitor_store.update(monitor["id"], status="invalid",
                                 last_result="链接失效：" + e.message)
            return {"status": "invalid", "msg": e.message}
        monitor_store.update(monitor["id"], status="error", last_result="列举失败：" + e.message)
        return {"status": "error", "msg": e.message}
    except TokenExpired:
        monitor_store.update(monitor["id"], status="paused")
        return {"status": "paused", "msg": "登录已失效"}

    # 3) 差异检测：本次集合 - 上次集合 = 新增
    cur_ids = {f["contentID"] for f in files}
    prev = set(monitor.get("last_files") or [])
    new_files = [f for f in files if f["contentID"] not in prev]

    added = 0
    if new_files:
        co_paths = [f["path"] for f in new_files]
        try:
            save_share_files(monitor["link_id"], co_paths, [], target_catalog,
                             need_password=bool(monitor["pwd"]), phone=_PHONE,
                             token=client.token)
            added = len(new_files)
        except ShareError as e:
            if e.fatal:
                monitor_store.update(monitor["id"], status="invalid",
                                     last_result="转存失败(链接失效)：" + e.message)
                return {"status": "invalid", "msg": e.message}
            monitor_store.update(monitor["id"], status="error", last_result="转存失败：" + e.message)
            return {"status": "error", "msg": e.message}
        except TokenExpired:
            monitor_store.update(monitor["id"], status="paused")
            return {"status": "paused", "msg": "登录已失效"}

        # 4) 索引延迟 + 生成 CAS（重试）
        if monitor["auto_cas"]:
            time.sleep(INDEX_WAIT)
            last_err = ""
            for attempt in range(GEN_RETRY):
                try:
                    client.generate(target_catalog, delete_source=monitor["delete_source"])
                    last_err = ""
                    break
                except TokenExpired:
                    monitor_store.update(monitor["id"], status="paused")
                    return {"status": "paused", "msg": "登录已失效"}
                except Exception as e:
                    last_err = str(e)
                    if attempt < GEN_RETRY - 1:
                        time.sleep(GEN_RETRY_WAIT)
            if last_err:
                monitor_store.update(monitor["id"], status="error",
                                     last_result="生成CAS失败：" + last_err)

    # 5) 回写基线（无论有无新增都更新，保持 last_scan 新鲜）
    monitor_store.update(monitor["id"], last_files=list(cur_ids), last_scan=_now(),
                         status="ok", last_result=_summary(added, len(files)))
    return {"status": "ok", "added": added, "total": len(files)}


def add_and_baseline(link_id, pwd, target, auto_cas, delete_source, interval):
    """添加监控项，并立即拉一次链接建立文件基线（last_files）。"""
    mon = monitor_store.add({
        "link_id": link_id, "pwd": pwd, "target": target,
        "auto_cas": auto_cas, "delete_source": delete_source, "interval_min": interval,
    })
    client = _client()
    if client is not None and not _expired():
        try:
            files = list_all_share_files(link_id, pwd, token=client.token)
            monitor_store.update(mon["id"], last_files=[f["contentID"] for f in files],
                                 last_scan=_now(), status="ok",
                                 last_result="已添加，基线 %d 个视频" % len(files))
            mon = monitor_store.get(mon["id"])
        except Exception:
            # 基线建立失败不影响添加，下个周期再扫
            pass
    return mon


class MonitorScheduler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            time.sleep(60)
            if not _LOGGED_IN:
                continue  # 尚未登录：不扫描，避免后台 139 流量与登录抢资源
            if _expired():
                continue  # 全局登录失效：暂停所有监控
            now = time.time()
            for m in monitor_store.list_all():
                if not m.get("enabled"):
                    continue
                if m.get("status") == "invalid":
                    continue
                iv = max(MIN_INTERVAL, int(m.get("interval_min") or MIN_INTERVAL)) * 60
                last = m.get("last_scan")
                if not last:
                    due = True
                else:
                    try:
                        lt = time.mktime(time.strptime(last, "%Y-%m-%d %H:%M:%S"))
                        due = (now - lt) >= iv
                    except Exception:
                        due = True
                if due:
                    try:
                        scan_one(m)
                    except Exception as e:
                        try:
                            monitor_store.update(m["id"], status="error",
                                                 last_result="扫描异常：" + str(e))
                        except Exception:
                            pass

    def stop(self):
        self._stop.set()


_scheduler = None


def start_scheduler():
    global _scheduler
    if _scheduler is None or not _scheduler.is_alive():
        _scheduler = MonitorScheduler()
        _scheduler.start()
