# Repository Map

## 文档目标

本文档的目标不是简单罗列目录，而是帮助你快速找到“某类问题应该从哪里开始看代码”。

对这个项目来说，按目录树机械阅读效率很低，因为真正重要的是入口关系：

- Web 入口在哪
- 推荐链路从哪开始
- 搜索链路从哪开始
- RAG 从哪开始
- 任务系统从哪开始

## 1. 顶层结构速览

```text
RecommendationService/
├─ app.py
├─ config.json
├─ requirements.txt
├─ pyproject.toml
├─ all.sql
├─ app/
├─ data/
├─ models/
├─ logs/
└─ docs/
```

## 2. 顶层文件说明

### app.py

这是 Web 服务入口。你只需要知道两件事：

- 它不是业务逻辑文件。
- 它负责调用应用工厂创建 Flask app。

如果你要理解启动流程，读完它后应该立刻跳到 [app/__init__.py](../../app/__init__.py)。

### config.json

这是当前项目的核心配置来源。它控制：

- MySQL 与 Redis 连接
- 模型路径与索引路径
- 缓存行为
- RAG provider
- Tag 倒排召回开关

如果服务“能启动但行为不对”，第一时间应该检查这里，而不是先怀疑代码。

### all.sql

这是数据库结构事实来源。它定义的不是“示意表”，而是当前系统真正依赖的表结构。

如果你要理解：

- 训练数据来自哪里
- 搜索字段从哪里来
- RAG embedding 存在哪
- 后台任务如何持久化

那就一定要看这个文件。

## 3. app/ 目录怎么读

### 3.1 app/__init__.py

角色：应用工厂。

负责：

- 初始化 Flask
- 注册蓝图
- 注册异常处理器
- 触发 warmup 和后台线程启动

这是整个运行期真正的系统入口。

### 3.2 app/api/

角色：HTTP 接口层。

特点：

- 尽量只做参数校验与路由分发
- 把实际业务交给 service 或 ops 层

如果你在看接口行为，优先顺序是：

1. 先看 `v1.py` 了解蓝图结构
2. 再看具体 `v1_recommend.py`、`v1_search.py`、`v1_rag.py`、`v1_admin.py`

### 3.3 app/common/

角色：横切关注点。

重点文件：

- `settings.py`：配置模型
- `logging_setup.py`：日志初始化
- `errors.py`：统一异常处理
- `responses.py`：统一响应包装
- `validation.py`：参数校验
- `runtime_health.py`：运行态快照
- `redis_cache.py`：Redis 键和缓存实现

如果你遇到“同样的异常为什么响应格式一致”“健康接口的数据从哪里来”“Redis key 是怎么定的”，都应该先来这里。

### 3.4 app/services/

角色：接口层与引擎层之间的服务门面。

重点文件：

- `recommendation_service.py`
- `search_service.py`
- `runtime_health_service.py`

这是大多数 API 真正的业务协调层。建议把这里视为“每条接口链路的第一个业务入口”。

### 3.5 app/repositories/

角色：数据访问组织层。

重点文件：

- `cache_repository.py`：Redis 缓存访问包装
- `search_repository.py`：搜索 SQL 构造与执行
- `trending_repository.py`：热门榜数据库打分与读取

如果你关心的是“数据是怎么查出来的”，而不是“接口怎么接收参数”，这里才是关键区域。

### 3.6 app/reco/

角色：推荐与检索引擎核心目录。

这是整个仓库最重要、也最容易让人一开始读乱的目录。建议不要直接深挖子目录，而是按下面顺序看：

1. `factory.py`
2. `pipeline.py`
3. `types.py`
4. `startup.py`
5. `runtime.py`
6. 再进入 recall / ranking / reranking / rag_service

原因很简单：

- `factory.py` 告诉你现在系统到底装配了什么组件。
- `pipeline.py` 告诉你这些组件如何协作。
- `types.py` 告诉你各阶段交换的数据长什么样。
- `startup.py` 告诉你系统何时初始化这些组件。

### 3.7 app/ops/

角色：运行维护与后台任务层。

重点文件：

- `task_ops.py`：统一任务表操作
- `model_ops.py`：训练任务与模型刷新
- `train_worker.py`：训练 worker
- `rag_embedding_ops.py`：RAG 重建任务状态操作
- `rag_embedding_worker.py`：RAG 重建 worker
- `cache_ops.py`：缓存和搜索统计预计算
- `admin_service.py`：管理接口聚合服务

如果你要理解“后台任务为什么能被创建、查询、消费和更新”，必须读这个目录，而不是去接口层停留。

## 4. 按目标读代码：推荐从哪里开始

### 目标 A：我要看用户推荐怎么出来的

推荐阅读顺序：

