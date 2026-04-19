# RecommendationService API

更新时间：2026-04-19

本文档以当前 Flask 代码实现为准，覆盖已注册的 14 个 HTTP 接口。历史文档中关于鉴权、健康状态和部分接口行为的描述与实现已有漂移，调用方应优先参考本文档。

## 1. 基础约定

- 基础路径：`/api/v1`
- 普通接口响应类型：`application/json`
- 流式接口响应类型：`text/event-stream; charset=utf-8`
- JSON 字符编码：`UTF-8`
- 当前服务未内建 API 鉴权逻辑。若需要鉴权，应在网关、反向代理或上游服务层实现。

当前代码入口：

- 应用注册：`app/__init__.py`
- 路由聚合：`app/api/v1.py`
- 推荐接口：`app/api/v1_recommend.py`
- 搜索接口：`app/api/v1_search.py`
- RAG 接口：`app/api/v1_rag.py`
- 管理接口：`app/api/v1_admin.py`
- 健康接口：`app/common/health.py`

## 2. 通用响应模型

### 2.1 JSON 成功响应

所有非流式接口统一返回：

```json
{
  "code": 200,
  "message": "success",
  "data": {}
}
```

### 2.2 JSON 错误响应

所有非流式错误也使用统一包裹结构：

```json
{
  "code": 400,
  "message": "invalid request",
  "data": null
}
```

当前错误映射如下：

| 异常来源 | HTTP 状态码 | code | message |
| --- | --- | --- | --- |
| `ParamError` | 400 | 400 | `invalid request parameters` |
| `ValueError` / `KeyError` | 400 | 400 | `invalid request` |
| `RuntimeError` | 500 | 500 | `service execution failed` |
| 其他未处理异常 | 500 | 500 | `internal server error` |
| `HTTPException(404 等)` | 对应状态码 | 对应状态码 | `<500 为 invalid request，否则 internal server error>` |

说明：

- 当前全局错误处理不会把原始参数错误详情透传给客户端。
- 管理接口中某些业务冲突虽然在内部会抛出带细节的异常，但最终仍可能只返回通用的 `invalid request`。

### 2.3 SSE 响应约定

`POST /api/v1/recommend/rag/stream` 不走 JSON 包裹结构，而是输出 SSE 事件流。

说明：

- 流开始后即使内部出错，HTTP 状态通常仍为 `200`。
- 客户端必须监听 `event: error` 事件，而不能只依据 HTTP 状态判断成功与否。

## 3. 接口总览

| 分类 | Method | Path |
| --- | --- | --- |
| Health | GET | `/api/v1/health` |
| Health | GET | `/api/v1/health/runtime` |
| Recommend | GET | `/api/v1/recommend/user` |
| Recommend | GET | `/api/v1/recommend/item` |
| Recommend | GET | `/api/v1/recommend/trending` |
| Search | GET | `/api/v1/search` |
| RAG | POST | `/api/v1/recommend/rag/stream` |
| Admin | POST | `/api/v1/admin/train` |
| Admin | POST | `/api/v1/admin/rag/enqueue` |
| Admin | POST | `/api/v1/admin/rag/rebuild` |
| Admin | POST | `/api/v1/admin/refresh` |
| Admin | GET | `/api/v1/admin/tasks/{task_id}` |
| Admin | GET | `/api/v1/admin/tasks` |
| Admin | GET | `/api/v1/admin/status` |

## 4. Health 接口

### 4.1 GET `/api/v1/health`

返回服务摘要健康状态。

成功响应 `data`：

```json
{
  "status": "ok",
  "ready": true
}
```

说明：

- `status` 只会是 `ok` 或 `degraded`。
- `ready` 取决于运行时健康快照中的 `overall.ready`。
- 当前 `overall.ready` 的判定条件是：`warmup_ready && pipeline_ready && rag_ready`。

### 4.2 GET `/api/v1/health/runtime`

返回组件级运行健康快照。

成功响应 `data` 顶层字段：

- `generated_at`
- `overall.ready`
- `overall.warmup_ready`
- `overall.pipeline_ready`
- `overall.rag_ready`
- `components`

