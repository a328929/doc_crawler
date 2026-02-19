# 使用轻量级 Python 镜像
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 设置时区为上海
RUN ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime

# 安装系统依赖（lxml 可能需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .

# 安装依赖 (使用清华源加速)
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制项目文件
COPY . .

# 创建临时文件夹并设置权限
RUN mkdir -p temp_docs && chmod 777 temp_docs

# 暴露 5000 端口
EXPOSE 5000

# 使用 gunicorn 启动，设置工作线程数（可以根据 CPU 核心数调整）
# --threads 表示每个 worker 的线程数，适合 I/O 密集型应用
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", "app:app"]