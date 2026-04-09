# RecommendationService API 对接文档（当前实现）

更新时间：2026-04-06  
适用版本：当前仓库主干代码

## 1. 文档目标

本文档用于给其他部门做联调与后续开发，内容完全基于当前 Flask 路由与服务实现，不包含未来规划接口。

## 2. 基础信息

- Base Path：`/api/v1`
- 服务默认端口：`5000`（本地启动）
- 协议：HTTP（可由网关升级 HTTPS）
- 默认返回编码：UTF-8 JSON（RAG 流接口为 SSE）

## 3. 鉴权规则

- 当 `settings.core.internal_secret` 为空：不做内部鉴权。
- 当 `settings.core.internal_secret` 非空：所有请求必须带请求头 `X-Internal-Secret`。
- 鉴权失败：HTTP 401。

示例：

```json
{
  "code": 401,
  "message": "unauthorized",
  "data": null
}
```

## 4. 统一响应与错误约定

JSON 类接口统一结构：

```json
{
  "code": 200,
  "message": "success",
  "data": {}
}
```

全局异常映射：

- `ParamError` -> HTTP 400，`message = "invalid request parameters"`
- `ValueError` / `KeyError` -> HTTP 400，`message = "invalid request"`
- `HTTPException` -> HTTP 原状态码，`message = "invalid request"`（4xx）或 `"internal server error"`（5xx）
- `RuntimeError` -> HTTP 500，`message = "service execution failed"`
- 其他未捕获异常 -> HTTP 500，`message = "internal server error"`

说明：

- 业务错误细节不会直接透传到客户端，调用方请以 `code` 和 `message` 做流程分支。
- 除 SSE 接口外，错误响应均为 JSON。

## 5. 接口总表

| 模块 | 方法 | 路径 | 用途 |
| --- | --- | --- | --- |
| 健康 | GET | `/health` | 服务就绪摘要 |
| 健康 | GET | `/health/runtime` | 运行时组件健康快照 |
| 推荐 | GET | `/recommend/user` | 个性化推荐 |
| 推荐 | GET | `/recommend/item` | 相似影片推荐 |
| 推荐 | GET | `/recommend/trending` | 热门趋势推荐 |
| 搜索 | GET | `/search` | 电影搜索（关键词检索 + 可选标签过滤） |
| 管理 | POST | `/admin/train` | 提交训练任务 |
| 管理 | POST | `/admin/refresh` | 刷新在线模型 |
| 管理 | GET | `/admin/tasks/<task_id>` | 查询单任务状态 |
| 管理 | GET | `/admin/tasks` | 分页查询任务 |
| 管理 | GET | `/admin/status` | 查询配置与产物 |
| RAG | POST | `/recommend/rag/stream` | SSE 流式推荐 |

以下路径均已包含 Base Path，完整 URL 为 `/api/v1` + 路径。

## 6. 详细接口定义

### 6.1 GET /health

用途：返回整体 readiness 摘要。

请求参数：无。

成功示例：

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

字段说明：

- `status`：`ok` 或 `degraded`
- `ready`：布尔值

### 6.2 GET /health/runtime

用途：返回运行时组件健康快照。

请求参数：无。

成功示例（结构示意）：

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "generated_at": "2026-04-06T09:10:00Z",
    "overall": {
      "ready": true,
      "warmup_ready": true,
      "pipeline_ready": true
    },
    "components": {
      "warmup": {
        "name": "warmup",
        "ready": true,
        "status": "ok",
        "last_success_at": "2026-04-06T09:09:00Z",
        "last_error_at": null,
        "last_error": null,
        "details": {}
      },
      "pipeline": {
        "name": "pipeline",
        "ready": true,
        "status": "ok",
        "last_success_at": "2026-04-06T09:09:00Z",
        "last_error_at": null,
        "last_error": null,
        "details": {}
      },
      "rag": {
        "name": "rag",
        "ready": false,
        "status": "pending",
        "last_success_at": null,
        "last_error_at": null,
        "last_error": null,
        "details": {}
      },
      "cache_precompute": {
        "name": "cache_precompute",
        "ready": false,
        "status": "pending",
        "last_success_at": null,
        "last_error_at": null,
        "last_error": null,
        "details": {}
      }
    }
  }
}
```

### 6.3 GET /recommend/user

用途：个性化推荐（猜你喜欢）。

Query 参数：

- `user_id`：必填，`int`
- `n`：可选，`int`，默认 `10`

成功示例：

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "user_id": 1001,
    "items": [101, 202, 303],
    "n": 10
  }
}
```

