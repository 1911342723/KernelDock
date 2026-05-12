# Code Executor Service

给 LLM / AI Agent 用的 Python 代码沙箱。接收代码，在 Docker 容器里跑，返回 stdout、图表（自动拦截 matplotlib）、表格数据。不需要 Jupyter，不需要 E2B，一个 `docker compose up` 就完事。

核心卖点：
- 容器内常驻 Kernel Server（TCP），变量在多轮对话间保持，不用每次重新 import
- 底层劫持 matplotlib/seaborn 输出，LLM 不写 `savefig` 也能拿到图
- 预热容器池，无状态请求从池里借容器执行完归还，冷启动零感知
- AST 静态分析 + 只读 rootfs + network=none + 非 root 用户，四层隔离

---

## 部署

### Docker Compose（推荐）

```bash
# 1. 构建 base + app 双层镜像
./build.sh all

# 或分开构建
./build.sh base
./build.sh app

# 2. 启动服务
docker compose up -d --build
```

双层镜像约定：

- `Dockerfile.base`：Ubuntu 24.04 + uv + Python 3.11 + 系统依赖
- `Dockerfile.sandbox`：基于 base 镜像安装 `requirements.lock` 并复制 `sandbox_runtime`

默认沙箱镜像标签是 `code-executor-sandbox:v2.0.0`，可通过环境变量 `SANDBOX_DOCKER_IMAGE` 覆盖。

服务跑在 `http://localhost:8080`。网关进程会自动拉起 6 个预热沙箱容器。

### 关键配置

在 `docker-compose.yml` 的 `environment` 里改。核心参数：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SANDBOX_POOL__POOL_SIZE` | 6 | 预热容器数，决定并发上限 |
| `SANDBOX_QUEUE__MAX_CONCURRENT_EXECUTIONS` | 6 | 信号量大小，应等于 pool_size |
| `SANDBOX_RESOURCE__DEFAULT_MEMORY_MB` | 256 | 每个沙箱内存限制 |
| `SANDBOX_RESOURCE__DEFAULT_CPU` | 1.0 | 每个沙箱 CPU 限制 |
| `SANDBOX_TIMEOUT__EXECUTION_TIMEOUT` | 300 | 代码执行超时（秒） |
| `CORS_ALLOWED_ORIGINS` | 空(=*) | 生产环境填实际域名，逗号分隔 |

4C8G 机器建议 pool_size=6，每个沙箱 256MB，总占约 1.5GB。

### 生产安全

- 网关挂载了 Docker socket，生产环境建议用 [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy) 限制 API 面
- 沙箱容器已经做了 network=none + read-only rootfs + 非 root，想更硬可以换 gVisor 运行时
- `CORS_ALLOWED_ORIGINS` 务必设置为实际域名

---

## API

### 系统

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查，返回池状态和资源占用 |
| GET | `/metrics` | Prometheus 格式指标 |
| GET | `/statistics` | 全局统计（沙箱数、执行数、队列状态） |
| GET | `/queue/status` | 排队队列实时状态 |
| POST | `/cleanup` | 清理过期会话 |

### 无状态执行（推荐，用于 Agent 场景）

**POST /execute**

从容器池借一个容器，执行完归还。无需管理 session。

```json
// 请求
{
  "code": "import pandas as pd\ndf = pd.DataFrame({'a':[1,2,3]})\nprint(df.describe())",
  "timeout": 30,
  "data_files": {                    // 可选，base64 编码的数据文件
    "sales.csv": "YSxiLGMKMSwyLDMK..."
  }
}
```

```json
// 响应
{
  "success": true,
  "output": "         a\ncount  3.0\nmean   2.0\n...",
  "stdout": "...",
  "stderr": "",
  "charts": [{"format": "svg", "base64": "PHN2Zy...", "path": null}],
  "tables": [{"id": "...", "name": "...", "columns": [...], "data": [...]}],
  "images": [],
  "error": null,
  "queue_info": {
    "position_on_entry": 1,
    "waited_seconds": 0.0,
    "estimated_wait_seconds": 3.5,
    "queue_depth": 0,
    "executing_count": 1,
    "max_concurrent": 6,
    "avg_execution_time": 3.5,
    "total_enqueued": 42,
    "total_executed": 41
  },
  "sandbox_info": {
    "mode": "stateless_pool_kernel",
    "pool_available": 5,
    "pool_total": 6
  },
  "execution_info": {
    "execution_time_ms": 142,
    "execution_path": "stateless_pool_kernel",
    "code_size_bytes": 78,
    "timeout_configured": 30,
    "timed_out": false,
    "chart_count": 1,
    "table_count": 0,
    "output_truncated": false,
    "output_size_bytes": 320
  }
}
```

### 有状态会话

用于需要多轮交互、变量跨轮保持的场景。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/sessions` | 创建会话。body: `{"session_id": "可选"}` |
| GET | `/sessions/{id}` | 查看会话信息 |
| DELETE | `/sessions/{id}` | 销毁会话和关联沙箱 |
| POST | `/sessions/{id}/execute` | 执行代码。body: `{"code": "...", "timeout": 300}` |
| POST | `/sessions/{id}/upload` | 上传文件（multipart/form-data） |
| POST | `/sessions/{id}/load-data` | 加载 JSON 数据。body: `{"data_json": "...", "filename": "data.csv"}` |
| GET | `/sessions/{id}/schemas` | 获取已加载表的 schema |
| GET | `/sessions/{id}/context` | 多表上下文（列名、行数、可能的 join 列） |
| GET | `/sessions/{id}/files` | 列出数据/输出文件 |
| GET | `/sessions/{id}/files/{type}/{name}` | 下载指定文件 |

