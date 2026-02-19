FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 设置时区为上海 (可选)
RUN ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime

# 复制当前目录所有文件到容器中
COPY . /app

# 安装依赖 (使用清华源加速)
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 暴露 5000 端口
EXPOSE 5000

# 启动命令
CMD ["python", "app.py"]