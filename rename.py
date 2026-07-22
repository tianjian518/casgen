"""L1 本地正则重命名（零依赖 / 零联网）。

把杂乱的 .cas 显示名整理成干净标准名：
  - 去水印：发布组 / 网站域名 / 下载广告词（保留分辨率、编码等技术标签）
  - 统一季集：第X季第X集 / 1x01 / EP01 / 第X话  ->  S01E01
只改 .cas 的「显示名」，不动文件内容；恢复靠 .cas 内部的 sha256，改名完全不影响播放，天然低风险。

L2（TMDB）/ L3（LLM）预留 RenameProvider 接口，本文件当前只实现 L1（LocalRegexProvider）。
"""

import os
import re


# 圆括号/方括号里出现的域名（含常见后缀）——典型水印
_RE_DOMAIN_BRACKET = re.compile(
    r'[\(\[\{]\s*(?:www\.)?[a-z0-9\-]+\.(?:com|cn|net|org|tv|cc|me|xyz|top|vip|live)\b[^\]\)\}]*[\)\]\}]',
    re.IGNORECASE,
)

# 明确是水印/广告的中文词，直接删除
_WATERMARK_WORDS = [
    "迅雷下载", "免费下载", "百度云", "百度网盘", "夸克网盘", "夸克",
    "阿里云盘", "天翼云盘", "移动云盘", "高清影视", "影视资源",
    "电影下载", "电视剧下载", "在线观看", "BT下载", "磁力下载",
    "种子下载", "资源分享", "影视分享", "免费观看", "独家首发",
    "高清下载", "下载观看", "影视吧", "电影吧", "剧集吧",
    "资源君", "影视君", "分享君", "网盘资源", "云盘资源",
]

# 方括号/圆括号里的「技术标签」白名单——保留，不当水印删
_TECH_KEEP = {
    "1080P", "1080I", "720P", "480P", "4K", "2160P", "BD", "BDRIP",
    "WEB", "WEBRIP", "WEB-DL", "HDR", "HD", "H264", "H265", "HEVC",
    "X264", "X265", "TRUEHD", "DTS", "DTS-HD", "FLAC", "AAC", "MP4",
    "MKV", "REMUX", "ATMOS", "10BIT", "10BITHDR",
}

# 捕获所有方/圆/花括号内容，回调里判断是否水印
_RE_BRACKET = re.compile(r'[\(\[\{]([^\]\)\}]*)[\)\]\}]')


def _strip_watermarks(s: str) -> str:
    s = _RE_DOMAIN_BRACKET.sub(" ", s)
    for w in _WATERMARK_WORDS:
        s = s.replace(w, " ")
    def _br(m):
        inner = m.group(1).strip()
        up = re.sub(r'[\s\-]', "", inner).upper()
        # 保留技术标签
        if up in _TECH_KEEP:
            return m.group(0)
        # 保留纯年份 [2023]/(2023)
        if re.fullmatch(r'19\d{2}|20\d{2}', inner):
            return m.group(0)
        # 其余（发布组名/水印）删掉
        return " "
    s = _RE_BRACKET.sub(_br, s)
    return s


def _normalize_episode(s: str) -> str:
    # 第X季第X集 -> S01E01
    m = re.search(r'第\s*(\d+)\s*季\s*第\s*(\d+)\s*集', s)
    if m:
        s = re.sub(r'第\s*\d+\s*季\s*第\s*\d+\s*集',
                   'S%02dE%02d' % (int(m.group(1)), int(m.group(2))), s)
    # 第X季 -> S01
    s = re.sub(r'第\s*(\d+)\s*季', lambda m: 'S%02d' % int(m.group(1)), s)
    # 第X集 -> E01
    s = re.sub(r'第\s*(\d+)\s*集', lambda m: 'E%02d' % int(m.group(1)), s)
    # 第X话 -> E01
    s = re.sub(r'第\s*(\d+)\s*话', lambda m: 'E%02d' % int(m.group(1)), s)
    # 1x01 / 1X01 -> S01E01（注意避免把 DTS5.1 的 S5 误判，前置 (?<![A-Za-z])）
    s = re.sub(r'(?<![A-Za-z])(\d{1,2})[xX](\d{1,3})',
               lambda m: 'S%02dE%02d' % (int(m.group(1)), int(m.group(2))), s)
    # EP01 -> E01
    s = re.sub(r'\bEP\s*(\d{1,3})\b', lambda m: 'E%02d' % int(m.group(1)), s, flags=re.IGNORECASE)
    # 已有 SxEy 规范化前导零
    s = re.sub(r'S(\d{1,2})E(\d{1,3})',
               lambda m: 'S%02dE%02d' % (int(m.group(1)), int(m.group(2))), s)
    return s


def l1_normalize(filename: str) -> str:
    """对单个文件名做 L1 规范化，返回新文件名（保持原扩展名）。"""
    base, ext = os.path.splitext(filename)
    s = base
    s = _strip_watermarks(s)
    s = _normalize_episode(s)
    # 清理分隔符：多个空格/点 -> 单空格；去首尾空格/点/连字符/下划线
    s = re.sub(r'[\s\.]+', ' ', s).strip(' .-_')
    if not s:
        s = base  # 兜底：规范化后空了就不动
    return s + ext


# ----------------------- RenameProvider 接口（L2/L3 预留） -----------------------
class RenameProvider:
    """重命名提供者接口。L2=TMDB、L3=LLM 后续实现，L1=本地正则。"""
    name = "base"

    def normalize(self, filename: str) -> str:
        raise NotImplementedError


class LocalRegexProvider(RenameProvider):
    name = "L1"

    def normalize(self, filename: str) -> str:
        return l1_normalize(filename)


def default_provider() -> RenameProvider:
    return LocalRegexProvider()


if __name__ == "__main__":
    # 简单自测
    tests = [
        "电影[资源社].1080p.mkv.cas",
        "权利的游戏 第1季第3集 HD.cas",
        "Breaking.Bad.1x05.某组.cas",
        "剧集(www.example.com)EP07.cas",
        "非凡任务 2023 高清.cas",
        "测试 第12话.cas",
    ]
    for t in tests:
        print("%-40s -> %s" % (t, l1_normalize(t)))
