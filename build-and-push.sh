#!/bin/bash
# 构建并推送 Docker 镜像

set -e

# 配置
IMAGE_NAME="kerneldock"
VERSION="${1:-latest}"
REGISTRY="${DOCKER_REGISTRY:-}"  # 可选：docker.io/username 或私有仓库地址

echo "=== Building KernelDock ==="
echo "Version: $VERSION"

# 构建镜像
echo "Building Docker image..."
docker build -t ${IMAGE_NAME}:${VERSION} .

# 如果指定了仓库，则标记并推送
if [ -n "$REGISTRY" ]; then
    FULL_IMAGE="${REGISTRY}/${IMAGE_NAME}:${VERSION}"
    
    echo "Tagging image as ${FULL_IMAGE}..."
    docker tag ${IMAGE_NAME}:${VERSION} ${FULL_IMAGE}
    
    echo "Pushing to registry..."
    docker push ${FULL_IMAGE}
    
    echo "=== Done ==="
    echo "Image pushed: ${FULL_IMAGE}"
else
    echo "=== Done ==="
    echo "Image built: ${IMAGE_NAME}:${VERSION}"
    echo ""
    echo "To push to Docker Hub:"
    echo "  export DOCKER_REGISTRY=your-username"
    echo "  ./build-and-push.sh ${VERSION}"
fi
