# Code Executor Service (沙箱代码执行服务)

这是一款高性能、无状态的 Python 代码执行服务，专门为数据分析、科学计算和数据可视化场景设计。它采用 Docker + gVisor 架构，提供生产级别的安全隔离性能。

## 🌟 核心特性

- **� 零延迟启动**: 优化的沙箱池机制，秒级创建执行环境。
- **�️ 深度安全加固**:
  - **gVisor (runsc)**: 采用用户态内核隔离，防止容器逃逸。
  - **AST 静态分析**: 预检代码风险，禁止危险模块（`os`, `subprocess`, `socket` 等）。
  - **资源限制**: 严格限制 CPU、内存、磁盘和进程数（PID limit）。
  - **网络隔离**: 默认禁用出站网络，防止数据泄漏。
- **📊 智能数据分析**:
  - **自动加载**: 识别并自动加载 `/data` 目录下的 CSV/Excel 文件。
  - **图表捕获**: 自动拦截并转换 matplotlib/seaborn 图像为 SVG/Base64。
  - **表格预览**: 支持 pandas DataFrame 分页预览。
- **📦 内置运行时 (sandbox_runtime)**: 预装丰富的数据分析库（pandas, numpy, scikit-learn, plotly 等）。

---

## 🏗️ 架构概览

```
                            [ API Client ]
                                  │
                                  ▼
                    ┌───────────────────────────┐
                    │   Code Executor (FastAPI)  │
                    └─────────────┬─────────────┘
                                  │
                  ┌───────────────┴───────────────┐
                  │        Sandbox Manager        │
                  │ (Docker + gVisor + AST Check) │
                  └───────────────┬───────────────┘
                                  │
            ┌─────────────────────┼─────────────────────┐
            ▼                     ▼                     ▼
     ┌───────────────┐     ┌───────────────┐     ┌───────────────┐
     │  Sandbox (1)  │     │  Sandbox (2)  │     │  Sandbox (n)  │
     │ [ gVisor Kernel]    │ [ gVisor Kernel]    │ [ gVisor Kernel]
     │ [ Python App  ]    │ [ Python App  ]    │ [ Python App  ]
     └───────────────┘     └───────────────┘     └───────────────┘
```

---

## 🚦 快速启动

### 1. 本地开发模式 (Windows / macOS / Linux)

本地模式下不涉及 gVisor，使用 standard Docker 运行时。

```bash
# 1. 构建镜像
docker build -t code-executor-sandbox:latest -f Dockerfile.sandbox .
docker build -t code-executor-service:latest .

# 2. 启动服务 (使用 docker-compose)
docker-compose up -d
```

### 2. 生产安全模式 (Linux / WSL2)

在 Linux 环境下，您可以启用 gVisor 提供硬件级隔离。

#### 步骤 A: 安装 gVisor (runsc)

gVisor 是谷歌开发的内核隔离技术。你可以根据操作系统选择以下安装方式：

##### 1. 在 Linux 或 WSL2 (Ubuntu) 上安装
我们提供了自动化安装脚本，会自动下载 `runsc` 二进制文件并配置 Docker：

```bash
# 1. 如果你在 Windows 上，请先进入 WSL (例如 Ubuntu)
# 2. 切换到脚本目录
cd scripts/

# 3. 运行安装脚本
chmod +x install_gvisor.sh
sudo ./install_gvisor.sh
```

##### 2. Windows 特有步骤 (使用 Docker Desktop)
如果脚本执行后 `systemctl restart docker` 报错（这在 Docker Desktop WSL 后端很常见），请进行手动配置：

1.  **打开 Docker Desktop 设置**: 点击右上角齿轮图标 -> `Docker Engine`。
2.  **添加运行时配置**: 在 JSON 配置中添加 `runtimes` 节点：
    ```json
    {
      "runtimes": {
        "runsc": {
          "path": "/usr/local/bin/runsc"
        }
      }
    }
    ```
3.  **应用并重启**: 点击 `Apply & Restart`。

##### 3. 验证安装
在终端（WSL 或 Linux）运行以下命令：
```bash
docker run --rm --runtime=runsc hello-world
```
如果输出 `Hello from Docker!`，说明配置成功。

#### 步骤 B: 启用配置
修改 `.env` 文件或设置环境变量：
```bash
# 启用 gVisor
SANDBOX_SECURITY__USE_GVISOR=true
```

#### 步骤 C: 部署服务
```bash
docker-compose up -d
```

---

## 🛠️ 环境配置

| 变量名 | 默认值 | 说明 |
|------|--------|------|
| `SANDBOX_DOCKER_IMAGE` | `code-executor-sandbox:latest` | 执行环境镜像名称 |
| `SANDBOX_SECURITY__USE_GVISOR` | `false` | 是否开启 gVisor 隔离 |
| `SANDBOX_RESOURCE__DEFAULT_MEMORY_MB` | `512` | 单个沙箱默认内存限制 |
| `SANDBOX_TIMEOUT__EXECUTION_TIMEOUT` | `300` | 单次执行超时时间 (秒) |

---

## 📖 API 接口摘要

### 代码执行
`POST /sessions/{session_id}/execute`
- **输入**: `{ "code": "import pandas as pd; print(df.head())" }`
- **输出**: 包含 `stdout`, `stderr`, `charts` (SVG), `tables` (JSON) 等。

### 文件管理
- `POST /sessions/{session_id}/upload`: 上传数据文件。
- `GET /sessions/{session_id}/files`: 查看沙箱内生成的文件。

---

## 🧪 测试

```bash
# 运行单元测试
pytest tests/

# 运行安全检查器测试
python test_validator_quick.py
```

## 📜 License

MIT
