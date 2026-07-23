"""Docker HEALTHCHECK 探针：访问 /api/health 判断服务可用性。

使用 urllib 标准库，无额外依赖；超时 5s，失败返回非零让 Docker 判不健康。
"""
import os
import sys
import urllib.request

port = os.environ.get("PORT", "5000")
url = f"http://127.0.0.1:{port}/api/health"
try:
    r = urllib.request.urlopen(url, timeout=5)
    sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)