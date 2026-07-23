"""casgen WebDAV 模块 - 纯标准库实现，零外部依赖。

把 139 云盘目录树通过 WebDAV 协议暴露出来，让网易爆米花、飞牛影视、其他 NAS 播放器
直接挂载浏览 .strm 文件，并自动把 .strm 解析为 139 直链播放。

支持方法：OPTIONS, PROPFIND, GET, HEAD, PUT, DELETE, MKCOL, MOVE, LOCK, UNLOCK。
认证：HTTP Basic Auth，用户名/密码通过环境变量 CASGEN_WEBDAV_USER / CASGEN_WEBDAV_PASS 配置。
路径前缀：/dav/   例如 /dav/电影/xxx.strm
"""

import base64
import hashlib
import hmac
import os
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
import xml.sax.saxutils as saxutils

# =============================================================================
# 配置（从环境变量读取）
# =============================================================================
WEBDAV_USER = os.environ.get("CASGEN_WEBDAV_USER", "").strip()
WEBDAV_PASS = os.environ.get("CASGEN_WEBDAV_PASS", "").strip()
WEBDAV_ROOT = os.environ.get("CASGEN_WEBDAV_ROOT", "root").strip() or "root"
WEBDAV_ENABLED = bool(WEBDAV_USER and WEBDAV_PASS)

# 解析后的根目录 catalogID 会在首次使用时填充
_WEBDAV_ROOT_FID = None

# =============================================================================
# 与 app.py 的耦合：通过 bind 注入 CLIENT getter 和 AUTH_EXPIRED getter
# =============================================================================
_CLIENT_GETTER = None
_AUTH_EXPIRED_GETTER = None


def bind(client_getter, auth_expired_getter):
    """app.py 启动时调用，注入 Yun139 实例获取函数和登录失效状态获取函数。"""
    global _CLIENT_GETTER, _AUTH_EXPIRED_GETTER
    _CLIENT_GETTER = client_getter
    _AUTH_EXPIRED_GETTER = auth_expired_getter


def _client():
    return _CLIENT_GETTER() if _CLIENT_GETTER else None


def _expired():
    return bool(_AUTH_EXPIRED_GETTER()) if _AUTH_EXPIRED_GETTER else False


# =============================================================================
# 公共信息
# =============================================================================
def is_enabled():
    return WEBDAV_ENABLED


def get_info():
    return {
        "enabled": WEBDAV_ENABLED,
        "user": WEBDAV_USER,
        "root": WEBDAV_ROOT,
        "urlPrefix": "/dav/",
    }


# =============================================================================
# 路径处理
# =============================================================================
def _dav_rel_path(handler):
    """从 handler.path 提取 /dav/ 后面的相对路径（URL 解码、去首尾斜杠）。"""
    if handler.path.startswith("/dav/"):
        rel = handler.path[len("/dav/"):]
    elif handler.path == "/dav" or handler.path == "/dav/":
        rel = ""
    else:
        rel = handler.path
    return urllib.parse.unquote(rel).strip("/")


def _dav_href(rel):
    """生成 WebDAV href（UTF-8 编码）。"""
    parts = [p for p in rel.split("/") if p]
    if not parts:
        return "/dav/"
    encoded = "/".join(urllib.parse.quote(p, safe="/") for p in parts)
    return "/dav/" + encoded


def _resolve_root_fid(client):
    """把 WEBDAV_ROOT（如 '电影' 或 '电影/动画'）解析为 catalogID。"""
    global _WEBDAV_ROOT_FID
    if _WEBDAV_ROOT_FID is not None:
        return _WEBDAV_ROOT_FID
    if WEBDAV_ROOT in ("root", "/", ""):
        _WEBDAV_ROOT_FID = "root"
    else:
        _WEBDAV_ROOT_FID = client.resolve_folder(WEBDAV_ROOT)
    return _WEBDAV_ROOT_FID


