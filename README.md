# Code Executor Service

无状态 Python 代码执行服务，用于数据分析和可视化。可替代 E2B 沙箱服务。

## 功能特性

- 🐍 Python 代码执行（支持 pandas, numpy, matplotlib, seaborn 等）
- 📊 自动捕获图表（SVG 格式）
- 📋 表格数据捕获和导出
- 📁 文件上传和下载
- 🔄 会话管理（支持多用户）
- 🐳 Docker 部署

## 快速开始

### 本地运行

```bash
# 安装依赖
pip install -r requirements.txt

# 启动服务
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### Docker 运行

```bash
# 构建镜像
docker build -t code-executor-service:latest .

# 运行容器
docker run -d -p 8080:8080 --name code-executor code-executor-service:latest
```

### Docker Compose

```bash
docker-compose up -d
```

## API 接口

### 健康检查
```
GET /health
```

### 会话管理

```bash
# 创建会话
POST /sessions
{
  "session_id": "optional-custom-id"
}

# 获取会话
GET /sessions/{session_id}

# 删除会话
DELETE /sessions/{session_id}
```

### 代码执行

```bash
POST /sessions/{session_id}/execute
{
  "code": "import pandas as pd\nprint('Hello')",
  "timeout": 300
}
```

### 数据加载

```bash
# 加载 JSON 数据
POST /sessions/{session_id}/load-data
{
  "data_json": "[{\"a\": 1}, {\"a\": 2}]",
  "filename": "data.csv"
}

# 上传文件
POST /sessions/{session_id}/upload
Content-Type: multipart/form-data
file: <binary>
filename: data.csv
```

### 文件管理

```bash
# 列出文件
GET /sessions/{session_id}/files

# 下载文件
GET /sessions/{session_id}/files/{data|output}/{filename}

# 获取表格模式
GET /sessions/{session_id}/schemas

# 获取多表格上下文
GET /sessions/{session_id}/context
```

## Python 客户端

```python
from client import CodeExecutorClient

async def main():
    async with CodeExecutorClient("http://localhost:8080") as client:
        # 创建会话
        session = await client.create_session("my-session")
        
        # 上传数据
        with open("data.csv", "rb") as f:
            await client.upload_file("my-session", f.read(), "data.csv")
        
        # 执行代码
        result = await client.execute_code("my-session", """
import pandas as pd
import matplotlib.pyplot as plt

print(df.head())
df.plot(kind='bar')
plt.savefig('chart.png')
""")
        
        print(result.output)
        print(f"Charts: {len(result.charts)}")
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| WORKSPACE_DIR | /workspace | 工作空间目录 |
| PYTHONUNBUFFERED | 1 | Python 输出不缓冲 |

## 推送到 Docker Hub

```bash
# 登录
docker login

# 标记镜像
docker tag code-executor-service:latest your-username/code-executor-service:latest

# 推送
docker push your-username/code-executor-service:latest

#启动
docker run -d -p 8080:8080 --name code-executor code-executor-service:latest
```

## 在 Kubernetes 中部署

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: code-executor
spec:
  replicas: 2
  selector:
    matchLabels:
      app: code-executor
  template:
    metadata:
      labels:
        app: code-executor
    spec:
      containers:
      - name: code-executor
        image: your-username/code-executor-service:latest
        ports:
        - containerPort: 8080
        resources:
          limits:
            cpu: "2"
            memory: "4Gi"
          requests:
            cpu: "500m"
            memory: "512Mi"
        livenessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 10
          periodSeconds: 30
---
apiVersion: v1
kind: Service
metadata:
  name: code-executor
spec:
  selector:
    app: code-executor
  ports:
  - port: 8080
    targetPort: 8080
  type: ClusterIP
```

## 安全注意事项

⚠️ 此服务执行任意 Python 代码，请确保：

1. 在隔离的网络环境中运行
2. 限制容器资源（CPU、内存）
3. 不要暴露到公网
4. 定期清理过期会话
5. 考虑添加认证机制

## License

MIT
