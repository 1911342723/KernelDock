# KernelDock 分布式部署设计

> 目标：多台服务器水平扩展沙箱并发，加节点操作尽量简单。
> 状态：compose 双节点路由、节点自注册/心跳/TTL 摘除、K8s 集群形态均已落地
> （随附端到端测试 `tests/router_e2e.py`、`tests/router_phase2_e2e.py`）。
> 真实大规模 K8s 集群实测留作后续。

## 1. 现状评估：哪些状态把服务钉在单机上

| 状态 | 位置 | 分布式影响 |
|---|---|---|
| 会话沙箱容器 | 节点本地 Docker | **硬约束**：会话被钉在创建它的节点，跨节点不可见 |
| `SandboxManager._sandboxes` | 进程内存 dict | 同上，与容器同生命周期 |
| `SessionStore` | 节点本地 SQLite | 单机重启恢复可用，跨机不可用 |
| `ContainerPool` | 节点本地 Docker | 池容量天然 per-node，无需改动 |
| `JobManager` | 纯内存 | job 结果只存在于执行节点，轮询必须回到同一节点 |
| `ExecutionQueue` / 限流桶 | 进程内 | per-node 语义本来就正确，无需改动 |
| WebSocket `/ws/{session_id}` | 进程内连接 | 需要与会话相同的粘性路由 |

核心结论：**会话 = 节点本地容器**，分布式的本质不是共享状态，而是"把请求路由到拥有该资源的节点"。无状态执行（`/execute`、`/execute/shell`、无 session 的 `/jobs`）天然可分散——这正是并发扩容的主要收益来源（单机 4C8G 实测 51 req/s，N 台 ≈ N×51）。

## 2. 方案对比

### A. 前置一致性哈希（nginx/traefik 按 path 参数 hash）
现成组件，但有结构性缺陷：节点增减时哈希环重排，存量会话被路由到错误节点直接 404；无状态请求也被钉死，失去按负载调度的能力；`job_id` 与 session 无关联，jobs 无法用同一规则路由；没有聚合健康/指标。**否决**。

### B. 中心化共享状态（Redis/etcd 存 session→node 映射）
路由准确、可做故障转移，但引入外部依赖与运维面，违背本项目"小型服务器、零外部服务依赖、操作简单"的定位；且会话本体（容器）本来就不可迁移，共享映射表的收益撑不起成本。**否决**（Phase 4 若有真实需求再评估）。

### C. 资源 ID 前缀路由（推荐）
新增轻量 `kerneldock-router`（FastAPI + httpx 反向代理，复用现有技术栈）：

- 创建类响应中的资源 ID 改写为带节点前缀：`session_id = "n2:{uuid}"`、`job_id = "n1:job-xxx"`、`sandbox_id` 同理；
- 后续所有带 ID 的请求解析前缀直达对应节点（代理时剥掉前缀）；
- ID 对客户端本来就是不透明字符串（SDK/MCP/E2B 适配层都不解析其内部结构），改写完全透明；
- **router 因此完全无状态**：不存映射表、不依赖外部存储，多开两个实例 + DNS/VIP 即高可用；
- 节点增减不影响存量会话（前缀指名道姓，不靠哈希环）。

代价：router 需要对创建类端点做 JSON 响应重写（仅 4~5 个端点）；流式/WS 端点按前缀透传。**采纳**。

### D. K8s 多副本
复用 C 的 router 作为 Deployment、节点变 StatefulSet（pod 名即节点 ID），调度/自愈交给 K8s。作为 Phase 3 的部署形态，不是独立方案。

## 3. 推荐架构（Phase 1 MVP）

```
              ┌────────────────┐    stateless: 按队列水位加权分发
  client ────▶│ kerneldock-    │───▶ node-1（现有 compose 栈，零改动）
  (SDK/MCP/   │ router(无状态) │───▶ node-2
   E2B 适配)  └────────────────┘───▶ node-N
                带ID请求: 按前缀直达
```

