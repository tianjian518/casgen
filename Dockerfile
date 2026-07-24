FROM python:3.12-slim

# 非 root 运行（安全最佳实践）
RUN useradd -m -u 1000 casgen

WORKDIR /app

# casgen 纯标准库、零 pip 依赖，复制全部运行所需文件即可。
# 注意：app.py 会 import rename / monitor / share139 / healthcheck，必须一并复制，
# 否则容器启动即报 ModuleNotFoundError（飞牛/抱脸都一样）。
COPY --chown=casgen:casgen app.py yidong.py index.html utils.js README.md \
     convert.html share.html strm.html restore.html rename.html \
     rename.py monitor.py monitor_store.py share139.py healthcheck.py webdav.py ./

# 端口策略（关键，避免抱脸 Spaces 一直重启）：
#   - Hugging Face Spaces 不注入 PORT，而是探测固定端口 7860，因此 Dockerfile 默认值必须是 7860；
#   - 飞牛 FnOS / FnDepot / 根 docker-compose 通过 environment: PORT=5000 覆盖为 5000（见各自 compose）。
# 这样同一份 Dockerfile 推到 HF / GitHub / Docker Hub 都不会因端口错配而崩溃。
ENV PORT=7860

EXPOSE 7860

# 健康检查：每 30s 探活一次，5s 超时，启动预留 15s，连 3 次失败判不健康
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python3", "healthcheck.py"]

USER casgen

CMD ["python3", "app.py"]