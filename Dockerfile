FROM python:3.12-slim

# 非 root 运行（安全最佳实践）
RUN useradd -m -u 1000 casgen

WORKDIR /app

# casgen 纯标准库、零 pip 依赖，复制全部运行所需文件即可。
# 注意：app.py 会 import rename / monitor / share139 / healthcheck，必须一并复制，
# 否则容器启动即报 ModuleNotFoundError（飞牛/抱脸都一样）。
# [8.0.1] v8.0.0 起 5 个独立前端页面（convert/share/strm/restore/rename.html + utils.js）
# 已全部内联进 index.html，这些文件已从仓库删除。此处若继续 COPY 会让 docker build
# 报 `failed to calculate checksum ... "/convert.html": not found`，导致镜像构建失败。
COPY --chown=casgen:casgen app.py yidong.py index.html README.md \
     rename.py monitor.py monitor_store.py share139.py healthcheck.py webdav.py ./

# 端口策略（关键，避免端口错配导致容器不健康 / 一直重启）：
#   - 默认 PORT=5000，与 docker-compose 的端口映射(5000:5000)和 CI 冒烟测试(-p 5050:5000)一致；
#   - Hugging Face Spaces 会在运行时通过环境变量注入 PORT=7860，覆盖此默认值，因此 HF 上仍监听 7860。
# 这样同一份 Dockerfile 推到 HF / GitHub / Docker Hub 都不会因端口错配而崩溃。
ENV PORT=5000

# 运行时数据目录（登录态 / 监控清单 / 调试日志）：把宿主机目录挂到 /data 即可持久化。
# ⚠️ 切记不要把宿主机目录挂到 /app——那会把容器里的代码整个遮住，容器启动即失败。
# 不挂卷也能正常跑，只是容器重建后登录态和监控清单会丢。
ENV CASGEN_DATA_DIR=/data
RUN mkdir -p /data && chown casgen:casgen /data
VOLUME ["/data"]

EXPOSE 5000

# 健康检查：每 30s 探活一次，5s 超时，启动预留 15s，连 3 次失败判不健康
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python3", "healthcheck.py"]

USER casgen

CMD ["python3", "app.py"]