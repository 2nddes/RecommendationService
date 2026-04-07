# RecommendationService 架构详细说明

- 文档日期: 2026-04-02
- 文档目标: 以工程落地视角说明项目整体架构、关键模块职责、核心数据与控制流、训练运维机制、稳定性设计与后续演进方向。

## 1. 项目定位与总体架构

RecommendationService 是一个面向内部系统的推荐微服务，承担以下职责:

1. 提供推荐相关接口能力（个性化推荐、相似推荐、趋势推荐、RAG 流式推荐）。
2. 管理推荐模型训练与刷新流程（异步任务、状态查询、产物登记）。
3. 将推荐逻辑拆分为可演进的三段式流水线（召回、排序、重排）。

系统采用分层模块化架构，主路径为:

客户端请求 -> API 接口层 -> 推荐流水线 -> 召回模块 -> 排序模块 -> 重排模块 -> 统一响应

并行存在运维路径:

管理员请求 -> Admin 接口层 -> 任务管理器 -> 训练主流程 -> 产物落盘/登记 -> 模型刷新

## 2. 目录级架构分层

### 2.1 启动与应用装配层

- app.py
  - 进程入口，启动 Flask 服务。
- app/__init__.py
  - create_app 工厂，完成日志初始化、路由注册、错误处理注册、启动后台任务。

该层职责是把通用能力组装起来，不承载业务算法细节。

### 2.2 接口层 API

- app/api/v1.py
  - 聚合并注册业务蓝图。
- app/api/v1_recommend.py
  - 推荐主接口（用户推荐、相似推荐、趋势推荐）。
- app/api/v1_rag.py
  - RAG 流式推荐接口（SSE）。
- app/api/v1_admin.py
  - 运维接口（训练、刷新、任务状态、系统状态）。
- app/api/v1_search.py
  - 搜索接口占位，当前返回空结果结构。

接口层主要处理参数校验、上下文构建、调用业务模块并返回统一响应。

### 2.3 推荐业务层 reco

- app/reco/pipeline.py
  - RecommendationPipeline，统一编排 recall -> rank -> rerank。
- app/reco/factory.py
  - 当前默认装配 two_tower 召回 + mmoe 排序 + random_shuffle 重排。
- app/reco/recall/
  - 召回能力（当前主实现为 two_tower）。
- app/reco/ranking/
  - 排序能力（当前主实现为 mmoe）。
- app/reco/reranking/
  - 重排能力（当前实现随机扰动策略）。
- app/reco/rag_service.py
  - RAG 检索服务，基于 LangChain + FAISS。
- app/reco/startup.py
  - 启动后后台构建与周期刷新任务。

该层是推荐核心算法与流程的承载层。

### 2.4 运维与训练层 ops

- app/ops/admin_service.py
  - 训练任务创建、任务查询、状态聚合。
- app/ops/tasks.py
  - 进程内线程任务执行器。
- app/ops/model_ops.py
  - 训练主流程与分发（MMoE、Two-Tower、XGB）。
- app/ops/artifact_store.py
  - 产物状态持久化（data/admin_state.json）。

该层负责离线训练和线上模型状态协同，是工程可运营性的核心。

### 2.5 公共基础层 common

- app/common/config.py
  - 从 config.json 读取配置。
- app/common/settings.py
  - 业务使用的强类型配置对象。
- app/common/logging_setup.py
  - 日志初始化与轮转。
- app/common/errors.py
  - 异常处理注册。
- app/common/responses.py
  - 统一响应结构。
- app/common/validation.py
  - 参数校验。
- app/common/health.py
  - 健康检查接口。

该层提供稳定的基础设施能力，减少业务层重复代码。

## 3. 核心请求链路说明

### 3.1 用户个性化推荐链路

入口接口:

- GET /api/v1/recommend/user

执行步骤:

1. API 层解析 user_id、n。
2. 通过 Settings 构建 pipeline。
3. 进入 RecommendationPipeline.recommend。
4. 召回阶段 Two-Tower 生成候选集。
5. 排序阶段 MMoE 对候选打分。
6. 重排阶段 random_shuffle 调整顺序。
7. 返回最终 item id 列表。

设计要点:

- 召回负责扩大覆盖，排序负责精准度，重排负责结果形态控制。
- pipeline 内部统一做候选去重与多召回源合并。

### 3.2 相似影片推荐链路

入口接口:

- GET /api/v1/recommend/item

执行步骤:

1. 根据 movie_id 构建物品向量。
2. 在 ANN 索引中检索近邻。
3. 过滤自身 movie_id，截断到 n。
4. 返回相似物品列表。

设计要点:

- 该链路直接使用向量检索，不经过完整 pipeline。

### 3.3 趋势推荐链路

入口接口:

- GET /api/v1/recommend/trending

执行步骤:

1. 校验时间窗口参数（daily、weekly、monthly、half_year、one_year、all_time）。
2. SQL 聚合 movie 与 user_click 的热度信号。
3. 按综合得分排序输出热门列表。

设计要点:

- 使用统计规则而非个性化模型，适合冷启动流量与榜单位。

### 3.4 RAG 流式推荐链路

入口接口:

- POST /api/v1/recommend/rag/stream

执行步骤:

1. 接口读取 query、n、rebuild_index。
2. 获取 MovieRagService。
3. 加载或重建 FAISS 索引。
4. 进行语义相似检索并逐条通过 SSE 推送。
5. 返回 start、movie、done 事件。

