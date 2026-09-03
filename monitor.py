"""3.0 分享链接定时监控（纯标准库，零依赖）。

调度线程按每个监控项自己的间隔（下限 60 分钟）扫描：
  1. 列举分享链接当前所有视频文件
  2. 与上次记录的 contentID 集合取差集 → 新增剧集
  3. 仅转存新增文件到目标目录
  4. 转存后等待云盘索引（INDEX_WAIT），再对目标目录生成 CAS（generate 幂等，全目录重算无害）
  5. 回写新的文件集合作为基线

[6.1.6 调度健壮性重写] 修复「定时扫描没用 / 记录永远冻在添加时刻」的根因：
  - 根因①：scan_one 的 6 处提前返回（未登录 / 登录失效 / TokenExpired）都**不更新 last_scan**，
    一旦令牌个人云会话失效，每次调度都卡在第一步就 return，last_scan 永远停在添加时刻，
    表现就是「从早到晚只有添加时那一条记录」。现改为：任何路径返回前都刷新 last_scan + last_result。
  - 根因②：调度循环顶部 `if _expired(): continue` 在全局登录失效时**整体永久空转**，
    且循环体未包 try/except、无看门狗，线程一旦因异常死亡就再也不会重启。
    现改为：循环体整体受保护永不崩溃；过期时降级重试（仍刷新心跳）而非冻结；
    新增看门狗线程，调度器意外死亡自动重启。
  - 另：添加监控即后台全量首扫（添加即处理现有内容，而非把现有全部预先吞成基线导致「无新增」）。
"""

import time
import threading
import traceback

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


def _mark(mid, status, result, **extra):
    """统一回写监控项状态，并强制刷新 last_scan（关键：避免记录冻结在添加时刻）。"""
    fields = {"last_scan": _now(), "status": status, "last_result": result}
    fields.update(extra)
    monitor_store.update(mid, **fields)


def _target_existing_names(client, target_catalog, preserve_structure):
    """收集目标目录子树已存在的文件名集合（视频原名 + 去掉 .cas 后缀的名），
    用于「云盘感知去重」：分享里算出来的「新增」文件，若云盘目标里已存在同名
    （或同名 .cas），说明分享一条龙 / 上次扫描已处理过，跳过不再转存，根治重复副本。

    [6.1.7] 专门根治：分享一条龙勾选「加入监控」后，一条龙已转存+生成CAS，监控首扫
    却把全部文件当「新增」再转一遍 → 每一集两份副本。加上这层云盘感知，首扫会跳过
    已存在于云盘的文件。

    保留结构时文件落在子目录，必须递归（iter_all）才能看到；扁平转存时只在直接子项，
    用 list_dir 即可（也更省调用）。"""
    names = set()
    try:
        if preserve_structure:
            for it in client.iter_all(target_catalog, max_depth=10):
                if client._is_folder(it):
                    continue
                nm = client._name(it) or ""
                names.add(nm[:-4] if nm.endswith(".cas") else nm)
        else:
            cursor = None
            while True:
                items, cursor, _ = client.list_dir(target_catalog, cursor)
                for it in (items or []):
                    if client._is_folder(it):
                        continue
                    nm = client._name(it) or ""
                    names.add(nm[:-4] if nm.endswith(".cas") else nm)
                if not cursor:
                    break
    except Exception:
        pass
    return names


