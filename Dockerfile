FROM python:3.12-slim

WORKDIR /app

# 复制运行所需文件
COPY app.py yidong.py index.html README.md ./

EXPOSE 5000

ENV PORT=5000

CMD ["python3", "app.py"]
