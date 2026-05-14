# Service Dashboard v2

三机 Docker 服务对标 + 一键互推 + PG 同步。

## 架构

```
S1 (100.96.28.120)        Mac Mini (100.77.50.100)     Lenovo (100.95.148.117)
┌────────────────────┐    ┌──────────────────────┐    ┌──────────────────────┐
│ svc-dashboard:8080  │    │ svc-agent             │    │ svc-agent             │
│ svc-registry:5000   │    │  ├─ 上报容器/镜像     │    │  ├─ 上报容器/镜像     │
│ svc-registry-ui:5001│    │  ├─ 执行部署任务      │    │  ├─ 执行部署任务      │
│ svc-agent           │    │  └─ PG dump/restore   │    │  └─ PG dump/restore   │
│ 生产服务             │    │                       │    │                       │
└────────┬───────────┘    └──────────┬───────────┘    └──────────┬───────────┘
         │                           │                           │
         │     POST /api/heartbeat   │                           │
         │◄──────────────────────────┴───────────────────────────┘
         │
         │     返回 pending tasks
         │──────────────────────────► 执行 docker pull + up -d
         │──────────────────────────► 执行 pg_dump | psql
```

## 新功能

### 🚀 一键互推

在 Dashboard 上看到版本差异后，点一个按钮就推过去：

1. 服务对标表里，版本不一致的行出现 `→S1` `→Mac` `→Len` 按钮
2. 点击 → 确认 → 创建 deploy task
3. 目标机器的 Agent 自动执行 `docker compose pull && docker compose up -d`
4. 看板实时轮询任务状态，完成后自动刷新

### 🗄️ PG 一键同步

支持三种模式同步 PostgreSQL：

| 模式 | 说明 | 命令 |
|------|------|------|
| 仅结构 | 只同步 schema | `pg_dump --schema-only` |
| 仅数据 | 只同步数据 | `pg_dump --data-only` |
| 完整 | schema + 数据 | `pg_dump \| psql` |

Agent 会自动检测 PostgreSQL 容器。在 Dashboard 的 PG 页面可以：
- 查看所有节点的 PG 实例
- 选择来源 → 目标 → 一键同步
- 从服务对标表直接点 PG 按钮触发同步

## 快速开始

### 1. S1 启动

```bash
cd service-dashboard
docker compose up -d --build
```

三个服务：
- **Dashboard**: http://100.96.28.120:8080
- **Registry**: http://100.96.28.120:5000
- **Registry UI**: http://100.96.28.120:5001

### 2. 构建 Agent 镜像

```bash
cd service-dashboard/agent
docker build -t svc-agent .
```

### 3. 在各节点启动 Agent

**S1（本身也跑 Agent）：**
```bash
docker run -d --name svc-agent --restart unless-stopped \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e DASHBOARD_URL=http://100.96.28.120:8080 \
  -e NODE_NAME=S1 \
  -e NODE_IP=100.96.28.120 \
  -e COMPOSE_DIR=/data/compose \
  -e PG_CONNECTIONS='{"S1:postgres":"host=localhost dbname=postgres user=postgres","MacMini:postgres":"host=100.77.50.100 dbname=postgres user=postgres","Lenovo:postgres":"host=100.95.148.117 dbname=postgres user=postgres"}' \
  -v /path/to/compose:/data/compose:ro \
  svc-agent
```

**Mac Mini：**
```bash
docker run -d --name svc-agent --restart unless-stopped \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e DASHBOARD_URL=http://100.96.28.120:8080 \
  -e NODE_NAME=MacMini \
  -e NODE_IP=100.77.50.100 \
  -e COMPOSE_DIR=/data/compose \
  -v /path/to/compose:/data/compose:ro \
  svc-agent
```

**Lenovo（WSL2）：**
```bash
docker run -d --name svc-agent --restart unless-stopped \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e DASHBOARD_URL=http://100.96.28.120:8080 \
  -e NODE_NAME=Lenovo \
  -e NODE_IP=100.95.148.117 \
  -e COMPOSE_DIR=/data/compose \
  -v /path/to/compose:/data/compose:ro \
  svc-agent
```

### 4. Agent 环境变量说明

| 变量 | 必填 | 说明 |
|------|------|------|
| `DASHBOARD_URL` | ✅ | Dashboard 地址 |
| `NODE_NAME` | ✅ | 节点名称（唯一标识） |
| `NODE_IP` | ✅ | 本机 Tailscale IP |
| `REPORT_INTERVAL` | 否 | 上报间隔，默认 30s |
| `COMPOSE_DIR` | 否 | docker-compose.yml 所在目录（部署用） |
| `PG_CONNECTIONS` | 否 | PG 连接字符串 JSON, 格式见下 |

**PG_CONNECTIONS 格式：**
```json
{
  "S1:postgres": "host=100.96.28.120 dbname=postgres user=postgres",
  "MacMini:mydb": "host=100.77.50.100 dbname=mydb user=postgres",
  "Lenovo:mydb": "host=100.95.148.117 dbname=mydb user=postgres"
}
```

Key 格式：`{节点名}:{数据库名}`

## 日常工作流

### 开发 → 部署

```bash
# Mac Mini 开发
docker build -t 100.96.28.120:5000/myapp:v2 .
docker push 100.96.28.120:5000/myapp:v2

# 打开 Dashboard → 看到版本差异 → 点 "→S1"
# Agent 自动 pull + restart
```

### PG 同步

```bash
# Dashboard PG 页面:
# 同步模式: 仅结构 / 仅数据 / 完整
# 选 MacMini:postgres → S1:postgres
# Agent 执行 pg_dump(来源) | psql(目标)
```

## API 一览

| 路径 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 服务对标主页 |
| `/node/{name}` | GET | 节点详情 |
| `/tasks` | GET | 任务历史 |
| `/pg` | GET | PG 管理 |
| `/api/heartbeat` | POST | Agent 上报 |
| `/api/deploy` | POST | 创建部署任务 |
| `/api/pg-sync` | POST | 创建 PG 同步任务 |
| `/api/tasks/{id}` | GET | 查询任务状态 |
| `/api/task/{id}/update` | POST | 更新任务状态 |
| `/api/overview` | GET | 服务对标 JSON |
| `/api/nodes` | GET | 节点列表 |
| `/api/pg-instances` | GET/POST | PG 实例管理 |
| `/health` | GET | 健康检查 |