def scan_one(monitor, on_phase=None):
    """扫描单个监控项一次，返回结果 dict。on_phase(阶段名, 信息dict) 可选，用于分步提示。"""
    mid = monitor.get("id")
    def _phase(ph, **kw):
        if callable(on_phase):
            try:
                on_phase(ph, kw)
            except Exception:
                pass

    client = _client()
    if client is None:
        _mark(mid, "no_client", "尚未登录，无法扫描（请在网页登录后自动恢复）")
        return {"status": "no_client", "msg": "尚未登录"}
    if _expired():
        _mark(mid, "paused", "登录已失效，已暂停（网页重新登录后自动恢复）")
        return {"status": "paused", "msg": "登录已失效，暂停监控"}

    # 1) 解析目标目录
    _phase("解析目标目录")
    try:
        target_catalog = client.resolve_folder(monitor["target"])
    except TokenExpired:
        _mark(mid, "paused", "登录已失效，已暂停（网页重新登录后自动恢复）")
        return {"status": "paused", "msg": "登录已失效，暂停监控"}
    except Exception as e:
        _mark(mid, "error", "目录解析失败：" + str(e))
        return {"status": "error", "msg": "目录解析失败: " + str(e)}

    # 2) 列举分享当前文件
    _phase("列举分享文件")
    try:
        files = list_all_share_files(monitor["link_id"], monitor["pwd"], token=client.token)
    except ShareError as e:
        if e.fatal:
            _mark(mid, "invalid", "链接失效：" + e.message)
            return {"status": "invalid", "msg": e.message}
        _mark(mid, "error", "列举失败：" + e.message)
        return {"status": "error", "msg": e.message}
    except TokenExpired:
        _mark(mid, "paused", "登录已失效，已暂停（网页重新登录后自动恢复）")
        return {"status": "paused", "msg": "登录已失效"}

    # 3) 差异检测：本次集合 - 上次集合 = 新增
    _phase("差异检测")
    cur_ids = {f["contentID"] for f in files}
    prev = set(monitor.get("last_files") or [])
    new_files = [f for f in files if f["contentID"] not in prev]

    # [6.1.7] 云盘感知去重：分享里算出来的「新增」文件，若云盘目标目录里已存在同名
    # （或同名 .cas），说明分享一条龙 / 上次扫描已处理过，跳过不再转存，根治重复副本。
    existing_names = _target_existing_names(client, target_catalog,
                                           monitor.get("preserve_structure", True))
    if existing_names:
        before = len(new_files)
        new_files = [f for f in new_files
                     if (f.get("contentName") or f.get("coName") or "") not in existing_names]
        skipped = before - len(new_files)
        if skipped:
            _phase("跳过已存在", skipped=skipped)

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
                _mark(mid, "invalid", "转存失败(链接失效)：" + e.message)
                return {"status": "invalid", "msg": e.message}
            _mark(mid, "error", "转存失败：" + e.message)
            return {"status": "error", "msg": e.message}
        except TokenExpired:
            _mark(mid, "paused", "登录已失效，已暂停（网页重新登录后自动恢复）")
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
                    _mark(mid, "paused", "登录已失效，已暂停（网页重新登录后自动恢复）")
                    return {"status": "paused", "msg": "登录已失效"}
                except Exception as e:
                    last_err = str(e)
                    if attempt < GEN_RETRY - 1:
                        time.sleep(GEN_RETRY_WAIT)
            if last_err:
                _mark(mid, "error", "生成CAS失败：" + last_err)

    # 5) 回写基线（已转存的文件必须并入基线，避免下次重复转存；
    #    但 CAS 生成失败时不要掩盖错误状态——generate 是全目录幂等重算，
    #    下次扫描会对这些文件重试 CAS，无需把它们移出基线）
    #    ⚠️ 必须回写 last_files（当前全部 contentID），否则下次扫描差集会再次全量转存
    _mark(mid, ("error" if last_err else "ok"),
          (("生成CAS失败：" + last_err) if last_err else _summary(added, len(files))),
          last_files=list(cur_ids))
    _phase("完成", added=added)
    return {"status": ("error" if last_err else "ok"), "added": added, "total": len(files)}


# 扫描串行性：后台首扫线程、调度器、手动「立即扫描」共用 _scan_lock，
# 避免同一监控项并发扫描导致重复转存 / 重复生成 CAS。
_scan_lock = threading.Lock()


def _first_scan(mid):
    """后台首次全量扫描：添加监控后立即调用，对分享现有内容做一次处理。
    scan_one 跑完会自行回写基线（last_files=本次全部），实现『添加即处理现有 + 后续自动追更』。
    首次扫描时 prev 为空 → 全部文件当新增 → 全量转存+生成CAS。"""
    try:
        with _scan_lock:
            m = monitor_store.get(mid)
            if m:
                scan_one(m)
    except Exception:
        pass


def add_and_baseline(link_id, pwd, target, auto_cas, delete_source, interval, preserve_structure=True, skip_first_scan=False):
    """添加监控项。

    [6.1.6] 添加即后台全量首扫（添加即处理现有内容 + 后续自动追更）。
    [6.1.7] 新增 skip_first_scan：分享一条龙在转存/生成CAS已完成后才把链接加入监控，
            此时不必再首扫转存（否则会和一条龙重复，造成每集两份副本），只把当前分享
            文件设为基线、让监控专职「后续追更」即可。
    """
    mon = monitor_store.add({
        "link_id": link_id, "pwd": pwd, "target": target,
        "auto_cas": auto_cas, "delete_source": delete_source,
        "interval_min": interval, "preserve_structure": preserve_structure,
    })
    client = _client()
    if client is not None and not _expired():
        total = 0
        link_name = ""
        share_files = []
        try:
            share_files = list_all_share_files(link_id, pwd, token=client.token)
            total = len(share_files)
            try:
                sinfo = get_share_info(link_id, pwd, "root", 1, 1,
                                       getattr(client, "account", ""), client.token)
                link_name = (sinfo or {}).get("lkName") or ""
            except Exception:
                pass
        except Exception:
            pass
        if skip_first_scan:
            # [6.1.7] 由分享一条龙调用：一条龙已转存+生成CAS，这里只把当前分享文件设为基线，
            # 监控专职「后续追更」，避免首扫再把全部文件当新增转存一遍（=2 份副本）。
            monitor_store.update(mon["id"], link_name=link_name, status="ok",
                                 last_result="已加入监控（内容由分享一条龙处理），等待追更",
                                 last_files=[f["contentID"] for f in share_files],
                                 last_scan=_now())
        else:
            # 独立添加：立即后台全量首扫（scan_one 自带云盘感知去重，已存在的跳过）
            monitor_store.update(mon["id"], link_name=link_name, status="pending",
                                 last_result="已添加，后台开始首扫（共 %d 个视频）" % total)
            threading.Thread(target=_first_scan, args=(mon["id"],), daemon=True).start()
    else:
        monitor_store.update(mon["id"], status="paused",
                             last_result="未登录/登录已失效，添加成功但暂未首扫（网页登录后自动补扫）")
    return mon


