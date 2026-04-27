# Code Executor Service Dockerfile
# 基于 Python 3.11，包含数据分析和可视化依赖
# 安全增强：以非 root 用户运行，符合 Requirement 6.1

FROM python:3.11-slim AS builder

ARG USE_CHINA_MIRROR=1
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

# 设置构建阶段环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 安装构建依赖
WORKDIR /build

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖到临时目录
RUN if [ "$USE_CHINA_MIRROR" = "1" ]; then \
      pip install --no-cache-dir -i "$PIP_INDEX_URL" --trusted-host "$PIP_TRUSTED_HOST" --target=/build/deps -r requirements.txt; \
    else \
      pip install --no-cache-dir --target=/build/deps -r requirements.txt; \
    fi

# ============================================
# 最终镜像阶段
# ============================================
FROM python:3.11-slim

ARG USE_CHINA_MIRROR=1
ARG INSTALL_FULL_CJK_FONTS=0
ARG INSTALL_SIMHEI_FONT=0

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUTF8=1 \
    MPLBACKEND=Agg \
    QT_QPA_PLATFORM=offscreen \
    WORKSPACE_DIR=/workspace \
    # 设置 Python 路径以包含安装的依赖
    PYTHONPATH=/app/deps:$PYTHONPATH

# 安装系统依赖（合并 RUN 命令以减少镜像层数）
RUN if [ "$USE_CHINA_MIRROR" = "1" ]; then \
      sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
        /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends \
    # 基础工具（健康检查需要）
    curl \
    wget \
    # 轻量中文字体支持
    fonts-wqy-microhei \
    fontconfig \
    # matplotlib 依赖
    libfreetype6 \
    libpng16-16 \
    && if [ "$INSTALL_FULL_CJK_FONTS" = "1" ]; then \
      apt-get install -y --no-install-recommends fonts-noto-cjk fonts-noto-cjk-extra fonts-wqy-zenhei; \
    fi \
    # 清理缓存以减小镜像大小
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean \
    && rm -rf /var/cache/apt/archives/* \
    && if [ "$INSTALL_SIMHEI_FONT" = "1" ]; then \
      mkdir -p /usr/share/fonts/truetype/simhei \
      && wget -q -O /usr/share/fonts/truetype/simhei/SimHei.ttf \
         "https://github.com/StellarCN/scp_zh/raw/master/fonts/SimHei.ttf"; \
    fi \
    # 刷新字体缓存
    && fc-cache -fv

# 创建非 root 用户（符合 Requirement 6.1：以非 root 用户身份运行）
# 使用固定 UID/GID 以确保一致性
ARG USER_UID=1000
ARG USER_GID=1000

RUN groupadd --gid ${USER_GID} sandbox \
    && useradd --uid ${USER_UID} --gid ${USER_GID} --shell /bin/bash --create-home sandbox

# 创建工作目录并设置权限（在复制文件之前）
RUN mkdir -p /workspace /data /output /var/sandbox/workspaces \
    && chown -R sandbox:sandbox /workspace /data /output /var/sandbox \
    && chmod 755 /workspace /data /output /var/sandbox /var/sandbox/workspaces

# 创建应用目录
WORKDIR /app

# 从构建阶段复制 Python 依赖
COPY --from=builder /build/deps /app/deps

# 复制应用代码
COPY --chown=sandbox:sandbox app/ ./app/

# 复制 sandbox_runtime 模块到 Python 路径
COPY --chown=sandbox:sandbox sandbox_runtime/ ./sandbox_runtime/

# 确保 /app 目录权限正确
RUN chown -R sandbox:sandbox /app

# 切换到非 root 用户
USER sandbox

# 暴露端口
EXPOSE 8080

# 健康检查配置
# --interval: 检查间隔
# --timeout: 超时时间
# --start-period: 启动等待时间
# --retries: 失败重试次数
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# 启动命令
# --ws-max-size: WebSocket 消息大小限制（50MB，支持大数据返回）
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--ws-max-size", "52428800"]
