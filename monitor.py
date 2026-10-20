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
from yidong import TokenExpired, NetworkError, is_network_error
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


def _summary(added, total, missing=0):
    if added:
        base = "%s 扫描：新增 %d 个视频（共 %d），已转存并生成 CAS" % (_now(), added, total)
    else:
        base = "%s 扫描：无新增（共 %d）" % (_now(), total)
    if missing:
        base += "；仍有 %d 个未落地，下轮自动重试" % missing
    return base


# 在途宽限：刚提交转存的任务 139 是异步搬运，文件不会立刻可见。
# 宽限期内的文件不重复转存（防首扫/调度/手扫并发造成副本），只等它自己落地。
PENDING_GRACE = 7200  # 秒（2 小时）


def _list_dir_split(client, parent):
    """列一个目录，返回 (文件名的 set, {文件夹名: folderID}, ok)。
    ok=False 表示列举出错（如登录失效/网络抖动）——调用方据此判断「无法确认」，避免误判。"""
    files, folders = set(), {}
    try:
        cursor = None
        while True:
            items, cursor, _ = client.list_dir(parent, cursor)
            for it in (items or []):
                if client._is_folder(it):
                    folders[client._name(it) or ""] = client._fid(it)
                else:
                    files.add(client._name(it) or "")
            if not cursor:
                break
        return files, folders, True
    except Exception:
        return files, folders, False


def _dest_of(f, flat):
    """分享文件在目标云盘里的相对路径：(文件夹元组, 文件名)。
    flat=True（不保留结构）时文件夹为空，全部平铺进目标目录。"""
    name = f.get("contentName") or f.get("coName") or ""
    if flat:
        return ((), name)
    return (tuple(f.get("folders") or []), name)


def _target_existing_paths(client, target_catalog, dest_folders, _cache=None):
    """返回目标云盘里已存在文件的「目标相对路径」集合：{(文件夹元组, 文件名)}，
    以及 all_ok（是否所有目录都列举成功）。

    .cas 会去掉后缀按原视频名计入（删源后只剩 .cas，也算已存在）。
    只列 dest_folders 里关心的目录，不遍历整棵树——省接口调用。

    ⚠️ [6.1.8] 必须按「目录 + 文件名」精确判定。6.1.7 用「全局文件名集合」去重是错的：
    不同影视剧常有同名「第N集.mkv」，目标目录里只要别的剧有这些文件名，本剧新剧集
    就会被误判成「已存在」而跳过不转存——表现就是界面显示 16 集、云盘还是 5 集。
    """
    if _cache is None:
        _cache = {}

    def _list(parent):
        if parent not in _cache:
            _cache[parent] = _list_dir_split(client, parent)
        return _cache[parent]

    existing = set()
    all_ok = True
    for folders in dest_folders:
        cur = target_catalog
        ok = True
        for nm in folders:
            _, sub, sub_ok = _list(cur)
            if not sub_ok:
                ok = False
                all_ok = False
                break
            if nm not in sub:
                ok = False
                break
            cur = sub[nm]
        if not ok:
            continue
        files, _, files_ok = _list(cur)
        if not files_ok:
            all_ok = False
            continue
        for nm in files:
            existing.add((tuple(folders), nm[:-4] if nm.endswith(".cas") else nm))
    return existing, all_ok


def _mark(mid, status, result, **extra):
    """统一回写监控项状态，并强制刷新 last_scan（关键：避免记录冻结在添加时刻）。"""
    fields = {"last_scan": _now(), "status": status, "last_result": result}
    fields.update(extra)
    # 扫描成功即清零连续失败计数（无论此前是网络问题还是业务错误）
    if status in ("ok", "invalid", "paused", "no_client"):
        fields.setdefault("fail_count", 0)
    monitor_store.update(mid, **fields)


# 连续失败多少次后把状态从 net_error 升级为 error（避免网络长期不通时界面一直显示「正常」）
NET_ERR_ESCALATE = 6
# 处于异常状态时的快速重试间隔（秒）：比正常扫描间隔短，网络恢复后尽快自动补扫
NET_ERR_RETRY_SEC = 300