def _resolve_path(client, rel):
    """把 WebDAV 相对路径解析为 (fileId, is_folder, item)。

    rel 是去掉 /dav/ 前缀后的路径。空路径表示 WebDAV 根目录。
    返回的 fileId 是 139 云盘的 catalogID / fileId。
    """
    rel = (rel or "").strip().strip("/")
    root_fid = _resolve_root_fid(client)

    if not rel:
        return root_fid, True, None

    parts = [p for p in rel.split("/") if p]
    cur = root_fid

    for i, part in enumerate(parts):
        is_last = (i == len(parts) - 1)
        items, _, _ = client.list_dir(cur)

        if is_last:
            # 最后一个部分：优先匹配文件夹，再匹配文件
            for it in items:
                if client._is_folder(it) and client._name(it) == part:
                    return client._fid(it), True, it
            for it in items:
                if not client._is_folder(it) and client._name(it) == part:
                    return client._fid(it), False, it
            raise FileNotFoundError("路径不存在: %s" % rel)
        else:
            found = False
            for it in items:
                if client._is_folder(it) and client._name(it) == part:
                    cur = client._fid(it)
                    found = True
                    break
            if not found:
                raise FileNotFoundError("文件夹不存在: %s" % part)

    # 不应该执行到这里
    raise FileNotFoundError("路径不存在: %s" % rel)


# =============================================================================
# 认证
# =============================================================================
def _require_auth(handler):
    """校验 Basic Auth。通过返回 True；否则发送 401 并返回 False。"""
    if not WEBDAV_ENABLED:
        _send_webdav_error(handler, 503, "WebDAV 未启用（请配置 CASGEN_WEBDAV_USER / CASGEN_WEBDAV_PASS）")
        return False

    auth_header = handler.headers.get("Authorization", "")
    if not auth_header.lower().startswith("basic "):
        _send_auth_required(handler)
        return False

    try:
        encoded = auth_header[6:].strip()
        decoded = base64.b64decode(encoded).decode("utf-8", "replace")
        user, _, pwd = decoded.partition(":")
    except Exception:
        _send_auth_required(handler)
        return False

    if not (hmac.compare_digest(user, WEBDAV_USER) and hmac.compare_digest(pwd, WEBDAV_PASS)):
        _send_auth_required(handler)
        return False

    return True


def _send_auth_required(handler):
    body = b"Unauthorized"
    handler.send_response(401)
    handler.send_header("WWW-Authenticate", 'Basic realm="casgen"')
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


# =============================================================================
# 读取请求体
# =============================================================================
def _read_body(handler):
    n = int(handler.headers.get("Content-Length", 0) or 0)
    return handler.rfile.read(n) if n else b""


# =============================================================================
# XML 工具
# =============================================================================
def _xml_escape(s):
    return saxutils.escape(str(s))


def _http_date(ts=None, item=None):
    """生成 HTTP-date (RFC 7231)。"""
    # 优先从 item 取时间字段
    if item is not None:
        raw = (item.get("updateTime") or item.get("updatedAt") or
               item.get("createTime") or item.get("createdAt") or
               item.get("modifyDate") or item.get("lastModifyTime") or
               item.get("gmtModified") or item.get("gmtCreate"))
        if raw:
            try:
                t_int = int(raw)
                if t_int > 1e12:  # 毫秒
                    t_int = t_int // 1000
                return time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(t_int))
            except (ValueError, TypeError):
                try:
                    t = time.strptime(str(raw)[:19], "%Y-%m-%d %H:%M:%S")
                    return time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.mktime(t)))
                except Exception:
                    pass
    if ts is None:
        ts = time.time()
    return time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(ts))


def _mime_type(name):
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    mime_map = {
        "strm": "text/plain",
        "cas": "application/octet-stream",
        "txt": "text/plain",
        "html": "text/html",
        "htm": "text/html",
        "css": "text/css",
        "js": "application/javascript",
        "json": "application/json",
        "xml": "application/xml",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "mp4": "video/mp4",
        "mkv": "video/x-matroska",
        "avi": "video/x-msvideo",
        "mov": "video/quicktime",
        "pdf": "application/pdf",
        "zip": "application/zip",
    }
    return mime_map.get(ext, "application/octet-stream")


