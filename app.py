"""CAS 转换网页服务 - 纯标准库（无需 pip / 无需 Docker）。

运行方式：
    python3 app.py
然后浏览器打开 http://localhost:5000  （若在 N1 盒子上跑，则用 http://N1的IP:5000）

所有功能都在网页里用鼠标点，不用碰命令行、不用改代码。
"""
import base64
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from yidong import Yun139, TokenExpired, __version__ as VERSION
from share139 import (parse_share_input, get_share_info, list_all_share_files,
                      save_share_files, ShareError)
import monitor
import monitor_store
import rename
import webdav
# from tianyi import Tianyi189  # 天翼(189)模块暂未完成

CLIENT = None
PROVIDER = None
AUTH_EXPIRED = False  # 登录态是否失效（token 过期等）；失效则提示重登并暂停监控
MSISDN = ""  # 当前账号手机号（msisdn）：139 转存接口必填，登录时从 token 解码
RECORDS = []  # 最近一次转换的记录，供"恢复播放"使用
ROOT = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(ROOT, "index.html")
DEBUG_LOG = os.path.join(ROOT, "casgen_debug.log")
AUTH_FILE = os.path.join(ROOT, "casgen_auth.json")  # 登录态持久化（存明文 token，本地单用户 NAS 可接受）
_START_TS = time.time()  # 进程启动时间，供 /api/health 计算 uptime


def _log_debug(tag, obj):
    """把关键调试信息追加写入 casgen_debug.log，方便小白用户直接把文件发来分析。"""
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (tag, json.dumps(obj, ensure_ascii=False)))
    except Exception:
        pass


# ============================ 手机号(msisdn) 提取 ============================
def _extract_phone(auth):
    """从 Authorization(base64) 里抠出手机号(msisdn)。139 转存接口必填。
    解码后格式： pc:<11位手机号>:<其余>。"""
    if not auth:
        return ""
    try:
        try:
            raw = base64.b64decode(auth)
        except Exception:
            raw = base64.b64decode(auth + "===")
        parts = raw.decode("utf-8", "ignore").split(":")
        if len(parts) >= 2 and re.fullmatch(r"\d{11}", parts[1] or ""):
            return parts[1]
    except Exception:
        pass
    return ""


def _apply_msisdn(auth):
    """登录时调用：解码手机号并同步给监控模块（转存必填）。"""
    global MSISDN
    MSISDN = _extract_phone(auth)
    try:
        monitor.set_phone(MSISDN)
    except Exception:
        pass


# ============================ 登录态持久化（3.0 模块一） ============================
def _save_auth(auth, provider):
    """把登录凭证落盘，重启/刷新页面后自动恢复，无需重新粘贴。"""
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


