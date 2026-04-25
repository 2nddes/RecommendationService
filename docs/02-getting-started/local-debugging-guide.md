# Local Debugging Guide

## 文档目标

这不是一份泛泛的“调试建议”清单，而是一份面向当前仓库实际问题形态的排障手册。重点覆盖：

- 启动失败
- warmup 失败
- 模型或索引未就绪
- Redis / MySQL 连接失败
- 搜索结果异常
- RAG 不可用
- 训练与任务相关问题

## 使用方式

排障时请优先遵循这个顺序：

1. 先看日志文件。
2. 再看 `/api/v1/health/runtime`。
3. 再看 `/api/v1/admin/status`。
4. 最后再回到具体模块代码。

不要一上来就盲目改代码。这个项目多数问题都出在：

- 配置
- 路径
- 外部依赖
- 启动期资源未就绪

## 1. 服务启动后立即退出

### 典型现象

- `python app.py` 执行后进程很快结束。
- 终端只看到异常摘要。
- 还没来得及请求接口服务就退出。

### 先看哪里

1. [config.json](../../config.json)
2. 日志文件路径对应的日志
3. 模型和索引文件路径

### 最常见原因

- MySQL DSN 错误
- Redis 初始化失败
- Two-Tower 活跃模型不存在
- Two-Tower HNSW 索引不存在
- MMoE 模型不存在
- RAG provider 配置错误，且 warmup 阶段初始化失败

### 建议动作

1. 确认 `core.mysql_dsn` 能连通。
2. 确认 Redis 地址与端口正确。
3. 确认 `two_tower.model_path`、`two_tower.index_path`、`mmoe.model_path` 实际存在。
4. 如果错误是 `ModuleNotFoundError: hnswlib`，先安装 `hnswlib`。

## 2. 服务能启动，但 health 是 degraded

### 典型现象

- `/api/v1/health` 返回 `degraded`
- `/api/v1/health/runtime` 中 `overall.ready=false`

### 重点看哪些字段

- `overall.warmup_ready`
- `overall.pipeline_ready`
- `overall.rag_ready`
- `components` 中各组件的 `status`、`last_error`

### 常见原因

- warmup 没完成
- pipeline 初始化失败
- RAG 初始化失败
- 某个后台 worker 启动失败

### 建议动作

1. 找出第一个 `status=error` 的组件。
2. 看它的 `last_error.type` 和 `last_error.message`。
3. 回到对应模块处理，而不是在接口层瞎查。

## 3. 推荐接口返回空结果

### 典型现象

- `/api/v1/recommend/user` 成功返回，但 `items` 为空
- 热门榜或相似片结果很短甚至为空

### 推荐优先排查顺序

1. 确认测试使用的 `user_id` 或 `movie_id` 在数据库中真实存在。
2. 检查 pipeline 是否 ready。
3. 检查 Two-Tower 模型和索引是否可加载。
4. 如果开启了 Tag 倒排召回，检查 Redis 是否可用、倒排缓存是否已预热。
5. 检查用户近期行为是否过少，导致召回信号不足。(推测)

### 特别注意：用户推荐交付模式

当前配置若为 `pop`：

- 你必须使用 `n`
- 不能使用 `page` / `page_size`

否则你会得到参数错误，而不是结果为空。

## 4. 推荐接口非常慢

### 典型原因

- Redis 不可用，导致缓存层退化
- 用户推荐缓存频繁 miss
- 构建锁时间过短，导致重复构建
- Two-Tower 或 MMoE 初始化虽然成功，但模型 / 索引路径指向了不合适的大文件或慢存储

### 建议动作

1. 查看 Redis 是否真的连通。
2. 检查日志中是否存在频繁的 cache miss 和 build lock 相关日志。
3. 检查 `cache.user_reco_cache_size` 和 `cache.user_reco_build_lock_seconds` 是否合理。

## 5. 搜索接口报错

### 典型现象

- `/api/v1/search` 返回 500
- 搜索有时成功有时失败

### 重点排查项

1. `core.mysql_dsn`
2. `movie` 表和相关索引是否存在
3. 搜索预计算字段是否可用，例如 `collect_count`、`bayesian_rating`
4. 查询参数是否非法，例如时间范围、时长范围、排序字段

### 常见情况

#### 情况 A：fulltext 失败

仓库已经内置 fulltext 到 like 的降级逻辑，因此 fulltext 失败不一定会直接报错，但会影响性能或结果质量。

#### 情况 B：browse 模式返回异常结果

这通常说明：

- 数据库表结构不完整
- 筛选字段值异常
- 预计算统计没有更新

## 6. RAG 接口无响应或直接报错

### 典型现象

- SSE 连接建立后没有内容
- 返回 `error` 事件
- 请求直接 500