错误场景：

- 缺少 `user_id` 或类型错误 -> 400
- `n` 不是整数 -> 400

### 6.4 GET /recommend/item

用途：相似影片推荐（看了又看）。

Query 参数：

- `movie_id`：必填，`int`
- `n`：可选，`int`，默认 `8`

成功示例：

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "source_id": 1024,
    "items": [2048, 4096],
    "n": 8
  }
}
```

错误场景：

- 缺少 `movie_id` 或参数类型错误 -> 400
- 物品向量缺失等业务异常（内部抛 `ValueError`）-> 400

### 6.5 GET /recommend/trending

用途：热门趋势推荐。

Query 参数：

- `window`：可选，默认 `weekly`，可选值：`daily`、`weekly`、`monthly`、`half_year`、`one_year`、`all_time`
- `n`：可选，`int`，默认 `10`

成功示例：

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "window": "weekly",
    "items": [11, 22, 33],
    "n": 10
  }
}
```

补充说明：

- 当 `window=all_time` 时，榜单按 `rating_count` 从高到低排序。

错误场景：

- `window` 不在允许集合中 -> 400
- `n` 不是整数 -> 400

### 6.6 GET /search

用途：电影搜索主入口（支持关键词检索，支持多值标签过滤）。

Query 参数：

- `query`：可选，`string`，去空格后可为空
- `n`：可选，`int`，默认 `20`，必须 `> 0`
- `offset`：可选，`int`，默认 `0`，必须 `>= 0`
- 扩展参数：支持任意参数透传；支持多值参数（例如 `tag=a&tag=b`）

约束：

- `query` 和 `tag` 不能同时为空（至少提供一个）

请求示例：

```http
GET /api/v1/search?query=科幻%20诺兰&n=20&offset=0&tag=悬疑&tag=推理
```

成功示例：

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "query": "科幻 诺兰",
    "n": 20,
    "offset": 0,
    "passthrough": {
      "tag": ["悬疑", "推理"]
    },
    "total": 123,
    "results": [
      {
        "movie_id": 10086,
        "title": "星际穿越",
        "year": 2014,
        "poster": "https://example.com/posters/10086.jpg",
        "summary": "人类面临生存危机，前往深空寻找新家园...",
        "rating_avg": 9.3,
        "rating_count": 998877,
        "score": 11.2043
      }
    ]
  }
}
```

说明：

- 搜索会在 `movie.title` / `movie.summary` 上执行关键词匹配，并按相关性与热度排序。
- 当提供 `tag` 参数时，会按标签做过滤（支持多个 `tag`）。
- 未保留的扩展参数会在响应 `data.passthrough` 中按原样返回（单值为字符串，多值为数组）。

### 6.7 POST /admin/train

用途：创建模型训练任务（写入 DB 队列）。

请求体（JSON）：

- `component`：必填，`string`
- `model`：必填，`string`

请求示例：

```json
{
  "component": "ranking",
  "model": "mmoe"
}
```

成功示例：

```json
{
  "code": 200,
  "message": "Training task started",
  "data": {
    "task_id": "31",
    "train_job_id": 31,
    "estimated_time": "unknown"
  }
}
```

错误场景：

- 缺少字段或字段类型错误 -> 400
- DB 写入异常等运行时错误 -> 500

### 6.8 POST /admin/refresh

用途：加载最新本地模型并重建在线推荐流水线。

请求体：无。

成功示例：

```json
{
  "code": 200,
  "message": "Refresh completed",
  "data": {
    "status": "completed",
    "reason": null
  }
}
```

失败语义：

- 刷新失败时内部会抛 `RuntimeError`，返回 HTTP 500（`message = "service execution failed"`）。

### 6.9 GET /admin/tasks/<task_id>

用途：按任务 ID 查询训练任务状态。

Path 参数：

- `task_id`：数字字符串

成功示例：

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "id": "31",
    "name": "train_job",
    "status": "processing",
    "created_at": "2026-04-06T09:20:00",
    "started_at": null,
    "finished_at": null,
    "error": null,
    "result": {
      "train_job_id": 31,
      "mode": "full",
      "status": "processing",
      "metrics": {
        "component": "ranking",
        "model": "mmoe",
        "task_id": "train_20260406_092000_ab12cd",
        "queue": "db_worker"
      }
    }
  }
}
```

