FROM python:3.12-slim

# 非 root 运行（安全最佳实践）
RUN useradd -m -u 1000 casgen

WORKDIR /app

# casgen 纯标准库、零 pip 依赖，复制全部运行所需文件即可。
# 注意：app.py 会 import rename / monitor / share139 / healthcheck，必须一并复制，
# 否则容器启动即报 ModuleNotFoundError（飞牛/抱脸都一样）。
COPY --chown=casgen:casgen app.py yidong.py index.html utils.js README.md \
     convert.html share.html strm.html restore.html rename.html \
     rename.py monitor.py monitor_store.py share139.py healthcheck.py ./

# 监听端口由 PORT 环境变量决定：Hugging Face Spaces 注入 7860，本地/飞牛默认 5000。
ENV PORT=5000

EXPOSE 5000

# 健康检查：每 30s 探活一次，5s 超时，启动预留 15s，连 3 次失败判不健康
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python3", "healthcheck.py"]

USER casgen

CMD ["python3", "app.py"]