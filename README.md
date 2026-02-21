# 🚀 AI Agent Code Executor (智能体专属代码沙箱)

[![Docker Build](https://img.shields.io/badge/docker-build-blue.svg)](https://docs.docker.com/engine/reference/commandline/build/)
[![Python Requirements](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

专为 **大语言模型 (LLM) 和 AI Agent** 数据分析场景打造的轻量化、即用即毁型 (Fire-and-Forget) 无状态 Python 代码执行沙箱。它是沉重的 Jupyter Kernel 架构的极佳替代品，让你的 Agent 具备极速且安全的代码执行、图表捕获以及数据挖掘能力。

---

## 💡 为什么不直接用 Jupyter 或 E2B？

在构建数据分析 Agent 时，你可能会遇到以下痛点：
1. **Jupyter Kernel 太重且不稳定**：为每个用户维护一个长连接的 Kernel 成本极高，容易死锁、出现僵尸进程，并发能力拉胯。
2. **需要繁琐的 Prompt 引导制图**：大模型经常记不住要写 `plt.savefig()`，导致生成的图表拿不到。
3. **商业沙箱（如 E2B）有网络/隐私顾虑**：对于企业内部的私有化部署，数据不能出域。

**我们的代码沙箱完美解决了这些问题：**
* ⚡ **极简的架构与高并发**：利用单实例 Docker + 内部 TCP 全局保活命名空间，同样的宿主机资源能支撑比 Jupyter 高 3-5 倍的并发请求。
* 📊 **“读懂”数据的原生捕获**：沙箱在底层劫持了 Matplotlib/Seaborn 等库的输出，不管 LLM 怎么写，自动提取 SVG/PNG 图表以及 JSON 表格，无需严苛复杂的 Prompt 指令。
* 🔒 **私有化与完全掌控**：纯 Docker 容器部署，支持灵活扩展，让数据 100% 留在你的服务器上。

---

## ✨ 核心特性

- 🐍 **原生 Python 科学计算**：开箱即用集成 Pandas, NumPy, Matplotlib, Seaborn 等数据科学栈。
- 📊 **自动视觉输出提取**：利用底层拦截自动捕获代码生成的图表并将其 base64 化，省去大模型对保存路径的心智负担。
- 📁 **本地数据集预加载 (Context Caching)**：可以提前加载文件至目录，代码直接享有 `df` (DataFrame) 全局变量，省去每次重新载入的时间。
- 🔄 **即用即毁，干净无残留**：隔离的用户 Session，支持一键清空和状态恢复。
- 🛡️ **安全阻断**：严格的超时中断机制（Signal + Thread 双重防线），防止死循环。
- ⚡ **智能排队与并发控制**：基于令牌桶的执行队列，自动限制真实并发数（匹配 CPU 核心数），避免资源竞争导致的性能退化。
- 📡 **实时排队状态推送**：通过 WebSocket 实时推送排队位置和预估等待时间，让用户清楚知道"前方还有几人，预计等待多久"。

---

## 🚀 快速开始

本项目推荐直接使用 Docker 部署，因为沙箱的执行强依赖于独立的容器环境以实现真正的隔离。

### 方式一：Docker Compose（推荐）

```bash
# 仅仅一行命令启动
docker-compose up -d --build
```
服务将在本地 `http://localhost:8080` 启动，并暴露出完整的 REST API。

# 1. 查看配置示例
cat code-executor-service/.env.example

# 2. 创建自定义配置（可选）
cp code-executor-service/.env.example code-executor-service/.env
# 编辑 .env 文件调整参数

# 3. 重启服务应用配置
cd code-executor-service
docker-compose down
docker-compose up -d

# 4. 查看日志
docker-compose logs -f code-executor


### 方式二：纯 Docker 运行

```bash
# 构建沙箱的基础镜像
docker build -t code-executor-service:latest .

# 运行容器
docker run -d \
  -p 8080:8080 \
  --name code-executor \
  --security-opt no-new-privileges \
  code-executor-service:latest
```

---

## 📖 API 接口参考

沙箱暴露标准的 RESTful API，完美契合 LangChain、AutoGen 或者你手写的 Agent Orchestrator。 

> 💡 **Tip:** 沙箱本质上使用 `session_id` 作为空间隔离标识。

### 1. 健康检查

```http
GET /health
```
**响应:** `{"status": "ok", "version": "1.0.0"}`

### 2. 创建一个隔离的执行环境（Session）

```http
POST /sessions
Content-Type: application/json

{
  "session_id": "usr_abc123" // 不传则系统自动生成 UUID
}
```

### 3. 为沙箱上传数据（挂载上下文）

可以直接将 CSV/Excel 上传给沙箱，沙箱环境里会自动出现名为 `df` 的全局 DataFrame 变量供 AI 使用。

```http
POST /sessions/{session_id}/upload
Content-Type: multipart/form-data
Body:
  file: <二进制文件>
  filename: "sales_data.csv"
```

### 4. 🧠 核心：执行 Python 代码

运行 AI 生成的代码。这段代码享有之前的环境上下文，不需要反复执行繁琐的 `import pandas` 操作。

```http
POST /sessions/{session_id}/execute
Content-Type: application/json

{
  "code": "print(df.describe())\nplt.plot(df['date'], df['sales'])\nplt.title('Sales Trend')",
  "timeout": 30
}
```

**绝佳返回示例：**
```json
{
  "success": true,
  "stdout": "           sales\ncount  100.000000\nmean    50.500000\n...",
  "stderr": "",
  "output": "           sales\ncount  100.000000\nmean    50.500000\n...",
  "charts": [
    {
      "format": "svg",
      "base64": "PHN2ZyB4bWxuc... (省略极长的图表编码)",
      "path": null
    }
  ],
  "tables": [],
  "execution_time_ms": 142
}
```
*注意：你看，大模型根本没写图表保存的代码，但图表照样被拦截进了 `charts` 数组中返回给前端！*

### 5. 清理 Session

```http
DELETE /sessions/{session_id}
```

---

## 🚦 智能排队与并发控制

### 为什么需要排队机制？

在高并发场景下，如果所有请求同时执行 Python 代码，会导致：
- **CPU 上下文切换开销剧增**：过多的并发进程争抢 CPU 资源
- **内存压力过大**：每个执行环境都需要加载数据和库
- **整体性能下降**：反而比限制并发更慢

我们的解决方案：**基于令牌桶的执行队列**

### 工作原理

```
请求1 ──┐
请求2 ──┤
请求3 ──┼──> [执行队列] ──> [令牌桶: 4个并发槽] ──> 执行
请求4 ──┤                    ├─ 槽1: 执行中
请求5 ──┘                    ├─ 槽2: 执行中
                              ├─ 槽3: 执行中
                              └─ 槽4: 执行中
```

- **自动限流**：最大并发数 = CPU 核心数（可配置）
- **公平排队**：先到先服务（FIFO）
- **实时反馈**：通过 WebSocket 推送排队位置和预估等待时间

### 配置并发数

在 `.env` 文件中配置：

```bash
# 最大并发执行数（建议 = CPU 核心数）
MAX_CONCURRENT_EXECUTIONS=4

# 平均执行时间估算（秒，用于计算预估等待时间）
AVG_EXECUTION_TIME=5.0

# 排队超时时间（秒）
QUEUE_TIMEOUT=300
```

### WebSocket 实时排队状态

使用 WebSocket 连接可以实时接收排队状态更新：

```javascript
// 连接 WebSocket
const ws = new WebSocket('ws://localhost:8080/ws');

ws.onopen = () => {
  // 发送执行请求
  ws.send(JSON.stringify({
    type: 'request',
    id: 'req_123',
    action: 'execute',
    data: {
      session_id: 'usr_abc123',
      code: 'print(df.head())',
      timeout: 30
    }
  }));
};

ws.onmessage = (event) => {
  const message = JSON.parse(event.data);
  
  if (message.type === 'queue_status') {
    // 实时排队状态更新
    const { position, estimated_wait_seconds, status, message: msg } = message.data;
    
    if (status === 'queued') {
      console.log(`排队中：前方还有 ${position} 个任务，预计等待 ${estimated_wait_seconds} 秒`);
      // 更新 UI 显示排队进度
    } else if (status === 'executing') {
      console.log('开始执行代码');
    }
  } else if (message.type === 'response') {
    // 执行结果
    console.log('执行完成:', message.data);
  }
};
```

### 查询全局队列状态

```http
GET /queue/status
```

**响应示例：**
```json
{
  "queued_count": 3,
  "executing_count": 4,
  "max_concurrent": 4,
  "avg_execution_time": 5.2,
  "total_enqueued": 127,
  "total_executed": 124,
  "total_timed_out": 0
}
```

### 性能优化建议

**4C8G 服务器配置示例：**
- `MAX_CONCURRENT_EXECUTIONS=4`（匹配 CPU 核心数）
- 前 4 个请求：立即执行（<1秒）
- 第 5+ 个请求：自动排队，实时推送位置
- 预估等待时间：基于历史执行时间的指数移动平均

**8C16G 服务器配置示例：**
- `MAX_CONCURRENT_EXECUTIONS=8`
- 可同时处理 8 个代码执行请求
- 更高的吞吐量，更短的排队时间

---

## 💻 Python 客户端 SDK (以 Agent 调用为例)

为了方便集成到你自己的 AI 项目中，我们提供了一个极其顺手的 Async Python 客户端。

```python
import asyncio
from client import CodeExecutorClient

async def run_data_analysis_agent():
    # 连接到你部署的沙箱服务
    async with CodeExecutorClient("http://localhost:8080") as executor:
        session_id = "agent_demo_01"
        await executor.create_session(session_id)
        
        # 1. 向沙箱喂入源数据
        with open("company_revenue.csv", "rb") as f:
            await executor.upload_file(session_id, f.read(), "company_revenue.csv")
        
        # 2. 假设这是 LLM 生成的分析代码
        llm_code = """
import seaborn as sns
# 注意：df 大图变量已经在内存里了，无需 pd.read_csv!
top_regions = df.groupby('Region')['Revenue'].sum().reset_index()
sns.barplot(data=top_regions, x='Region', y='Revenue')
        """
        
        # 3. 提交给沙箱安全执行
        result = await executor.execute_code(session_id, llm_code, timeout=60)
        
        if result.success:
            print("控制台输出:", result.output)
            print(f"Agent 一共绘制了 {len(result.charts)} 张图表！")
            
            # 你可以直接把 result.charts[0].base64 渲染到前端给用户看
        else:
            print("代码报错啦！快让 LLM 修复：", result.error)

if __name__ == "__main__":
    asyncio.run(run_data_analysis_agent())
```

---

## 🛠️ 进阶系统架构说明

1. **FastAPI (Control Plane)**: 外层作为一个控制平面，负责接收 HTTP 请求，调度和维护分配各个运行的 Worker 空间。它把文件暂存在本地。
2. **Kernel Server (Data Plane)**: 我们不是像 E2B 那样每个请求开个虚拟机（太慢），也不是直接用 OS 执行（太危险）。我们在容器内部常驻了一个轻量级的 TCP `kernel_server.py`。
3. **命名空间劫持方案**：当 `/execute` 指令下达，系统会将代码利用 `exec()` 送入全局隔离的 Python 命名空间。这里提前埋伏了被劫持版本的 `matplotlib` 及 `IPython.display` 指令。任何绘图操作都会被转录成 Base64 SVG 直接通过 TCP Socket 返回。

---

## 🔒 生产环境安全指北 (Security)

> ⚠️ 这个服务允许执行 **任意的 Python 代码**！虽然运行在 Docker 中，但是如果你将它暴露在非常开阔的网络体系下依然会有风险。

当推向公网生产环境时，强烈建议你进行以下加固：
1. **彻底阻断外网**: 强制禁止此 Docker 容器能请求公网 IP（防止 LLM 写的代码发送内网敏感数据出去，或变身为 DDoS 节点）。
2. **算力熔断限制**: 在部署（K8s / docker-compose）时设定严格的 `cpu_quota` 以及 `mem_limit` (例如：限制 512MB RAM，超过该值直接被 cgroup kill 掉)。
3. **替换为 gVisor/runsc 运行时**: 更进一步杜绝极其罕见的容器内核逃逸。可以配置通过 `gVisor` 接管它的运行隔离。

---

## 📄 License
MIT License. 欢迎随时 PR 贡献！你的 Star 是对项目最大的支持！
