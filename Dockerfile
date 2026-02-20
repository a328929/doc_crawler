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

# 使用 gunicorn 启动
# 注意：任务队列与状态当前保存在进程内内存中，多 worker 会导致 /api/start 与 /api/stream 命中不同进程
# 从而出现“任务不存在或已过期”。这里使用单 worker + 多线程保证状态一致性。
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "app:app"]