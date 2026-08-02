# ===========================================================================
# 图片词条机器人 - Docker 镜像
# 基于 ErisPulse 官方镜像
# ===========================================================================
FROM ghcr.io/erispulse/erispulse:latest AS base

WORKDIR /app

# 复制项目文件
COPY pyproject.toml requirements.txt ./
COPY shbot/ ./shbot/
COPY main.py ./
COPY config/ ./config/

# 安装模块
RUN uv pip install --system -e .

# 创建图片存储目录
RUN mkdir -p /app/images

VOLUME ["/app/images", "/app/config"]

EXPOSE 8000

CMD ["epsdk", "run", "main.py"]