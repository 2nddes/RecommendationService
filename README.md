# Python 推荐算法微服务接口设计文档 (v1.0)
## 架构与通信概述
通信协议: HTTP/1.1 (RESTful)
数据格式: JSON字符编码: UTF-8
服务定位: 内部微服务（不直接暴露给 Vue 前端，所有请求由 Spring Boot 代理）。
鉴权: 内部网络互信，或通过 Header 传递 X-Internal-Secret 简单校验。

### 通用响应格式
所有接口均返回统一的 JSON 结构：

JSON{
  "code": 200,          // 200 成功, 500 内部错误, 400 参数错误
  "message": "success", // 描述信息
  "data": { ... }       // 具体业务数据，出错时为 null
}

错误场景也使用同一结构，HTTP 状态码与 `code` 保持一致，例如参数错误：

JSON{
  "code": 400,
  "message": "invalid 'user_id', expected integer",
  "data": null
}

## 核心推荐接口 (Recommendation APIs)
这些接口主要服务于 C 端用户体验，由 Spring Boot 获取 ID 列表后，查询数据库组装电影详情返回给 Vue。

### 个性化推荐 (猜你喜欢)
基于 User-Based 或 Model-Based 协同过滤算法，根据用户历史行为计算推荐结果。

URL: /api/v1/recommend/user
Method: GET
描述: 传入用户ID，返回该用户可能感兴趣的电影 ID 列表。

|参数名|类型|必选|默认值|说明|
|---|---|---|---|---|
|user_id|Integer|是|-|用户的唯一标识|
|n|Integer|否|10|返回推荐的数量|

响应示例:

JSON{
  "code": 200,
  "message": "success",
  "data": {
    "user_id": 1001,
    "items": [1024, 8848, 3096, 5201, 1234]  // 电影 ID 列表
  }
}

## 召回实现说明（已落地）

本项目已实现“多通道召回”阶段，并通过根目录配置文件 `config.json` 接入 MySQL。

### 1) MySQL 连接

- 配置项（`config.json`）：`MYSQL_DSN`
- 示例:
  - `mysql+pymysql://user:password@127.0.0.1:3306/movie_recommend?charset=utf8mb4`

未配置 `MYSQL_DSN` 时，MySQL 召回通道会自动返回空列表，不会影响服务可用性。

### 2) 召回通道

`RECALL_CHANNELS` 在 `config.json` 中推荐使用数组（也兼容逗号分隔字符串），支持以下名称：

- `user_collection`：用户收藏影片 -> `rec_similarity` 相似影片召回
- `user_high_rating_similar`：用户高评分影片 -> `rec_similarity` 相似影片召回
- `user_interest_tag`：用户兴趣标签（静态/动态）-> 按标签权重聚合召回
- `item_similar_by_tags`：给 `/recommend/item` 用的标签交集相似召回

若不设置 `RECALL_CHANNELS`，默认启用：

- `user_collection,user_high_rating_similar,user_interest_tag`

### 3) 可调参数（可选，均在 `config.json` 中配置）

- `RECALL_TOPK_USER_COLLECTION`（默认 200）
- `RECALL_PER_SEED_TOPK_USER_COLLECTION`（默认 50）
- `RECALL_TOPK_USER_HIGH_RATING`（默认 300）
- `RECALL_RATING_THRESHOLD`（默认 8）
- `RECALL_TOPK_USER_INTEREST_TAG`（默认 300）
- `RECALL_TOPK_ITEM_SIMILAR_TAG`（默认 200）

## 排序阶段（XGBoost / MMoE）

本项目支持通过 `config.json` 切换排序器，排序阶段当前可使用 `xgb` 与 `mmoe`。

- `RANKING_METHOD`
  - `cf` / `xgb` / `mmoe`
  - 示例：在 `config.json` 中设置 `"RANKING_METHOD": "xgb"`
- `XGB_MODEL_PATH`：XGBoost 模型文件路径（可选）。
  - 未提供时会自动回退到“手工权重打分”，保证服务可用。
- `XGB_USE_MYSQL_FEATURES`：是否从 MySQL 拉取影片侧特征（`movie.rating_avg/rating_count/year/duration_min`）。
  - `true`（默认）开启；`false` 关闭。
- `XGB_ALLOW_FALLBACK`：当未安装 xgboost 或模型加载失败时是否允许回退。
  - 默认 `true`，用于开发/部署早期不阻塞服务启动。

### MMoE 多任务精排（MySQL 真数据训练）

- `MMOE_MODEL_PATH`：MMoE 模型路径（可选）。
- `MMOE_TRAIN_LIMIT` / `MMOE_TRAIN_EPOCHS` / `MMOE_TRAIN_BATCH_SIZE` / `MMOE_TRAIN_LR`：训练参数。
- 任务目标：`click`、`collect`、`comment`、`rating`。
- 标签定义：
  - 点击 = 1（`user_action.action_type='view'`）
  - 收藏 = 1（`user_action.action_type='collect'` 或 `user_collect_movie`）
  - 评论 = 1（`user_action.action_type='comment'` 或 `movie_comment`）
  - 评分 = 1（`rating.rating > 5`）
- 数据仅从 MySQL 拉取，不使用模拟/造数。

手工特征的入口在 [app/reco/ranking/xgb_features.py](app/reco/ranking/xgb_features.py)，后续要替换算法时建议保留特征构造模块不变，仅替换 ranker/scorer 实现。

### 相似影片推荐 (看了又看)
基于 Item-Based 协同过滤或 Content-Based (Embedding 相似度)。

URL: /api/v1/recommend/item
Method: GET
描述: 在电影详情页使用。传入当前电影 ID，返回相似电影。

