#!/bin/bash
# =============================================================================
# gVisor (runsc) 安装脚本
# =============================================================================
# 用于在 Linux 服务器或 WSL2 上安装 gVisor
# 
# 使用方法:
#   chmod +x install_gvisor.sh
#   sudo ./install_gvisor.sh
# =============================================================================

set -e

echo "=== gVisor 安装脚本 ==="
echo ""

# 检查是否以 root 运行
if [ "$EUID" -ne 0 ]; then 
    echo "错误：请使用 sudo 运行此脚本"
    exit 1
fi

# 检测架构
ARCH=$(uname -m)
if [ "$ARCH" = "x86_64" ]; then
    ARCH="x86_64"
elif [ "$ARCH" = "aarch64" ]; then
    ARCH="aarch64"
else
    echo "错误：不支持的架构: $ARCH"
    exit 1
fi

echo "检测到架构: $ARCH"

# 下载目录
INSTALL_DIR="/usr/local/bin"
GVISOR_URL="https://storage.googleapis.com/gvisor/releases/release/latest/${ARCH}"

echo ""
echo "=== 步骤 1: 下载 gVisor 组件 ==="

# 下载 runsc
echo "下载 runsc..."
wget -q "${GVISOR_URL}/runsc" -O "${INSTALL_DIR}/runsc"
chmod +x "${INSTALL_DIR}/runsc"
echo "runsc 已安装到 ${INSTALL_DIR}/runsc"

# 下载 containerd shim
echo "下载 containerd-shim-runsc-v1..."
wget -q "${GVISOR_URL}/containerd-shim-runsc-v1" -O "${INSTALL_DIR}/containerd-shim-runsc-v1"
chmod +x "${INSTALL_DIR}/containerd-shim-runsc-v1"
echo "containerd-shim 已安装到 ${INSTALL_DIR}/containerd-shim-runsc-v1"

echo ""
echo "=== 步骤 2: 配置 Docker daemon ==="

# 备份现有配置
DOCKER_CONFIG="/etc/docker/daemon.json"
if [ -f "$DOCKER_CONFIG" ]; then
    cp "$DOCKER_CONFIG" "${DOCKER_CONFIG}.backup.$(date +%Y%m%d%H%M%S)"
    echo "已备份现有 Docker 配置"
fi

# 检查是否已有 runsc 配置
if [ -f "$DOCKER_CONFIG" ] && grep -q "runsc" "$DOCKER_CONFIG"; then
    echo "Docker 已配置 runsc 运行时，跳过配置"
else
    # 创建或更新配置
    if [ -f "$DOCKER_CONFIG" ]; then
        # 使用 jq 合并配置（如果有 jq）
        if command -v jq &> /dev/null; then
            jq '. + {"runtimes": {"runsc": {"path": "/usr/local/bin/runsc"}}}' "$DOCKER_CONFIG" > "${DOCKER_CONFIG}.tmp"
            mv "${DOCKER_CONFIG}.tmp" "$DOCKER_CONFIG"
        else
            echo "警告：未安装 jq，请手动编辑 Docker 配置"
            echo "请在 $DOCKER_CONFIG 中添加以下内容："
            echo '  "runtimes": {"runsc": {"path": "/usr/local/bin/runsc"}}'
        fi
    else
        # 创建新配置
        cat > "$DOCKER_CONFIG" << 'EOF'
{
  "runtimes": {
    "runsc": {
      "path": "/usr/local/bin/runsc"
    }
  }
}
EOF
        echo "已创建 Docker 配置文件"
    fi
fi

echo ""
echo "=== 步骤 3: 重启 Docker 服务 ==="
systemctl restart docker
echo "Docker 服务已重启"

echo ""
echo "=== 步骤 4: 验证安装 ==="

# 验证 runsc 版本
echo "runsc 版本:"
runsc --version

# 测试运行
echo ""
echo "测试 gVisor 容器..."
if docker run --rm --runtime=runsc hello-world > /dev/null 2>&1; then
    echo "✅ gVisor 安装成功！"
else
    echo "⚠️ gVisor 容器测试失败，请检查配置"
    exit 1
fi

echo ""
echo "=== 安装完成 ==="
echo ""
echo "现在可以使用 gVisor 运行容器："
echo "  docker run --runtime=runsc your-image"
echo ""
echo "要在 KernelDock 中启用 gVisor，设置环境变量："
echo "  SANDBOX_SECURITY__USE_GVISOR=true"
echo ""
