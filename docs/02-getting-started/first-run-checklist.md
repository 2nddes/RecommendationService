# First Run Checklist

## 文档目标

本文档是一份面向交付验收和第一次本地启动的顺序化清单。它不讲太多原理，只关心一件事：

你能不能从零开始，把服务跑起来，并用几个关键接口证明系统真的可用。

## 使用方式

请按顺序执行，不要跳步。这个项目启动时会做 warmup，任何前置条件缺失都可能在启动阶段集中暴露出来。

## Step 0：确认你已经看过前置文档

开始之前，请至少快速扫过：

- [environment-setup.md](environment-setup.md)
- [configuration-guide.md](configuration-guide.md)

如果你还没确认：

- MySQL 已准备好
- Redis 已准备好
- 活跃模型和索引路径存在
- RAG provider 配置可访问

那就不要进入下一步。

## Step 1：创建并导入数据库

### 目标

确保 [all.sql](../../all.sql) 中的表结构已经完整进入你的目标数据库。

### 操作

```powershell
mysql -u root -p movie_rec < all.sql
```

### 成功标准

- 数据库存在。
- 关键表存在，例如：`movie`、`rating`、`movie_embeddings`、`ops_task`。

### 如果失败

先检查：

- 数据库名是否与 [config.json](../../config.json) 中 `core.mysql_dsn` 一致。
- MySQL 用户是否有建表权限。

## Step 2：检查并修正 config.json

### 目标

确保连接、路径和 provider 配置都与你本地环境一致。

### 必查项

- `core.mysql_dsn`
- `redis.host` / `redis.port`
- `two_tower.model_path`
- `two_tower.index_path`
- `two_tower.vector_db_path`
- `mmoe.model_path`
- `xgb.model_path`
- `rag.embedding_api_base_url`
- `rag.embedding_api_key`
- `rag.llm_api_base_url`
- `rag.llm_api_key`

### 成功标准

- 所有连接类配置指向真实可访问资源。
- 所有模型和索引路径指向真实存在文件。

## Step 3：确认 Redis 已经可以访问

### 目标

在启动服务前先排除缓存基础设施问题。

### 建议检查方式

如果本机已装 `redis-cli`：

```powershell
redis-cli -h 127.0.0.1 -p 6379 ping
```

### 成功标准

- 返回 `PONG`。

### 如果失败

先不要启动服务，先把 Redis 连通性问题解决。否则 warmup、缓存预热和倒排召回都会偏离正常行为。

## Step 4：确认活跃模型与索引文件存在

### 目标

避免启动时因为路径缺失直接失败。

### 当前配置默认要求

- `data/models/two_tower_latest.pt`
- `data/models/mmoe_latest.pt`
- `data/models/xgb_latest.json`
- `data/two_tower_items.hnsw`
- `data/two_tower_vectors.db`

### 成功标准

- 以上路径要么真实存在，要么你已经改过配置并指向现有文件。

### 如果失败

通常有两种修法：

1. 把配置改到现有文件。
2. 把现有产物复制或链接到配置要求的活跃路径。

## Step 5：安装依赖并启动服务

### 操作

```powershell
pip install -r requirements.txt
python app.py
```

如果报缺少 `hnswlib`，继续执行：

```powershell
pip install hnswlib
python app.py
```

### 成功标准

- 进程不立即退出。
- 日志文件开始写入。
- 终端或日志中可以看到应用初始化完成的信息。(具体措辞可能略有差异)

### 如果失败

立即去看：

- [local-debugging-guide.md](local-debugging-guide.md)
- `logs/` 目录下的日志文件

## Step 6：检查基础健康状态

### 目标

先确认服务活着，再确认组件是否准备完成。

### 检查 1：简版健康接口

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/v1/health" -Method Get
```

### 期望结果

返回 JSON，且 `data.status` 为 `ok` 或至少不是不可解析的错误。

### 检查 2：运行时健康接口

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/v1/health/runtime" -Method Get
```

### 重点看什么

- `overall.ready`
- `overall.warmup_ready`
- `overall.pipeline_ready`
- `overall.rag_ready`
- `components`

### 成功标准

- warmup、pipeline、rag 三个关键组件至少处于 ready。

## Step 7：检查管理总览接口

