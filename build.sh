#!/usr/bin/env bash

set -euo pipefail

BASE_IMAGE="${BASE_IMAGE:-kerneldock-sandbox-base:latest}"
APP_IMAGE="${APP_IMAGE:-kerneldock-sandbox:v2.0.0}"
USE_CHINA_MIRROR="${USE_CHINA_MIRROR:-1}"
MAX_APP_IMAGE_MB="${MAX_APP_IMAGE_MB:-3072}"

image_size_mb() {
  local image="$1"
  local size_bytes
  size_bytes="$(docker image inspect "$image" --format '{{.Size}}')"
  echo $((size_bytes / 1024 / 1024))
}

build_base() {
  docker build \
    --build-arg USE_CHINA_MIRROR="${USE_CHINA_MIRROR}" \
    -f Dockerfile.base \
    -t "${BASE_IMAGE}" \
    .
}

build_app() {
  docker build \
    --build-arg USE_CHINA_MIRROR="${USE_CHINA_MIRROR}" \
    --build-arg BASE_IMAGE="${BASE_IMAGE}" \
    -f Dockerfile.sandbox \
    -t "${APP_IMAGE}" \
    .
}

report_sizes() {
  local base_mb
  local app_mb
  base_mb="$(image_size_mb "${BASE_IMAGE}")"
  app_mb="$(image_size_mb "${APP_IMAGE}")"

  echo "base_image=${BASE_IMAGE} size_mb=${base_mb}"
  echo "app_image=${APP_IMAGE} size_mb=${app_mb}"

  if [ "${app_mb}" -gt "${MAX_APP_IMAGE_MB}" ]; then
    echo "app image exceeds ${MAX_APP_IMAGE_MB}MB: ${app_mb}MB" >&2
    exit 1
  fi
}

main() {
  local target="${1:-all}"
  case "${target}" in
    base)
      build_base
      ;;
    app)
      build_app
      report_sizes
      ;;
    all)
      build_base
      build_app
      report_sizes
      ;;
    *)
      echo "usage: $0 [base|app|all]" >&2
      exit 1
      ;;
  esac
}

main "$@"
