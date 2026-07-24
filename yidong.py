"""移动139 云盘模块 - 纯标准库实现（无需 pip 安装任何依赖）。

⚠️ 已对照 OpenList 官方 139 驱动源码 + 52pojie 逆向帖修正：
   - 真实 API 主机是【动态的】，必须先调路由策略接口
     POST https://user-njs.yun.139.com/user/route/qryRoutePolicy
     取回 personal 模块的 httpsUrl，再用作 /file/* 的 host。
     （之前写死 yun.139.com 是网页门户，所以返回整页 HTML。）
   - 每个请求都要带 mcloud-sign 签名，算法见 cal_sign()。
   - 列举文件：POST {host}/file/list  （body 见 list_dir）
   - 文件夹判断：item["type"] == "folder"
   - 文件字段：fileId / name / size / type
   - 上传 cas / 恢复(秒传) / 删除 接口已按 OpenList 驱动实现，但【尚未真机实测】，
     需用户试跑反馈后再微调（见文件末尾说明）。
"""

import base64
import hashlib
import json
import os
import random
import string
import time
import urllib.parse
import urllib.request
import urllib.error

# 视频扩展名白名单：generate 只转这些，避免误转图片/文本，并根治 .cas 后缀叠加 bug
VIDEO_EXT = {
    ".mp4", ".mkv", ".ts", ".avi", ".mov", ".flv", ".wmv",
    ".rmvb", ".mpg", ".mpeg", ".m4v", ".webm", ".3gp", ".vob",
}

# 版本号：每次重打包都在这里改，方便核对是否用上了最新修复
# 4.9.0：新增 WebDAV 服务（/dav/），播放器可直接挂载 .strm 文件
__version__ = "5.0.0"


# ============================ 签名相关（来自 52pojie 逆向） ============================
def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _encode_uri_component(s: str) -> str:
    """等价于 JavaScript 的 encodeURIComponent（与 Go 版 url.QueryEscape+替换 行为一致）。"""
    # Go 的 url.QueryEscape 只保留 A-Za-z0-9 -_.~ 不转义，空格->'+'；
    # 这里用 quote(safe='-_.!~*'()') 保留同样字符集，空格等转 %XX。
    r = urllib.parse.quote(s, safe="-_.!~*'()")
    return r


def cal_sign(body: str, ts: str, rand_str: str) -> str:
    """移动云盘 mcloud-sign 签名算法（与官方一致）。

    sign = Upper( MD5( MD5(Base64(sort(encodeURIComponent(body)))) + MD5(ts + ":" + randStr) ) )
    """
    body = _encode_uri_component(body)
    chars = sorted(list(body))          # Go sort.Strings：按字节/字符升序
    body = "".join(chars)
    body = base64.b64encode(body.encode("utf-8")).decode("ascii")
    res = _md5(body) + _md5(ts + ":" + rand_str)
    return _md5(res).upper()


_RAND_CHARS = string.ascii_letters + string.digits


def _rand_str(n=16) -> str:
    return "".join(random.choice(_RAND_CHARS) for _ in range(n))


# ============================ 登录失效检测 ============================
class TokenExpired(Exception):
    """139 登录态失效（token 过期/被踢/异地登录），需提示用户重新登录。"""


# 已知失效错误码（取自 share139.FATAL_CODES 中与认证相关的项）
_AUTH_FAIL_CODES = {"05050006", "04000005"}
# 失效关键词兜底：接口返回的 message/desc 里出现这些即视为失效，
# 覆盖「码未知但语义明确是登录失效」的情况，越稳妥越好。
_AUTH_FAIL_KW = ("token", "失效", "过期", "未登录", "重新登录", "重新登陆",
                "登录已", "未授权", "unauthorized", "please login",
                "auth fail", "auth expired", "login expired")


def _detect_auth_failure(data):
    """识别 139 个人云接口的登录失效响应，命中则抛 TokenExpired。
    网络层错误（_error / _raw）不属于认证失效，不在此处理，交由调用方判断。
    """
    if not isinstance(data, dict):
        return
    if data.get("_error") is not None or data.get("_raw") is not None:
        return
    code = str(data.get("code") or data.get("errorCode") or data.get("resultCode") or "")
    msg = str(data.get("message") or data.get("desc") or data.get("errorMsg")
              or data.get("resultDesc") or "")
    if code in _AUTH_FAIL_CODES:
        raise TokenExpired("登录已失效（code=%s）：%s" % (code, msg))
    low = msg.lower()
    if any(kw in low for kw in _AUTH_FAIL_KW):
        raise TokenExpired("登录已失效：%s" % msg)
    if data.get("success") is False and any(kw in low for kw in _AUTH_FAIL_KW):
        raise TokenExpired("登录已失效：%s" % msg)