# ===== 调度器：崩溃自愈 + 看门狗 + 过期降级（6.1.6） =====
# 心跳：每次循环都刷新，供 UI 判断调度器是否存活（不依赖某次扫描是否更新 last_scan）
SCHEDULER_LAST_TICK = {"t": "", "errors": 0}
_SCHEDULER = None
_SCHEDULER_LOCK = threading.Lock()
_WATCHDOG = None


def scheduler_alive():
    return _SCHEDULER is not None and _SCHEDULER.is_alive()


def scheduler_status():
    """供 app.py 在 monitor_list 中返回调度器健康度，便于排查『扫描没用』。"""
    return {
        "alive": scheduler_alive(),
        "last_tick": SCHEDULER_LAST_TICK.get("t", ""),
        "errors": SCHEDULER_LAST_TICK.get("errors", 0),
    }


def ensure_scheduler():
    """确保调度器线程存活；若未启动或已死则（重新）启动。看门狗与 Web 请求共用此入口。"""
    global _SCHEDULER
    with _SCHEDULER_LOCK:
        if _SCHEDULER is None or not _SCHEDULER.is_alive():
            _SCHEDULER = MonitorScheduler()
            _SCHEDULER.start()
            return True
    return False


class MonitorScheduler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="casgen-monitor-scheduler")
        self._stop = threading.Event()

    def run(self):
        """调度主循环。整体包 try/except：任何异常都不让线程崩溃退出，
        否则扫描会永久停止（表现即『定时扫描没用』）。"""
        global SCHEDULER_LAST_TICK
        while not self._stop.is_set():
            try:
                SCHEDULER_LAST_TICK["t"] = _now()
                # [6.1.7] 不再因全局过期就整体 continue——那样 scan_one 内部「过期也刷新
                # last_scan」的逻辑永远执行不到，扫描时间会冻结在添加时刻（表现即
                # 「定时扫描没用 / 手动扫描也无效」）。改为让 scan_one 照常运行：过期时它
                # 会标记 paused 并刷新 last_scan，UI 能看到真实状态，登录恢复后下次即正常。
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
                            # 与后台首扫 / 手动扫描共用锁，避免并发重复转存
                            with _scan_lock:
                                scan_one(m)
                        except Exception as e:
                            # 单条扫描异常不影响其他监控项，也绝不崩溃线程
                            try:
                                monitor_store.update(m["id"], last_scan=_now(), status="error",
                                                     last_result="扫描异常：" + str(e))
                            except Exception:
                                pass
            except Exception as e:
                # 循环体整体异常：记录后继续，不让线程死掉
                SCHEDULER_LAST_TICK["errors"] = SCHEDULER_LAST_TICK.get("errors", 0) + 1
                try:
                    print("[monitor] 调度循环异常（已自愈，继续运行）: %s" %
                          (str(e)[:200]), flush=True)
                    traceback.print_exc()
                except Exception:
                    pass
            # 固定 60s 轮询节奏（扫描过程中的阻塞睡眠已计入，这里补足间隔）
            time.sleep(60)

    def stop(self):
        self._stop.set()


def _watchdog_loop():
    """看门狗：每 60s 检查调度器是否存活，意外死亡则自动重启。"""
    while True:
        try:
            time.sleep(60)
            if _SCHEDULER is None or not _SCHEDULER.is_alive():
                try:
                    print("[monitor] 看门狗：调度器线程已停止，自动重启", flush=True)
                except Exception:
                    pass
                ensure_scheduler()
        except Exception:
            pass


def start_scheduler():
    """启动调度器 + 看门狗（幂等，重复调用安全）。"""
    ensure_scheduler()
    global _WATCHDOG
    if _WATCHDOG is None or not _WATCHDOG.is_alive():
        _WATCHDOG = threading.Thread(target=_watchdog_loop, daemon=True,
                                     name="casgen-monitor-watchdog")
        _WATCHDOG.start()
