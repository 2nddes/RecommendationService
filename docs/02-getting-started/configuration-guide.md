# Configuration Guide

## 文档目标

本文档逐段解释 [config.json](../../config.json) 中当前出现的配置项，重点回答以下问题：

- 每个字段是做什么的
- 它影响哪个子系统
- 配错后会出现什么现象
- 哪些字段只影响训练，哪些字段直接影响在线请求

为了便于交付，本指南按配置段拆分，而不是按代码模块拆分。

## 1. 配置使用原则

### 1.1 先理解配置对系统的影响范围

这个项目的配置并不是“启动参数集合”，而是运行事实的一部分。配置直接决定：

- 连哪个数据库和缓存
- 加载哪个模型
- 使用哪个索引
- 用户推荐如何从缓存交付
- 搜索缓存策略
- RAG 用什么 provider
- Tag 倒排召回是否开启

### 1.2 修改配置时的基本顺序

建议每次都按这个顺序改：

1. 先改连接类配置：数据库、Redis、provider。
2. 再改路径类配置：模型、索引、向量库、离线文件。
3. 最后再改训练超参与缓存参数。

### 1.3 交付时最重要的安全要求

`rag.embedding_api_key` 和 `rag.llm_api_key` 当前是明文配置。对外交付前必须替换为安全注入方式，至少不要把真实 key 提交到共享仓库。

## 2. core 段

这一段决定全局基础连接和少量通用行为。

| 字段 | 作用 | 影响面 | 常见风险 |
| --- | --- | --- | --- |
| `mysql_dsn` | MySQL 连接串 | 搜索、推荐特征、训练、RAG 冷存储、任务系统几乎都依赖它 | 配错后不仅搜索失败，warmup、训练、任务查询也会失败 |
| `reranking_seed` | 重排阶段随机种子 | 影响 `RandomShuffleReranker` 的稳定性 | 为空时每次进程启动的重排顺序可能不稳定 |

### 额外说明

- `mysql_dsn` 是当前最关键的配置，没有可用的 MySQL，系统只会剩下极少部分功能。
- `reranking_seed` 只影响当前随机重排，不影响召回和排序得分本身。

## 3. redis 段

这一段决定 Redis 是否启用以及如何连接。

| 字段 | 作用 | 影响面 | 常见风险 |
| --- | --- | --- | --- |
| `enabled` | 是否启用 Redis | 决定缓存、倒排召回、推荐构建锁等功能是否可用 | 设为 false 时多项在线能力会退化 |
| `host` | Redis 主机地址 | 所有 Redis 交互 | 地址错误会导致缓存层失效 |
| `port` | Redis 端口 | 所有 Redis 交互 | 端口错误常表现为连接超时或拒绝连接 |
| `db` | Redis DB 编号 | Redis 键空间隔离 | 多环境共用实例时容易写错 DB |
| `username` | Redis 用户名 | 启用 ACL 时使用 | 未启用 ACL 时通常可为空 |
| `password` | Redis 密码 | 连接鉴权 | 密码错误会导致缓存初始化失败 |
| `ssl` | 是否启用 TLS | 云 Redis 或受管服务 | 本地 Redis 一般为 false，配错会导致连接失败 |
| `socket_timeout_s` | 读写超时 | 高并发下的缓存调用 | 过小可能导致误判 Redis 不稳定 |
| `connect_timeout_s` | 建连超时 | 启动和首次访问缓存 | 过小会放大网络抖动的影响 |

### 额外说明

- 即便某些接口在逻辑上允许 Redis 缺失，项目的设计目标仍然是假设 Redis 可用。
- 如果你在本地只想快速验证接口，Redis 也建议优先搭好，否则你看到的行为会偏离正常部署状态。

## 4. cache 段

这一段定义的是缓存系统行为，不只是“是否缓存”。它同时影响：

- 键空间命名
- TTL
- 用户推荐缓存交付模式
- 搜索缓存策略
- 热门榜预热规模