### 目标

确认模型路径、产物状态和运行摘要一致。

### 操作

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/v1/admin/status" -Method Get
```

### 重点检查项

- `data.config.pipeline`
- `data.models`
- `data.runtime_summary`
- `data.artifact_summary`

### 成功标准

- 管理接口可返回。
- 运行时摘要与健康接口没有明显矛盾。
- 当前配置路径对应的模型确实存在。

## Step 8：验证热门榜接口

### 目标

先验证一条依赖较少、最容易成功的推荐接口。

### 操作

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/v1/recommend/trending?window=weekly&n=5" -Method Get
```

### 成功标准

- 返回结构合法。
- `data.items` 存在，哪怕数量不多。

### 如果失败

重点排查：

- MySQL 是否可访问
- Redis 是否可访问
- `movie` 表中是否有可用数据

## Step 9：验证用户推荐接口

### 目标

确认主推荐链路可以从接口跑通。

### 注意

当前配置中 `user_reco_delivery_mode` 是 `pop`，因此你应该使用 `n` 参数，而不是 `page/page_size`。

### 操作

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/v1/recommend/user?user_id=1&n=10" -Method Get
```

### 成功标准

- 接口不报参数错误。
- 能返回 `items`、`total`、`has_next` 等字段。

### 如果失败

重点排查：

- `user_id` 是否存在于数据中
- Two-Tower 模型与索引是否已就绪
- MMoE 模型是否可加载
- Redis 构建锁或推荐缓存是否异常

## Step 10：验证搜索接口

### 目标

确认搜索链路和 MySQL 查询路径工作正常。

### 操作

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/v1/search?query=love&n=5&offset=0" -Method Get
```

如果你更想验证 browse 模式：

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/v1/search?n=5&offset=0" -Method Get
```

### 成功标准

- 返回结果包含 `total`、`results`。
- 单条结果中含有 `movie_id`、`title`、`summary`、`rating_avg` 等字段。

### 如果失败

重点排查：

- `movie` 表和搜索相关字段是否存在
- `core.mysql_dsn` 是否正确
- 搜索统计字段是否已被预计算更新

## Step 11：验证相似影片推荐接口

### 目标

确认 RAG embedding 或 Two-Tower 回退路径至少有一条可用。

### 操作

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/v1/recommend/item?movie_id=1&n=5" -Method Get
```

### 成功标准

- 返回 `source_id` 和 `items`。

### 失败时的典型原因

- 影片不存在
- RAG 索引未初始化
- 影片 embedding 缺失，且 Two-Tower item vector 也无法构造

## Step 12：验证 RAG SSE 接口

### 目标

确认 RAG 检索与流式回答完整可用。

### 操作建议

SSE 在 PowerShell 中不如 Postman、curl 或专门的前端调试器直观。若本地已安装 curl，可使用：

```powershell
curl -N -X POST "http://127.0.0.1:5000/api/v1/recommend/rag/stream" ^
  -H "Content-Type: application/json" ^
  -d "{\"query\":\"推荐几部高分爱情电影\",\"thinking\":false}"
```

### 成功标准

- 能看到 `event: start`
- 持续收到 `event: answer_delta`
- 最终收到 `event: answer_done`

### 如果失败

重点排查：

- RAG provider 是否可访问
- `movie_embeddings` 是否已有可用向量
- RAG 索引是否初始化成功

## Step 13：验收清单

当你准备说“这个项目已经跑起来了”时，至少应满足以下条件：

- `GET /api/v1/health` 成功
- `GET /api/v1/health/runtime` 返回 ready
- `GET /api/v1/admin/status` 成功
- 热门榜接口成功
- 用户推荐接口成功
- 搜索接口成功
- 相似影片接口成功
- RAG SSE 接口至少能返回 start 和 answer_done

如果你只完成了“服务没报错启动”，那还不能算交付级成功。

## Step 14：下一步去哪里

如果首启失败，请去 [local-debugging-guide.md](local-debugging-guide.md)。

如果首启成功，并且你想开始看系统怎么实现，请回到：

- [../01-overview/architecture-overview.md](../01-overview/architecture-overview.md)
- [../01-overview/repository-map.md](../01-overview/repository-map.md)