class H(BaseHTTPRequestHandler):
    def handle(self):
        """记录请求开始时间，供 log_request 计算耗时。"""
        self._t0 = time.monotonic()
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            # 客户端提前断开（播放器跳播/用户关闭页面），属于正常现象，静默忽略
            pass

    def log_message(self, *a):
        # 屏蔽 BaseHTTPRequestHandler 默认 stderr 日志（格式对运维不友好），改用 log_request
        pass

    def log_request(self, code="-", size="-"):
        try:
            dur_ms = (time.monotonic() - getattr(self, "_t0", time.monotonic())) * 1000
        except Exception:
            dur_ms = 0
        try:
            line = "%s - %s %s -> %s (%sB, %.1fms)\n" % (
                time.strftime("%H:%M:%S"), self.command, self.path, code, size, dur_ms)
            sys.stderr.write(line)
            sys.stderr.flush()
        except Exception:
            pass

    def log_error(self, format, *args):
        # 错误也走统一格式
        try:
            sys.stderr.write("%s - ERROR %s %s: %s\n" % (
                time.strftime("%H:%M:%S"), self.command, self.path, format % args))
            sys.stderr.flush()
        except Exception:
            pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # WebDAV GET 优先
        if self.path.startswith("/dav/"):
            return webdav.handle_get(self, schedule_cleanup_fn=self._schedule_cas_cleanup)
        # 静态 HTML 文件服务
        html_paths = ("/", "/index.html", "/convert.html", "/share.html", "/strm.html",
                      "/restore.html", "/rename.html", "/utils.js")
        if self.path in html_paths:
            filename = self.path.lstrip("/") or "index.html"
            fpath = os.path.join(ROOT, filename)
            # 防御路径穿越：确保解析后的绝对路径仍在 ROOT 内
            if not os.path.abspath(fpath).startswith(os.path.abspath(ROOT)):
                self._json({"error": "forbidden"}, 403)
                return
            if not os.path.exists(fpath):
                self._json({"error": f"{filename} 文件不存在"}, 404)
                return
            try:
                with open(fpath, "rb") as f:
                    body = f.read()
                ctype = "text/html; charset=utf-8" if filename.endswith(".html") else "application/javascript; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif self.path.startswith("/cas/"):
            self.handle_cas(self.path[len("/cas/"):])
        elif self.path == "/api/health":
            self._json({
                "ok": True,
                "version": VERSION,
                "loggedIn": CLIENT is not None,
                "expired": AUTH_EXPIRED,
                "schedulerRunning": monitor.is_scheduler_running() if hasattr(monitor, "is_scheduler_running") else False,
                "uptime_s": int(time.time() - _START_TS),
            })
        elif self.path == "/api/version":
            public_url = os.environ.get("CASGEN_PUBLIC_URL", "").strip()
            self._json({
                "ok": True,
                "version": VERSION,
                "publicUrlConfigured": bool(public_url),
                "publicUrl": public_url or None,
                "webdav": webdav.get_info(),
                "endpoints": {
                    "health": "/api/health",
                    "version": "/api/version",
                    "casGateway": "/cas/<139相对路径>/<xxx.cas>",
                    "webdav": "/dav/" if webdav.is_enabled() else None,
                },
            })
        else:
            self._json({"error": "not found", "hint": "试试 /api/health 或 /api/version"}, 404)

    # ---------- WebDAV 方法 ----------
    def do_OPTIONS(self):
        if self.path.startswith("/dav/"):
            return webdav.handle_options(self)
        self.send_response(200)
        self.send_header("Allow", "GET,POST,OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_PROPFIND(self):
        if self.path.startswith("/dav/"):
            return webdav.handle_propfind(self)
        self._json({"error": "not found"}, 404)

    def do_HEAD(self):
        if self.path.startswith("/dav/"):
            return webdav.handle_head(self)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()

    def do_PUT(self):
        if self.path.startswith("/dav/"):
            return webdav.handle_put(self)
        self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        if self.path.startswith("/dav/"):
            return webdav.handle_delete(self)
        self._json({"error": "not found"}, 404)

    def do_MKCOL(self):
        if self.path.startswith("/dav/"):
            return webdav.handle_mkcol(self)
        self._json({"error": "not found"}, 404)

    def do_MOVE(self):
        if self.path.startswith("/dav/"):
            return webdav.handle_move(self)
        self._json({"error": "not found"}, 404)

    def do_LOCK(self):
        if self.path.startswith("/dav/"):
            return webdav.handle_lock(self)
        self._json({"error": "not found"}, 404)

    def do_UNLOCK(self):
        if self.path.startswith("/dav/"):
            return webdav.handle_unlock(self)
        self._json({"error": "not found"}, 404)

    def handle_cas(self, rel_path):
        """CAS→Strm 播放网关：/cas/<139相对路径>/<xxx.cas> -> 读.cas -> 秒传恢复 -> 302 直链。
        播放器（网易爆米花/飞牛影视）读 .strm 里的 URL 命中这里，直连 139 直链播放，不耗 casgen 带宽。"""
        if CLIENT is None:
            self._json({"error": "未登录 139 云盘，请先在网页登录"}, 401)
            return
        rel_path = urllib.parse.unquote(rel_path)
        try:
            cas_fid = CLIENT.resolve_path_readonly(rel_path)
            cas_name = rel_path.rsplit("/", 1)[-1]
            url, temp_fid = CLIENT.cas_get_play_link(cas_fid, cas_name)
        except TokenExpired:
            global AUTH_EXPIRED
            AUTH_EXPIRED = True
            self._json({"error": "登录已失效，请重新登录"}, 401)
            return
        except FileNotFoundError as e:
            self._json({"error": "未找到: %s" % e}, 404)
            return
        except Exception as e:
            self._json({"error": "CAS 播放失败: %s" % e}, 500)
            return
        # 302 重定向到 139 直链（播放器直连，不耗 casgen 带宽）
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()
        # 异步延迟删除临时恢复文件，保住「省空间」核心价值
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
            self._json({"ok": False, "needLogin": True, "error": "登录已失效，请重新登录"})

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
                    return self._json({"ok": False, "error": "请填写 Authorization 头"}, 400)
                try:
                    c = Yun139(auth)
                    items, _, data = c.list_dir("root")  # 用列根目录验证账号有效
                    CLIENT = c
                    PROVIDER = "139"
                    AUTH_EXPIRED = False
                    _apply_msisdn(auth)
                    monitor.mark_logged_in()  # 用户主动登录成功 → 允许调度器开始扫描
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
                    return self._json({"ok": False, "error": "登录/列举失败：" + str(e)}, 400)
            elif prov == "189":
                return self._json({"ok": False, "error": "天翼(189)模块暂未完成，请先用移动(139)"}, 400)
            return self._json({"ok": False, "error": "未知网盘"}, 400)

        if CLIENT is None:
            return self._json({"ok": False, "error": "请先登录"}, 400)

        if AUTH_EXPIRED:
            return self._json({"ok": False, "needLogin": True, "error": "登录已失效，请重新登录"}, 400)

        if action == "list":
            parent = p.get("parent", "root")
            try:
                items, _, data = CLIENT.list_dir(parent)
            except Exception as e:
                import traceback; traceback.print_exc()
                return self._json({"ok": False, "error": f"获取目录列表失败：{str(e)}"}, 500)
            fmt = [self._fmt(i) for i in items]
            # 透传 139 原始 fileId，防止 _fmt 归一化在个别字段名下丢失，
            # 前端点击展开时用 it.fileId || it._rawId 兜底，避免 parent 为空退化成根目录。
            for raw, f in zip(items, fmt):
                f["_rawId"] = (raw.get("fileId") or raw.get("contentID") or raw.get("id")
                               or raw.get("catalogId") or raw.get("fid") or "")
            return self._json({"ok": True, "items": fmt, "raw": data})

        # ---------- 目录选择器：在指定目录下新建子目录 ----------
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
        # 前端 strm.html 发送 action:"strm_create"，兼容旧版 "generate_strm"
        if action in ("generate_strm", "strm_create"):
            root = p.get("root", "root") or "root"
            root_path = (p.get("rootPath") or "").strip()
            # 旧 .strm 自动清理：勾选后删除同名旧 .strm 再重新生成（v5.0）
            clean_old = bool(p.get("clean_old", False))
            public_url = (p.get("publicUrl") or os.environ.get("CASGEN_PUBLIC_URL", "")).strip()
            try:
                results = CLIENT.generate_strm(root, public_url, path_prefix=root_path, clean_old=clean_old)
                ok_n = sum(1 for r in results if r.get("status") == "uploaded")
                return self._json({"ok": True, "count": len(results),
                                   "uploaded": ok_n, "results": results,
                                   "publicUrl": public_url})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)

        # ---------- [4.0] L1 本地正则重命名 ----------
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
                return self._json({"ok": False, "needLogin": True, "error": "登录已失效，请重新登录"})
            text = (p.get("text") or "").strip()
            link_id, pwd = parse_share_input(text)
            if not link_id:
                return self._json({"ok": False,
                                    "error": "未能从输入中解析出分享链接ID（形如 https://yun.139.com/shareweb/#/w/i/xxxx）"})
            try:
                info = get_share_info(link_id, pwd, phone=MSISDN, token=CLIENT.token)
                files = list_all_share_files(link_id, pwd, phone=MSISDN, token=CLIENT.token, video_only=True)
            except ShareError as e:
                return self._json({"ok": False, "error": e.message, "fatal": e.fatal, "code": e.api_code})
            except Exception as e:
                return self._json({"ok": False, "error": "解析分享失败：" + str(e)})
            return self._json({
                "ok": True, "linkID": link_id, "needPwd": bool(pwd),
                "linkName": (info or {}).get("lkName", ""),
                "expireTime": (info or {}).get("expireTime", ""),
                "files": files, "count": len(files),
            })

        if action == "share_save":
            if CLIENT is None:
                return self._json({"ok": False, "needLogin": True, "error": "请先登录"})
            if AUTH_EXPIRED:
                return self._json({"ok": False, "needLogin": True, "error": "登录已失效，请重新登录"})
            text = (p.get("text") or "").strip()
            target = (p.get("target") or "root").strip() or "root"
            auto_cas = bool(p.get("auto_cas", False))
            # 删除原视频仅在「自动生成 CAS」勾选时才有意义，否则忽略
            delete_source = bool(p.get("delete_source", False)) and auto_cas
            # [3.0+] 勾选「转存后加入监控」：把该分享链接自动列入定时监控，
            # 后续新增剧集自动转 CAS 并删原视频（详见 monitor.scan_one）。
            add_to_monitor = bool(p.get("add_to_monitor", False))
            interval = max(monitor_store.MIN_INTERVAL,
                           int(p.get("interval_min") or monitor_store.MIN_INTERVAL))
            link_id, pwd = parse_share_input(text)
            if not link_id:
                return self._json({"ok": False, "error": "未能解析出分享链接ID"})
            try:
                target_catalog = CLIENT.resolve_folder(target)
                # 整文件夹转存：取分享根目录的顶层目录与文件，
                # 顶层目录走 ca_path_lst（139 会连带子目录树+所有类型文件一起存，保留原结构），
                # 顶层零散文件走 co_path_lst。这样整部《凡人修仙传》文件夹原样落到目标目录。
                dirs, files = list_share_root_items(link_id, pwd, phone=MSISDN, token=CLIENT.token)
                ca_paths = [f"{d['parentCatalogID']}/{d['catalogID']}" for d in dirs]
                co_paths = [f"{f['parentCatalogID']}/{f['contentID']}" for f in files]
                if not ca_paths and not co_paths:
                    return self._json({"ok": False, "error": "该分享内没有任何可转存的内容"})
                res = save_share_files(link_id, co_paths, ca_paths, target_catalog,
                                       need_password=bool(pwd), phone=MSISDN,
                                       token=CLIENT.token)
                saved = len(ca_paths) + len(co_paths)
                # 仅供前端展示：统计分享内视频总数
                try:
                    all_videos = list_all_share_files(link_id, pwd, phone=MSISDN, token=CLIENT.token, video_only=True)
                    video_count = len(all_videos)
                except Exception:
                    video_count = None
                cas_results = None
                # [2.0+] 勾选「自动生成 CAS」则转存后直接对该目录生成 CAS，形成一条龙
                if auto_cas:
                    cas_results = CLIENT.generate(target_catalog, delete_source=delete_source)
                # [3.0+] 勾选「转存后加入监控」则建立监控基线；新增剧集自动转 CAS + 删原视频
                monitor_info = None
                if add_to_monitor:
                    try:
                        mon = monitor.add_and_baseline(link_id, pwd, target,
                                                       auto_cas=True, delete_source=True,
                                                       interval=interval)
                        monitor_info = {"id": mon["id"], "status": mon.get("status"),
                                        "lastResult": mon.get("last_result"),
                                        "intervalMin": mon.get("interval_min")}
                    except Exception as e:
                        # 转存已成功，监控登记失败不应掩盖成功结果
                        monitor_info = {"error": "已转存，但加入监控失败：" + str(e)}
            except ShareError as e:
                return self._json({"ok": False, "error": e.message, "fatal": e.fatal, "code": e.api_code})
            except Exception as e:
                return self._json({"ok": False, "error": "转存失败：" + str(e)})
            return self._json({"ok": True, "targetCatalog": target_catalog,
                               "saved": saved, "videoCount": video_count, "result": res,
                               "autoCas": auto_cas, "casResults": cas_results,
                               "addToMonitor": add_to_monitor, "monitor": monitor_info})

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
            interval = max(monitor_store.MIN_INTERVAL,
                           int(p.get("interval_min") or monitor_store.MIN_INTERVAL))
            link_id, pwd = parse_share_input(text)
            if not link_id:
                return self._json({"ok": False, "error": "未能解析出分享链接ID"})
            if CLIENT is None:
                return self._json({"ok": False, "needLogin": True, "error": "请先登录"})
            if AUTH_EXPIRED:
                return self._json({"ok": False, "needLogin": True, "error": "登录已失效，请重新登录"})
            try:
                mon = monitor.add_and_baseline(link_id, pwd, target, auto_cas, delete_source, interval)
            except ShareError as e:
                return self._json({"ok": False, "error": "链接验证失败：" + e.message, "fatal": e.fatal})
            except TokenExpired:
                return self._json({"ok": False, "needLogin": True, "error": "登录已失效"})
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
                return self._json({"ok": False, "needLogin": True, "error": "登录已失效，请重新登录"})
            res = monitor.scan_one(m)
            return self._json({"ok": True, **res})

        return self._json({"ok": False, "error": "未知操作"}, 400)

    @staticmethod
    def _fmt(i):
        if not isinstance(i, dict):
            i = {}
        # ID：兼容 139 个人云多种返回字段名（personal / personal_new / 旧版 contentID 等）
        fid = (i.get("fileId") or i.get("contentID") or i.get("id")
               or i.get("catalogId") or i.get("fid") or i.get("fileIdStr") or "")
        # 文件夹判断：type=="folder"/"dir"，或 fileType/contentType 为数字 1（1=文件夹, 2=文件），
        # 或显式 isFolder 标记。
        t = i.get("type") or i.get("fileType") or i.get("contentType") or ""
        is_folder = False
        if isinstance(t, str):
            is_folder = t.strip().lower() in ("folder", "dir")
        elif isinstance(t, (int, float)):
            is_folder = t == 1  # 1=文件夹, 2=文件
        if not is_folder and i.get("isFolder") is True:
            is_folder = True
        return {
            "fileId": fid,
            "name": i.get("name") or i.get("fileName") or i.get("contentName") or "",
            "type": "folder" if is_folder else "file",
            "size": i.get("size") or i.get("fileSize") or i.get("contentSize") or 0,
        }