| 字段 | 作用 | 影响面 | 常见风险 |
| --- | --- | --- | --- |
| `key_prefix` | Redis 键前缀 | 所有缓存键 | 多环境共用 Redis 时如果不区分前缀，容易串数据 |
| `feature_ttl_seconds` | 用户 / 影片特征缓存 TTL | 在线特征读取 | TTL 过短会加重回源压力 |
| `recall_ttl_seconds` | 召回类缓存 TTL | 倒排召回、候选缓存一类场景 | TTL 太长可能让召回更新不及时 |
| `trending_refresh_interval_seconds` | 热门榜刷新间隔 | cache worker 与热门榜缓存 | 太大时榜单更新慢，太小时后台压力大 |
| `static_recall_refresh_interval_seconds` | 静态召回刷新间隔 | Tag 倒排预计算 | 若与业务更新节奏不匹配，倒排索引会陈旧 |
| `search_cache_ttl_seconds` | 搜索结果缓存 TTL | SearchService | 太短命中率低，太长可能缓存旧结果 |
| `search_cache_max_offset` | 允许缓存的最大 offset | 搜索缓存 | 防止深分页缓存污染 Redis |
| `search_cache_max_n` | 允许缓存的最大单页条数 | 搜索缓存 | 请求过大时不会缓存 |
| `trending_topk` | 热门榜预计算条数 | 热门榜缓存回填 | 太小可能导致后续切页或扩展不够用 |
| `static_recall_topk` | 静态召回预热条数 | Tag 倒排缓存 | 太小会缩窄候选池 |
| `user_reco_cache_size` | 单用户推荐缓存目标容量 | 用户推荐缓存构建 | 太小会导致 pop 模式很快耗尽 |
| `user_reco_ttl_seconds` | 用户推荐缓存 TTL | 用户推荐 | 太短会频繁回源重算 |
| `user_reco_build_lock_seconds` | 构建锁 TTL | 防止缓存击穿 | 太短可能在构建未完成前锁过期 |
| `user_reco_delivery_mode` | 用户推荐交付模式，`paged` 或 `pop` | API 协议与缓存行为 | 配错会直接改变接口参数规则 |

### 当前最需要理解的字段

#### `user_reco_delivery_mode`

这是一个会直接改变接口使用方式的字段：

- `paged`：客户端使用 `page` 和 `page_size`
- `pop`：客户端使用 `n`，服务端从 Redis list 头部弹出

如果你改了这个值，调用 `/api/v1/recommend/user` 的前端和测试脚本也要一起改。

#### `user_reco_build_lock_seconds`

这个值不是普通 TTL，而是推荐缓存防击穿的并发控制窗口。它过短时，可能出现同一个用户被多个请求同时重建推荐缓存。

## 5. log 段

| 字段 | 作用 | 影响面 | 常见风险 |
| --- | --- | --- | --- |
| `level` | 日志级别 | 全局日志系统 | 设为 DEBUG 会明显增加日志量 |
| `file_path` | 日志文件路径 | 根日志输出 | 路径不可写会导致日志初始化失败或无日志文件 |

### 额外说明

- 当前日志系统偏向文件输出。
- Windows 本地调试时，如果你只看终端而不看日志文件，容易漏掉启动阶段的重要异常。

## 6. two_tower 段

这一段同时控制在线召回运行时与离线训练行为，因此是最容易被误改的一段之一。

### 6.1 在线运行相关字段

