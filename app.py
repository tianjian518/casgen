"""CAS 转换网页服务 - 纯标准库（无需 pip / 无需 Docker）?

运行方式??
    python3 app.py
然后浏览器打开 http://localhost:5000  （若?? N1 盒子上跑，则?? http://N1的IP:5000??

所有功能都在网页里用鼠标点，不用碰命令行、不用改代码??
"""
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from yidong import Yun139, TokenExpired, __version__ as VERSION, VIDEO_EXT, CAS_EXT
from share139 import (parse_share_input, get_share_info, list_all_share_files,
                      list_share_catalogs, save_share_files, transfer_share_files,
                      transfer_share_videos, ShareError)
import monitor
import monitor_store
import rename
# from tianyi import Tianyi189  # 天翼(189)模块暂未完成

CLIENT = None
PROVIDER = None
AUTH_EXPIRED = False  # 登录态是否失效（token 过期等）；失效则提示重登并暂停监??
RECORDS = []  # 最近一次转换的记录，供"恢复播放"使用
# 后台 CAS 生成任务??"转存并生?? CAS 一条龙"时，避免长请求卡死浏览器??
CAS_JOBS = {}
CAS_JOBS_LOCK = threading.Lock()
_JOB_SEQ = [0]
# 链接监控"立即扫描"的异步状态：mid -> {phase,status,msg,added,total,error}
MONITOR_SCAN_JOBS = {}
MONITOR_SCAN_LOCK = threading.Lock()
ROOT = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(ROOT, "index.html")
DEBUG_LOG = os.path.join(ROOT, "casgen_debug.log")
AUTH_FILE = os.path.join(ROOT, "casgen_auth.json")  # 登录态持久化（存明文 token，本地单用户 NAS 可接受）


def _log_debug(tag, obj):
    """把关键调试信息追加写?? casgen_debug.log，方便小白用户直接把文件发来分析??"""
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (tag, json.dumps(obj, ensure_ascii=False)))
    except Exception:
        pass


# ============================ 登录态持久化??3.0 模块一?? ============================
def _save_auth(auth, provider):
    """把登录凭证落盘，重启/刷新页面后自动恢复，无需重新粘贴??"""
    try:
        with open(AUTH_FILE, "w", encoding="utf-8") as f:
            json.dump({"token": auth, "provider": provider}, f)
    except Exception:
        pass


