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
# 视频 / 光盘镜像扩展名白名单（与 yidong.CAS_EXT 保持一致，用于只转存视频与镜像、避免误转图片/文本）
# 2026-08：新增 ISO / IMG / M2TS，分享里光盘镜像类也能一条龙转 CAS。
VIDEO_EXT = {
    ".mp4", ".mkv", ".ts", ".avi", ".mov", ".flv", ".wmv",
    ".rmvb", ".mpg", ".mpeg", ".m4v", ".webm", ".3gp", ".vob",
    ".iso", ".img", ".m2ts",
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
        with urllib.request.urlopen(req, timeout=300) as r:
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
       [{contentID, contentName, contentSize, path, pCaID, isVideo, folders}]
    folders = 该文件在分享内的祖先文件夹名列表（如 ["剧名A","第1季"]），顶层视频为 []。
    video_only=True 时仅保留视频扩展名白名单文件（默认行为，避免误转图片/文本）。
    """
    all_files = []

    def fetch(dir_id, folder_stack, start=1):
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
                "folders": list(folder_stack),
            })
        # 递归子目录
        for d in (info.get("caLst") or []):
            cid = str(d.get("catalogID") or d.get("caID") or "")
            if cid:
                nm = d.get("catalogName") or d.get("caName") or d.get("name") or cid
                fetch(cid, folder_stack + [nm])
        total = info.get("nodNum") or 0
        if start + 199 < total:
            fetch(dir_id, folder_stack, start + 200)

    fetch(p_ca_id, [])
    return all_files


def list_share_catalogs(link_id, pwd="", phone="", token=None, p_ca_id="root"):
    """返回分享根目录下的子文件夹（影视剧）列表：[{catalogID, name, path}]。
    path 直接采用 139 返回的 path（与文件 co_path 同一套 parentID/ID 格式），
    作为 createOuterLinkBatchOprTask 的 catalogInfoList 元素即可整目录转存，
    139 会自动在目标目录重建 <name>/ 并带上全部子内容。"""
    info = get_share_info(link_id, pwd, p_ca_id, 1, 200, phone, token)
    out = []
    for d in (info.get("caLst") or []):
        cid = str(d.get("catalogID") or d.get("caID") or "")
        if not cid:
            continue
        name = d.get("catalogName") or d.get("caName") or d.get("name") or ""
        path = d.get("path") or f"{p_ca_id}/{cid}"
        out.append({"catalogID": cid, "name": name, "path": path})
    return out


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



def transfer_share_videos(link_id, pwd, token, phone, catalogs, target_catalog_id,
                          make_folder, dry_run=False):
    """按分享目录结构、只转存【视频】文件到用户云盘 target_catalog_id 下。

    针对每个影视剧目录(catalogs 元素)在 target 下建同名子目录，递归遍历其内全部文件：
    仅把视频扩展名白名单(VIDEO_EXT)内的文件通过 createOuterLinkBatchOprTask 转存进对应子目录，
    非视频文件(图片/字幕/文本/压缩包等)一律跳过，绝不转存。同时保留分享内原有的嵌套子目录结构。

    make_folder(parent_id, name) -> folder_id : 由调用方用已登录的 Yun139 客户端实现
                                      （在用户云盘建/找目录）。
    dry_run=True 时只统计将转存的视频数，不真正调用转存接口、也不建目录。
    返回 (total_videos, actions)：total_videos=实际/将要转存的视频总数。
    """
    total = 0
    actions = []

    def _fetch_all(share_dir_id):
        out = []
        start = 1
        while True:
            info = get_share_info(link_id, pwd, share_dir_id, start, start + 199, phone, token)
            if not info:
                break
            out.append(info)
            nod = info.get("nodNum") or 0
            if start + 199 >= nod:
                break
            start += 200
        return out

    def _walk(share_dir_id, target_dir_id):
        nonlocal total
        video_paths = []
        subdirs = []
        for info in _fetch_all(share_dir_id):
            for f in (info.get("coLst") or []):
                name = f.get("contentName") or f.get("coName") or ""
                ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
                if ext not in VIDEO_EXT:
                    continue
                cid = f.get("contentID") or f.get("coID") or ""
                if not cid:
                    continue
                path = f.get("path") or f"{share_dir_id}/{cid}"
                video_paths.append(path)
            for d in (info.get("caLst") or []):
                cid = str(d.get("catalogID") or d.get("caID") or "")
                if not cid:
                    continue
                nm = d.get("catalogName") or d.get("caName") or d.get("name") or cid
                subdirs.append((cid, nm))
        if video_paths:
            if not dry_run:
                save_share_files(link_id, video_paths, [], target_dir_id,
                                 need_password=bool(pwd), phone=phone, token=token)
            total += len(video_paths)
            actions.append((target_dir_id, video_paths))
        for cid, nm in subdirs:
            child_id = folder_maker(target_dir_id, nm)
            _walk(cid, child_id)

    folder_maker = make_folder if not dry_run else (lambda p, n: p)
    for cat in catalogs:
        sub_id = folder_maker(target_catalog_id, cat["name"])
        _walk(cat["catalogID"], sub_id)
    return total, actions



def transfer_share_files(link_id, pwd, token, phone, file_list, target_catalog_id,
                         make_folder, flat=False, dry_run=False):
    """把给定分享文件列表（每项含 folders 祖先链与 path）按原目录结构转存到用户云盘 target_catalog_id。

    与 transfer_share_videos 的区别：本函数转存【调用方指定的文件列表】（用于监控增量扫描），
    而非整目录递归。每个文件的 folders（如 ["剧名A","第1季"]）决定它在 target 下的相对路径，
    逐层建/找目录后，把该层文件一次性转存进去。仅转存文件本身，非视频应在传入前已过滤。
    为保险起见，扩展名不在 VIDEO_EXT 的文件会被跳过（绝不转非视频）。

    flat=True 时忽略 folders，所有文件平铺进 target_catalog_id（对应一条龙里"不按结构分文件夹"）。
    make_folder(parent_id, name) -> folder_id : 由调用方用已登录 Yun139 客户端实现。
    dry_run=True 时不真正转存、不建目录，仅返回将转存的视频数。
    返回实际/将要转存的视频总数。
    """
    groups = {}
    for f in file_list:
        if not isinstance(f, dict):
            continue
        name = f.get("contentName") or f.get("coName") or ""
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        if ext not in VIDEO_EXT:
            continue
        path = f.get("path") or ""
        if not path:
            continue
        rel = () if flat else tuple(f.get("folders") or [])
        groups.setdefault(rel, []).append(path)
    folder_maker = make_folder if not dry_run else (lambda p, n: p)
    total = 0
    for rel, paths in groups.items():
        cur = target_catalog_id
        for nm in rel:
            cur = folder_maker(cur, nm)
        if not dry_run:
            save_share_files(link_id, paths, [], cur,
                             need_password=bool(pwd), phone=phone, token=token)
        total += len(paths)
    return total


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