def _parse_propfind(body):
    """解析 PROPFIND 请求体，返回请求的属性名集合；空集合表示 allprop。"""
    if not body:
        return set()
    try:
        root = ET.fromstring(body.decode("utf-8", "replace"))
    except ET.ParseError:
        return set()

    ns = {"D": "DAV:"}

    if root.find(".//D:allprop", ns) is not None:
        return set()
    if root.find(".//D:propname", ns) is not None:
        return {"propname"}

    prop_elem = root.find(".//D:prop", ns)
    if prop_elem is None:
        return set()

    props = set()
    for child in prop_elem:
        tag = child.tag
        if "}" in tag:
            tag = tag.split("}", 1)[1]
        props.add(tag)
    return props


def _build_propstat(rel, is_folder, item, client, requested_props):
    """构建单个资源的 <D:response> 片段。"""
    name = client._name(item) if item else (rel.rsplit("/", 1)[-1] if "/" in rel else rel)
    size = client._size(item) if item and not is_folder else 0
    fid = client._fid(item) if item else ""
    modified = _http_date(item=item)
    etag = '"%s"' % (fid or hashlib.md5(rel.encode("utf-8")).hexdigest())

    href = _dav_href(rel)

    props = []
    if not requested_props or "resourcetype" in requested_props:
        if is_folder:
            props.append("<D:resourcetype><D:collection/></D:resourcetype>")
        else:
            props.append("<D:resourcetype/>")
    if not requested_props or "getcontentlength" in requested_props:
        props.append("<D:getcontentlength>%d</D:getcontentlength>" % size)
    if not requested_props or "getlastmodified" in requested_props:
        props.append("<D:getlastmodified>%s</D:getlastmodified>" % modified)
    if not requested_props or "getcontenttype" in requested_props:
        ctype = "httpd/unix-directory" if is_folder else _mime_type(name)
        props.append("<D:getcontenttype>%s</D:getcontenttype>" % ctype)
    if not requested_props or "displayname" in requested_props:
        props.append("<D:displayname>%s</D:displayname>" % _xml_escape(name))
    if not requested_props or "getetag" in requested_props:
        props.append("<D:getetag>%s</D:getetag>" % _xml_escape(etag))

    prop_xml = "\n".join("            " + p for p in props)

    return """    <D:response>
      <D:href>%s</D:href>
      <D:propstat>
        <D:prop>
%s
        </D:prop>
        <D:status>HTTP/1.1 200 OK</D:status>
      </D:propstat>
    </D:response>""" % (_xml_escape(href), prop_xml)


def _build_multistatus(responses):
    body = """<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
%s
</D:multistatus>""" % "\n".join(responses)
    return body.encode("utf-8")


# =============================================================================
# 错误响应
# =============================================================================
def _send_webdav_error(handler, code, message=None):
    if message is None:
        message = {
            400: "Bad Request",
            401: "Unauthorized",
            403: "Forbidden",
            404: "Not Found",
            405: "Method Not Allowed",
            409: "Conflict",
            412: "Precondition Failed",
            415: "Unsupported Media Type",
            423: "Locked",
            500: "Internal Server Error",
            502: "Bad Gateway",
            503: "Service Unavailable",
            507: "Insufficient Storage",
        }.get(code, "Unknown Error")

    body = """<?xml version="1.0" encoding="utf-8"?>
<D:error xmlns:D="DAV:">
  <D:message>%s</D:message>
</D:error>""" % _xml_escape(message)
    body_bytes = body.encode("utf-8")

    handler.send_response(code)
    handler.send_header("Content-Type", "application/xml; charset=utf-8")
    handler.send_header("Content-Length", str(len(body_bytes)))
    handler.end_headers()
    handler.wfile.write(body_bytes)