def _load_auth():
    try:
        if os.path.exists(AUTH_FILE):
            with open(AUTH_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _count_videos(client, catalog):
    """ݹͳĳĿ¼£Ŀ¼Ƶļڵ 139 ӳ١
    ṹƵ target/<>/ Ƕײ㣬ݹ"""
    try:
        n = 0
        root = catalog if catalog not in ("root", "/") else "root"
        for it in client.iter_all(root, max_depth=20):
            if client._is_folder(it):
                continue
            name = client._name(it)
            ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
            if ext in CAS_EXT:
                n += 1
        return n
    except Exception:
        return 0


def _existing_stems(client, target_catalog):
    """收集 target_catalog 下（递归）已存在的 .cas 文件对应的视频文件名（含扩展名，如 '第11集.mkv'）。
    生成 CAS 后源视频会被删除，目标目录只剩 .cas，故以 .cas 是否存在判断某剧集是否已处理，
    从而智能跳过（不重复转存/生成）。"""
    stems = set()
    try:
        for it in client.iter_all(target_catalog):
            if client._is_folder(it):
                continue
            nm = client._name(it) or ""
            if not nm.endswith(".cas"):
                continue
            video_name = nm[:-4]  # 去掉 .cas 后缀 -> '第11集.mkv'
            if video_name:
                stems.add(video_name)
    except Exception as e:
        _log_debug("existing_scan", {"error": str(e)})
    return stems


def _fstem_of(f):
    """取分享视频的文件名（含扩展名），与 _existing_stems 的 video_name 对应，用于同名匹配。"""
    return f.get("contentName") or f.get("coName") or ""


def _run_cas_job(job_id, client, target_catalog, delete_source, expected=0, pre_count=0, on_phase=None):
    """后台线程：等 139 索引延迟过后，对转存后的目录生成 CAS。进度写? CAS_JOBS[job_id]，前端轮询。"""
    def _set_phase(ph):
        with CAS_JOBS_LOCK:
            j = CAS_JOBS.setdefault(job_id, {})
            j["phase"] = ph
        if callable(on_phase):
            try:
                on_phase(ph)
            except Exception:
                pass
    try:
        # 139 转存?"索引延迟"：刚保存的文件要等若干秒才出现在目录列表里?
        # 先轮询等到目录视频数达到"基线+预期新增"，或超时（最? 3 分钟）再生成?
        # 否则 generate 列到 0 个视? ? 生成 0 ? .cas（正是之?"转存成功但没生成"的原因）?
        baseline = pre_count  # 转存前已有视频数(递归计数)，由 share_save 在转存前测量后传入
        deadline = time.time() + 180
        with CAS_JOBS_LOCK:
            CAS_JOBS[job_id]["status"] = "waiting"
        _set_phase("转存完成，等待 139 索引更新")
        while time.time() < deadline:
            cur = _count_videos(client, target_catalog)
            # 关键：转存在后台任务启动前往往已完成，目录里可能已有这些视频?
            # 只要"当前视频? >= 预期新增?"?"比基线多出了预期?"，就说明已就位，立即生成?
            # 否则会一直等 baseline+expected 而永远等不到（表现为 CAS 卡死 3 分钟）?
            if expected and cur >= baseline + expected:
                break
            if not expected and cur > baseline:
                break
            time.sleep(8)
        with CAS_JOBS_LOCK:
            CAS_JOBS[job_id]["status"] = "running"
        _set_phase("正在生成 CAS")

        def _prog(n):
            with CAS_JOBS_LOCK:
                j = CAS_JOBS.get(job_id)
                if j:
                    j["done"] = n
        # generate 阶段回调：把内部阶段（生成中/删除原视频中/完成）映射回 phase
        def _gen_phase(ph, info=None):
            if ph == "generating":
                _set_phase("正在生成 CAS")
            elif ph == "deleting":
                _set_phase("正在删除原视频（已处理 %d 个）" % (info or {}).get("count", 0))
            elif ph == "done":
                _set_phase("全部完成")
        results = client.generate(target_catalog, delete_source=delete_source, progress=_prog, on_phase=_gen_phase)
        with CAS_JOBS_LOCK:
            CAS_JOBS[job_id].update({"status": "done", "phase": "全部完成", "results": results,
                                     "done": len(results) if results else CAS_JOBS[job_id].get("done", 0)})
    except Exception as e:
        import traceback as _tb
        _log_debug("cas_job_error", {"job": job_id, "error": repr(e), "trace": _tb.format_exc()})
        with CAS_JOBS_LOCK:
            CAS_JOBS[job_id].update({"status": "error", "phase": "失败：" + str(e), "error": str(e)})


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                with open(INDEX, "rb") as f:
                    body = f.read()
            except Exception:
                self._json({"error": "index.html 缺失，请确认它和 app.py 在同一目录"}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/cas/"):
            self.handle_cas(self.path[len("/cas/"):])
        elif self.path == "/api/health":
            # Docker HEALTHCHECK / CI 冒烟测试探测点
            self._json({"ok": True, "status": "up", "version": VERSION})
        elif self.path == "/api/version":
            self._json({"ok": True, "version": VERSION})
        else:
            self._json({"error": "not found"}, 404)

    def handle_cas(self, rel_path):
        """CAS→Strm 播放网关??/cas/<139相对路径>/<xxx.cas> -> ??.cas -> 秒传恢复 -> 302 直链??
        播放器（网易爆米??/飞牛影视）读 .strm 里的 URL 命中这里，直?? 139 直链播放，不?? casgen 带宽??"""
        if CLIENT is None:
            self._json({"error": "未登?? 139 云盘，请先在网页登录"}, 401)
            return
        rel_path = urllib.parse.unquote(rel_path)
        try:
            cas_fid = CLIENT.resolve_path_readonly(rel_path)
            cas_name = rel_path.rsplit("/", 1)[-1]
            url, temp_fid = CLIENT.cas_get_play_link(cas_fid, cas_name)
        except TokenExpired:
            global AUTH_EXPIRED
            AUTH_EXPIRED = True
            self._json({"error": "登录已失效，请重新登??"}, 401)
            return
        except FileNotFoundError as e:
            self._json({"error": "未找??: %s" % e}, 404)
            return
        except Exception as e:
            self._json({"error": "CAS 播放失败: %s" % e}, 500)
            return
        # 302 重定向到 139 直链（播放器直连，不?? casgen 带宽??
        # 临时恢复的副本仅供播放器拉流，139 直链签名 15 分钟内有效；播完异步删掉，避免云盘堆积 TEMP_*
        # 注意：临时文件只由 _schedule_cas_cleanup 统一调度一次删除，
        # 不要在此再启一个 Timer，否则会被删两次（第二次静默失败，但浪费一次请求）。
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()
        # 异步延迟删除临时恢复文件，保住「省空间」核心价??
        self._schedule_cas_cleanup(temp_fid)

    def _schedule_cas_cleanup(self, temp_fid):
        if not temp_fid:
            return
        delay = int(os.environ.get("CASGEN_CAS_CLEANUP_DELAY", "3600"))

        def _del():
            try:
                if CLIENT is not None:
                    CLIENT.delete([temp_fid])
            except Exception:
                pass
        threading.Timer(delay, _del).start()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            p = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            p = {}
        try:
            self.route(p)
        except TokenExpired:
            # 任一 139 接口返回登录失效，统一提示前端重登，并标记失效
            global AUTH_EXPIRED
            AUTH_EXPIRED = True
            self._json({"ok": False, "needLogin": True, "error": "登录已失效，请重新登??"})
        except ShareError as e:
            # 分享类已知错误，原样回传（绝大多数业务错误走这条路）
            self._json({"ok": False, "error": e.message, "fatal": e.fatal, "code": e.api_code})
        except Exception as e:
            # 兜底：任何未预期异常都返?? JSON，绝不中断连接（否则浏览器只会看??
            # "Failed to fetch"，且?? log_message 被置空而丢?? traceback）?
            import traceback as _tb
            _log_debug("unhandled", {"action": (p or {}).get("action"),
                                     "error": repr(e), "trace": _tb.format_exc()})
            self._json({"ok": False, "error": "服务器内部错误：" + str(e)})

    def route(self, p):
        global CLIENT, PROVIDER, RECORDS, AUTH_EXPIRED
        action = p.get("action")

        if action == "login_status":
            return self._json({"ok": True, "loggedIn": CLIENT is not None,
                               "expired": AUTH_EXPIRED, "provider": PROVIDER})

        if action == "login":
            prov = p.get("provider")
            if prov == "139":
                auth = (p.get("authorization") or "").strip()
                if not auth:
                    return self._json({"ok": False, "error": "请填?? Authorization ??"}, 400)
                try:
                    c = Yun139(auth)
                    items, _, data = c.list_dir("root")  # 用列根目录验证账号有??
                    CLIENT = c
                    PROVIDER = "139"
                    AUTH_EXPIRED = False
                    _save_auth(auth, "139")
                    resp = {"ok": True, "provider": "139",
                            "version": VERSION,
                            "root": [self._fmt(i) for i in items],
                            "count": len(items),
                            "host": getattr(c, "host", None),
                            "used_module": "personal_new" if getattr(c, "prefer_module", None) else "personal",
                            "debug_modules": getattr(c, "all_modules", []),
                            "raw": data}
                    _log_debug("login", resp)
                    return self._json(resp)
                except Exception as e:
                    _log_debug("login_error", {"error": str(e)})
                    return self._json({"ok": False, "error": "登录/列举失败??" + str(e)}, 400)
            elif prov == "189":
                return self._json({"ok": False, "error": "天翼(189)模块暂未完成，请先用移动(139)"}, 400)
            return self._json({"ok": False, "error": "未知网盘"}, 400)

        if CLIENT is None:
            return self._json({"ok": False, "error": "请先登录"}, 400)

        if AUTH_EXPIRED:
            return self._json({"ok": False, "needLogin": True, "error": "登录已失效，请重新登??"}, 400)

        if action == "list":
            parent = p.get("parent", "root")
            try:
                # 翻页：139 /file/list 单页最多 100 条，目录文件多时必须循环取完，
                # 否则 >100 条会被截断丢失（之前只取第一页，是真实 bug）。
                items = []
                cursor = None
                while True:
                    page, cursor, _ = CLIENT.list_dir(parent, cursor)
                    if not page:
                        break
                    items.extend(page)
                    if not cursor:
                        break
            except Exception as e:
                import traceback; traceback.print_exc()
                return self._json({"ok": False, "error": f"获取目录列表失败：{str(e)}"}, 500)
            # 调试：输出前几项的原始数据到日志文件（方便排查）
            with open("/tmp/casgen_list_debug.log", "a") as f:
                f.write(f"\n=== list(parent={parent}) ===\n")
                f.write(f"Items count: {len(items)}\n")
                for i, item in enumerate(items[:5]):  # 只记录前5项
                    f.write(f"Item {i}: type={item.get('type')}, fileType={item.get('fileType')}, contentType={item.get('contentType')}, name={item.get('name')}, fileId={item.get('fileId')}\n")
            fmt = [self._fmt(i) for i in items]
            # 透传 139 原始 fileId，防?? _fmt 归一化在个别字段名下丢失??
            # 前端点击展开时用 it.fileId || it._rawId 兜底，避?? parent 为空退化成根目录?
            for raw, f in zip(items, fmt):
                f["_rawId"] = (raw.get("fileId") or raw.get("contentID") or raw.get("id")
                               or raw.get("catalogId") or raw.get("fid") or "")
            return self._json({"ok": True, "items": fmt, "count": len(items)})

        # ---------- 目录选择器：在指定目录下新建子目?? ----------
        if action == "create_folder":
            parent = p.get("parent", "root")
            name = (p.get("name") or "").strip()
            if not name:
                return self._json({"ok": False, "error": "请输入目录名"}, 400)
            try:
                fid = CLIENT.find_or_create_folder(name, parent)
            except Exception as e:
                return self._json({"ok": False, "error": "创建文件夹失败：" + str(e)}, 400)
            return self._json({"ok": True, "fileId": fid, "name": name, "parent": parent})

        if action == "plan":
            root = p.get("root", "root")
            ok, skip, sample = CLIENT.plan(root)
            return self._json({"ok": True, "version": VERSION, "can": ok, "skip": skip,
                               "can_count": len(ok), "skip_count": len(skip),
                               "sample": sample,
                               "host": getattr(CLIENT, "host", None)})

        if action == "convert":
            root = p.get("root", "root")
            delete = bool(p.get("delete_source", False))
            res = CLIENT.generate(root, delete_source=delete)
            RECORDS = [r for r in res if r.get("status") == "uploaded"]
            _log_debug("convert", res)
            return self._json({"ok": True, "version": VERSION, "results": res})

        # ---------- [4.0] CAS→Strm 批量生成 ----------
        if action == "generate_strm":
            root = p.get("root", "root") or "root"
            public_url = (p.get("publicUrl") or os.environ.get("CASGEN_PUBLIC_URL", "")).strip()
            try:
                results = CLIENT.generate_strm(root, public_url)
                ok_n = sum(1 for r in results if r.get("status") == "uploaded")
                return self._json({"ok": True, "count": len(results),
                                   "uploaded": ok_n, "results": results,
                                   "publicUrl": public_url})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)

        # ---------- [4.0] L1 本地正则重命?? ----------
        if action == "rename_l1":
            root = p.get("root", "root") or "root"
            dry = bool(p.get("dryRun", False))
            try:
                renamed = skipped = failed = 0
                results = []
                for rel, it in CLIENT.walk(root):
                    if not rel.lower().endswith(".cas"):
                        continue
                    fid = CLIENT._fid(it)
                    old = CLIENT._name(it)
                    new = rename.l1_normalize(old)
                    if new == old:
                        skipped += 1
                        results.append({"old": old, "new": new, "status": "skip"})
                        continue
                    if dry:
                        renamed += 1
                        results.append({"old": old, "new": new, "status": "preview"})
                        continue
                    try:
                        CLIENT.rename_file(fid, new)
                        renamed += 1
                        results.append({"old": old, "new": new, "status": "renamed"})
                    except Exception as e:
                        failed += 1
                        results.append({"old": old, "new": new, "status": "failed", "error": str(e)})
                return self._json({"ok": True, "dryRun": dry, "renamed": renamed,
                                   "skipped": skipped, "failed": failed, "results": results})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)

        # ---------- [2.0] 分享链接一条龙 ----------
        if action == "share_parse":
            if CLIENT is None:
                return self._json({"ok": False, "needLogin": True, "error": "请先登录"})
            if AUTH_EXPIRED:
                return self._json({"ok": False, "needLogin": True, "error": "登录已失效，请重新登??"})
            text = (p.get("text") or "").strip()
            link_id, pwd = parse_share_input(text)
            if not link_id:
                return self._json({"ok": False,
                                    "error": "未能从输入中解析出分享链接ID（形?? https://yun.139.com/shareweb/#/w/i/xxxx??"})
            try:
                info = get_share_info(link_id, pwd, token=CLIENT.token)
                files = list_all_share_files(link_id, pwd, token=CLIENT.token, video_only=True)
                catalogs = list_share_catalogs(link_id, pwd, token=CLIENT.token)
            except ShareError as e:
                return self._json({"ok": False, "error": e.message, "fatal": e.fatal, "code": e.api_code})
            except Exception as e:
                return self._json({"ok": False, "error": "解析分享失败??" + str(e)})
            return self._json({
                "ok": True, "linkID": link_id, "needPwd": bool(pwd),
                "linkName": (info or {}).get("lkName", ""),
                "expireTime": (info or {}).get("expireTime", ""),
                "files": files, "count": len(files),
                "catalogs": catalogs, "catalogCount": len(catalogs),
            })

        if action == "share_save":
            if CLIENT is None:
                return self._json({"ok": False, "needLogin": True, "error": "请先登录"})
            if AUTH_EXPIRED:
                return self._json({"ok": False, "needLogin": True, "error": "登录已失效，请重新登??"})
            text = (p.get("text") or "").strip()
            target = (p.get("target") or "root").strip() or "root"
            auto_cas = bool(p.get("auto_cas", False))
            # 删除原视频仅在「自动生?? CAS」勾选时才有意义，否则忽??
            delete_source = bool(p.get("delete_source", False)) and auto_cas
            # Ƿ񰴷Ŀ¼ṹļУӰӾĿ¼ת浽Ŀ¼ĬϿǰȡ
            preserve_structure = bool(p.get("preserve_structure", True))
            link_id, pwd = parse_share_input(text)
            if not link_id:
                return self._json({"ok": False, "error": "未能解析出分享链接ID"})
            try:
                target_catalog = CLIENT.resolve_folder(target)
                files = list_all_share_files(link_id, pwd, token=CLIENT.token, video_only=True)
                catalogs = list_share_catalogs(link_id, pwd, token=CLIENT.token)
                # 结构保留：当分享根本有孙盹目录（当前给影规名称）时，
                # 整䛮录彪转存??139 会自动在 target 下恢?? <>/ 并上传全部子内容??
                preserve = preserve_structure and bool(catalogs)
                pre_count = _count_videos(CLIENT, target_catalog)
                # 智能增量：扫描目标目录已有的 .cas，过滤掉已生成的分享剧集，只转存/生成缺失的新剧集
                existing = _existing_stems(CLIENT, target_catalog)
                files_todo = [f for f in files if _fstem_of(f) not in existing]
                skipped = len(files) - len(files_todo)
                if preserve:
                    saved = transfer_share_files(
                        link_id, pwd, CLIENT.token, getattr(CLIENT, "account", ""),
                        files_todo, target_catalog,
                        make_folder=lambda pid, nm: CLIENT.find_or_create_folder(nm, pid),
                        flat=False,
                    ) if files_todo else 0
                    res = {"structured": True, "saved": saved, "skipped_existing": skipped}
                else:
                    co_paths = [f["path"] for f in files_todo]
                    if co_paths:
                        save_share_files(link_id, co_paths, [], target_catalog,
                                         need_password=bool(pwd), token=CLIENT.token,
                                         phone=getattr(CLIENT, "account", ""))
                    saved = len(co_paths)
                    res = {"saved": saved, "skipped_existing": skipped}
                job_id = None
                # [2.0+] 勾选「自动生?? CAS」：转存本身立即返回结果，CAS 生成放到后台线程跑，
                # 前端轮询进度。避免「保??+CAS」挤在同一 HTTP 请求里，把浏览器等超??/断开（表现为一??"转存??"）?
                if auto_cas:
                    with CAS_JOBS_LOCK:
                        _JOB_SEQ[0] += 1
                        job_id = "cas_%d" % _JOB_SEQ[0]
                        CAS_JOBS[job_id] = {"status": "running", "total": saved,
                                            "done": 0, "results": None, "error": None,
                                            "phase": "转存完成，正在等待 139 索引更新"}
                    threading.Thread(target=_run_cas_job,
                                     args=(job_id, CLIENT, target_catalog, delete_source, saved, pre_count),
                                     daemon=True).start()
                # [合并监控] 勾选则在转存成功后把链接加入定时监控（每小时间隔扫描新增剧集）
                # [BUG修复] 前端 share.html 提交的参数名是 add_to_monitor，后端原先只读 "monitor"，
                # 两边不一致 => 用户勾了「转存后加入监控」却永远走不到这里：静默失败、监控列表空空如也，
                # 且前端拿不到任何提示。现两种写法都认，确保勾选一定生效。
                monitor_added = False
                monitor_id = None
                _mon_info = None
                _want_monitor = bool(p.get("monitor", False)) or bool(p.get("add_to_monitor", False))
                if _want_monitor:
                    try:
                        _mon = monitor.add_and_baseline(link_id, pwd, target, auto_cas,
                                                        delete_source, 60, preserve_structure)
                        monitor_id = _mon.get("id")
                        monitor_added = True
                        # 前端读 d.monitor.id / d.monitor.intervalMin / d.monitor.error（驼峰 intervalMin）
                        _mon_info = {"id": _mon.get("id"),
                                     "intervalMin": _mon.get("interval_min") or 60,
                                     "error": None}
                        monitor.start_scheduler()
                    except Exception as _me:
                        _log_debug("monitor_add_from_share", {"error": str(_me)})
                        _mon_info = {"error": "加入监控失败：" + str(_me)}
            except ShareError as e:
                return self._json({"ok": False, "error": e.message, "fatal": e.fatal, "code": e.api_code})
            except Exception as e:
                import traceback as _tb
                _log_debug("unhandled", {"action": "share_save", "error": repr(e), "trace": _tb.format_exc()})
                return self._json({"ok": False, "error": "转存失败??" + str(e)})
            # [BUG修复] 前端 share.html 读的是 addToMonitor + monitor 对象（驼峰），
            # 后端原先只返回 monitorAdded/monitorId，前端 if(d.addToMonitor && d.monitor) 永远为假
            # => 既不会提示「已加入监控」，也不会刷新监控列表，用户以为成功了其实没加。
            return self._json({"ok": True, "targetCatalog": target_catalog,
                               "saved": saved, "result": res,
                               "autoCas": auto_cas, "jobId": job_id,
                               "structurePreserved": preserve,
                               "catalogCount": len(catalogs),
                               "monitorAdded": monitor_added,
                               "monitorId": monitor_id,
                               "addToMonitor": monitor_added,
                               "monitor": _mon_info})

        if action == "cas_job_status":
            job_id = p.get("jobId")
            with CAS_JOBS_LOCK:
                job = dict(CAS_JOBS.get(job_id, {}))
            if not job:
                return self._json({"ok": False, "error": "任务不存在或已过??"})
            return self._json({"ok": True, "status": job.get("status"),
                               "phase": job.get("phase"), "total": job.get("total"), "done": job.get("done"),
                               "results": job.get("results"), "error": job.get("error")})

        if action == "restore":
            sha = p.get("sha256")
            size = p.get("size")
            name = p.get("name")
            parent = p.get("parent", "root")
            if not (sha and size and name):
                return self._json({"ok": False, "error": "缺少参数"}, 400)
            r = CLIENT.restore(sha, size, name, parent)
            resp = {"ok": True, "result": r}
            _log_debug("restore", resp)
            return self._json(resp)

        # ---------- [3.0] 分享链接定时监控 ----------
        if action == "monitor_add":
            text = (p.get("text") or "").strip()
            target = (p.get("target") or "root").strip() or "root"
            auto_cas = bool(p.get("auto_cas", True))
            delete_source = bool(p.get("delete_source", False)) and auto_cas
            interval = max(60, int(p.get("interval_min") or 60))
            preserve_structure = bool(p.get("preserve_structure", True))
            link_id, pwd = parse_share_input(text)
            if not link_id:
                return self._json({"ok": False, "error": "未能解析出分享链接ID"})
            if CLIENT is None:
                return self._json({"ok": False, "needLogin": True, "error": "请先登录"})
            if AUTH_EXPIRED:
                return self._json({"ok": False, "needLogin": True, "error": "登录已失效，请重新登??"})
            try:
                mon = monitor.add_and_baseline(link_id, pwd, target, auto_cas, delete_source, interval, preserve_structure)
            except ShareError as e:
                return self._json({"ok": False, "error": "链接验证失败??" + e.message, "fatal": e.fatal})
            except TokenExpired:
                return self._json({"ok": False, "needLogin": True, "error": "登录已失??"})
            return self._json({"ok": True, "monitor": mon})

        if action == "monitor_list":
            return self._json({"ok": True, "monitors": monitor_store.list_all()})

        if action == "monitor_remove":
            mid = p.get("id")
            if not mid:
                return self._json({"ok": False, "error": "缺少 id"})
            monitor_store.remove(mid)
            return self._json({"ok": True})

        if action == "monitor_scan_now":
            mid = p.get("id")
            m = monitor_store.get(mid)
            if not m:
                return self._json({"ok": False, "error": "监控项不存在"})
            if CLIENT is None:
                return self._json({"ok": False, "needLogin": True, "error": "请先登录"})
            if AUTH_EXPIRED:
                return self._json({"ok": False, "needLogin": True, "error": "登录已失效，请重新登??"})
            # 改为后台线程执行，避免列举/转存/生成 CAS（可能十几秒）阻塞 HTTP 请求；
            # 前端轮询 monitor_scan_status 获取阶段提示与最终结果。
            def _scan_worker():
                import traceback as _tb
                with MONITOR_SCAN_LOCK:
                    MONITOR_SCAN_JOBS[mid] = {"phase": "准备中", "status": "scanning", "msg": "", "added": 0, "total": 0}
                def _on_phase(ph, info=None):
                    with MONITOR_SCAN_LOCK:
                        d = MONITOR_SCAN_JOBS.get(mid, {})
                        d["phase"] = ph
                        MONITOR_SCAN_JOBS[mid] = d
                try:
                    res = monitor.scan_one(m, on_phase=_on_phase)
                    with MONITOR_SCAN_LOCK:
                        MONITOR_SCAN_JOBS[mid].update({"status": "done", "phase": "完成",
                                                       "msg": res.get("msg", ""), "added": res.get("added", 0),
                                                       "total": res.get("total", 0), "result": res})
                except Exception as _e:
                    with MONITOR_SCAN_LOCK:
                        MONITOR_SCAN_JOBS[mid].update({"status": "error", "phase": "失败",
                                                       "error": str(_e), "trace": _tb.format_exc()})
            threading.Thread(target=_scan_worker, daemon=True).start()
            return self._json({"ok": True, "scanning": True})

        if action == "monitor_scan_status":
            mid = p.get("id")
            with MONITOR_SCAN_LOCK:
                st = dict(MONITOR_SCAN_JOBS.get(mid, {}))
            if not st:
                return self._json({"ok": True, "scanning": False})
            return self._json({"ok": True, "scanning": st.get("status") == "scanning", **st})

        return self._json({"ok": False, "error": "未知操作"}, 400)

    @staticmethod
    def _fmt(i):
        if not isinstance(i, dict):
            i = {}
        # ID：兼容 139 个人云多种返回字段名（personal / personal_new / 旧版 contentID 等）
        fid = (i.get("fileId") or i.get("contentID") or i.get("id")
               or i.get("catalogId") or i.get("fid") or i.get("fileIdStr") or "")
        # 文件夹判断：type=="folder"/"dir"，或 fileType/contentType 为数字 1/2（1=目录，2=文件），
        # 或显式 isFolder 标记。额外增加对 catalogType、isDir 等常见字段的兼容。
        t = i.get("type") or i.get("fileType") or i.get("contentType") or i.get("catalogType") or ""
        is_folder = False
        if isinstance(t, str):
            is_folder = t.strip().lower() in ("folder", "dir", "directory", "catalog")
        elif isinstance(t, (int, float)):
            is_folder = t in (1, 2, 10, 20)  # 扩展可能的数字值
        if not is_folder and i.get("isFolder") is True:
            is_folder = True
        if not is_folder and i.get("isFolder") == 1:
            is_folder = True
        if not is_folder and i.get("isDir") is True:
            is_folder = True
        if not is_folder and str(i.get("category", "")).lower() in ("folder", "directory", "catalog"):
            is_folder = True
        # 兜底：如果 name 以 / 结尾或者含有某些特征，也可能暗示是文件夹（保守策略）
        if not is_folder and isinstance(i.get("name"), str):
            if i["name"].endswith("/"):
                is_folder = True
        return {
            "fileId": fid,
            "name": i.get("name") or i.get("fileName") or i.get("contentName") or "",
            "type": "folder" if is_folder else "file",
            "size": i.get("size") or i.get("fileSize") or i.get("contentSize") or 0,
        }


