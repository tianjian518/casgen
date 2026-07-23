FROM python:3.12-slim

WORKDIR /app

# casgen 纯标准库、零 pip 依赖，复制全部运行所需文件即可。
# 注意：app.py 会 import rename / monitor / share139，必须一并复制，
# 否则容器启动即报 ModuleNotFoundError（飞牛/抱脸都一样）。
COPY app.py yidong.py index.html utils.js README.md convert.html share.html strm.html restore.html rename.html rename.py monitor.py monitor_store.py share139.py ./

# 监听端口由 PORT 环境变量决定：Hugging Face Spaces 注入 7860，本地/飞牛默认 5000。
# 程序已绑定 0.0.0.0 并读取 PORT，无需改任何代码即可在抱脸运行。
ENV PORT=5000

EXPOSE 5000

CMD ["python3", "app.py"]