错误场景：

- `task_id` 非数字或任务不存在 -> HTTP 404，`message = "invalid request"`

### 6.10 GET /admin/tasks

用途：分页查询训练任务列表。

Query 参数：

- `source`：可选，默认 `all`，可选值 `all`、`memory`、`db`
- `status`：可选，`pending`、`processing`、`completed`、`failed`
- `limit`：可选，`int`，默认 `20`
- `offset`：可选，`int`，默认 `0`

成功示例（source=db/all）：

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "items": [
      {
        "id": "31",
        "name": "train_job",
        "status": "completed",
        "created_at": "2026-04-06T09:20:00",
        "started_at": null,
        "finished_at": "2026-04-06T09:24:00",
        "error": null,
        "source": "db",
        "result": {
          "train_job_id": 31,
          "mode": "full",
          "status": "completed",
          "metrics": {}
        }
      }
    ],
    "total": 1,
    "limit": 20,
    "offset": 0,
    "source": "db",
    "status": null
  }
}
```

`source=memory` 示例：

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "items": [],
    "total": 0,
    "limit": 20,
    "offset": 0,
    "source": "memory",
    "status": null,
    "note": "in-memory task runner has been removed; use source=db"
  }
}
```

错误场景：

- `source` 非法 -> 400
- `status` 非法 -> 400
- `limit`/`offset` 非整数 -> 400

### 6.11 GET /admin/status

用途：查询当前推荐流水线配置和模型产物记录。

成功返回 `data` 关键字段：

- `config.pipeline`：当前 `recall` / `ranking` / `reranking` 选择
- `config.mmoe_model_path`
- `config.two_tower_model_path`
- `config.two_tower_index_path`
- `config.two_tower_vector_db_path`
- `config.two_tower_startup_build`
- `config.two_tower_daily_update_interval_hours`
- `artifacts`：产物存储记录（结构随存储实现变化）

### 6.12 POST /recommend/rag/stream

用途：RAG 流式推荐，响应为 SSE（`text/event-stream`）。

请求体（JSON）：

- `query`：必填，`string`，去空格后不能为空
- `n`：可选，`int`，默认 `8`，必须大于 `0`
- `rebuild_index`：可选，`bool`，默认 `false`

`rebuild_index` 支持值：

- 布尔类型 `true`/`false`
- 数字类型 `1`/`0`
- 字符串 `1`/`true`/`yes`/`on` 或 `0`/`false`/`no`/`off`

响应头：

- `Content-Type: text/event-stream`
- `Cache-Control: no-cache`
- `X-Accel-Buffering: no`

事件流格式：

1. start

```text
event: start
data: {"query":"诺兰 科幻","n":8}
```

2. movie（重复多次）

```text
event: movie
data: {"index":1,"item":{"movie_id":123,"title":"Inception","year":2010,"summary":"...","score":0.31}}
```

3. done

```text
event: done
data: {"count":8,"elapsed_ms":386}
```

错误场景：

- `query` 为空串或 `n <= 0` -> 400（JSON 错误响应）

## 7. 联调建议

- 普通接口按 JSON 协议对接，统一解析 `code`、`message`、`data`。
- RAG 接口按 SSE 客户端对接，事件类型使用 `start`、`movie`、`done`。
- 管理任务相关接口建议前端轮询 `GET /admin/tasks/<task_id>` 获取状态。
- 若部署环境配置了内部密钥，联调脚本必须统一加 `X-Internal-Secret`。

## 8. 代码来源（用于追溯）

- `app/__init__.py`
- `app/common/errors.py`
- `app/common/responses.py`
- `app/common/health.py`
- `app/common/runtime_health.py`
- `app/api/v1_recommend.py`
- `app/api/v1_search.py`
- `app/api/v1_admin.py`
- `app/api/v1_rag.py`
- `app/services/recommendation_service.py`
- `app/ops/admin_service.py`
