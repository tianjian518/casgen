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

# 版本号：每次重打包都在这里改，方便核对是否用上了最新修复
# 2026-07-22b：修复 file/create 的 parentFileId 错用文件自身 ID（00010002）的问题
__version__ = "2026-07-22b"


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

    def _post_json(self, url, base_headers, body):
        """发送 JSON POST，返回解析后的 dict；非 JSON 时返回 {'_raw': 文本}。"""
        body_str = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        headers = self._sign_headers(base_headers, body_str)
        data = body_str.encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
        except Exception as e:
            return {"_error": str(e)}
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
        return self._post_json(host + path, self.personal_headers, body)

    # ---------- 字段归一化（兼容多种命名） ----------
    @staticmethod
    def _is_folder(it):
        t = (it.get("type") or it.get("fileType") or it.get("contentType") or "").lower()
        return t in ("folder", "dir")

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

    # ---------- 上传 cas 小文件（已对齐光鸭版 OpenList 139 驱动：file/create → PUT → /file/complete） ----------
    def upload_cas(self, cas_name, content, parent):
        """把 .cas 种子文件真实上传到 139 云盘（三步，缺一不可）：
           1) POST /file/create 预上传，拿到 fileId / uploadId / partInfos[].uploadUrl
           2) PUT 内容到 uploadUrl
           3) POST /file/complete 提交完成 —— 139 必须走完这步文件才会真正显示在云盘
        """
        raw = content.encode("utf-8")
        size = len(raw)
        content_sha256 = hashlib.sha256(raw).hexdigest()
        body = {
            "contentHash": content_sha256,
            "contentHashAlgorithm": "SHA256",
            "contentType": "application/octet-stream",
            "parallelUpload": False,
            "partInfos": [{"partNumber": 1, "partSize": size, "parallelHashCtx": {"partOffset": 0}}],
            "size": size,
            "parentFileId": parent if parent not in ("root", "/", "") else "/",
            "name": cas_name,
            "type": "file",
            "fileRenameMode": "auto_rename",
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
            # 秒传命中（cas 是全新文件一般不会命中，保留兼容）
            resp["_file_id"] = file_id
            resp["_exist"] = True
            return resp
        # 真实上传：取分片上传地址
        part_infos = d.get("partInfos") or []
        upload_url = None
        for p in part_infos:
            upload_url = p.get("uploadUrl") or p.get("uploadUrl")
            if upload_url:
                break
        if not upload_url:
            resp["_missing_upload_url"] = True
            return resp
        # 2) PUT 内容到上传地址
        try:
            req = urllib.request.Request(upload_url, data=raw, headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(size),
                "Origin": "https://yun.139.com",
                "Referer": "https://yun.139.com/",
            }, method="PUT")
            urllib.request.urlopen(req, timeout=60).read()
        except Exception as e:
            resp["_upload_error"] = str(e)
            resp["_file_id"] = file_id
            return resp
        # 3) 完成上传 commit —— 139 必须这一步，文件才会真正显示
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
        # 回查确认文件真的出现（二次保险）
        if file_id:
            try:
                resp["_verify_found"] = self._verify_file(parent, file_id)
            except Exception:
                resp["_verify_found"] = "verify_error"
        resp["_file_id"] = file_id
        resp["_upload_id"] = upload_id
        return resp

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
        上传走完整三步（file/create → PUT → /file/complete），确保 .cas 真实显示在云盘，供光鸭版 OpenList 挂载播放。"""
        results = []
        to_delete = []
        for it in self.iter_all(root):
            if self._is_folder(it):
                continue
            sha = self.sha256_of(it)
            if not sha:
                results.append({"name": self._name(it), "status": "skipped_no_sha256"})
                continue
            name = self._name(it)
            # 父目录必须是文件夹 fileId：优先用 iter_all 注入的 _parent（当前所在文件夹），
            # 再退而求其次用 139 列表项自带的 parentFileId / parentId；最后退回 root。
            # 注意：绝不能用 self._fid(it)（那是文件自己的 ID），否则 file/create 报 00010002。
            parent = it.get("_parent") or it.get("parentFileId") or it.get("parentId") or root
            # cas 文件名 = 原文件名.cas（贴合光鸭版 OpenList 约定：靠文件名/内容还原原文件）
            cas_name = name + ".cas"
            content = self.build_cas(name, self._size(it), sha, it.get("contentHash", ""), parent)
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
                    "size": self._size(it),
                    "parent": parent,
                    "fileId": fid,
                    "verify_found": up.get("_verify_found"),
                    "error": err,
                    "detail": detail,
                })
            except Exception as e:
                results.append({"name": cas_name, "status": "upload_failed", "error": str(e)})
            if delete_source:
                to_delete.append(self._fid(it))
        if delete_source and to_delete:
            dresp = self.delete(to_delete)
            results.append({"name": f"[已删除 {len(to_delete)} 个原视频]", "status": "deleted", "detail": dresp})
        return results