`/sessions/{id}/execute` 的响应格式和 `/execute` 完全一致。

### 沙箱管理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/sandboxes` | 列出所有活跃沙箱，支持 `?state=running` 过滤 |
| GET | `/sandboxes/{id}` | 查看单个沙箱详情 |
| DELETE | `/sandboxes/{id}` | 强制销毁沙箱 |
| GET | `/sandboxes/{id}/metrics` | 沙箱级资源用量（CPU/内存/网络） |

---

## 响应结构说明

每次代码执行返回三块附加信息：

**queue_info** -- 排队状态

| 字段 | 类型 | 说明 |
|------|------|------|
| position_on_entry | int | 入队时排第几 |
| waited_seconds | float | 实际等了多久 |
| estimated_wait_seconds | float | 入队时的预估等待 |
| queue_depth | int | 当前排队人数 |
| executing_count | int | 正在执行的数量 |
| max_concurrent | int | 并发上限 |
| avg_execution_time | float | 滑动平均执行耗时（秒） |

**sandbox_info** -- 沙箱状态

| 字段 | 类型 | 说明 |
|------|------|------|
| mode | string | 执行模式：`stateless_pool_kernel` / `sandbox_kernel` / `local_subprocess` |
| sandbox_id | string? | 沙箱 ID（有状态模式下有值） |
| container_id_short | string? | 容器 ID 前 12 位 |
| state | string? | 沙箱状态（running/stopped/error） |
| cpu_limit / memory_limit_mb | number? | 资源限制 |
| pool_available / pool_total | int | 容器池可用数/总数 |

**execution_info** -- 执行细节

| 字段 | 类型 | 说明 |
|------|------|------|
| execution_time_ms | int | 执行耗时（毫秒） |
| execution_path | string | 执行走的路径 |
| code_size_bytes | int | 提交代码大小 |
| timeout_configured | int | 配置的超时值 |
| timed_out | bool | 是否超时 |
| chart_count / table_count | int | 图表/表格数量 |
| output_truncated | bool | 输出是否被截断（>100KB） |

---

## 架构

```
Client (LLM Agent)
  |
  v  HTTP
+-------------------+
| FastAPI Gateway    |  控制面：路由、队列、会话管理
| (code-executor)    |
+-------------------+
  |
  |  docker exec + TCP :9999
  v
+-------------------+     +-------------------+
| Sandbox Container |     | Sandbox Container |  x N（预热池）
| kernel_server.py  | ... | kernel_server.py  |
| Python namespace  |     | Python namespace  |
+-------------------+     +-------------------+
```

- Gateway 通过 Docker socket 管理沙箱容器的生命周期
- 每个沙箱内跑一个 `kernel_server.py`，监听 TCP 9999，保持 Python namespace 常驻
- 代码执行通过 `docker exec` 运行中继脚本连接容器内 kernel，kernel 不可达时回退到 `docker exec python` 直接执行
- 无状态模式从预热池借容器，执行完清理 namespace 后归还

---

## 测试

```bash
# 集成测试（验证所有响应字段）
python tests/test_enriched_response.py --url http://localhost:8080

# 并发压力测试
python tests/stress_test.py --url http://localhost:8080 --scenario all -c 10 -n 30
```

---

## License

MIT
