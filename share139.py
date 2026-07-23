"""移动139 分享链接解析与转存模块（casgen 2.0 新增，自维护实现）。

核心逻辑翻译自 azheng0108/cloudpan-auto-save 的 src/services/cloud139.js（TypeScript → Python 自维护），
采用 share-kd-njs.yun.139.com 的「明文 V6 协议」：
  - 分享信息/文件列表：POST /yun-share/richlifeApp/devapp/IOutLink/getOutLinkInfoV6
  - 转存到自己云盘：   POST /yun-share/richlifeApp/devapp/IBatchOprTask/createOuterLinkBatchOprTask
该协议【不需要 mcloud-sign 签名，也不需要请求体加密】，直接用 JSON 调用即可。

同时内置 OutLinkCrypto（AES-128-CBC + PKCS7，密钥 "PVGDwmcvfs1uV3d1"，IV 16 字节前置，整体 Base64），
对应官方 App 新版加密外链接口，作为备用能力（pycryptodome 为可选依赖，未安装时仅此类不可用，主流程不受影响）。

设计原则（用户要求）：自主可控、不依赖第三方 SDK、核心代码自己维护。
"""

import base64
import json
import re
import urllib.request
import urllib.error

# ============================ 常量 ============================
SHARE_HOST = "https://share-kd-njs.yun.139.com"

SHARE_HEADERS = {
    "caller": "web",
    "x-m4c-caller": "PC",
    "mcloud-client": "10701",
    "mcloud-version": "7.17.2",
    "mcloud-channel": "1000101",
    "Content-Type": "application/json",
}

# 视频扩展名白名单（与 yidong.generate 保持一致，用于只转存视频、避免误转图片/文本）
VIDEO_EXT = {
    ".mp4", ".mkv", ".ts", ".avi", ".mov", ".flv", ".wmv",
    ".rmvb", ".mpg", ".mpeg", ".m4v", ".webm", ".3gp", ".vob",
}

FATAL_CODES = {
    "200000727",  # 外链不存在/已被分享者取消
    "200000401",  # 外链已过期
    "200000402",  # 外链已达访问次数上限
    "05010003",   # 查询不到用户信息
    "04000005",   # 认证失败
    "05050006",   # token 失效
}


class ShareError(Exception):
    """分享/转存相关的错误。fatal=True 表示无需重试（链接失效/过期等）。"""

    def __init__(self, message, code=None, fatal=False):
        super().__init__(message)
        self.message = message
        self.api_code = code
        self.fatal = fatal


# ============================ 链接/提取码自动解析 ============================
def parse_share_input(text):
    """从用户粘贴的文本里自动抠出 (link_id, passwd)。

    用户提醒：不要让人手动分开填链接和提取码，能顺带解析就解析。
    支持：
      - 链接形如 https://yun.139.com/shareweb/#/w/i/<id> 或 /w/i/<id>（取 id）
      - 提取码在 URL 参数 ?pwd=xxx / ?code=xxx / ?passwd=xxx
      - 提取码在文本中 “提取码:xxxx” / “pwd:xxxx” / “密码:xxxx”
      - 以上都没有时返回 (link_id, "") 交由接口判断是否需密码
    """
    text = (text or "").strip()
    link_id = None
    pwd = ""

    # 1) 链接 ID（shareweb 路径里的 /w/i/<id>）
    m = re.search(r"/w/i/([A-Za-z0-9_-]+)", text)
    if not m:
        m = re.search(r"/i/([A-Za-z0-9_-]+)", text)
    if m:
        link_id = m.group(1)

    # 2) 提取码优先从 URL 参数取
    um = re.search(r"[?&](?:pwd|code|passwd|password)=([A-Za-z0-9]+)", text, re.I)
    if um:
        pwd = um.group(1)
    else:
        # 文本中的 “提取码:xxxx” 等
        pm = re.search(
            r"(?:提取码|访问码|密码|pwd|code)\s*[:：]?\s*([A-Za-z0-9]{3,8})",
            text, re.I,
        )
        if pm:
            pwd = pm.group(1)

    return link_id, pwd


# ============================ 底层请求 ============================
def _share_post(path, body, token=None):
    """POST 到 share-kd-njs，返回 data 层（或整包）。失败抛 ShareError。"""
    url = SHARE_HOST + path
    data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = dict(SHARE_HEADERS)
    if token:
        # token 可能是 "Basic xxxx" 或纯 base64，统一成 Basic
        t = token.strip()
        if not t.lower().startswith("basic "):
            t = "Basic " + t
        headers["Authorization"] = t
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            resp = json.loads(e.read().decode("utf-8"))
        except Exception:
            raise ShareError(f"139 分享接口 HTTP {e.code}: {e.reason}", code=str(e.code))
    except Exception as e:  # 网络层错误
        raise ShareError(f"请求 139 分享接口失败: {e}")

    code = resp.get("code")
    if code not in (0, "0", None):
        desc = resp.get("desc") or resp.get("message") or "未知错误"
        c = str(code)
        raise ShareError(f"139 分享API 错误 [{c}]: {desc}", code=c, fatal=(c in FATAL_CODES))
    return resp.get("data", resp)