def main():
    global CLIENT, PROVIDER, AUTH_EXPIRED
    # 每次启动时清空旧调试日志，避免多轮测试内容混在一起
    try:
        open(DEBUG_LOG, "w", encoding="utf-8").close()
    except Exception:
        pass
    # ⚠️ 关键修复：HTTP 服务必须先启动并立即监听，绝不能被启动期网络调用阻塞。
    # 旧逻辑（3.0+）在启动服务"之前"就调用 list_dir("root") 恢复登录态，
    # 一旦部署环境（如飞牛 ARM 虚拟机）到 139 的网络慢/抖动，这次网络调用会卡住整个进程，
    # 导致 HTTP 服务迟迟不监听 —— 用户点登录时请求一直排队，前端永远显示"登录中…"。
    # 1.0/2.0 没有这段启动期网络调用，所以登录一直正常。
    # 修复：先起服务并立即可用，登录态恢复改到后台线程异步做；调度器先启动但暂不扫描，
    #       等首次登录成功后才开始，避免后台 139 流量与登录抢资源。
    port = int(os.environ.get("PORT", "5000"))
    # 启动期配置校验：端口合法、报告关键环境变量状态
    if not (1 <= port <= 65535):
        print(f"[FATAL] PORT={port} 非法（合法范围 1-65535）", file=sys.stderr)
        sys.exit(2)
    public_url = os.environ.get("CASGEN_PUBLIC_URL", "").strip()
    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    monitor.bind(sys.modules[__name__])
    webdav.bind(lambda: CLIENT, lambda: AUTH_EXPIRED)
    monitor.start_scheduler()  # 守护线程：先空转，登录成功后才真正扫描

    def _restore_auth():
        """后台线程：异步恢复持久化登录态，绝不阻塞 HTTP 服务启动。"""
        try:
            auth = _load_auth()
            if not auth or not auth.get("token"):
                return
            c = Yun139(auth["token"])
            try:
                c.list_dir("root")  # 轻量校验 token 是否仍有效
                AUTH_EXPIRED = False
            except TokenExpired:
                # token 已过期：不挂 CLIENT，标记失效让用户主动重登
                AUTH_EXPIRED = True
                return
            except Exception:
                # 其他异常（如 139 临时网络抖动）：不轻易放弃恢复，
                # 仍挂上 CLIENT，让后续真实接口调用去检测/触发重登
                AUTH_EXPIRED = False
            global CLIENT, PROVIDER
            CLIENT = c
            PROVIDER = auth.get("provider", "139")
            _apply_msisdn(auth.get("token", ""))
            monitor.mark_logged_in()  # 恢复成功 → 允许调度器开始扫描
        except Exception:
            pass

    threading.Thread(target=_restore_auth, daemon=True).start()

    print("=" * 50)
    print(f"  CAS 转换服务已启动  v{VERSION}")
    print(f"  本机浏览器打开： http://localhost:{port}")
    print(f"  其他设备打开：   http://<本机IP>:{port}")
    print(f"  健康检查：       http://localhost:{port}/api/health")
    if webdav.is_enabled():
        print(f"  WebDAV 已启用：  http://<本机IP>:{port}/dav/")
        print(f"  WebDAV 用户：    {webdav.WEBDAV_USER}")
        print(f"  WebDAV 根目录：  {webdav.WEBDAV_ROOT}")
    else:
        print("  WebDAV：        未启用（设置 CASGEN_WEBDAV_USER / CASGEN_WEBDAV_PASS 开启）")
    if public_url:
        print(f"  CAS 网关公网：   {public_url}/cas/<路径>")
    else:
        print("  ⚠️  CASGEN_PUBLIC_URL 未配置：CAS→Strm 功能将无法生成可用的 .strm")
        print("     部署时建议设置环境变量：CASGEN_PUBLIC_URL=http://<你的IP>:" + str(port))
    print("按 Ctrl+C 停止")
    print("=" * 50)
    sys.stdout.flush()

    # 优雅停机：SIGTERM/SIGINT → 停调度器 → 关服务（Docker stop / Ctrl+C 都走这里）
    _stop_event = threading.Event()

    def _shutdown(signum, frame):
        sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        print(f"\n[{sig_name}] 收到停止信号，正在优雅停机…", flush=True)
        try:
            monitor.stop_scheduler()  # 停监控线程（如果暴露了）
        except Exception:
            pass
        # 通知 serve_forever 退出
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    try:
        signal.signal(signal.SIGINT, _shutdown)
    except Exception:
        pass

    try:
        srv.serve_forever()
    finally:
        print("HTTP 服务已停止，再见 👋", flush=True)


if __name__ == "__main__":
    main()