def _send_xml(handler, code, body_bytes, extra_headers=None):
    handler.send_response(code)
    handler.send_header("Content-Type", "application/xml; charset=utf-8")
    handler.send_header("Content-Length", str(len(body_bytes)))
    if extra_headers:
        for k, v in extra_headers.items():
            handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body_bytes)


def _send_empty(handler, code):
    handler.send_response(code)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


# =============================================================================
# .strm 特殊处理
# =============================================================================
def _is_own_cas_url(url):
    """判断 URL 是否指向自己的 /cas/ 网关。"""
    url = url.strip()
    public_url = os.environ.get("CASGEN_PUBLIC_URL", "").strip().rstrip("/")

    if url.startswith("http://") or url.startswith("https://"):
        if public_url and url.startswith(public_url + "/cas/"):
            return True
        # 额外匹配本地常见地址（播放器和 casgen 同机时）
        for prefix in ("http://127.0.0.1", "http://localhost", "http://0.0.0.0"):
            if url.startswith(prefix) and "/cas/" in url:
                return True
        return False

    if url.startswith("/cas/"):
        return True
    return False


def _extract_cas_path(url):
    """从自己的 /cas/ URL 中提取 .cas 相对路径。"""
    url = url.strip()
    public_url = os.environ.get("CASGEN_PUBLIC_URL", "").strip().rstrip("/")

    if public_url and url.startswith(public_url + "/cas/"):
        return url[len(public_url) + len("/cas/"):]

    if url.startswith("/cas/"):
        return url[len("/cas/"):]

    for prefix in ("http://127.0.0.1", "http://localhost", "http://0.0.0.0"):
        if url.startswith(prefix):
            idx = url.find("/cas/")
            if idx >= 0:
                return url[idx + len("/cas/"):]

    return ""


# =============================================================================
# WebDAV 方法实现
# =============================================================================
def handle_options(handler):
    """OPTIONS：返回支持的方法列表，无需认证（客户端探测用）。"""
    handler.send_response(200)
    handler.send_header("Allow", "OPTIONS,GET,HEAD,PROPFIND,PUT,DELETE,MKCOL,MOVE,LOCK,UNLOCK")
    handler.send_header("DAV", "1,2")
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def handle_propfind(handler):
    if not _require_auth(handler):
        return
    client = _client()
    if client is None:
        _send_webdav_error(handler, 401, "请先登录 139 云盘")
        return
    if _expired():
        _send_webdav_error(handler, 401, "139 登录已失效，请重新登录")
        return

    rel = _dav_rel_path(handler)
    depth = handler.headers.get("Depth", "infinity")
    body = _read_body(handler)
    requested_props = _parse_propfind(body)

    try:
        fid, is_folder, item = _resolve_path(client, rel)
    except FileNotFoundError as e:
        _send_webdav_error(handler, 404, str(e))
        return

    responses = []

    # 当前资源
    responses.append(_build_propstat(rel, is_folder, item, client, requested_props))

    # Depth: 1 → 列出直接子项
    if depth in ("1", "infinity") and is_folder:
        try:
            children, _, _ = client.list_dir(fid)
            for child in children:
                child_name = client._name(child)
                child_rel = (rel + "/" + child_name) if rel else child_name
                child_is_folder = client._is_folder(child)
                responses.append(_build_propstat(child_rel, child_is_folder, child, client, requested_props))
        except Exception as e:
            _send_webdav_error(handler, 500, "列举子目录失败: %s" % e)
            return

    xml_body = _build_multistatus(responses)
    _send_xml(handler, 207, xml_body)


