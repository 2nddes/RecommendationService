# RecommendationService API

更新时间：2026-04-15  
适用范围：当前仓库 Python/Flask 服务对外接口

## 1. 基础信息

- 基础路径：`/api/v1`
- 普通接口响应：`application/json`
- 流式接口响应：`text/event-stream`
- 字符编码：`UTF-8`
- 时间格式：ISO-8601 字符串

当前实现入口：

- Blueprint 注册：`app/api/v1.py`
- 推荐接口：`app/api/v1_recommend.py`
- RAG 接口：`app/api/v1_rag.py`
- 搜索接口：`app/api/v1_search.py`
- 管理接口：`app/api/v1_admin.py`
- 健康接口：`app/common/health.py`

## 2. 鉴权

### 2.1 内部密钥

如果配置了 `settings.core.internal_secret`，所有请求都需要携带：

```http
X-Internal-Secret: <secret>
```

未配置时不强制校验。

### 2.2 当前服务不处理用户登录态

本仓库中的公开 API 主要面向内部调用，不实现 Bearer Token 登录逻辑。上游服务如果需要用户鉴权，应在代理层完成。

## 3. 通用响应约定

### 3.1 普通 JSON 响应

所有普通接口都返回统一包裹结构：

```json
{
  "code": 200,
  "message": "success",
  "data": {}
}
```

### 3.2 错误响应

错误也使用同一结构：

```json
{
  "code": 400,
  "message": "invalid request",
  "data": null
}
```

当前错误处理规则：

- 参数校验错误 `ParamError`：`400 / invalid request parameters`
- 常见请求错误 `ValueError`、`KeyError`：`400 / invalid request`
- 运行时业务错误 `RuntimeError`：`500 / service execution failed`
- 其他未处理错误：`500 / internal server error`

### 3.3 任务对象模型

统一任务表已经替换原来的 `model_train_job`、`rag_rebuild_job`、`rag_embedding_job`。当前 RAG 重建运行时只会创建 `rag_rebuild_job`，每次请求只对应一条任务记录，任务行中只保留聚合统计信息。管理接口返回的任务对象结构如下：

```json
{
  "task_id": "rag_rebuild_job_42",
  "row_id": 42,
  "task_type": "rag_rebuild_job",
  "status": "processing",
  "parent_task_id": null,
  "parent_row_id": null,
  "retry_count": 0,
  "error": null,
  "payload": {
    "scope": "full_rebuild"
  },
  "progress": {
    "total_movies": 1000,
    "processed_movies": 120,
    "completed_jobs": 120,
    "failed_jobs": 0,
    "pruned_embeddings": 18,
    "flush_count": 4,
    "max_retry": 3
  },
  "result": {
    "scope": "full_rebuild",
    "total_movies": 1000,
    "processed_movies": 120,
    "completed_jobs": 120,
    "failed_jobs": 0,
    "pruned_embeddings": 18,
    "elapsed_ms": 15342,
    "failure_samples": []
  },
  "created_at": "2026-04-15T08:00:00",
  "updated_at": "2026-04-15T08:03:11",
  "started_at": "2026-04-15T08:00:00",
  "finished_at": null,
  "source": "db",
  "kind": "rag_rebuild_job",
  "name": "rag_rebuild_job",
  "rag_rebuild_job_id": 42
}
```

字段说明：

- `task_id`：推荐在后续查询中使用的公共任务 ID
- `row_id`：统一任务表主键
- `task_type`：任务类型，当前支持 `train_job`、`rag_rebuild_job`
- `parent_task_id`：父任务的公共任务 ID，当前任务类型通常为空
- `parent_row_id`：父任务的统一任务表主键，当前任务类型通常为空
- `payload`：任务输入和上下文
- `progress`：任务进度，仅记录任务级聚合统计信息
- `result`：任务结果或输出摘要，不记录逐请求或逐电影响应明细
- `kind`、`name`、`train_job_id`、`rag_rebuild_job_id`：兼容字段

## 4. 健康检查

### 4.1 `GET /health`

返回服务可用性摘要。