| 字段 | 作用 | 影响面 | 常见风险 |
| --- | --- | --- | --- |
| `dim` | 向量维度 | 在线 ANN 与模型结构 | 与真实模型维度不一致会导致索引或向量不兼容 |
| `seed` | 随机种子 | 训练与部分稳定性 | 主要影响可复现性 |
| `alpha` | 两塔内部加权参数 | Two-Tower 行为 | 需要结合训练实现理解，轻易不要随意改 |
| `recent_item_limit` | 在线构建用户兴趣时使用的近期条数 | 用户向量构造 | 太小会丢掉近期兴趣，太大增加计算开销 |
| `exclude_recent_n` | 在线召回时排除的近期影片数 | 用户推荐去重 | 太小会推荐刚看过的内容 |
| `recall_topk` | ANN 召回候选数 | Recall 阶段候选池大小 | 太小会限制排序空间，太大增加下游开销 |
| `hr_eval_k` | 评估指标 top-k | 训练评估 | 对线上请求不直接生效 |
| `space` | HNSW 距离空间，例如 `cosine` | 向量索引与得分解释 | 与索引、向量归一化方式不一致会出问题 |
| `index_path` | 活跃 HNSW 索引路径 | 在线召回 | 文件不存在会导致 warmup 失败或召回不可用 |
| `vector_db_path` | 活跃向量库路径 | Two-Tower 产物管理 | 与 model / index 不一致会增加维护风险 |
| `model_path` | 活跃 Two-Tower 模型路径 | 在线召回与 warmup | 文件不存在时运行时无法初始化 |

### 6.2 训练相关字段

| 字段 | 作用 | 影响面 | 常见风险 |
| --- | --- | --- | --- |
| `train_epochs` | 训练轮数 | Two-Tower 训练 | 过小欠拟合，过大训练慢 |
| `train_batch_size` | 批大小 | 训练吞吐与显存 / 内存 | 太大可能超资源 |
| `train_lr` | 学习率 | 收敛速度 | 过大不稳定，过小收敛慢 |
| `train_reg` | 正则化强度 | 泛化能力 | 过大导致欠拟合 |
| `train_negatives` | 每个正样本的负样本数 | 训练样本质量 | 太低区分度不够，太高训练成本上升 |
| `train_limit` | 抽样上限 | 训练数据规模 | 太小会削弱模型效果 |
| `train_steps_per_epoch` | 每轮训练步数 | 训练时长与采样规模 | 设置不当会让每轮训练过少或过多 |
| `train_min_user_interactions` | 过滤低活跃用户阈值 | 训练数据清洗 | 过高会丢掉大量长尾用户 |
| `train_min_item_interactions` | 过滤低曝光物品阈值 | 训练数据清洗 | 过高会使新内容学习不足 |
| `train_use_in_batch_negatives` | 是否使用 batch 内负样本 | 训练效率与效果 | 改动会直接影响训练分布 |
| `train_in_batch_temperature` | batch 内对比温度 | 训练对比学习稳定性 | 不宜无评估地修改 |
| `train_id_dropout` | ID 特征 dropout | 泛化能力 | 过高会损失 ID 信号 |
| `train_enable_deep_encoder` | 是否启用更深的 encoder | 模型结构 | 改动可能导致与历史权重不兼容 |
| `train_deep_hidden_mult` | 深层 hidden 放大倍数 | 模型容量 | 增大后训练和推理成本上升 |
| `train_deep_dropout` | 深层 dropout | 泛化与稳定性 | 过高会欠拟合 |

### 额外说明

- 在线运行真正依赖的是 `model_path`、`index_path`、`space`、`recall_topk` 等字段。
- 训练产物即使生成成功，也不会自动改变这些路径，除非你刷新或切换活跃产物。

## 7. xgb 段

当前 XGBoost 更偏训练产物与备用排序能力，不是默认在线排序主路径。

| 字段 | 作用 | 影响面 | 常见风险 |
| --- | --- | --- | --- |
| `model_path` | 活跃 XGB 模型路径 | 备用排序 / 后续扩展 | 文件不存在时相关能力不可用 |
| `use_mysql_features` | 是否启用 MySQL 特征 | 训练 / 推理辅助 | 关闭后特征会退化 |
| `train_limit` | 训练样本上限 | XGB 训练规模 | 太小导致样本代表性不足 |
| `train_max_depth` | 树深 | 模型复杂度 | 过大易过拟合 |
| `train_eta` | 学习率 | 收敛速度 | 过大不稳定 |
| `train_subsample` | 行采样比例 | 泛化 | 太低会损失信息 |
| `train_colsample` | 列采样比例 | 泛化 | 太低可能削弱特征表达 |
| `train_rounds` | boosting 轮数 | 训练时长与效果 | 过多可能过拟合 |

