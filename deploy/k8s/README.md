# K8s 部署（DinD sidecar 模式）

两种形态二选一：

| 文件 | 形态 | 适用 |
|---|---|---|
| `kerneldock.yaml` | 单副本 Deployment | 单节点够用、求最简 |
| `kerneldock-cluster.yaml` | router + 多节点 StatefulSet（分布式 Phase 3） | 水平扩展沙箱并发 |

## 为什么是 DinD

网关通过 Docker API 管理沙箱容器。在 containerd 集群上节点没有 docker.sock，
因此把一个 `docker:dind` 作为 sidecar 放进网关 Pod：

- 沙箱容器全部运行在 DinD 内，Pod 即完整隔离域，删除 Pod 即回收一切
- 不依赖、也不暴露节点容器运行时
- 代价：DinD 需要 `privileged: true`，且沙箱算力受 Pod limits 约束

## 镜像准备（两种形态通用）

```bash
docker build -t <registry>/kerneldock:latest .
docker build -f Dockerfile.base -t kerneldock-sandbox-base:latest .
docker build --build-arg BASE_IMAGE=kerneldock-sandbox-base:latest \
  -f Dockerfile.sandbox -t <registry>/kerneldock-sandbox:v2.0.0 .
docker build -f Dockerfile.router -t <registry>/kerneldock-router:latest .   # 仅集群形态需要
docker push <registry>/kerneldock:latest
docker push <registry>/kerneldock-sandbox:v2.0.0
docker push <registry>/kerneldock-router:latest
```

DinD 首次启动后需要拉取沙箱镜像（配置 `SANDBOX_DOCKER_IMAGE` 为 registry
地址即可自动拉取）；Pod 重建后 `dind-storage` 为 emptyDir 会重新拉取，
介意冷启动可改为 PVC。

## 形态一：单副本（kerneldock.yaml）

```bash
# 改镜像地址与 SANDBOX_API_KEYS 后
kubectl apply -f deploy/k8s/kerneldock.yaml
kubectl -n kerneldock port-forward svc/kerneldock 9527:9527
curl http://localhost:9527/health
```

## 形态二：分布式集群（kerneldock-cluster.yaml）

```bash
# 改三个镜像地址与 Secret 两个默认值后
kubectl apply -f deploy/k8s/kerneldock-cluster.yaml
kubectl -n kerneldock port-forward svc/kerneldock-router 9500:9500
curl http://localhost:9500/health        # 聚合健康（nodes_healthy 应为 2）
```

工作机制：节点 pod（`kerneldock-node-0/1/...`）启动即向 router 自注册
（Phase 2 心跳机制），pod 名即节点名、即资源 ID 前缀（`kerneldock-node-0:uuid`）。

- **加节点**：`kubectl -n kerneldock scale statefulset kerneldock-node --replicas=N`，
  新 pod 起来自动入集群，零配置
- **缩节点**：scale 下去即可，心跳断 `ROUTER_NODE_TTL`（30s）后 router 自动摘除；
  该节点上的会话按"节点宕机"语义丢失（客户端重建）
- **router 扩副本**：`scale statefulset kerneldock-router --replicas=2` 后，把
  `kerneldock-router-1...` 的 pod DNS 追加到 node ConfigMap 的 `ROUTER_URL`
  （逗号分隔）并 rollout 节点——每个 router 副本独立维护节点表，心跳必须发给每个副本
- **集群成员查询**：`GET /admin/nodes`（带 `X-Admin-Token`）

## 已知限制

- `privileged` DinD：若集群安全策略禁止，替代路线是 Sysbox runtime
  （免 privileged 的 DinD）或把沙箱层换成 Kata Containers
- gVisor 在 DinD 内不可用（需要节点级 runsc），K8s 形态下的强隔离
  建议直接上 Kata/节点池隔离
- 集群形态下节点用 `SANDBOX_KERNEL_TRANSPORT=relay`（网关容器不归 DinD
  daemon 管，direct 的"网关自连沙箱网络"不可行）；DinD 内 relay 即物理禁网语义
- 运行时 pip 装包（egress proxy）未包含在 K8s manifest 中，需要的话参照
  compose 的 egress profile 自行补 tinyproxy Deployment + 白名单 ConfigMap