当前默认组件包括：

- `warmup`
- `pipeline`
- `rag`
- `two_tower_refresh_worker`
- `cache_precompute_worker`
- `train_queue_worker`
- `rag_rebuild_worker`

单个组件对象字段包括：

- `name`
- `ready`
- `status`
- `last_success_at`
- `last_error_at`
- `last_error`
- `details`

## 5. Recommend 接口

### 5.1 GET `/api/v1/recommend/user`

个性化推荐接口。请求参数受服务端配置 `settings.cache.user_reco_delivery_mode` 控制。

Query 参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `user_id` | integer | 是 | 无 | 当前仅要求可转换为整数 |
| `page` | integer | 否 | `1` | 仅 `paged` 模式允许，且必须 `>= 1` |
| `page_size` | integer | 否 | `20` | 仅 `paged` 模式允许，范围 `1..100` |
| `n` | integer | 否 | `20` | 仅 `pop` 模式允许，范围 `1..100` |

模式规则：

- 当服务端模式为 `paged` 时，传入 `n` 会触发 `400 invalid request parameters`。
- 当服务端模式为 `pop` 时，传入 `page` 或 `page_size` 会触发 `400 invalid request parameters`。
- 客户端无法从接口自描述中发现当前模式，需与服务部署方对齐配置。

成功响应 `data` 示例：

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

说明：

- 返回字段在 `paged` 与 `pop` 模式下保持一致。
- `items` 仅返回电影 ID，不返回电影详情。

### 5.2 GET `/api/v1/recommend/item`

相似影片推荐接口。

Query 参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `movie_id` | integer | 是 | 无 | 当前电影 ID |
| `n` | integer | 否 | `8` | 推荐调用方传正整数；当前路由层未显式限制上限 |

成功响应 `data` 示例：

```json
{
  "source_id": 123,
  "items": [456, 789, 1001],
  "n": 8
}
```

说明：

- 若底层无法构建 `movie_id` 的向量，会返回 `400 invalid request`。
- 当前实现不会自动裁剪过大的 `n`。

### 5.3 GET `/api/v1/recommend/trending`

热门趋势推荐接口。

Query 参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `window` | string | 否 | `weekly` | 允许值见下方 |
| `n` | integer | 否 | `10` | 推荐调用方传正整数；当前路由层未显式限制上限 |

`window` 允许值：

- `daily`
- `weekly`
- `monthly`
- `half_year`
- `one_year`
- `all_time`

成功响应 `data` 示例：

```json
{
  "window": "weekly",
  "items": [11, 22, 33],
  "n": 10
}
```

## 6. Search 接口

### 6.1 GET `/api/v1/search`

基于 MySQL 的标题/简介检索，同时支持按标签过滤。

Query 参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `query` | string | 否 | 空字符串 | 标题与摘要检索词 |
| `tag_id` | integer[] | 否 | 空数组 | 可重复传参，例如 `tag_id=1&tag_id=2` |
| `n` | integer | 否 | `20` | 必须大于 0，当前没有上限保护 |
| `offset` | integer | 否 | `0` | 必须大于等于 0 |
| 其他任意 query 参数 | string 或 string[] | 否 | 无 | 不参与 SQL 条件，仅回显到 `passthrough` |

约束：

- `query` 与 `tag_id` 不能同时为空。
- 重复的 `tag_id` 会去重后再查询。