## 8. mmoe 段

这段配置同时决定当前在线主排序模型的活跃权重路径和离线训练参数。

### 8.1 在线运行关键字段

| 字段 | 作用 | 影响面 | 常见风险 |
| --- | --- | --- | --- |
| `model_path` | 活跃 MMoE 模型路径 | 在线排序 | 文件缺失时在线排序无法初始化 |

### 8.2 训练与模型结构字段

| 字段 | 作用 | 影响面 | 常见风险 |
| --- | --- | --- | --- |
| `train_limit` | 训练样本上限 | MMoE 训练规模 | 太小代表性不足 |
| `train_epochs` | 训练轮数 | 模型收敛 | 过小训练不充分 |
| `train_batch_size` | 批大小 | 训练吞吐 | 过大可能占满资源 |
| `train_lr` | 学习率 | 收敛速度 | 调错容易不稳定 |
| `emb_dim` | embedding 维度 | 模型容量 | 改动会影响模型结构兼容性 |
| `num_experts` | expert 数量 | MMoE 容量 | 增大会增加计算成本 |
| `expert_hidden_dim` | expert hidden 维度 | 模型容量 | 同上 |
| `tower_hidden_dim` | task tower hidden 维度 | 各任务表示能力 | 同上 |
| `in_batch_neg_ratio` | batch 内负样本比例 | 训练样本分布 | 影响多任务学习稳定性 |
| `click_neg_user_pool` | 点击任务负采样用户池 | 训练数据构造 | 过小会降低负样本多样性 |
| `click_neg_movie_pool` | 点击任务负采样影片池 | 训练数据构造 | 同上 |
| `click_parquet_path` | 点击离线文件路径 | 预留离线路径 | 路径与当前流程不一致时需确认是否被实际使用 |
| `collect_parquet_path` | 收藏离线文件路径 | 同上 | 同上 |
| `rate_parquet_path` | 评分离线文件路径 | 同上 | 同上 |
| `comment_parquet_path` | 评论离线文件路径 | 同上 | 同上 |
| `global_neg_ratio` | 全局负样本比例 | 数据集构造 | 太低区分度不够 |
| `loss_weight_click` | 点击任务权重 | 多任务损失平衡 | 配错会改变训练重点 |
| `loss_weight_collect` | 收藏任务权重 | 多任务损失平衡 | 同上 |
| `loss_weight_rate` | 评分任务权重 | 多任务损失平衡 | 同上 |
| `loss_weight_comment` | 评论任务权重 | 多任务损失平衡 | 同上 |
| `enable_dynamic_pos_weight` | 是否启用动态正样本权重 | 类别不平衡处理 | 关闭后长尾正样本影响可能减弱 |
| `dynamic_pos_weight_cap` | 动态权重上限 | 训练稳定性 | 过高可能导致训练震荡 |

### 额外说明

- MMoE 是当前在线排序核心，因此 `model_path` 的有效性优先级极高。
- 结构类参数一旦改变，通常意味着旧权重不能直接兼容新结构。

## 9. rag 段

这一段决定 RAG 检索和生成能力的全部外部依赖与索引参数。

| 字段 | 作用 | 影响面 | 常见风险 |
| --- | --- | --- | --- |
| `embedding_api_base_url` | embedding 服务基地址 | 证据向量生成、RAG 重建 | 不可访问时 embedding 相关流程失败 |
| `embedding_api_path` | embedding API 路径 | 同上 | 路径拼接错误会返回 404 或协议错误 |
| `embedding_api_key` | embedding 鉴权密钥 | 同上 | 错误会导致鉴权失败 |
| `embedding_model_name` | embedding 模型名 | 向量维度与语义空间 | 改动后可能与已有向量不兼容 |
| `llm_api_base_url` | 聊天服务基地址 | 流式问答 | 不可访问时 RAG 回答失败 |
| `llm_api_path` | 聊天 API 路径 | 同上 | 路径不对会报协议错误 |
| `llm_api_key` | 聊天鉴权密钥 | 同上 | 错误会导致 401 / 403 |
| `llm_model_name` | 聊天模型名 | 回答质量与时延 | 不同模型会改变生成质量和成本 |
| `ann_topk_default` | 默认取回证据条数 | RAG evidence 检索 | 太小会降低证据覆盖，太大增加 prompt 长度 |
| `redis_result_ttl_seconds` | RAG 短期缓存 TTL | RAG query / item cache | 太短命中率低，太长可能缓存旧结果 |
| `index_hnsw_m` | FAISS HNSW M 参数 | 索引结构 | 与索引质量和内存开销相关 |
| `index_hnsw_ef_search` | HNSW 搜索宽度 | 检索质量与时延 | 太低召回不足，太高时延上升 |
| `embedding_summary_max_chars` | 写入 embedding 文本时截断简介长度 | 向量输入文本 | 过小损失语义，过大增加成本 |