路由规则：

| 请求 | 路由策略 |
|---|---|
| `POST /execute`、`/execute/shell`、`POST /jobs`（无 session） | 健康节点中 pending 最少者（轮询 `/health` + `/queue/status` 缓存 2s） |
| `POST /sessions`、`POST /e2b/sandboxes` | 会话数最少节点；响应 ID 加前缀 |
| `/sessions/{id}/**`、`/jobs/{id}`、`/sandboxes/{id}`、`/e2b/sandboxes/{id}/**`、`/ws/{id}` | 解析前缀直达（WS 透传） |
| `/health`、`/metrics` | 聚合：pool/会话数求和，按 `node` label 区分 |
| `POST /jobs`（带 session_id） | 跟随 session 前缀到同一节点 |

节点侧**零改动**：现有 docker-compose 栈原样跑。router 配置 Phase 1 用静态节点表（`ROUTER_NODES=n1=http://10.0.0.1:9527,n2=http://10.0.0.2:9527`）。

安全：router 对外校验 API key（复用现有 Bearer/X-API-Key 逻辑）；router→node 走内网 + 节点内部 key；节点间无需互通。

## 4. 用户操作面（设计目标）

- 起集群：每台 node `docker compose up -d`（与单机完全一致）；router 机 `docker compose -f docker-compose.router.yml up -d`
- 加节点：新机 `docker compose up -d`，router 的 `ROUTER_NODES` 加一项后 `docker compose restart router`（Phase 2 改为自注册，免这一步）
- 客户端：把 base_url 从节点地址换成 router 地址，其余零感知（MCP server 只改 `KERNELDOCK_URL`）

容量模型（4C8G 实测基线外推）：N 节点 ≈ N×51 req/s 无状态吞吐、N×10 并发会话、N×16 并发执行闸门。router 是纯转发（无重计算），单实例万级 QPS 不构成瓶颈。

## 5. 故障语义（明确承诺，不过度设计）

- **节点宕机**：该节点上的会话/任务丢失（请求 404/503，客户端重建会话——与单机宕机的语义相同，但爆炸半径从 100% 降到 1/N）；无状态流量在下个健康检查周期（≤5s）自动摘除该节点。不做会话跨节点迁移/快照同步（Docker 路线无成熟 CRIU 方案）。
- **router 宕机**：router 无状态 → 文档指导跑双 router + DNS/VIP；不内置选主。
- **脑裂/新旧并存**：ID 前缀指名道姓，不存在两个节点都认为自己拥有同一会话的可能。

## 6. 分阶段落地计划

- **Phase 1（MVP）✅ 2026-06-12**：`router/` 服务（单文件 FastAPI 反代 + 节点健康缓存 + ID 前缀改写 + WS 桥接 + 聚合观测）、`docker-compose.router.yml`、双节点 e2e 16/16 + MCP 经 router 19/19
- **Phase 2（运维性）✅ 2026-06-12**：节点自注册 + 心跳 + TTL 自动摘除（`POST /admin/nodes` 幂等心跳；节点带 `ROUTER_URL` 启动即入集群；静态名不可抢占；`ROUTER_ADMIN_TOKEN` 可选鉴权）；e2e 8/8（含停容器摘除与重启回归）
- **Phase 3（K8s 形态）✅ 2026-06-12**：`deploy/k8s/kerneldock-cluster.yaml`——router StatefulSet（稳定 per-pod DNS、TCP readiness 防零节点死锁）+ node StatefulSet（DinD sidecar、pod 名即节点名、`kubectl scale` 即扩缩容自动注册/摘除）+ 心跳多 router 副本支持；HPA 按队列深度留作后续增强
- **Phase 4（远期，按需）**：跨节点共享数据面（/data 注入走 S3 兼容对象存储）、Redis 会话映射（仅当 ID 前缀方案遇到真实阻碍）