def main():
    global CLIENT, PROVIDER, AUTH_EXPIRED
    # 每次启动时清空旧调试日志，避免多轮测试内容混在一??
    try:
        open(DEBUG_LOG, "w", encoding="utf-8").close()
    except Exception:
        pass
    # 启动即恢复持久化的登录态（3.0 模块一??
    auth = _load_auth()
    if auth and auth.get("token"):
        try:
            c = Yun139(auth["token"])
            try:
                c.list_dir("root")  # 轻量校验 token 是否仍有??
                AUTH_EXPIRED = False
            except TokenExpired:
                AUTH_EXPIRED = True
            except Exception:
                # 其他异常（如 139 临时网络抖动）：不轻易放弃恢复，
                # 仍挂?? CLIENT，让后续真实接口调用去检??/触发重登
                AUTH_EXPIRED = False
            CLIENT = c
            PROVIDER = auth.get("provider", "139")
        except Exception:
            CLIENT = None
    # 启动分享链接监控调度??3.0 模块??/四）
    monitor.bind(sys.modules[__name__])
    monitor.start_scheduler()

    port = int(os.environ.get("PORT", "5000"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    print("=" * 50)
    print("CAS 转换服务已启??")
    print(f"  本机浏览器打开?? http://localhost:{port}")
    print(f"  其他设备打开??   http://<本机IP>:{port}")
    print("?? Ctrl+C 停止")
    print("=" * 50)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停??")


if __name__ == "__main__":
    main()