### 最重要的运行约束

1. embedding 模型一旦变化，历史 `movie_embeddings` 向量可能需要整体重建。
2. RAG 不只是聊天接口依赖它，相似影片检索也优先复用 embedding。
3. provider 即便支持 OpenAI-compatible，也可能不严格支持流式协议，代码里已经做了兼容，但不意味着所有 provider 都等价。

## 10. tag_recall 段

这一段控制 Tag 倒排召回能力。

| 字段 | 作用 | 影响面 | 常见风险 |
| --- | --- | --- | --- |
| `enabled` | 是否启用 Tag 倒排召回 | 推荐候选构成 | 关闭后用户推荐只靠主召回 |
| `min_rating_count_m` | 最低评分人数阈值 | 倒排构建质量控制 | 太高会过滤掉过多内容 |
| `retain_topn_per_tag` | 每个标签保留的候选数 | Redis 倒排容量 | 太小会丢掉长尾候选 |
| `user_topk_tags` | 每个用户保留的偏好标签数 | 在线召回 | 太小会缩窄兴趣覆盖 |
| `per_tag_fetch_m` | 每个标签在线拉取候选数 | 合并候选池大小 | 太小会影响召回多样性 |
| `online_candidate_multiplier` | 在线扩容倍数 | 返回给排序器的候选规模 | 太小限制排序空间 |
| `high_rating_threshold` | 高评分阈值 | 用户偏好标签提取 | 阈值不合适会影响偏好质量 |
| `recent_interaction_limit` | 近期交互窗口 | 排除集与导演偏好抽取 | 太小会丢失上下文 |
| `director_endorsement_source` | 导演偏好来源策略 | 导演召回解释逻辑 | 当前值需与实际实现语义保持一致 |

### 额外说明

- 即使 `enabled=true`，如果 Redis 不可用或倒排缓存未预热，Tag 倒排召回也可能返回空。
- 这个模块是补充召回，不是主召回替代品。

## 11. 配置修改后的最小验证动作

每次修改配置后，至少执行以下验证：

1. 启动服务并检查日志文件是否正常创建。
2. 请求 `GET /api/v1/health`，确认服务没有立刻进入错误状态。
3. 请求 `GET /api/v1/health/runtime`，确认 warmup、pipeline、rag 组件状态。
4. 请求 `GET /api/v1/admin/status`，确认模型路径与运行态摘要。

如果你改的是以下配置，还应追加专项验证：

- 改了 `mysql_dsn`：验证搜索与任务查询。
- 改了 Redis：验证热门榜、用户推荐缓存和搜索缓存。
- 改了模型路径：验证 warmup 与推荐接口。
- 改了 RAG provider：验证流式问答和相似影片接口。

## 12. 配置交付建议

对真实交付场景，建议至少准备三套配置策略：

1. 本地开发配置
2. 测试 / 演示环境配置
3. 生产环境配置模板

并且应满足：

- 不把真实密钥直接提交到仓库
- 对路径类配置给出示例目录结构
- 对模型与索引路径给出明确约定

## 13. 下一步建议阅读

如果配置已经理解，下一步直接看：

- [first-run-checklist.md](first-run-checklist.md)
- [local-debugging-guide.md](local-debugging-guide.md)