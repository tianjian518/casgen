FROM python:3.12-slim

WORKDIR /app

# 仅复制运行所需文件（纯标准库，无需 pip install）
COPY app.py yidong.py index.html README.md ./

EXPOSE 5000

ENV PORT=5000

CMD ["python3", "app.py"]