响应示例：

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "status": "ok",
    "ready": true
  }
}
```

### 4.2 `GET /health/runtime`

返回组件级运行健康快照。

响应 `data` 结构：

- `generated_at`
- `overall.ready`
- `overall.warmup_ready`
- `overall.pipeline_ready`
- `components.warmup|pipeline|rag|cache_precompute`

## 5. 推荐接口

### 5.1 `GET /recommend/user`

个性化推荐。

Query 参数：

- `user_id`：必填，正整数
- `page`：仅 `paged` 模式使用，默认 `1`
- `page_size`：仅 `paged` 模式使用，默认 `20`，范围 `1..100`
- `n`：仅 `pop` 模式使用，默认 `20`，范围 `1..100`

规则：

- 运行模式由 `settings.cache.user_reco_delivery_mode` 决定
- `paged` 模式下不能传 `n`
- `pop` 模式下不能传 `page/page_size`

响应 `data` 结构：

```json
{
  "user_id": 1001,
  "items": [1024, 2048, 4096],
  "n": 20,
  "page": 1,
  "page_size": 20,
  "total": 300,
  "has_next": true
}
```

### 5.2 `GET /recommend/item`

相似影片推荐。

Query 参数：

- `movie_id`：必填，正整数
- `n`：可选，默认 `8`

响应 `data` 结构：

```json
{
  "source_id": 123,
  "items": [456, 789, 1001],
  "n": 8
}
```

### 5.3 `GET /recommend/trending`

热门趋势推荐。

Query 参数：

- `window`：可选，默认 `weekly`
- `n`：可选，默认 `10`

`window` 允许值：

- `daily`
- `weekly`
- `monthly`
- `half_year`
- `one_year`
- `all_time`

响应 `data` 结构：

```json
{
  "window": "weekly",
  "items": [11, 22, 33],
  "n": 10
}
```

## 6. 搜索接口

### 6.1 `GET /search`

基于 MySQL 的标题/简介检索，并支持按标签过滤。

Query 参数：

- `query`：可选，字符串
- `tag_id`：可选，可多值
- `n`：可选，默认 `20`
- `offset`：可选，默认 `0`
- 其他任意参数：不会参与 SQL 条件，但会原样回显到 `passthrough`

约束：

- `query` 和 `tag_id` 不能同时为空
- `n` 必须为正整数
- `offset` 必须为非负整数

响应 `data` 结构：

```json
{
  "query": "科幻 诺兰",
  "tag_ids": [1, 2],
  "n": 20,
  "offset": 0,
  "passthrough": {
    "year_min": "2010"
  },
  "total": 126,
  "results": [
    {
      "movie_id": 1,
      "title": "Interstellar",
      "year": 2014,
      "poster": "https://...",
      "summary": "...",
      "rating_avg": 9.2,
      "rating_count": 100000,
      "score": 12.4
    }
  ]
}
```

## 7. RAG 接口

### 7.1 `POST /recommend/rag/stream`

RAG 流式推荐接口，返回 SSE。

请求头：

```http
Accept: text/event-stream
Content-Type: application/json
```

请求体：

```json
{
  "query": "想看高分悬疑推理电影",
  "n": 8
}
```

约束：

- `query` 必填，去空格后不能为空
- `n` 必须为正整数

SSE 事件：

- `start`

```json
{
  "query": "想看高分悬疑推理电影",
  "n": 8
}
```

- `answer_delta`

```json
{
  "text": "推荐片段"
}
```

- `answer_done`

```json
{
  "elapsed_ms": 1234,
  "cited_movie_ids": [1, 2, 3],
  "chars": 256
}
```

- `error`

```json
{
  "message": "...",
  "type": "llm_error"
}
```

## 8. 管理接口

管理接口都位于 `/admin/**` 路径下，当前依赖内部密钥控制。

### 8.1 `POST /admin/train`

提交训练任务，不在 API 进程内同步执行训练。

请求体：

```json
{
  "component": "ranking",
  "model": "mmoe"
}
```

响应：返回统一任务对象。训练任务的 `payload` 示例：

```json
{
  "component": "ranking",
  "model": "mmoe",
  "request_id": "train_20260415_080000_abcdef",
  "queue": "db_worker",
  "queued": true,
  "mode": "full"
}
```

### 8.2 `POST /admin/rag/enqueue`

提交单电影 RAG 重建任务。

请求体：

```json
{
  "movie_id": 123
}
```

响应：返回统一任务对象。任务类型为 `rag_rebuild_job`，`payload.scope=single_movie`，`payload.movie_id` 为目标电影 ID。

### 8.3 `POST /admin/rag/rebuild`

发起 RAG 全量重建任务。

请求体：

```json
{}
```

响应：返回统一任务对象。任务执行期间的 `progress` 包含：

- `total_movies`
- `processed_movies`
- `completed_jobs`
- `failed_jobs`
- `pruned_embeddings`

任务完成后的 `result` 还会包含：

- `elapsed_ms`
- `failure_samples`

### 8.4 `POST /admin/refresh`

重新加载当前模型产物。

响应 `data`：

```json
{
  "status": "completed",
  "reason": null
}
```

### 8.5 `GET /admin/tasks/{task_id}`

按任务 ID 查询任务详情。

路径参数：

- `task_id`：推荐直接使用前序响应中的 `task_id`

Query 参数：

- `task_type`：可选
- `kind`：可选，`task_type` 的兼容别名

允许的任务类型值：

- `train` / `train_job`
- `rag_rebuild` / `rag_rebuild_job`

说明：

- 推荐始终使用带前缀的公共任务 ID，例如 `train_job_12`
- 为兼容旧调用，带 `task_type/kind` 时仍可用纯数字查询迁移前的旧任务编号

### 8.6 `GET /admin/tasks`

查询任务列表。

Query 参数：

- `source`：可选，`all|memory|db`，默认 `all`
- `status`：可选，`pending|processing|completed|failed`
- `task_type`：可选
- `kind`：可选，`task_type` 兼容别名
- `parent_task_id`：可选，按父任务过滤，支持公共任务 ID 或行 ID
- `rebuild_job_id`：可选，`parent_task_id` 的兼容别名
- `limit`：可选，默认 `20`
- `offset`：可选，默认 `0`

响应 `data` 结构：

```json
{
  "items": [],
  "total": 0,
  "limit": 20,
  "offset": 0,
  "source": "all",
  "status": null,
  "task_type": "all",
  "kind": "all",
  "parent_task_id": null
}
```

### 8.7 `GET /admin/status`

返回当前推荐流水线配置和最近产物信息。

响应 `data` 顶层字段：

- `config.pipeline.recall`
- `config.pipeline.ranking`
- `config.pipeline.reranking`
- `config.mmoe_model_path`
- `config.two_tower_model_path`
- `config.two_tower_index_path`
- `config.two_tower_vector_db_path`
- `config.two_tower_startup_build`
- `config.two_tower_daily_update_interval_hours`
- `artifacts`

## 9. 任务类型与 Worker

当前独立 worker：

- 训练 worker：`python -m app.ops.train_worker`
- RAG rebuild worker：`python -m app.ops.rag_embedding_worker`

两者都消费统一任务表 `ops_task`。其中 RAG rebuild worker 每次只领取一条 `rag_rebuild_job`，并把整次任务的聚合统计写回同一行。
