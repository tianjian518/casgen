FROM python:3.12-slim

WORKDIR /app

# 2.0 起引入 pycryptodome，用于分享链接 AES-128-CBC 解析（非可选依赖）
RUN pip install --no-cache-dir pycryptodome

# 复制运行所需文件
COPY app.py yidong.py index.html README.md ./

EXPOSE 5000

ENV PORT=5000

CMD ["python3", "app.py"]