成功响应 `data` 示例：

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
      "poster": "https://example.invalid/poster.jpg",
      "summary": "...",
      "rating_avg": 9.2,
      "rating_count": 100000,
      "score": 12.4
    }
  ]
}
```

说明：

- `summary` 会在服务端截断到最多 300 个字符，超长时追加 `...`。
- 当前实现需要可用的 `settings.core.mysql_dsn`，否则会返回 `500 service execution failed`。
- `passthrough` 是“回显字段”，不是“实际过滤条件”。

## 7. RAG 接口

### 7.1 POST `/api/v1/recommend/rag/stream`

RAG 流式回答接口，返回 SSE 事件流。

请求头建议：

```http
Accept: text/event-stream
Content-Type: application/json
```

请求体：

```json
{
  "query": "想看高分悬疑推理电影",
  "thinking": false
}
```

请求约束：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `query` | string | 是 | 无 | 去空白后不能为空 |
| `n` | integer | 否 | `8` | 必须大于 0 |

事件类型：

#### `start`

```json
{
  "query": "想看高分悬疑推理电影",
  "thinking": false
}
```

#### `answer_delta`

```json
{
  "text": "推荐片段"
}
```

#### `answer_done`

```json
{
  "elapsed_ms": 1234,
  "cited_movie_ids": [1, 2, 3],
  "chars": 256
}
```

#### `error`

```json
{
  "message": "...",
  "type": "llm_error"
}
```

说明：

- 正常完成时，事件顺序通常为：`start` -> `answer_delta`(0..n) -> `answer_done`。
- 出错时，通常为：`start` -> `error`，也可能在输出若干 `answer_delta` 后再进入 `error`。
- 当前实现会把底层异常文本写入 `error.message`，调用方不应将其直接展示给最终用户。
- 响应头中已设置 `Cache-Control: no-cache, no-transform`、`Connection: keep-alive`、`X-Accel-Buffering: no`。

## 8. Admin 接口

管理接口均位于 `/api/v1/admin/**`。当前代码未内建鉴权、限流和幂等保护，生产环境应放在受控网络或代理层之后。

### 8.1 POST `/api/v1/admin/train`

提交模型训练任务，只负责入队，不在 API 进程内同步执行训练。

请求体：

```json
{
  "component": "ranking",
  "model": "mmoe"
}
```

当前训练 worker 逻辑中支持的组合：

- `ranking` + `xgb`
- `ranking` + `mmoe`
- `recall` + `two_tower`

成功响应 `data` 示例：

```json
{
  "task_id": "train_job_12",
  "row_id": 12,
  "task_type": "train_job",
  "status": "pending",
  "parent_task_id": null,
  "parent_row_id": null,
  "retry_count": 0,
  "error": null,
  "payload": {
    "queued": true,
    "component": "ranking",
    "model": "mmoe",
    "request_id": "train_20260419_090000_abcdef",
    "queue": "db_worker",
    "mode": "full"
  },
  "progress": {},
  "result": {},
  "created_at": "2026-04-19T09:00:00",
  "updated_at": "2026-04-19T09:00:00",
  "started_at": null,
  "finished_at": null,
  "source": "db",
  "kind": "train_job",
  "name": "train_job",
  "estimated_time": "unknown"
}
```

说明：

- 请求参数只做“存在且为字符串”的校验，组合合法性由后续训练逻辑决定。
- 未启动训练 worker 时，任务会长期停留在 `pending`。

### 8.2 POST `/api/v1/admin/rag/enqueue`

同步刷新单个电影的 RAG embedding。

请求体：

```json
{
  "movie_id": 123
}
```

成功响应 `data` 示例：

```json
{
  "movie_id": 123,
  "embedding_id": 456,
  "status": "completed"
}
```

说明：

- `movie_id` 必须为正整数。
- 当前接口是同步执行，不会创建 `rag_rebuild_job` 任务。
- 若 RAG 依赖配置缺失，会返回 `500 service execution failed`。

### 8.3 POST `/api/v1/admin/rag/rebuild`

提交全量 RAG 重建任务。

请求体可为空：

```json
{}
```

成功响应：返回统一任务对象，`task_type` 为 `rag_rebuild_job`。

说明：

- 当前只允许一个活动中的全量重建任务。
- 若已存在活动任务，接口会返回 `400 invalid request`。

### 8.4 POST `/api/v1/admin/refresh`

刷新当前在线模型与推荐运行时。

成功响应 `data`：

```json
{
  "status": "completed",
  "reason": null
}
```

说明：

- 失败时会转成 `500 service execution failed`，不会把内部失败原因直接返回给客户端。
- 当前接口为同步刷新，请避免高频并发调用。

### 8.5 GET `/api/v1/admin/tasks/{task_id}`

按任务 ID 查询任务详情。

Path 参数：

- `task_id`：可以是纯数字行 ID，也可以是公共任务引用，例如 `train_job_12`

Query 参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `task_type` | string | 否 | 无 | `kind` 的兼容别名 |
| `kind` | string | 否 | 无 | 与 `task_type` 同义 |

允许值：

- `all`
- `train`
- `train_job`
- `rag_rebuild`
- `rag_rebuild_job`

规则：

- 若 `task_type` 与 `kind` 同时出现且不一致，返回 `400 invalid request parameters`。
- 未找到任务时返回 `404 invalid request`。

### 8.6 GET `/api/v1/admin/tasks`

查询任务列表。

Query 参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `source` | string | 否 | `all` | 允许值：`all`、`memory`、`db` |
| `status` | string | 否 | 无 | 允许值：`pending`、`processing`、`completed`、`failed` |
| `task_type` | string | 否 | 无 | 与 `kind` 互为别名 |
| `kind` | string | 否 | 无 | 与 `task_type` 互为别名 |
| `parent_task_id` | string | 否 | 无 | 父任务过滤 |
| `rebuild_job_id` | string | 否 | 无 | `parent_task_id` 兼容别名 |
| `limit` | integer | 否 | `20` | 当前仅做整型转换，建议传非负且适度的值 |
| `offset` | integer | 否 | `0` | 当前仅做整型转换，建议传非负值 |

成功响应 `data` 示例：

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

补充行为：

- 若 `source=memory`，当前实现固定返回空列表，并额外带上 `note` 字段说明内存任务运行器已移除。
- 若 `parent_task_id` 与 `rebuild_job_id` 同时出现且值不同，返回 `400 invalid request parameters`。
- 查询结果默认排除 `rag_embedding_job`。

### 8.7 GET `/api/v1/admin/status`

返回当前推荐流水线配置、产物信息和运行健康快照。

成功响应 `data` 顶层字段：

- `config.pipeline.recall`
- `config.pipeline.ranking`
- `config.pipeline.reranking`
- `config.mmoe_model_path`
- `config.two_tower_model_path`
- `config.two_tower_index_path`
- `config.two_tower_vector_db_path`
- `artifacts`
- `runtime_health`

## 9. 任务对象模型

`/admin/train`、`/admin/rag/rebuild`、`/admin/tasks/{task_id}`、`/admin/tasks` 会返回统一任务对象。当前基础字段如下：

```json
{
  "task_id": "rag_rebuild_job_42",
  "row_id": 42,
  "task_type": "rag_rebuild_job",
  "status": "completed",
  "parent_task_id": null,
  "parent_row_id": null,
  "retry_count": 0,
  "error": null,
  "payload": {},
  "progress": {},
  "result": {},
  "created_at": "2026-04-19T09:00:00",
  "updated_at": "2026-04-19T09:03:11",
  "started_at": "2026-04-19T09:00:00",
  "finished_at": "2026-04-19T09:03:11",
  "source": "db",
  "kind": "rag_rebuild_job",
  "name": "rag_rebuild_job"
}
```

针对 `rag_rebuild_job`，当前还可能附加：

- `scope`
- `movie_id`

## 10. 调用前置条件与注意事项

### 10.1 运行依赖

- `/search` 依赖可用的 MySQL 连接。
- `/recommend/rag/stream` 与 `/admin/rag/*` 依赖 RAG 配置与下游模型服务。
- `/admin/train` 依赖独立训练 worker 执行真实训练。
- `/admin/rag/rebuild` 依赖独立的 RAG rebuild worker 消费任务。

### 10.2 当前实现层面的注意事项

- 当前代码未内建鉴权和限流，管理接口不应直接暴露到公网。
- `/recommend/item`、`/recommend/trending`、`/admin/tasks` 缺少完整的请求上限保护，调用方应自行限制参数范围。
- RAG SSE 接口当前会把底层错误文本透给客户端，前端不应原样展示。
- 当前服务没有 OpenAPI/Swagger 自动导出能力，Postman collection 需手工维护。