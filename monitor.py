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

from share139 import (list_all_share_files, get_share_info, save_share_files,
                      transfer_share_files, ShareError, parse_share_input)
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


def scan_one(monitor, on_phase=None):
    """扫描单个监控项一次，返回结果 dict。on_phase(阶段名, 信息dict) 可选，用于分步提示。"""
    def _phase(ph, **kw):
        if callable(on_phase):
            try:
                on_phase(ph, kw)
            except Exception:
                pass
    client = _client()
    if client is None:
        return {"status": "no_client", "msg": "尚未登录"}
    if _expired():
        monitor_store.update(monitor["id"], status="paused")
        return {"status": "paused", "msg": "登录已失效，暂停监控"}

    # 1) 解析目标目录
    _phase("解析目标目录")
    try:
        target_catalog = client.resolve_folder(monitor["target"])
    except TokenExpired:
        monitor_store.update(monitor["id"], status="paused")
        return {"status": "paused", "msg": "登录已失效，暂停监控"}
    except Exception as e:
        return {"status": "error", "msg": f"目录解析失败: {str(e)}"}

    # 2) 列举分享当前文件
    _phase("列举分享文件")
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
    _phase("差异检测")
    cur_ids = {f["contentID"] for f in files}
    prev = set(monitor.get("last_files") or [])
    new_files = [f for f in files if f["contentID"] not in prev]

    added = 0
    last_err = ""  # 关键修复：auto_cas 关闭或无可转存新文件时该变量不会被赋值，必须在顶部初始化，否则末尾回写基线时 UnboundLocalError 导致扫描直接 500
    if new_files:
        _phase("转存新增", added=len(new_files))
        try:
            flat = not monitor.get("preserve_structure", True)
            added = transfer_share_files(
                monitor["link_id"], monitor["pwd"], client.token,
                getattr(client, "account", ""), new_files, target_catalog,
                make_folder=lambda pid, nm: client.find_or_create_folder(nm, pid),
                flat=flat)
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
            _phase("等待索引")
            time.sleep(INDEX_WAIT)
            last_err = ""
            for attempt in range(GEN_RETRY):
                _phase("生成 CAS" if attempt == 0 else "生成 CAS（重试 %d）" % (attempt + 1))
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

    # 5) 回写基线（已转存的文件必须并入基线，避免下次重复转存；
    #    但 CAS 生成失败时不要掩盖错误状态——generate 是全目录幂等重算，
    #    下次扫描会对这些文件重试 CAS，无需把它们移出基线）
    monitor_store.update(monitor["id"], last_files=list(cur_ids), last_scan=_now(),
                         status=("error" if last_err else "ok"),
                         last_result=(("生成CAS失败：" + last_err) if last_err else _summary(added, len(files))))
    _phase("完成", added=added)
    return {"status": ("error" if last_err else "ok"), "added": added, "total": len(files)}


def add_and_baseline(link_id, pwd, target, auto_cas, delete_source, interval, preserve_structure=True):
    """添加监控项，并立即拉一次链接建立文件基线（last_files）。"""
    mon = monitor_store.add({
        "link_id": link_id, "pwd": pwd, "target": target,
        "auto_cas": auto_cas, "delete_source": delete_source,
        "interval_min": interval, "preserve_structure": preserve_structure,
    })
    client = _client()
    if client is not None and not _expired():
        try:
            files = list_all_share_files(link_id, pwd, token=client.token)
            link_name = ""
            try:
                sinfo = get_share_info(link_id, pwd, "root", 1, 1,
                                       getattr(client, "account", ""), client.token)
                link_name = (sinfo or {}).get("lkName") or ""
            except Exception:
                pass
            monitor_store.update(mon["id"], last_files=[f["contentID"] for f in files],
                                 link_name=link_name,
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