|参数名|类型|必选|默认值|说明|
|---|---|---|---|---|
|movie_id|Integer|是|-|当前电影ID|
|n|Integer|否|8|返回数量|

响应示例:

JSON{
  "code": 200,
  "message": "success",
  "data": {
    "source_id": 1024,
    "items": [2048, 4096, 5012]
  }
}

### 趋势推荐 (热门榜单)
基于时间窗口内的交互热度或评分加权统计。

URL: /api/v1/recommend/trending
Method: GET
描述: 获取全站热门、周榜、月榜等。

|参数名|类型|必选|默认值|说明|
|---|---|---|---|---|
|window|String|否|weekly|时间窗: daily, weekly, monthly, all_time|
|n|Integer|否|10|返回数量|

响应示例:

JSON{
  "code": 200,
  "message": "success",
  "data": {
    "window": "weekly",
    "items": [101, 102, 103]
  }
}

实现说明（当前版本）：

- 数据来源：`movie` + `user_action`
- 时间窗：`daily` / `weekly` / `monthly` / `all_time`
- 排序分：评分均值、评分人数、窗口内行为数加权组合
- 返回值：热门电影 ID 列表（由 Java 侧再查详情）

## 搜索服务接口 (Search APIs)
虽然简单的 SQL LIKE 查询可以在 Java 端做，但 Python 端可以利用 NLP 技术做语义搜索 (Semantic Search)。

## RAG 流式推荐接口 (LangChain + FAISS)

新增接口：`POST /api/v1/recommend/rag/stream`

- 使用 `LangChain` 组织检索流程。
- 向量库使用 `FAISS`，索引目录来自 `RAG_FAISS_DIR`。
- Embedding 模型默认 `BAAI/bge-large-zh-v1.5`（可通过 `RAG_EMBEDDING_MODEL_NAME` 修改）。
- 返回类型为 `text/event-stream`，事件包含：`start`、`movie`、`done`。

请求体示例:

JSON{
  "query": "想看高分悬疑推理电影",
  "n": 8,
  "rebuild_index": false
}

事件示例:

```
event: start
data: {"query":"想看高分悬疑推理电影","n":8}

event: movie
data: {"index":1,"item":{"movie_id":123,"title":"...","year":2019,"summary":"...","score":0.31}}

event: done
data: {"count":8,"elapsed_ms":386}
```

相关配置项（`config.json`）:

- `RAG_EMBEDDING_MODEL_NAME`：默认 `BAAI/bge-large-zh-v1.5`
- `RAG_FAISS_DIR`：默认 `data/faiss/movie_rag`
- `RAG_FAISS_INDEX_NAME`：默认 `movie_index`
- `RAG_BUILD_LIMIT`：构建索引时最多读取电影条数，默认 `50000`

### 混合搜索
URL: /api/v1/search
Method: POST (使用 POST 以便扩展复杂的过滤条件)
描述: 支持关键词模糊匹配或向量语义检索。

请求体:

JSON{
  "query": "科幻 诺兰 时间旅行",  // 搜索关键词
  "n": 20,
  "filters": {
     "genre": "Sci-Fi",    // 可选过滤
     "year_min": 2010
  }
}

响应示例:

JSON{
  "code": 200,
  "message": "success",
  "data": {
    "total": 5,
    "results": [
      {"id": 550, "score": 0.98},
      {"id": 880, "score": 0.85}
    ]
  }
}

## 管理员管理接口 (Admin/Ops APIs)
这些接口由 Java 后台管理系统触发，用于控制 Python 服务的状态和数据更新。

### 触发模型重训练 (全量)
URL: /api/v1/admin/train
Method: POST
描述: 强制 Python 服务从数据库重新拉取全量数据，重新构建相似度矩阵或训练神经网络模型。此过程可能耗时。

请求体字段（可选）:
- mode: full | incremental（默认 full）
- component（或 module）: recall | ranking
- model: two_tower | xgb | mmoe

说明:
- 不传 component/module 和 model：按当前配置训练已启用模型（保持原行为）
- 只传 component：训练该模块下支持的模型（当前 recall=two_tower, ranking=xgb/mmoe）
- 只传 model：按模型名定向训练（two_tower、xgb 或 mmoe）
- 同时传 component + model：训练指定模块中的指定模型（若不匹配将返回 400）

响应示例:

JSON{
  "code": 200,
  "message": "Training task started",
  "data": {
    "task_id": "task_20231027_001",
    "estimated_time": "30s"
  }
}

### 查询任务列表
URL: /api/v1/admin/tasks
Method: GET
描述: 查询后台任务列表（支持内存任务与数据库训练任务聚合查询）。

查询参数（可选）:
- source: all | memory | db（默认 all）
- status: pending | running | succeeded | failed
- limit: 正整数（默认 20）
- offset: 非负整数（默认 0）

响应示例:

JSON{
  "code": 200,
  "message": "success",
  "data": {
    "items": [
      {
        "id": "2",
        "name": "train_job",
        "status": "succeeded",
        "source": "db",
        "created_at": "2026-02-25T12:40:26",
        "started_at": null,
        "finished_at": "2026-02-25T12:41:13",
        "error": null,
        "result": {
          "train_job_id": 2,
          "mode": "full",
          "status": "completed",
          "metrics": {}
        }
      }
    ],
    "total": 1,
    "limit": 20,
    "offset": 0,
    "source": "all",
    "status": null
  }
}

### 刷新缓存/增量更新
URL: /api/v1/admin/refresh
Method: POST
描述: 当有新电影入库或用户冷启动数据产生时，通知 Python 更新局部特征缓存，无需全量训练。