def handle_get(handler, schedule_cleanup_fn=None):
    if not _require_auth(handler):
        return
    client = _client()
    if client is None:
        _send_webdav_error(handler, 401, "请先登录 139 云盘")
        return
    if _expired():
        _send_webdav_error(handler, 401, "139 登录已失效，请重新登录")
        return

    rel = _dav_rel_path(handler)
    try:
        fid, is_folder, item = _resolve_path(client, rel)
    except FileNotFoundError as e:
        _send_webdav_error(handler, 404, str(e))
        return

    if is_folder:
        _send_webdav_error(handler, 403, "不能下载文件夹")
        return

    name = client._name(item) if item else rel.rsplit("/", 1)[-1]

    # .strm 特殊处理：读取内容 → 302 重定向
    if name.lower().endswith(".strm"):
        try:
            strm_content = client.read_file_text(fid).strip()
        except Exception as e:
            _send_webdav_error(handler, 500, "读取 .strm 文件失败: %s" % e)
            return

        if not strm_content:
            _send_webdav_error(handler, 502, ".strm 文件内容为空")
            return

        if _is_own_cas_url(strm_content):
            cas_path = _extract_cas_path(strm_content)
            if not cas_path:
                _send_webdav_error(handler, 500, "无法解析 .strm 中的 CAS 路径")
                return
            try:
                cas_fid = client.resolve_path_readonly(cas_path)
                cas_name = cas_path.rsplit("/", 1)[-1]
                url, temp_fid = client.cas_get_play_link(cas_fid, cas_name)
                if schedule_cleanup_fn and temp_fid:
                    schedule_cleanup_fn(temp_fid)
                handler.send_response(302)
                handler.send_header("Location", url)
                handler.send_header("Content-Length", "0")
                handler.end_headers()
                return
            except FileNotFoundError:
                _send_webdav_error(handler, 404, ".cas 文件不存在: %s" % cas_path)
                return
            except Exception as e:
                _send_webdav_error(handler, 500, "CAS 播放失败: %s" % e)
                return
        else:
            # 外部 URL，直接 302
            handler.send_response(302)
            handler.send_header("Location", strm_content)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return

    # .cas 特殊处理：读取种子 → 秒传恢复 → 302 到 139 直链
    # 这样播放器通过 WebDAV 挂载后，直接点 .cas 文件就能播放视频，无需先生成 .strm
    if name.lower().endswith(".cas"):
        try:
            url, temp_fid = client.cas_get_play_link(fid, name)
            if schedule_cleanup_fn and temp_fid:
                schedule_cleanup_fn(temp_fid)
            handler.send_response(302)
            handler.send_header("Location", url)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        except Exception as e:
            _send_webdav_error(handler, 500, "CAS 播放失败: %s" % e)
            return

    # 普通文件：直接下载
    try:
        content = client.read_file_text(fid)
        content_bytes = content.encode("utf-8")
    except Exception as e:
        _send_webdav_error(handler, 500, "下载失败: %s" % e)
        return

    handler.send_response(200)
    handler.send_header("Content-Type", _mime_type(name))
    handler.send_header("Content-Length", str(len(content_bytes)))
    handler.send_header("ETag", '"%s"' % fid)
    handler.send_header("Last-Modified", _http_date(item=item))
    handler.end_headers()
    handler.wfile.write(content_bytes)


def handle_head(handler):
    if not _require_auth(handler):
        return
    client = _client()
    if client is None:
        _send_webdav_error(handler, 401, "请先登录 139 云盘")
        return
    if _expired():
        _send_webdav_error(handler, 401, "139 登录已失效，请重新登录")
        return

    rel = _dav_rel_path(handler)
    try:
        fid, is_folder, item = _resolve_path(client, rel)
    except FileNotFoundError as e:
        _send_webdav_error(handler, 404, str(e))
        return

    name = client._name(item) if item else rel.rsplit("/", 1)[-1]
    size = client._size(item) if item and not is_folder else 0

    handler.send_response(200)
    handler.send_header("Content-Type", "httpd/unix-directory" if is_folder else _mime_type(name))
    handler.send_header("Content-Length", str(size))
    handler.send_header("ETag", '"%s"' % fid)
    handler.send_header("Last-Modified", _http_date(item=item))
    handler.end_headers()