# ============================ 分享信息 / 文件列表 ============================
def get_share_info(link_id, pwd="", p_ca_id="root", start=1, end=200, phone="", token=None):
    """调用 getOutLinkInfoV6，返回 data 层（含 caLst 文件夹、coLst 文件、nodNum、lkName 等）。"""
    body = {
        "getOutLinkInfoReq": {
            "account": phone or "",
            "linkID": link_id,
            "passwd": pwd or "",
            "pCaID": p_ca_id,
            "caSrt": 0,
            "coSrt": 0,
            "srtDr": 1,
            "bNum": start,
            "eNum": end,
        }
    }
    return _share_post(
        "/yun-share/richlifeApp/devapp/IOutLink/getOutLinkInfoV6", body, token=token
    )


def list_all_share_files(link_id, pwd="", phone="", token=None, p_ca_id="root", video_only=True):
    """递归列出分享内所有文件，返回扁平列表：
       [{contentID, contentName, contentSize, path, pCaID, isVideo}]
    video_only=True 时仅保留视频扩展名白名单文件（默认行为，避免误转图片/文本）。
    """
    all_files = []

    def fetch(dir_id, start=1):
        info = get_share_info(link_id, pwd, dir_id, start, start + 199, phone, token)
        if not info:
            return
        for f in (info.get("coLst") or []):
            name = f.get("contentName") or f.get("coName") or ""
            cid = f.get("contentID") or f.get("coID") or ""
            path = f.get("path") or f"{dir_id}/{cid}"
            ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
            is_video = ext in VIDEO_EXT
            if video_only and not is_video:
                continue
            all_files.append({
                "contentID": cid,
                "contentName": name,
                "contentSize": f.get("contentSize") or f.get("coSize") or 0,
                "path": path,
                "pCaID": dir_id,
                "isVideo": is_video,
            })
        # 递归子目录
        for d in (info.get("caLst") or []):
            cid = str(d.get("catalogID") or d.get("caID") or "")
            if cid:
                fetch(cid)
        total = info.get("nodNum") or 0
        if start + 199 < total:
            fetch(dir_id, start + 200)

    fetch(p_ca_id)
    return all_files


# ============================ 转存到自己云盘 ============================
def save_share_files(link_id, co_path_lst, ca_path_lst, target_catalog_id,
                     need_password=False, phone="", token=None):
    """把分享里的文件/目录转存到自己云盘的 target_catalog_id 目录。

    co_path_lst : 文件 path 列表（来自分享列表的 path 字段，格式 parentID/fileID）
    ca_path_lst : 目录 path 列表（格式 parentID/catalogID）
    target_catalog_id : 自己云盘的目标目录 catalogID（根目录传 "root"）
    """
    body = {
        "createOuterLinkBatchOprTaskReq": {
            "msisdn": phone or "",
            "ownerAccount": "",
            "taskType": 1,
            "linkID": link_id,
            "needPassword": bool(need_password),
            "taskInfo": {
                "linkID": link_id,
                "needPassword": bool(need_password),
                "contentInfoList": list(co_path_lst),
                "catalogInfoList": list(ca_path_lst),
                "newCatalogID": target_catalog_id,
            },
        }
    }
    return _share_post(
        "/yun-share/richlifeApp/devapp/IBatchOprTask/createOuterLinkBatchOprTask",
        body, token=token,
    )


# ============================ 新版加密外链（备用 / 自维护） ============================
class OutLinkCrypto:
    """官方 App 新版外链接口的 AES-128-CBC 加解密（对应 khkj6.com 逆向结论）。

    算法：AES-128-CBC + PKCS7；密钥固定 "PVGDwmcvfs1uV3d1"(UTF-8)；
          IV 随机 16 字节，拼接在密文前；整体 Base64（标准，非 URL-safe）。
    当前分享解析走明文 V6 协议用不到它；pycryptodome 为可选依赖，采用惰性导入，
    未安装时仅此类不可用，不影响主流程（解析/转存仍正常工作）。
    """

    KEY = b"PVGDwmcvfs1uV3d1"

    @staticmethod
    def encrypt(plaintext_json: str) -> str:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad
        from Crypto.Random import get_random_bytes
        iv = get_random_bytes(16)
        cipher = AES.new(OutLinkCrypto.KEY, AES.MODE_CBC, iv)
        ct = cipher.encrypt(pad(plaintext_json.encode("utf-8"), AES.block_size))
        return base64.b64encode(iv + ct).decode("ascii")

    @staticmethod
    def decrypt(b64_text: str) -> str:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
        raw = base64.b64decode(re.sub(r"\s+", "", b64_text))
        iv, ct = raw[:16], raw[16:]
        cipher = AES.new(OutLinkCrypto.KEY, AES.MODE_CBC, iv)
        return unpad(cipher.decrypt(ct), AES.block_size).decode("utf-8")