1. [app/api/v1_recommend.py](../../app/api/v1_recommend.py)
2. [app/services/recommendation_service.py](../../app/services/recommendation_service.py)
3. [app/reco/factory.py](../../app/reco/factory.py)
4. [app/reco/pipeline.py](../../app/reco/pipeline.py)
5. [app/reco/recall/two_tower/recaller.py](../../app/reco/recall/two_tower/recaller.py)
6. [app/reco/recall/tag_inverted.py](../../app/reco/recall/tag_inverted.py)
7. [app/reco/ranking/mmoe/ranker.py](../../app/reco/ranking/mmoe/ranker.py)
8. [app/reco/reranking/random_shuffle.py](../../app/reco/reranking/random_shuffle.py)

### 目标 B：我要看搜索为什么这样排

推荐阅读顺序：

1. [app/api/v1_search.py](../../app/api/v1_search.py)
2. [app/services/search_service.py](../../app/services/search_service.py)
3. [app/repositories/search_repository.py](../../app/repositories/search_repository.py)
4. [app/ops/cache_ops.py](../../app/ops/cache_ops.py)

### 目标 C：我要看 RAG 怎么做的

推荐阅读顺序：

1. [app/api/v1_rag.py](../../app/api/v1_rag.py)
2. [app/reco/rag_service.py](../../app/reco/rag_service.py)
3. [app/reco/rag_clients.py](../../app/reco/rag_clients.py)
4. [all.sql](../../all.sql) 中的 `movie_embeddings` 表结构

### 目标 D：我要看训练和后台任务怎么跑

推荐阅读顺序：

1. [app/api/v1_admin.py](../../app/api/v1_admin.py)
2. [app/ops/admin_service.py](../../app/ops/admin_service.py)
3. [app/ops/task_ops.py](../../app/ops/task_ops.py)
4. [app/ops/model_ops.py](../../app/ops/model_ops.py)
5. [app/ops/train_worker.py](../../app/ops/train_worker.py)
6. [app/ops/rag_embedding_ops.py](../../app/ops/rag_embedding_ops.py)
7. [app/ops/rag_embedding_worker.py](../../app/ops/rag_embedding_worker.py)

## 5. reco/ 子目录职责细分

### factory.py

负责装配当前在线推荐 pipeline。你可以把它理解为“在线能力清单”。

它直接告诉你：

- 当前用了哪些 recaller
- 当前用了哪个 ranker
- 当前用了哪个 reranker

### pipeline.py

负责串联召回、排序、重排，是真正的在线推荐执行骨架。

### startup.py

负责：

- warmup
- pipeline 初始化
- RAG 初始化
- 缓存预计算
- 后台线程启动

如果你不知道“为什么一启动就开始做很多事”，答案在这里。

### runtime.py

负责推荐 pipeline 的全局单例状态。适合用来理解：

- 运行时为何不在每个请求里重建 pipeline
- refresh 后为什么在线模型会切换

### recall/

负责召回阶段。

当前最重要的是：

- `two_tower/`：主召回
- `tag_inverted.py`：补充召回

### ranking/

负责排序阶段。

当前：

- 在线主排序是 `mmoe/`
- `xgb/` 主要体现为训练链路与备用排序能力

### reranking/

负责结果顺序最终处理。当前只有随机打散实现。

### rag_service.py

虽然它位于 reco 目录下，但它不只是“另一个推荐器”，而是一个独立的语义检索与生成子系统。

## 6. data/、models/、logs/ 分别意味着什么

### data/

当前承载：

- 训练产物历史目录
- Two-Tower 活跃索引或向量库文件
- 管理状态文件 `admin_state.json`

### models/

仓库快照中存在该目录，但当前配置默认指向的是 `data/models/*`。这意味着：

- 目录存在并不代表当前运行时一定会使用这里的文件。
- 真正生效的是 `config.json` 中的路径。

### logs/

运行日志默认写到配置中的日志文件路径。不要只盯控制台，当前日志系统更偏向文件输出。

## 7. 如果你要改某类东西，优先看哪里

| 你要改的东西 | 优先看这些文件 |
| --- | --- |
| 新增推荐接口 | `app/api/v1_recommend.py`、`app/services/recommendation_service.py` |
| 调整用户推荐缓存逻辑 | `app/services/recommendation_service.py`、`app/common/redis_cache.py` |
| 调整搜索过滤条件 | `app/api/v1_search.py`、`app/repositories/search_repository.py` |
| 调整热门榜计算 | `app/repositories/trending_repository.py`、`app/ops/cache_ops.py` |
| 调整 RAG provider | `app/common/settings.py`、`app/reco/rag_clients.py`、`config.json` |
| 调整 RAG 检索逻辑 | `app/reco/rag_service.py` |
| 新增任务类型 | `app/ops/task_ops.py`、`app/ops/admin_service.py`、对应 worker |
| 调整 warmup 行为 | `app/reco/startup.py` |
| 调整统一错误响应 | `app/common/errors.py`、`app/common/responses.py` |

## 8. 最后一个建议

这个仓库最容易犯的阅读错误是“一头扎进某个模型目录”。

在你知道系统整体是如何装配之前，不要先看：

- 某个训练脚本的细节
- 某个 SQL 片段的局部实现
- 某个模型内部网络结构

先看入口，再看装配，再看链路，再看实现细节，效率会高很多。