# ============================ 139 云盘客户端 ============================
class Yun139:
    # 路由策略接口（host 固定）
    ROUTE_HOST = "https://user-njs.yun.139.com"
    ROUTE_PATH = "/user/route/qryRoutePolicy"

    def __init__(self, authorization: str):
        # 用户粘贴的可能是 "Basic xxxx" 或纯 "xxxx"，统一剥离 Basic 前缀，存纯 base64
        auth = (authorization or "").strip()
        if auth.lower().startswith("basic "):
            auth = auth[6:].strip()
        self.token = auth
        self.host = None          # 真实个人云 host，调路由策略后填入
        self.all_modules = []     # 路由策略返回的全部模块 (modName, httpsUrl)
        self.prefer_module = None # 可选：强制使用的模块名（如 "personal_new"）
        self.account = self._decode_account()

        # 公共请求头（大小写无所谓，值是关键）
        self.common_headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "Authorization": "Basic " + self.token,
            "Cms-Device": "default",
            "Mcloud-Channel": "1000101",
            "Mcloud-Client": "10701",
            "Mcloud-Version": "7.14.0",
            "x-DeviceInfo": "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||",
            "x-huawei-channelSrc": "10000034",
            "x-inner-ntwk": "2",
            "x-m4c-caller": "PC",
            "x-m4c-src": "10002",
            "x-SvcType": "1",
        }
        # 路由策略接口专用头
        self.route_headers = dict(self.common_headers)
        self.route_headers.update({
            "mcloud-client": "10701",
            "Origin": "https://yun.139.com",
            "Referer": "https://yun.139.com/w/",
            "x-inner-ntwk": "2",
            "Inner-Hcy-Router-Https": "1",
        })
        # 个人云 /file/* 接口专用头
        self.personal_headers = dict(self.common_headers)
        self.personal_headers.update({
            "Caller": "web",
            "Mcloud-Route": "001",
            "X-Yun-Api-Version": "v1",
            "X-Yun-App-Channel": "10000034",
            "X-Yun-Channel-Source": "10000034",
            "X-Yun-Client-Info": "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||dW5kZWZpbmVk||",
            "X-Yun-Module-Type": "100",
            "X-Yun-Svc-Type": "1",
        })

    # ---------- 账号解析（用于路由策略的 accountName） ----------
    def _decode_account(self):
        try:
            raw = base64.b64decode(self.token + "==="[:(-len(self.token)) % 4])
            parts = raw.decode("utf-8", "replace").split(":")
            if len(parts) >= 3:
                return parts[1]          # 格式通常为 A:B(账号):C|D|E|F
        except Exception:
            pass
        return ""

    # ---------- 底层：带签名的请求 ----------
    def _sign_headers(self, base_headers, body_str):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        rnd = _rand_str(16)
        sign = cal_sign(body_str, ts, rnd)
        h = dict(base_headers)
        h["mcloud-sign"] = f"{ts},{rnd},{sign}"
        return h

    def _post_json(self, url, base_headers, body, timeout=20, retries=2, backoff=0.6):
        """发送 JSON POST，返回解析后的 dict；非 JSON 时返回 {'_raw': 文本}。
        timeout 默认 20s：139 接口正常 <2s 返回，弱网/容器环境也应在 20s 内响应，
        避免登录等请求无限挂起（否则前端一直显示"登录中…"）。

        retries/退避：连接错误、超时、5xx 自动重试（指数退避 0.6s→1.2s→2.4s），
        避免飞牛 ARM 等弱网环境偶发抖动直接失败。仅对瞬态错误重试，
        4xx（参数错/认证失败）不重试，避免浪费配额/账号风控。"""
        body_str = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        headers = self._sign_headers(base_headers, body_str)
        data = body_str.encode("utf-8")
        attempt, wait = 0, backoff
        last_error = None
        while True:
            attempt += 1
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                return self._parse_response(raw)
            except urllib.error.HTTPError as e:
                # 5xx 服务端错误 → 重试；4xx 客户端错误 → 不重试（参数/认证问题重试也没用）
                code = e.code
                try:
                    raw = e.read().decode("utf-8", "replace")
                except Exception:
                    raw = ""
                if 500 <= code <= 599 and attempt <= retries:
                    last_error = (code, raw[:200])
                    time.sleep(wait); wait *= 2; continue
                # 非 5xx 或已用尽重试：尝试解析为业务 JSON 返回
                parsed = self._parse_response(raw)
                if isinstance(parsed, dict):
                    return parsed
                return {"_error": "HTTP %d" % code, "_raw": raw[:2000]}
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                # 网络瞬态错误：超时、连接重置、DNS 等 → 重试
                last_error = str(e)
                if attempt > retries:
                    return {"_error": "网络错误（已重试 %d 次）: %s" % (retries, last_error)}
                time.sleep(wait); wait *= 2; continue
            except Exception as e:
                # 其他未知异常 → 不重试
                return {"_error": str(e)}

    @staticmethod
    def _parse_response(raw):
        try:
            return json.loads(raw)
        except Exception:
            return {"_raw": raw[:2000]}

    # ---------- 路由策略：拿真实 host ----------
    def request_route_policy(self):
        body = {
            "userInfo": {
                "userType": 1,
                "accountType": 1,
                "accountName": self.account,
            },
            "modAddrType": 1,
        }
        resp = self._post_json(self.ROUTE_HOST + self.ROUTE_PATH, self.route_headers, body)
        if isinstance(resp, dict) and resp.get("_raw") is not None:
            raise RuntimeError("路由策略返回非JSON(host=%s): %s" % (self.ROUTE_HOST, str(resp["_raw"])[:800]))
        if isinstance(resp, dict) and resp.get("_error") is not None:
            raise RuntimeError("路由策略请求出错(host=%s): %s" % (self.ROUTE_HOST, str(resp["_error"])[:800]))
        # 防御：data 可能为 null（接口常返回 {"data": null, "errorCode":..., "errorMsg":...}）
        data = resp.get("data") if isinstance(resp, dict) else None
        if not isinstance(data, dict):
            raise RuntimeError("路由策略返回异常(data 非对象, host=%s): %s"
                               % (self.ROUTE_HOST, json.dumps(resp, ensure_ascii=False)[:800]))
        lst = data.get("routePolicyList") or []
        self.all_modules = [(p.get("modName"), p.get("httpsUrl")) for p in lst]
        if not lst:
            raise RuntimeError("路由策略未返回任何模块(host=%s): %s"
                               % (self.ROUTE_HOST, json.dumps(resp, ensure_ascii=False)[:800]))
        # 选择模块：prefer_module 优先 > 精确 personal > 包含 personal
        host = None
        pref = getattr(self, "prefer_module", None)
        if pref:
            for p in lst:
                if p.get("modName") == pref:
                    host = p.get("httpsUrl"); break
        if not host:
            for p in lst:
                if p.get("modName") == "personal":
                    host = p.get("httpsUrl"); break
        if not host:
            for p in lst:
                if "personal" in (p.get("modName") or "").lower():
                    host = p.get("httpsUrl"); break
        if not host:
            # 没匹配到 personal，把全部模块列出来让用户看
            raise RuntimeError("路由策略里没有 personal 模块，全部模块=" + json.dumps(self.all_modules, ensure_ascii=False)[:600])
        self.host = host
        return {"host": self.host}

    def _ensure_host(self):
        if not self.host:
            r = self.request_route_policy()
            if not self.host:
                raise RuntimeError("无法获取个人云 host：" + json.dumps(r, ensure_ascii=False)[:300])
        return self.host

    # ---------- 个人云请求封装 ----------
    def personal_post(self, path, body):
        host = self._ensure_host()
        data = self._post_json(host + path, self.personal_headers, body)
        # 统一在个人云层识别登录失效，命中即抛 TokenExpired，
        # 上层（app.py 路由 / 监控线程）捕获后提示重登并暂停。
        _detect_auth_failure(data)
        return data

    # ---------- 字段归一化（兼容多种命名） ----------
    @staticmethod
    def _is_folder(it):
        """判断是否文件夹，兼容字符串和数字类型。
        139 API 数字类型：1=文件夹, 2=文件；字符串类型：folder/dir。"""
        t = it.get("type") or it.get("fileType") or it.get("contentType") or ""
        if isinstance(t, str):
            return t.strip().lower() in ("folder", "dir")
        if isinstance(t, (int, float)):
            return t == 1  # 1=文件夹, 2=文件
        if it.get("isFolder") is True:
            return True
        return False

    @staticmethod
    def _name(it):
        return it.get("name") or it.get("fileName") or it.get("contentName") or ""

    @staticmethod
    def _fid(it):
        return it.get("fileId") or it.get("contentID") or it.get("id") or ""

    @staticmethod
    def _size(it):
        return it.get("size") or it.get("fileSize") or it.get("contentSize") or 0

    # ---------- 目录遍历（POST {host}/file/list） ----------
    def list_dir(self, parent, cursor=None):
        if not parent or parent in ("root", "/"):
            parent = "/"           # 个人云根目录 parentFileId 用 "/"
        body = {
            "imageThumbnailStyleList": ["Small", "Large"],
            "orderBy": "updated_at",
            "orderDirection": "DESC",
            "pageInfo": {"pageCursor": cursor or "", "pageSize": 100},
            "parentFileId": parent,
        }
        data = self.personal_post("/file/list", body)
        # 非 JSON / 请求异常：直接抛出来看原文
        if isinstance(data, dict) and data.get("_raw") is not None:
            raise RuntimeError("列出文件返回非JSON(host=%s): %s" % (self.host, str(data["_raw"])[:600]))
        if isinstance(data, dict) and data.get("_error") is not None:
            raise RuntimeError("列出文件请求出错(host=%s): %s" % (self.host, str(data["_error"])[:600]))
        # 防御：data 可能为 null
        d = data.get("data") if isinstance(data, dict) else None
        if not isinstance(d, dict):
            d = {}
        items = (d.get("items") or d.get("Items")
                 or (data.get("items") if isinstance(data, dict) else None)
                 or (data.get("Items") if isinstance(data, dict) else None) or [])
        nxt = (d.get("nextPageCursor")
               or (data.get("nextPageCursor") if isinstance(data, dict) else None) or None)
        # 若 139 明确返回失败，把原文抛出来便于诊断
        if isinstance(data, dict) and data.get("success") is False:
            msg = (data.get("message") or data.get("resultDesc") or data.get("desc")
                   or data.get("code") or "")
            raise RuntimeError("列出文件被拒绝(host=%s, msg=%s) 原文=%s"
                               % (self.host, msg, json.dumps(data, ensure_ascii=False)[:600]))
        return items, nxt, data

    def iter_all(self, root, max_depth=20):
        stack = [(root, 0)]
        while stack:
            parent, depth = stack.pop()
            if depth > max_depth:
                continue
            cursor = None
            while True:
                items, cursor, _ = self.list_dir(parent, cursor)
                if not items:
                    break
                for it in items:
                    # 把"当前所在文件夹的 fileId"注入给每个文件，作为 .cas 的父目录。
                    # 这是真正的文件夹 ID（folder fileId），file/create 的 parentFileId 必须是它，
                    # 不能错用文件自己的 fileId，否则 139 报 00010002 请求参数不合法。
                    rec = dict(it)
                    rec["_parent"] = parent
                    yield rec
                    if self._is_folder(it):
                        stack.append((self._fid(it), depth + 1))
                if not cursor:
                    break

    # ---------- 创建后回查：确认文件真的出现在列表里 ----------
    def _verify_file(self, parent, file_id):
        """创建文件后，立即重新列举该目录，确认 fileId 是否真的出现。
        用于发现『接口返回成功但文件不显示』的空间/路径错位问题。"""
        if not file_id:
            return False
        try:
            items, _, _ = self.list_dir(parent)
        except Exception:
            return "verify_error"
        for it in items:
            if self._fid(it) == file_id:
                return True
        return False

    # ---------- CAS 核心：哈希与种子 ----------
    @staticmethod
    def sha256_of(item):
        """从列表/详情里尽量取出 sha256。139 列表项未必带哈希，需真机确认。"""
        alg = (item.get("contentHashAlgorithm") or item.get("sha256Algorithm") or "").lower()
        h = item.get("contentHash") or item.get("sha256") or item.get("sha256Hash") or ""
        if alg == "sha256" and len(h) == 64:
            return h.lower()
        # 若字段直接叫 sha256 且长度 64
        if len(h) == 64 and all(c in "0123456789abcdef" for c in h.lower()):
            return h.lower()
        return None

    @staticmethod
    def build_cas(name, size, sha256, md5="", parent_file_id=""):
        payload = {
            "provider": "139",
            "name": name,
            "size": size,
            "md5": md5,
            "sliceMd5": md5 or "",
            "sha1": "",
            "preID": "",
            "sha256": sha256,
            "parentFileId": parent_file_id,
            "create_time": str(int(time.time())),
        }
        return base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()

    # ---------- 通用文件上传（三步：file/create → PUT → /file/complete） ----------
    def upload_file(self, name, content, parent, content_type="application/octet-stream"):
        """通用二进制/文本文件上传到 139 云盘。

        content 可以是 str 或 bytes；content_type 默认 application/octet-stream。
        返回 dict，成功时含 _file_id，失败时含 _upload_error / _complete_error 等。
        """
        if isinstance(content, str):
            content = content.encode("utf-8")
        size = len(content)
        content_sha256 = hashlib.sha256(content).hexdigest()
        body = {
            "contentHash": content_sha256,
            "contentHashAlgorithm": "SHA256",
            "contentType": content_type,
            "parallelUpload": False,
            "partInfos": [{"partNumber": 1, "partSize": size}],
            "size": size,
            "parentFileId": parent if parent not in ("root", "/", "") else "/",
            "name": name,
            "type": "file",
            # 139 真机 /file/create 拒收 fileRenameMode 字段（任意取值均 04000002），不发送。
        }
        resp = self.personal_post("/file/create", body)
        if isinstance(resp, dict) and (resp.get("_raw") is not None or resp.get("_error") is not None):
            resp["_create_failed"] = True
            return resp
        d = resp.get("data") if isinstance(resp, dict) else None
        if not isinstance(d, dict):
            resp["_create_failed"] = True
            resp["_no_data"] = True
            return resp
        file_id = d.get("fileId") or resp.get("fileId")
        upload_id = d.get("uploadId") or resp.get("uploadId")
        exist = d.get("exist") or d.get("rapidUpload")
        if exist and file_id:
            resp["_file_id"] = file_id
            resp["_exist"] = True
            return resp
        part_infos = d.get("partInfos") or []
        upload_url = None
        for p in part_infos:
            upload_url = p.get("uploadUrl") or p.get("uploadurl")
            if upload_url:
                break
        if not upload_url:
            resp["_missing_upload_url"] = True
            return resp
        try:
            req = urllib.request.Request(upload_url, data=content, headers={
                "Content-Type": content_type,
                "Content-Length": str(size),
                "Origin": "https://yun.139.com",
                "Referer": "https://yun.139.com/",
            }, method="PUT")
            urllib.request.urlopen(req, timeout=120).read()
        except Exception as e:
            resp["_upload_error"] = str(e)
            resp["_file_id"] = file_id
            return resp
        if file_id and upload_id:
            try:
                comp = self.personal_post("/file/complete", {
                    "contentHash": content_sha256,
                    "contentHashAlgorithm": "SHA256",
                    "fileId": file_id,
                    "uploadId": upload_id,
                })
                resp["_complete"] = comp
            except Exception as e:
                resp["_complete_error"] = str(e)
        if file_id:
            try:
                resp["_verify_found"] = self._verify_file(parent, file_id)
            except Exception:
                resp["_verify_found"] = "verify_error"
        resp["_file_id"] = file_id
        resp["_upload_id"] = upload_id
        return resp

    def upload_cas(self, cas_name, content, parent):
        """上传 .cas 种子文件（委托 upload_file）。"""
        return self.upload_file(cas_name, content, parent, "application/octet-stream")

    def upload_text_file(self, name, text, parent):
        """上传纯文本文件（委托 upload_file）。"""
        return self.upload_file(name, text, parent, "text/plain")

    # ---------- 恢复（按 sha256 秒传，未实测） ----------
    def restore(self, sha256, size, name, parent):
        body = {
            "contentHash": sha256,
            "contentHashAlgorithm": "SHA256",
            "contentType": "application/octet-stream",
            "parallelUpload": False,
            "partInfos": [{"partNumber": 1, "partSize": size}],
            "size": size,
            "parentFileId": parent if parent not in ("root", "/", "") else "/",
            "name": name,
            "type": "file",
            "fileRenameMode": "auto_rename",
        }
        resp = self.personal_post("/file/create", body)
        d = resp.get("data") if isinstance(resp, dict) else None
        fid = resp.get("fileId") or (d.get("fileId") if isinstance(d, dict) else None)
        if fid:
            try:
                resp["_verify_found"] = self._verify_file(parent, fid)
            except Exception:
                resp["_verify_found"] = "verify_error"
        return resp

    # ---------- 删除（进回收站，未实测） ----------
    def delete(self, file_ids):
        if not file_ids:
            return {}
        return self.personal_post("/recyclebin/batchTrash", {"fileIds": file_ids})

    # ---------- 高层动作（给网页用） ----------
    def plan(self, root):
        ok, skip, sample = [], [], None
        for it in self.iter_all(root):
            if self._is_folder(it):
                continue
            sha = self.sha256_of(it)
            if sha:
                ok.append({"name": self._name(it), "size": self._size(it)})
            else:
                skip.append({"name": self._name(it)})
            if sample is None:
                sample = it        # 留一个原始样本，供前端展示真实字段
        return ok, skip, sample

    def generate(self, root, delete_source=False):
        """把原视频转成几 KB 的 .cas 文件，直接上传到 139 云盘的【原文件夹】里。
        .cas 只是一颗种子：含原文件的 sha256 哈希。恢复时靠这颗种子做秒传，把视频在云盘复活。
        上传走完整三步（file/create → PUT → /file/complete），确保 .cas 真实显示在云盘，供光鸭版 OpenList 挂载播放。
        delete_source=True 时采用「先删原视频释放空间、再上传 .cas」两步法：139 免费云盘配额有限，
        整部剧往往已撑满配额，若先传 .cas 再删视频，tiny .cas 也会因「资源配额不足(00010012)」失败；
        先删大视频腾出空间，几 KB 的 .cas 即可顺利上传。"""
        results = []
        to_delete = []
        records = []  # 待生成 .cas 的视频：(name, size, sha, parent, fid, contentHash)
        existing_cache = {}  # parent_fid -> {已存在的文件名}，避免重复生成 .cas（替代被 139 拒收的 overwrite）
        for it in self.iter_all(root):
            if self._is_folder(it):
                continue
            name = self._name(it)
            # [2.0 修复] 只转视频白名单，跳过 .cas（根治后缀叠加）/ 非视频
            ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
            if ext == ".cas":
                results.append({"name": name, "status": "skipped_cas"})
                continue
            if ext not in VIDEO_EXT:
                results.append({"name": name, "status": "skipped_non_video"})
                continue
            sha = self.sha256_of(it)
            if not sha:
                results.append({"name": name, "status": "skipped_no_sha256"})
                continue
            # 父目录必须是文件夹 fileId：优先用 iter_all 注入的 _parent（当前所在文件夹），
            # 再退而求其次用 139 列表项自带的 parentFileId / parentId；最后退回 root。
            # 注意：绝不能用 self._fid(it)（那是文件自己的 ID），否则 file/create 报 00010002。
            parent = it.get("_parent") or it.get("parentFileId") or it.get("parentId") or root
            # cas 文件名 = 原文件名.cas（贴合光鸭版 OpenList 约定：靠文件名/内容还原原文件）
            cas_name = name + ".cas"
            # 已存在同名 .cas 则跳过（139 真机 /file/create 拒收 fileRenameMode，故改用前置检查）
            cache = existing_cache.get(parent)
            if cache is None:
                try:
                    cit, _, _ = self.list_dir(parent)
                    cache = {self._name(x) for x in cit}
                except Exception:
                    cache = set()
                existing_cache[parent] = cache
            if cas_name in cache:
                results.append({"name": cas_name, "status": "skipped_existing"})
                continue
            ch = it.get("contentHash", "")
            records.append((name, self._size(it), sha, parent, self._fid(it), ch))
            if delete_source:
                to_delete.append(self._fid(it))
        # [配额友好] 勾选"删除原视频"时，先删大视频释放空间，再上传几 KB 的 .cas，
        # 避免 139 免费云盘配额被整部剧撑满后，连 .cas 都因「资源配额不足(00010012)」传不进去。
        # .cas 仅由 sha256+size 构成（无需原文件），删完视频仍可重建，安全可控。
        if delete_source and to_delete:
            # 分批删除（每批 100），避免 139 批量接口上限导致部分原视频残留、继续占用配额
            ok_del = 0
            for i in range(0, len(to_delete), 100):
                batch = to_delete[i:i + 100]
                try:
                    self.delete(batch)
                    ok_del += len(batch)
                except Exception:
                    pass
            results.append({"name": f"[已删除 {ok_del}/{len(to_delete)} 个原视频]", "status": "deleted"})
        # 上传 .cas（若已删原视频，此时空间已释放）
        for (name, size, sha, parent, _, ch) in records:
            cas_name = name + ".cas"
            content = self.build_cas(name, size, sha, ch, parent)
            try:
                up = self.upload_cas(cas_name, content, parent)
                fid = up.get("_file_id") or up.get("fileId") or (up.get("data") or {}).get("fileId")
                up_ok = bool(fid) and not up.get("_upload_error") and not up.get("_complete_error")
                status = "uploaded" if up_ok else "upload_failed"
                detail = {k: up.get(k) for k in (
                    "_upload_error", "_complete_error", "_missing_upload_url",
                    "_create_failed", "_no_data", "_exist"
                ) if up.get(k) is not None}
                err = (up.get("error") or up.get("_upload_error") or up.get("_complete_error")
                       or up.get("message") or (up.get("data") or {}).get("message")
                       or up.get("code"))
                results.append({
                    "name": cas_name,
                    "status": status,
                    "sha256": sha,
                    "size": size,
                    "parent": parent,
                    "fileId": fid,
                    "verify_found": up.get("_verify_found"),
                    "error": err,
                    "detail": detail,
                })
            except Exception as e:
                results.append({"name": cas_name, "status": "upload_failed", "error": str(e)})
        return results

    # ---------- [2.0] 分享转存：目标目录解析 ----------
    def find_or_create_folder(self, name, parent="root"):
        """在 parent 下找同名文件夹，没有就创建，返回其 catalogID（未实测，需真机验证）。"""
        items, _, _ = self.list_dir(parent)
        for it in items:
            if self._is_folder(it) and self._name(it) == name:
                return self._fid(it)
        body = {
            "parentFileId": parent if parent not in ("root", "/") else "/",
            "name": name,
            "type": "folder",
            "fileRenameMode": "force_rename",  # 139 真机仅接受 force_rename（overwrite 被拒）
        }
        d = self.personal_post("/file/create", body)
        dd = d.get("data") if isinstance(d, dict) else None
        return (dd or {}).get("fileId") or d.get("fileId")

    def resolve_folder(self, target):
        """把用户输入的目录名/路径（如 '分享转存' 或 '/电影/分享'）解析成 catalogID。
        空或 root 返回 'root'；多级路径逐级 find_or_create。"""
        target = (target or "").strip()
        if not target or target in ("root", "/"):
            return "root"
        cur = "root"
        for part in [t for t in target.split("/") if t]:
            cur = self.find_or_create_folder(part, cur)
        return cur

    # ======================== [4.0] CAS→Strm 播放网关支持 ========================
    # 对齐光鸭版 OpenList 139 驱动（drivers/139/cas.go 的 linkCASVideo）：
    #   读 .cas -> 139 秒传临时恢复 -> personalGetLink 拿直链 -> 异步删临时文件
    # 139 各接口按 OpenList 官方 139 驱动 + 52pojie 逆向实现，【真机未实测，待用户试跑微调】。
    CAS_TEMP_DIR = "TEMP"

    def ensure_temp_dir(self):
        """在根目录建/找 TEMP 文件夹，用于临时恢复 .cas 对应的真实文件（恢复后异步删除，保住省空间）。"""
        items, _, _ = self.list_dir("root")
        for it in items:
            if self._is_folder(it) and self._name(it) == self.CAS_TEMP_DIR:
                return self._fid(it)
        body = {"parentFileId": "/", "name": self.CAS_TEMP_DIR,
                "type": "folder", "fileRenameMode": "force_rename"}
        d = self.personal_post("/file/create", body)
        dd = d.get("data") if isinstance(d, dict) else None
        return (dd or {}).get("fileId") or d.get("fileId")

    def get_download_link(self, file_id):
        """对齐 OpenList 139 驱动 personalGetLink：POST /file/getDownloadUrl。
        返回可直接拉流的直链（播放器 302 直连，不耗 casgen 带宽）。
        优先返回 CDN 链接（cdnSwitch=true 时），否则返回普通链接。"""
        resp = self.personal_post("/file/getDownloadUrl", {"fileId": file_id})
        if isinstance(resp, dict):
            d = resp.get("data") or {}
            # CDN 链接优先（cdnSwitch=true 时 cdnUrl 可用）
            cdn_url = d.get("cdnUrl") or ""
            cdn_switch = d.get("cdnSwitch")
            if cdn_url and cdn_switch:
                return cdn_url
            # 回退到普通链接
            url = d.get("url") or d.get("downloadUrl") or d.get("download_url")
            if url:
                return url
        raise Exception("139 获取下载直链失败: %r" % (resp,))

    def read_file_text(self, file_id):
        """下载小文件（如 .cas 文本）内容。139 直链可能需 Referer，先带上报。"""
        url = self.get_download_link(file_id)
        req = urllib.request.Request(url, headers={
            "Referer": "https://yun.139.com/",
            "User-Agent": "Mozilla/5.0",
        })
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")

    def cas_get_play_link(self, cas_file_id, cas_name):
        """核心：给定 .cas 的 file_id + 文件名，返回 (直链URL, 临时恢复的file_id)。
        调用方拿到 URL 后 302 给播放器，并负责异步删除临时 file_id。"""
        text = self.read_file_text(cas_file_id)
        try:
            payload = json.loads(base64.b64decode(text.strip()))
        except Exception as e:
            raise Exception("解析 .cas 失败（不是合法的 casgen .cas）: %s" % e)
        sha256 = payload.get("sha256", "")
        size = payload.get("size", 0)
        name = payload.get("name") or (cas_name[:-4] if cas_name.endswith(".cas") else cas_name)
        if not sha256:
            raise Exception(".cas 缺少 sha256，无法秒传恢复")
        temp_dir = self.ensure_temp_dir()
        temp_name = "TEMP_%d_%s" % (int(time.time() * 1000), name)
        # 临时恢复（auto_rename 避免冲突；恢复的是云盘里的临时文件，稍后删除）
        resp = self.restore(sha256, size, temp_name, temp_dir)
        temp_fid = (resp.get("_file_id") or resp.get("fileId")
                    or (resp.get("data") or {}).get("fileId"))
        if not temp_fid:
            raise Exception("139 秒传恢复失败（云端去重池可能无此源）: %r" % (resp,))
        url = self.get_download_link(temp_fid)
        return url, temp_fid

    def resolve_path_readonly(self, path):
        """只读把 139 相对路径（如 电影/流浪地球2/流浪地球2.cas）解析为 file_id。不创建文件夹。"""
        path = (path or "").strip().strip("/")
        if not path:
            return "root"
        parts = [p for p in path.split("/") if p]
        cur = "root"
        for part in parts[:-1]:
            cur = self._find_folder_readonly(part, cur)
        return self._find_file_readonly(parts[-1], cur)

    def resolve_path(self, path, root="root"):
        """把 139 相对路径解析为 (fileId, is_folder, item)。

        与 resolve_path_readonly 的区别：
        1. 支持路径指向目录（末尾部分可以是文件夹名或文件名）
        2. 返回 item 原始数据，供 PROPFIND 提取元数据
        3. 可通过 root 参数指定解析起点（WebDAV 子树挂载用）

        返回: (fileId: str, is_folder: bool, item: dict | None)
        异常: FileNotFoundError 路径不存在
        """
        path = (path or "").strip().strip("/")
        cur = root if root not in ("/", "", "root") else "root"

        if not path:
            return cur, True, None

        parts = [p for p in path.split("/") if p]
        for i, part in enumerate(parts):
            is_last = (i == len(parts) - 1)
            items, _, _ = self.list_dir(cur)

            if is_last:
                # 末尾：先匹配文件夹，再匹配文件
                for it in items:
                    if self._is_folder(it) and self._name(it) == part:
                        return self._fid(it), True, it
                for it in items:
                    if not self._is_folder(it) and self._name(it) == part:
                        return self._fid(it), False, it
                raise FileNotFoundError("路径不存在: %s" % path)
            else:
                found = False
                for it in items:
                    if self._is_folder(it) and self._name(it) == part:
                        cur = self._fid(it)
                        found = True
                        break
                if not found:
                    raise FileNotFoundError("文件夹不存在: %s" % part)

        return cur, True, None

    def _find_folder_readonly(self, name, parent):
        items, _, _ = self.list_dir(parent)
        for it in items:
            if self._is_folder(it) and self._name(it) == name:
                return self._fid(it)
        raise FileNotFoundError("文件夹不存在: %s" % name)

    def _find_file_readonly(self, name, parent):
        items, _, _ = self.list_dir(parent)
        for it in items:
            if not self._is_folder(it) and self._name(it) == name:
                return self._fid(it)
        raise FileNotFoundError("文件不存在: %s" % name)

    # ======================== [4.0] .strm 生成器 ========================
    @staticmethod
    def _join_path(*parts):
        """拼接路径片段，忽略空片段。确保 .strm 链接包含从云盘根到文件的上层目录。"""
        return "/".join(p for p in parts if p)

    def walk(self, root, prefix="", path_prefix="", _parent=None):
        """递归遍历 root 下所有文件，yield (相对路径, item)。item 注入 _parent（父目录 fileId）。"""
        parent_id = _parent if _parent is not None else root
        cursor = None
        while True:
            items, cursor, _ = self.list_dir(root, cursor)
            if not items:
                break
            for it in items:
                name = self._name(it)
                rec = dict(it)
                rec["_parent"] = parent_id
                if self._is_folder(it):
                    sub = (prefix + "/" + name) if prefix else name
                    yield from self.walk(self._fid(it), sub, path_prefix, self._fid(it))
                else:
                    rel = (prefix + "/" + name) if prefix else name
                    yield self._join_path(path_prefix, rel), rec
            if not cursor:
                break

    def generate_strm(self, root, public_url, path_prefix="", clean_old=False, progress_cb=None):
        """遍历 root 下所有 .cas，为每个生成同名 .strm（内容指向 casgen 网关 URL），上传到同目录。
        public_url 即 CASGEN_PUBLIC_URL（casgen 网关对外地址）。
        progress_cb(dict): 可选进度回调，用于前端实时显示进度，dict 含 phase/scanned/cas_found/done/total/name 等。"""
        public_url = (public_url or "").rstrip("/")
        if root not in ("root", "/", "") and not path_prefix:
            raise Exception("无法获取完整目录路径（path_prefix 为空）。请在首页重新点「✔ 选定此文件夹」后再生成 .strm。")
        if not public_url:
            raise Exception("未配置 CASGEN_PUBLIC_URL（casgen 网关对外地址），无法生成 .strm")

        def _report(**kw):
            if progress_cb:
                try:
                    progress_cb(kw)
                except Exception:
                    pass

        # ---------- 阶段一：扫描目录树，收集所有 .cas 条目（不上传，仅统计） ----------
        cas_entries = []
        scanned = 0
        scan_error = None
        try:
            for rel, it in self.walk(root, path_prefix=path_prefix):
                scanned += 1
                if rel.lower().endswith(".cas"):
                    cas_entries.append((rel, it))
                if scanned % 25 == 0:
                    _report(phase="scan", scanned=scanned, cas_found=len(cas_entries))
        except Exception as e:
            # 遍历中途某个目录异常（个别目录报错 / 令牌过期）：记录后继续用已收集条目，
            # 不让整个任务中断，否则一个坏目录会导致全部 .strm 都没生成。
            scan_error = str(e)
            _report(phase="scan", scanned=scanned, cas_found=len(cas_entries),
                    note="scan interrupted: %s" % scan_error)
        total = len(cas_entries)
        _report(phase="scan", scanned=scanned, cas_found=total)
        _report(phase="gen", done=0, total=total)

        # ---------- 阶段二：逐个生成 .strm（上传，较慢，带进度） ----------
        results = []
        existing_cache = {}
        for idx, (rel, it) in enumerate(cas_entries, 1):
            cleaned = False
            cas_name = self._name(it)
            base = cas_name[:-4] if cas_name.endswith(".cas") else cas_name
            strm_name = base + ".strm"
            strm_url = "%s/cas/%s" % (public_url, rel)
            parent = it.get("_parent") or it.get("parentFileId") or it.get("parentId") or root
            if progress_cb:
                _report(phase="gen", done=idx - 1, total=total, name=strm_name)
            cache = existing_cache.get(parent)
            if cache is None:
                try:
                    cit, _, _ = self.list_dir(parent)
                    cache = {self._name(x): self._fid(x) for x in cit}
                except Exception:
                    cache = set()
                existing_cache[parent] = cache
            if strm_name in cache:
                if clean_old:
                    old_fid = cache.pop(strm_name, None)
                    if old_fid:
                        try:
                            self.delete([old_fid])
                            cleaned = True
                        except Exception as de:
                            results.append({"name": strm_name, "status": "clean_failed",
                                            "url": strm_url,
                                            "error": "删除旧 .strm 失败：%s（保留旧文件）" % de})
                            continue
                else:
                    results.append({"name": strm_name, "status": "skipped_existing", "url": strm_url})
                    continue
            try:
                up = self.upload_text_file(strm_name, strm_url, parent)
                fid = (up.get("_file_id") or up.get("fileId")
                       or (up.get("data") or {}).get("fileId"))
                ok = bool(fid) and not up.get("_upload_error") and not up.get("_complete_error")
                results.append({
                    "name": strm_name,
                    "status": ("recreated" if cleaned else "uploaded") if ok else "failed",
                    "url": strm_url,
                    "error": up.get("_upload_error") or up.get("_complete_error"),
                })
            except Exception as e:
                results.append({"name": strm_name, "status": "failed", "error": str(e)})
        # 遍历阶段若曾中断（个别目录异常 / 令牌过期），把被跳过的目录记入结果，便于用户感知
        if scan_error:
            results.append({"name": "（部分目录遍历失败，已跳过）", "status": "scan_failed",
                            "error": scan_error})
        _report(phase="done", done=total, total=total)
        return results

    # ======================== [4.0] L1 本地正则重命名 ========================
    def rename_file(self, file_id, new_name):
        """139 重命名（对齐光鸭版 OpenList 139 驱动 personal_new 分支）：
        POST /file/update {fileId, name, description:''}。只改显示名，不动内容。
        【真机未实测，响应字段待确认】。"""
        resp = self.personal_post("/file/update",
                                  {"fileId": file_id, "name": new_name, "description": ""})
        return resp
