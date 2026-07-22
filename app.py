"""CAS 转换网页服务 - 纯标准库（无需 pip / 无需 Docker）。

运行方式：
    python3 app.py
然后浏览器打开 http://localhost:5000  （若在 N1 盒子上跑，则用 http://N1的IP:5000）

所有功能都在网页里用鼠标点，不用碰命令行、不用改代码。
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from yidong import Yun139, __version__ as VERSION
# from tianyi import Tianyi189  # 天翼(189)模块暂未完成

CLIENT = None
PROVIDER = None
RECORDS = []  # 最近一次转换的记录，供"恢复播放"使用
ROOT = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(ROOT, "index.html")
DEBUG_LOG = os.path.join(ROOT, "casgen_debug.log")


def _log_debug(tag, obj):
    """把关键调试信息追加写入 casgen_debug.log，方便小白用户直接把文件发来分析。"""
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (tag, json.dumps(obj, ensure_ascii=False)))
    except Exception:
        pass


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
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            p = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            p = {}
        self.route(p)

    def route(self, p):
        global CLIENT, PROVIDER, RECORDS
        action = p.get("action")

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

        if action == "list":
            parent = p.get("parent", "root")
            items, _, data = CLIENT.list_dir(parent)
            return self._json({"ok": True, "items": [self._fmt(i) for i in items], "raw": data})

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

        return self._json({"ok": False, "error": "未知操作"}, 400)

    @staticmethod
    def _fmt(i):
        t = (i.get("type") or i.get("fileType") or i.get("contentType") or "").lower()
        is_folder = t in ("folder", "dir")
        return {
            "fileId": i.get("fileId") or i.get("contentID") or i.get("id"),
            "name": i.get("name") or i.get("fileName") or i.get("contentName") or "",
            "type": "folder" if is_folder else "file",
            "size": i.get("size") or i.get("fileSize") or i.get("contentSize") or 0,
        }


def main():
    # 每次启动时清空旧调试日志，避免多轮测试内容混在一起
    try:
        open(DEBUG_LOG, "w", encoding="utf-8").close()
    except Exception:
        pass
    port = int(os.environ.get("PORT", "5000"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    print("=" * 50)
    print("CAS 转换服务已启动")
    print(f"  本机浏览器打开： http://localhost:{port}")
    print(f"  其他设备打开：   http://<本机IP>:{port}")
    print("按 Ctrl+C 停止")
    print("=" * 50)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