def _net_or_error(mid, prefix, e):
    """[8.0.1] 区分「网络瞬断」与「真实故障」，返回统一结果 dict。

    - 网络类错误：状态记 net_error，保持 enabled、下轮照常重试，连续 6 次（约 6 小时）
      仍不通才升级为 error 提示用户排查网络；期间界面显示为「网络重试中」而非红色故障。
    - 业务类错误：维持原逻辑记 error。
    这样无论在 Windows 本机、Linux 服务器、Docker 容器还是群晖等环境，
    DNS 抖动/出网抖动都只会「自动重试」，不会把用户工作量甩到手动重扫上。
    """
    msg = str(e)
    if isinstance(e, NetworkError) or is_network_error(e):
        m = monitor_store.get(mid) or {}
        cnt = int(m.get("fail_count") or 0) + 1
        if cnt >= NET_ERR_ESCALATE:
            _mark(mid, "error", "网络持续异常（已重试 %d 次）：%s" % (cnt, msg), fail_count=cnt)
            return {"status": "error", "msg": "网络持续异常: " + msg, "network": True}
        _mark(mid, "net_error", "网络异常，下轮自动重试（第 %d 次）：%s" % (cnt, msg), fail_count=cnt)
        return {"status": "net_error", "msg": msg, "network": True}
    _mark(mid, "error", prefix + "：" + msg)
    return {"status": "error", "msg": prefix + ": " + msg}