def handle_put(handler):
    if not _require_auth(handler):
        return
    client = _client()
    if client is None:
        _send_webdav_error(handler, 401, "请先登录 139 云盘")
        return
    if _expired():
        _send_webdav_error(handler, 401, "139 登录已失效，请重新登录")
        return

    rel = _dav_rel_path(handler)
    if not rel:
        _send_webdav_error(handler, 403, "不能在根目录 PUT")
        return

    content = _read_body(handler)
    content_type = handler.headers.get("Content-Type", "application/octet-stream")

    # 解析父目录和文件名
    if "/" in rel:
        parent_rel, file_name = rel.rsplit("/", 1)
    else:
        parent_rel, file_name = "", rel

    try:
        parent_fid, _, _ = _resolve_path(client, parent_rel)
    except FileNotFoundError:
        _send_webdav_error(handler, 409, "父目录不存在")
        return

    # 已存在则先删除（覆盖）
    try:
        existing_fid, existing_is_folder, _ = _resolve_path(client, rel)
        if existing_is_folder:
            _send_webdav_error(handler, 405, "目标为目录，无法覆盖")
            return
        client.delete([existing_fid])
    except FileNotFoundError:
        pass
    except Exception as e:
        _send_webdav_error(handler, 500, "删除旧文件失败: %s" % e)
        return

    try:
        result = client.upload_file(file_name, content, parent_fid, content_type)
    except Exception as e:
        _send_webdav_error(handler, 500, "上传失败: %s" % e)
        return

    if result.get("_file_id"):
        _send_empty(handler, 201)
    else:
        err = (result.get("_upload_error") or result.get("_complete_error") or
               result.get("_create_failed") or "未知错误")
        _send_webdav_error(handler, 500, "上传失败: %s" % err)


def handle_delete(handler):
    if not _require_auth(handler):
        return
    client = _client()
    if client is None:
        _send_webdav_error(handler, 401, "请先登录 139 云盘")
        return
    if _expired():
        _send_webdav_error(handler, 401, "139 登录已失效，请重新登录")
        return

    rel = _dav_rel_path(handler)
    if not rel:
        _send_webdav_error(handler, 403, "不能删除 WebDAV 根目录")
        return

    try:
        fid, is_folder, _ = _resolve_path(client, rel)
    except FileNotFoundError:
        _send_webdav_error(handler, 404)
        return

    if is_folder:
        try:
            items, _, _ = client.list_dir(fid)
            if items:
                _send_webdav_error(handler, 409, "目录不为空，无法删除")
                return
        except Exception as e:
            _send_webdav_error(handler, 500, "检查目录失败: %s" % e)
            return

    try:
        client.delete([fid])
    except Exception as e:
        _send_webdav_error(handler, 500, "删除失败: %s" % e)
        return

    _send_empty(handler, 204)


def handle_mkcol(handler):
    if not _require_auth(handler):
        return
    client = _client()
    if client is None:
        _send_webdav_error(handler, 401, "请先登录 139 云盘")
        return
    if _expired():
        _send_webdav_error(handler, 401, "139 登录已失效，请重新登录")
        return

    rel = _dav_rel_path(handler)
    if not rel:
        _send_webdav_error(handler, 405, "根目录已存在")
        return

    try:
        _resolve_path(client, rel)
        _send_webdav_error(handler, 405, "资源已存在")
        return
    except FileNotFoundError:
        pass

    if "/" in rel:
        parent_rel, dir_name = rel.rsplit("/", 1)
    else:
        parent_rel, dir_name = "", rel

    try:
        parent_fid, _, _ = _resolve_path(client, parent_rel)
    except FileNotFoundError:
        _send_webdav_error(handler, 409, "父目录不存在")
        return

    try:
        client.find_or_create_folder(dir_name, parent_fid)
    except Exception as e:
        _send_webdav_error(handler, 500, "创建目录失败: %s" % e)
        return

    _send_empty(handler, 201)