### 重点排查项

1. `rag.embedding_*` 和 `rag.llm_*` 配置是否正确。
2. provider 是否可访问。
3. `movie_embeddings` 表是否已有数据。
4. RAG 索引是否初始化成功。

### 常见原因

#### provider 鉴权失败

表现为：

- HTTP 401 / 403
- 日志中出现 provider request failed 或 http error

#### provider 不支持标准流式

代码里已经兼容了一部分“`stream=true` 但返回整包 JSON”的情况，但如果 provider 返回格式更偏离协议，仍然会失败。

#### embedding 向量维度与已有索引不兼容

如果你切换了 embedding 模型，但没有重建 `movie_embeddings` 和索引，可能出现维度不匹配或结果异常。

## 7. 相似影片接口失败

### 典型现象

- `/api/v1/recommend/item` 返回 500
- 明明有影片，结果却报 `Item vector not found`

### 机制提醒

相似片检索有两条路径：

1. 优先走 RAG embedding
2. 不可用时回退到 Two-Tower item vector

### 排查顺序

1. 该 `movie_id` 是否存在且是 `published`。
2. RAG 索引是否 ready。
3. 该影片是否已有 embedding。
4. Two-Tower runtime 是否已初始化。
5. Two-Tower 是否能构建该影片的 item vector。

## 8. admin/status 返回的模型路径和你以为的不一致

### 典型现象

- 仓库里明明有文件，但 `admin/status` 显示当前配置路径不存在
- 训练过后管理状态显示最新产物，但线上仍然不是那个文件

### 原因

系统区分：

- 配置指定的活跃路径
- 历史训练产物路径
- `admin_state.json` 中记录的最新训练产物

这三者如果没有统一约定，很容易让人误判“训练已生效”。

### 建议动作

1. 先看 [config.json](../../config.json) 中的活跃路径。
2. 再看 `/api/v1/admin/status` 中 `models` 与 `artifacts`。
3. 最后再检查磁盘上的实际文件。

## 9. 训练任务一直不动

### 典型现象

- `/api/v1/admin/train` 能创建任务
- `/api/v1/admin/tasks` 中任务一直是 `pending`

### 常见原因

- 训练 worker 没启动
- worker 启动了但崩溃
- MySQL 连接不可用，worker 无法 claim 任务

### 建议动作

1. 看 `/api/v1/health/runtime` 中 `train_queue_worker` 的状态。
2. 看日志中是否有 `Train queue worker crashed` 一类信息。
3. 检查 `ops_task` 表中任务是否真的写入成功。

## 10. RAG 重建任务一直失败或停住

### 典型现象

- `/api/v1/admin/rag/rebuild` 创建了任务
- 任务失败或一直不完成

### 常见原因

- embedding provider 不可用
- `movie_embeddings` 写入异常
- `load_from_mysql()` 重建索引失败
- worker 重启前遗留 processing 任务

### 建议动作

1. 看 `/api/v1/health/runtime` 中 `rag_rebuild_worker` 状态。
2. 看任务表中的 `progress` 和 `result`。
3. 检查失败任务的 `error` 字段。
4. 检查 provider 网络与鉴权。

## 11. 搜索有结果，但排序看起来不合理

### 先别急着怀疑模型

搜索的排序主要依赖 SQL 排序规则和预计算统计字段，不是在线排序模型。

优先检查：

- `sort_by` 是否符合预期
- `sort_order` 是否正确
- `collect_count` 与 `bayesian_rating` 是否已更新
- query 是否触发了 fulltext 还是 like

## 12. 日志里信息很多，但不知道先看什么

### 建议先看这几类信息

1. 启动阶段：warmup 是否完成。
2. pipeline 初始化：模型和索引是否加载成功。
3. RAG 初始化：FAISS 是否 ready。
4. worker 状态：是否 crashed。
5. 当前请求对应的 service 日志：例如 recommendation、search、rag。

### 不建议的做法

不要一上来就全局搜索异常字符串而不结合组件状态，因为这个项目启动期和请求期都会打很多日志，脱离上下文容易误判。

## 13. 推荐的排障最短路径

面对大多数问题，建议按这个路径走：

1. 看 [config.json](../../config.json)
2. 看日志文件
3. 请求 `/api/v1/health/runtime`
4. 请求 `/api/v1/admin/status`
5. 定位到对应模块代码

## 14. 如果问题依然无法定位

请至少收集以下信息再继续深入：

- 当前 `config.json` 的有效配置片段
- 日志中的首个异常堆栈
- `/api/v1/health/runtime` 返回结果
- `/api/v1/admin/status` 返回结果
- 触发问题的具体接口与参数

这些信息足够你或其他维护者快速缩小问题范围。