def _target_existing_names_LEGACY(client, target_catalog, preserve_structure):
    """[6.1.7 遗留，6.1.8 起停用] 按全局文件名去重——会把别的剧的同名剧集误判成
    「已存在」，导致新剧集被跳过不转存（界面 16 集、云盘 5 集）。保留仅作注释参考。"""
    return set()


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
        # [8.0.1] 网络瞬断（DNS 解析失败/连接抖动）≠ 监控项故障：
        # 任何设备/服务器/容器都可能遇到，下轮自动重试即可，不标成 error 让人手动点。
        return _net_or_error(mid, "目录解析失败", e)

    # 2) 列举分享当前文件
    _phase("列举分享文件")
    try:
        files = list_all_share_files(monitor["link_id"], monitor["pwd"], token=client.token)
    except ShareError as e:
        if e.fatal:
            _mark(mid, "invalid", "链接失效：" + e.message)
            return {"status": "invalid", "msg": e.message}
        # fatal=False 里既有真实业务错误，也有「请求 139 分享接口失败」（网络层）——
        # 后者必须按网络瞬断处理，下轮自动重试。
        return _net_or_error(mid, "列举失败", e)
    except TokenExpired:
        _mark(mid, "paused", "登录已失效，已暂停（网页重新登录后自动恢复）")
        return {"status": "paused", "msg": "登录已失效"}
    except Exception as e:
        return _net_or_error(mid, "列举失败", e)

    # 3) 差异检测 + 云盘实况确认
    _phase("差异检测")
    prev = set(monitor.get("last_files") or [])
    flat = not monitor.get("preserve_structure", True)
    now_ts = time.time()
    pending = dict(monitor.get("pending_files") or {})
    inflight = {cid for cid, ts in pending.items()
                if isinstance(ts, (int, float)) and now_ts - ts < PENDING_GRACE}

    # [6.1.8] 云盘实况是唯一真相：先确认目标云盘里已有哪些（同目录同名 / 同名 .cas）。
    # ⚠️ 旧逻辑有两个叠加缺陷，正是「界面 16 集、云盘 5 集」的根因：
    #   ① 去重按「全局文件名」——别的剧有同名「第N集.mkv」就会把本剧新剧集误判成已存在；
    #   ② 基线无条件写成「分享当前全部」——转存被跳过/失败后，缺失剧集永远被标记成已处理，
    #      下轮差集为 0，再也不补（旧基线被污染后同样永不恢复）。
    # 现改为：按「目标路径」精确去重；待转存 = 云盘里确实缺失的文件，一轮内直接补，自愈。
    _phase("检查云盘实况")
    dest_folders = {_dest_of(f, flat)[0] for f in files}
    existing_paths, ex_ok = _target_existing_paths(client, target_catalog, dest_folders)
    if ex_ok:
        prev_confirmed = {f["contentID"] for f in files if _dest_of(f, flat) in existing_paths}
        todo = [f for f in files if f["contentID"] not in prev_confirmed]
    else:
        # 云盘实况无法确认（列举出错）：退回按基线差集，避免把已有内容当缺失全量重转
        prev_confirmed = prev
        todo = [f for f in files if f["contentID"] not in prev]
    waiting = [f for f in todo if f["contentID"] in inflight]
    todo = [f for f in todo if f["contentID"] not in inflight]
    if waiting:
        _phase("等待在途转存落地", waiting=len(waiting))

    added = 0
    last_err = ""  # 关键修复：auto_cas 关闭或无可转存新文件时该变量不会被赋值，必须在顶部初始化，否则末尾回写基线时 UnboundLocalError 导致扫描直接 500
    submitted_ids = []
    if todo:
        _phase("转存新增", added=len(todo))
        try:
            added = transfer_share_files(
                monitor["link_id"], monitor["pwd"], client.token,
                getattr(client, "account", ""), todo, target_catalog,
                make_folder=lambda pid, nm: client.find_or_create_folder(nm, pid),
                flat=flat)
            submitted_ids = [f["contentID"] for f in todo]
        except ShareError as e:
            if e.fatal:
                _mark(mid, "invalid", "转存失败(链接失效)：" + e.message)
                return {"status": "invalid", "msg": e.message}
            return _net_or_error(mid, "转存失败", e)
        except TokenExpired:
            _mark(mid, "paused", "登录已失效，已暂停（网页重新登录后自动恢复）")
            return {"status": "paused", "msg": "登录已失效"}
        except Exception as e:
            # 转存中途网络中断：文件可能已部分提交，交由 pending_files 宽限机制处理，
            # 下轮按「云盘实况」重新差集，不会重复转存也不会漏补。
            return _net_or_error(mid, "转存失败", e)

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
                # 生成 CAS 失败时区分网络类：网络问题下轮自愈，不刷成 error 让人手动点
                return _net_or_error(mid, "生成CAS失败", Exception(last_err))

    # 5) 回写基线（[6.1.8] 关键修复）：基线只收录「已确认在目标云盘里真实存在」的文件，
    #    没落地的下轮扫描自动重试；在途文件记入 pending_files 宽限管理，防止重复转存。
    _phase("确认落地")
    # 注意：这里必须重新列举（不能用转存前的缓存），139 转存是异步任务，要拿到最新实况
    confirmed_paths, cf_ok = _target_existing_paths(client, target_catalog, dest_folders)
    if cf_ok:
        confirmed_ids = {f["contentID"] for f in files if _dest_of(f, flat) in confirmed_paths}
        missing = len(files) - len(confirmed_ids)
        new_pending = {cid: ts for cid, ts in pending.items() if cid not in confirmed_ids}
        for cid in submitted_ids:
            if cid not in confirmed_ids:
                new_pending[cid] = now_ts
        base_ids = confirmed_ids
    else:
        # 无法确认落地（列举出错）：保守回退，绝不因一次查询失败就清空基线
        # （否则下轮会把全部文件当新增重新转存，造成大批量副本）。
        base_ids = prev | set(submitted_ids)
        missing = 0
        new_pending = pending

    _mark(mid, ("error" if last_err else "ok"),
          (("生成CAS失败：" + last_err) if last_err else _summary(added, len(files), missing)),
          last_files=sorted(base_ids),
          pending_files=new_pending)
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
    [6.1.9] 同一链接重复添加时去重（monitor_store.add 判重）：已存在则只更新配置，
            不新建记录、不重复首扫；返回值带 _existed=True 供调用方提示用户。
            特例：已有记录但从未成功扫过（last_scan 为空）且非一条龙路径，仍触发一次首扫补齐。
    """
    mon, existed = monitor_store.add({
        "link_id": link_id, "pwd": pwd, "target": target,
        "auto_cas": auto_cas, "delete_source": delete_source,
        "interval_min": interval, "preserve_structure": preserve_structure,
    })
    if existed:
        # [6.1.9] 重复添加：保留已有运行状态（基线/上次扫描时间/在途文件全部不动），
        # 只把新配置写进去（monitor_store.add 已做）。绝不重复首扫——否则又和
        # 6.1.7 的撞车一样，把云盘已有的再转一遍。
        mon = dict(mon)
        mon["_existed"] = True
        if not skip_first_scan and not mon.get("last_scan"):
            # 例外：这条监控加了却从未扫过（比如当时未登录），补一次首扫
            client0 = _client()
            if client0 is not None and not _expired():
                monitor_store.update(mon["id"], status="pending",
                                     last_result="重新添加，后台开始首扫")
                threading.Thread(target=_first_scan, args=(mon["id"],), daemon=True).start()
            else:
                monitor_store.update(mon["id"], status="paused",
                                     last_result="未登录/登录已失效，配置已更新，登录后自动补扫")
        return mon
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
                    # [8.0.1] 网络异常状态用更短的间隔快速重试（5 分钟），不必等满 1 小时：
                    # 家庭宽带/Docker DNS 抖动通常几十秒就恢复，早点重试能自动补上本轮更新。
                    if m.get("status") in ("net_error", "error"):
                        iv = min(iv, NET_ERR_RETRY_SEC)
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
                            # 单条扫描异常不影响其他监控项，也绝不崩溃线程；同样区分网络类
                            try:
                                _net_or_error(m["id"], "扫描异常", e)
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