设计要点:

- 采用流式返回降低首包等待感。
- 支持强制重建索引用于数据更新后的快速恢复。

## 4. 推荐算法架构详解

### 4.1 召回层 Two-Tower

核心思想:

- 通过双塔编码器分别学习用户向量和物品向量。
- 在线按用户向量召回近邻物品。

离线阶段:

1. 从行为表构建训练样本。
2. 使用 BPR 风格目标训练编码器。
3. 导出用户向量、物品向量与 encoder 元数据。
4. 物品向量写入向量库并构建 HNSW 索引。

在线阶段:

1. 根据用户画像与最近行为构建用户向量。
2. ANN 检索候选，过滤已交互物品。
3. 输出候选给排序层。

工程点:

- 支持索引缓存与变更自动刷新。
- 无 hnswlib 时可回退到 numpy 检索，保证可用性。

### 4.2 排序层 MMoE

核心思想:

- 使用多任务专家网络同时学习 click、collect、comment、rating 目标。
- 通过共享 experts 提升任务间迁移，通过任务塔保持目标差异。

训练阶段:

1. 从多表行为构建多任务标签。
2. 拼接数值特征、画像特征、标签序列特征、短期与长期兴趣特征。
3. 训练并输出模型参数和特征统计信息。

推理阶段:

1. 对候选逐条构建与训练一致的特征输入。
2. 获得四任务概率并做聚合得分。
3. 生成最终排序结果。

工程点:

- 模型 bundle 包含索引映射与归一化统计，保证训推一致。
- 对缺失特征存在 fallback 逻辑并记录告警。

### 4.3 重排层

当前策略:

- RandomShuffleReranker。

目标:

- 在不明显损伤相关性的前提下提供一定多样性。

演进方向:

- 可替换为规则重排或可解释重排策略。

## 5. 训练与运维架构

### 5.1 管理接口职责

- 触发训练: POST /api/v1/admin/train
- 刷新模型: POST /api/v1/admin/refresh
- 查询单任务: GET /api/v1/admin/tasks/<task_id>
- 任务列表: GET /api/v1/admin/tasks
- 当前状态: GET /api/v1/admin/status

### 5.2 任务执行模型

当前采用进程内线程任务执行器:

1. 创建任务记录。
2. 后台线程执行训练函数。
3. 维护 pending/running/succeeded/failed 状态。
4. 返回任务结果与错误信息。

优势:

- 轻量、实现成本低、便于本地与中小规模部署。

限制:

- 多实例场景下调度与一致性能力不足。

### 5.3 模型训练分发

train_current_models 根据 component/model 分发到具体训练函数:

- ranking + mmoe
- ranking + xgb
- recall + two_tower

同时可把训练状态同步到数据库作业表并记录指标。

### 5.4 产物管理

- ArtifactStore 负责维护最近产物路径等状态信息。
- data/admin_state.json 保存管理状态，供 admin/status 查询。

## 6. 配置架构与运行时行为

### 6.1 配置来源

- 根目录 config.json。
- app/common/config.py 负责加载。
- app/common/settings.py 负责类型化映射。

### 6.2 配置分类

1. 基础运行配置:
   - INTERNAL_SECRET
   - MYSQL_DSN
   - LOG_LEVEL
   - LOG_FILE_PATH
2. Two-Tower 配置:
   - TWO_TOWER_DIM
   - RECALL_TOPK_TWO_TOWER
   - TWO_TOWER_SPACE
   - TWO_TOWER_INDEX_PATH
   - TWO_TOWER_VECTOR_DB_PATH
   - TWO_TOWER_MODEL_PATH
   - TWO_TOWER_STARTUP_BUILD
   - TWO_TOWER_DAILY_UPDATE_INTERVAL_HOURS
3. RAG 配置:
   - RAG_EMBEDDING_MODEL_NAME
   - RAG_FAISS_DIR
   - RAG_FAISS_INDEX_NAME
   - RAG_BUILD_LIMIT
4. MMoE 及训练参数:
   - 由 Settings 提供默认字段并按需使用。

### 6.3 运行时策略

- 配置缺失时多数采用默认值，优先保障服务启动。
- 关键依赖不可用时返回可识别错误或空结果，避免进程崩溃。

## 7. 可观测性与稳定性设计

### 7.1 日志体系

- 使用按大小轮转文件日志。
- 关键流程均有阶段日志（请求、训练、刷新、fallback、异常）。

### 7.2 错误与容错

- API 层统一错误响应结构。
- 算法层对数据缺失和依赖异常普遍设有 fallback。
- 管理接口对任务异常提供可追踪错误信息。

### 7.3 启动自愈能力

- 启动后可自动执行 Two-Tower 索引构建。
- 周期任务可持续刷新索引，减轻手动维护成本。

## 8. 当前架构边界与改进建议

### 8.1 已知边界

1. 搜索接口当前仍为占位实现。
2. 任务系统是进程内线程模型，不适合大规模分布式调度。
3. 评估闭环（离线指标、线上 A/B）尚未完整产品化。

### 8.2 建议演进路径

1. 先补齐搜索链路与关键回归测试。
2. 再完善离线评估和线上效果监控。
3. 最后升级任务系统与发布回滚流程，提升工程稳定性和扩展性。

## 9. 一句话总结

当前架构已经形成可运行、可训练、可维护的推荐服务闭环，下一阶段重点应从功能可用转向效果可证与工程可扩展。