def handle_move(handler):
    if not _require_auth(handler):
        return
    client = _client()
    if client is None:
        _send_webdav_error(handler, 401, "请先登录 139 云盘")
        return
    if _expired():
        _send_webdav_error(handler, 401, "139 登录已失效，请重新登录")
        return

    rel = _dav_rel_path(handler)
    destination = handler.headers.get("Destination", "")
    overwrite = handler.headers.get("Overwrite", "T") != "F"

    # 解析目标路径
    if destination.startswith("http://") or destination.startswith("https://"):
        parsed = urllib.parse.urlparse(destination)
        dest_path = parsed.path
    else:
        dest_path = destination

    if dest_path.startswith("/dav/"):
        dest_path = dest_path[len("/dav/"):]
    dest_path = urllib.parse.unquote(dest_path).strip("/")

    if not rel or not dest_path:
        _send_webdav_error(handler, 400, "缺少源路径或目标路径")
        return

    # 检查目标是否已存在
    try:
        dest_fid, _, _ = _resolve_path(client, dest_path)
        if not overwrite:
            _send_webdav_error(handler, 412, "目标已存在")
            return
        client.delete([dest_fid])
    except FileNotFoundError:
        pass
    except Exception as e:
        _send_webdav_error(handler, 500, "删除目标失败: %s" % e)
        return

    # 解析源
    try:
        src_fid, _, item = _resolve_path(client, rel)
    except FileNotFoundError:
        _send_webdav_error(handler, 404, "源文件不存在")
        return

    # 解析目标父目录和文件名
    if "/" in dest_path:
        dest_parent_rel, dest_name = dest_path.rsplit("/", 1)
    else:
        dest_parent_rel, dest_name = "", dest_path

    try:
        dest_parent_fid, _, _ = _resolve_path(client, dest_parent_rel)
    except FileNotFoundError:
        _send_webdav_error(handler, 409, "目标父目录不存在")
        return

    # 139 不支持真正的 move，只能重命名；简化处理：仅支持同目录内重命名
    src_parent_rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
    if src_parent_rel != dest_parent_rel:
        _send_webdav_error(handler, 502, "139 云盘不支持跨目录移动")
        return

    try:
        client.rename_file(src_fid, dest_name)
    except Exception as e:
        _send_webdav_error(handler, 500, "重命名失败: %s" % e)
        return

    _send_empty(handler, 201)


def handle_lock(handler):
    if not _require_auth(handler):
        return
    client = _client()
    if client is None:
        _send_webdav_error(handler, 401, "请先登录 139 云盘")
        return

    rel = _dav_rel_path(handler)
    try:
        _resolve_path(client, rel)
    except FileNotFoundError as e:
        _send_webdav_error(handler, 404, str(e))
        return

    # 139 不支持真实文件锁，返回一个假 token；播放器/客户端通常只需要不报错即可
    lock_token = "urn:uuid:" + str(uuid.uuid4())
    xml = """<?xml version="1.0" encoding="utf-8"?>
<D:prop xmlns:D="DAV:">
  <D:lockdiscovery>
    <D:activelock>
      <D:locktype><D:write/></D:locktype>
      <D:lockscope><D:exclusive/></D:lockscope>
      <D:depth>0</D:depth>
      <D:timeout>Second-604800</D:timeout>
      <D:locktoken><D:href>%s</D:href></D:locktoken>
    </D:activelock>
  </D:lockdiscovery>
</D:prop>""" % _xml_escape(lock_token)

    body_bytes = xml.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/xml; charset=utf-8")
    handler.send_header("Lock-Token", "<%s>" % lock_token)
    handler.send_header("Content-Length", str(len(body_bytes)))
    handler.end_headers()
    handler.wfile.write(body_bytes)


def handle_unlock(handler):
    if not _require_auth(handler):
        return
    _send_empty(handler, 204)
