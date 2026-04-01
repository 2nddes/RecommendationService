# RecommendationService MMOE 训练流程详解

## 1. 结论速览

本项目的 MMOE 排序模型训练是一个完整的离线流水线，核心特点如下：

1. 训练入口由管理接口触发，走异步任务并写入训练作业状态。
2. 样本来自 MySQL 多表行为日志，按 `(user_id, movie_id)` 聚合成多任务标签。
3. 特征由三部分组成：数值特征、离散 ID/画像特征、序列兴趣特征。
4. 模型是标准 MMOE 结构，4 个任务共享 experts，各任务单独 gate 和 tower。
5. 损失函数是 4 个任务 BCE 直接相加，训练后输出按任务 AUC 和均值 AUC。
6. 保存产物不仅有参数，还包含索引映射与标准化统计，保证训练/推理一致。

关键代码位置：

- 训练调度与主流程：[app/ops/model_ops.py](app/ops/model_ops.py)
- 模型定义：[app/reco/ranking/mmoe/model.py](app/reco/ranking/mmoe/model.py)
- 推理特征构建：[app/reco/ranking/mmoe/ranker.py](app/reco/ranking/mmoe/ranker.py)
- 特征工具函数：[app/reco/ranking/mmoe/features.py](app/reco/ranking/mmoe/features.py)
- 训练触发 API：[app/api/v1_admin.py](app/api/v1_admin.py)
- 训练任务管理：[app/ops/admin_service.py](app/ops/admin_service.py)
- 配置项定义：[app/common/settings.py](app/common/settings.py)

---

## 2. 训练入口与任务编排

### 2.1 HTTP 触发

管理端通过 `POST /api/v1/admin/train` 触发训练，要求请求体包含 `component` 和 `model`。

实现位置：

- 接口入口：[app/api/v1_admin.py#L14](app/api/v1_admin.py#L14)

当传入 `component=ranking, model=mmoe` 时，最终进入 `start_train_task(...)`。

### 2.2 异步任务与 DB 作业状态

`start_train_task(...)` 会同时做两件事：

1. 在数据库创建一条 `model_train_job`（初始 `pending`）。
2. 启动内存线程任务执行训练函数。

实现位置：

- 任务启动：[app/ops/admin_service.py#L24](app/ops/admin_service.py#L24)
- 内存任务执行器：[app/ops/tasks.py#L25](app/ops/tasks.py#L25)

### 2.3 统一调度到 MMOE 训练

统一训练入口 `train_current_models(...)` 根据参数分发到 `_train_mmoe(settings)`。

实现位置：

- 分发逻辑：[app/ops/model_ops.py#L30](app/ops/model_ops.py#L30)
- MMOE 主函数：[app/ops/model_ops.py#L870](app/ops/model_ops.py#L870)

---

## 3. 训练样本与标签构造

### 3.1 样本来源 SQL

`_fetch_mmoe_training_rows(...)` 使用 `UNION ALL` 汇总以下事件：

1. `user_action.action_type='view'` 映射为 `click`。
2. `user_action.action_type='collect'` + `user_collect_movie` 映射为 `collect`。
3. `user_action.action_type='comment'` + `movie_comment` 映射为 `comment`。
4. `rating` 表映射为 `rating` 任务（阈值见下）。

实现位置：

- 样本抓取函数：[app/ops/model_ops.py#L506](app/ops/model_ops.py#L506)

### 3.2 标签定义

聚合后每条训练样本是一个 `(user_id, movie_id)`，目标标签为四维向量：

$$
\mathbf{y} = [y_{click}, y_{collect}, y_{comment}, y_{rating}] \in \{0,1\}^4
$$

具体规则：

1. `click`: 有 view 事件则为 1。
2. `collect`: 有 collect 事件（任一来源）则为 1。
3. `comment`: 有 comment 事件（任一来源）则为 1。
4. `rating`: `rating > 5` 为 1，否则 0。

说明：标签是“多任务多标签”而非单任务互斥分类。

### 3.3 训练前的标签可训练性检查

代码会先检查：

1. 总样本数必须 > 1。
2. 每个任务都必须有正样本（四个任务最小正样本数不能为 0）。

实现位置：

- 样本检查与跳过逻辑：[app/ops/model_ops.py#L1053](app/ops/model_ops.py#L1053)

这一步能避免训练“看似成功但任务退化为常数预测”的模型。

---

## 4. 辅助特征抽取（MySQL）

`_fetch_mmoe_aux_training_features(...)` 拉取训练所需上下文特征。

实现位置：

- 辅助特征函数：[app/ops/model_ops.py#L646](app/ops/model_ops.py#L646)

### 4.1 电影统计特征

对电影抓取：

1. 评分均值、评分数。
2. 评论数。
3. 点击总量、近 1 小时点击、近 24 小时点击。
4. 年份、时长。

### 4.2 标签与用户画像

还会抓取：

1. 电影静态标签（`tag_dict.type='static'`）。
2. 用户画像（gender, birth）。
3. 用户短期点击历史（最近 10 条 view 电影）。
4. 用户长期兴趣标签（高分评分电影对应标签，最多 100）。
5. 用户对静态标签的点击计数及总点击数（用于 CTR 特征）。

### 4.3 退化策略（Fallback）

当 MySQL 不可用、输入为空、查询失败时，函数返回空字典结构并在日志告警。后续会用默认值和 PAD 序列补齐，不会直接崩溃。

这是一种“可训练性优先”的容错设计，但会影响模型上限。

---

## 5. 特征工程与编码

### 5.1 数值特征集合

数值特征顺序由 `bundle_feature_order()` 固定，包含 15 个维度：

1. recall_score
2. movie_rating_avg
3. movie_rating_count
4. movie_comment_count
5. movie_click_count
6. movie_click_1h
7. movie_click_24h
8. movie_year
9. movie_duration_min
10. user_static_tag_ctr
11. src_user_collection
12. src_user_high_rating_similar
13. src_user_interest_tag
14. src_item_similar_by_tags
15. src_two_tower

实现位置：

- 特征顺序定义：[app/reco/ranking/mmoe/features.py#L6](app/reco/ranking/mmoe/features.py#L6)

### 5.2 默认值与标准化

若某电影统计缺失，使用全局均值（基于当前训练集可得统计）兜底。随后对每一列进行标准化：

$$
x' = \frac{x - \mu}{\sigma}
$$

其中：

1. `src_*` one-hot 列被强制设定 `mean=0, std=1`，不做重新缩放。
2. 若某列方差极小（`std <= 1e-8`），强制 `std=1` 避免除零。

实现位置：

- 统计计算与归一化：[app/ops/model_ops.py#L1114](app/ops/model_ops.py#L1114)

### 5.3 离散索引与序列编码

训练时动态构造索引：

1. `user_index`: 用户 ID -> embedding 索引（从 1 开始）。
2. `item_index`: 电影 ID -> embedding 索引（从 1 开始）。
3. `tag_index`: 标签 ID -> embedding 索引（从 2 开始，保留 0/1）。
4. `gender_index` 与 `age_bucket_index`: 使用预设映射。

序列长度约束：

1. 短期行为序列长度：10。
2. 长期兴趣标签序列长度：100。
3. 物品标签序列长度：12。

不足补 PAD（0），超长截断。

实现位置：

- 序列长度常量：[app/reco/ranking/mmoe/features.py#L26](app/reco/ranking/mmoe/features.py#L26)
- 序列补齐函数：[app/reco/ranking/mmoe/features.py#L66](app/reco/ranking/mmoe/features.py#L66)

---

## 6. 模型结构（MMoENet）

模型定义在 `MMoENet`，任务集合固定为：`click/collect/comment/rating`。

实现位置：

- 任务常量与模型类：[app/reco/ranking/mmoe/model.py#L11](app/reco/ranking/mmoe/model.py#L11)

### 6.1 输入拼接

基础输入：

1. user embedding
2. item embedding
3. 数值特征向量

可选输入（本项目训练中均开启）：

1. gender embedding
2. age bucket embedding
3. item tag pooling（tag emb 的 masked mean）
4. target attention（query=item emb，对短期历史 item 序列做注意力）
5. long-interest pooling（长期兴趣 tag 序列 masked mean）

因此输入维度为：

$$
d_{in} = 2d_e + d_n + \mathbb{1}_{gender}d_e + \mathbb{1}_{age}d_e + \mathbb{1}_{item\_tag}d_e + \mathbb{1}_{attn}d_e + \mathbb{1}_{long\_tag}d_e
$$

其中 $d_e$ 是 embedding 维度，$d_n$ 是数值特征维度。

### 6.2 Experts / Gates / Towers

结构为标准 MMOE：

1. Experts：`num_experts` 个共享 MLP（两层 `Linear+ReLU`）。
2. 每个任务一个 gate：`Linear(d_in -> num_experts) + Softmax`。
3. 每个任务一个 tower：`Linear -> ReLU -> Linear(1)`。

任务输出经过 `sigmoid` 变为概率。

### 6.3 注意力与池化细节

`_target_attention` 采用缩放点积注意力，带 mask：

$$
\alpha = \text{softmax}\left(\frac{KQ}{\sqrt{d}} + mask\right)
$$

再按权重对历史 item embedding 求和得到兴趣向量。空位由 PAD mask 屏蔽。

---

## 7. 训练循环与损失

### 7.1 切分方式

当前使用顺序切分（前 80% 训练，后 20% 测试），没有随机分层。实现函数：

- [app/ops/model_ops.py#L186](app/ops/model_ops.py#L186)

### 7.2 优化器与超参

1. 优化器：Adam。
2. 损失：BCE（每任务一个 BCE 后求和）。
3. 批训练：按 `batch_size` 遍历，epoch 内打乱训练索引。

损失形式：

$$
\mathcal{L} = \mathcal{L}_{click} + \mathcal{L}_{collect} + \mathcal{L}_{comment} + \mathcal{L}_{rating}
$$

超参数来自配置：

- [app/common/settings.py#L36](app/common/settings.py#L36)

关键项包括：

1. `MMOE_TRAIN_LIMIT`
2. `MMOE_TRAIN_EPOCHS`
3. `MMOE_TRAIN_BATCH_SIZE`
4. `MMOE_TRAIN_LR`
5. `MMOE_EMB_DIM`
6. `MMOE_NUM_EXPERTS`
7. `MMOE_EXPERT_HIDDEN_DIM`
8. `MMOE_TOWER_HIDDEN_DIM`

---

## 8. 评估与产物保存

### 8.1 评估指标

测试集上分别计算 4 个任务的二分类 AUC，再求可用任务的均值 AUC。

实现位置：

- AUC 函数：[app/ops/model_ops.py#L128](app/ops/model_ops.py#L128)
- MMOE 测试评估：[app/ops/model_ops.py#L1204](app/ops/model_ops.py#L1204)

### 8.2 保存内容

`torch.save(bundle, artifact_path)` 保存如下关键对象：

1. `state_dict`
2. `model_meta`（结构超参）
3. `tasks`
4. `feature_order`
5. `feature_stats`
6. `user_index/item_index/tag_index`
7. `gender_index/age_bucket_index`

实现位置：

- bundle 构建与保存：[app/ops/model_ops.py#L1228](app/ops/model_ops.py#L1228)

同时会把最新路径写入 artifact store：

- [app/ops/model_ops.py#L1254](app/ops/model_ops.py#L1254)

---

## 9. 训练与线上推理一致性分析

从代码看，一致性设计做得比较完整：

1. 推理从 bundle 读 `feature_order` 和 `feature_stats`，按训练时统计归一化。
2. 推理也使用同一套 `*_index` 映射，不会出现 embedding 索引漂移。
3. 序列长度、PAD、mask 逻辑与训练一致。
4. 模型结构参数从 `model_meta` 还原，不依赖硬编码默认值。

实现位置：

- 推理重建模型：[app/reco/ranking/mmoe/ranker.py#L89](app/reco/ranking/mmoe/ranker.py#L89)
- 推理特征张量构建：[app/reco/ranking/mmoe/ranker.py#L112](app/reco/ranking/mmoe/ranker.py#L112)

这意味着只要 `bundle` 文件可用，线上可稳定复现训练时的特征语义与模型输入分布（除实时数据漂移外）。

---

## 10. 当前实现的优点与风险

### 10.1 优点

1. 多任务共享表示，适合 click/collect/comment/rating 的相关目标。
2. 结合用户画像、内容标签、短期行为和长期兴趣，信息密度高。
3. 训练与推理通过 bundle 对齐，工程可维护性较好。
4. MySQL 失败时可退化运行，系统鲁棒性好。

### 10.2 风险点

1. 数据切分是顺序 8:2，若按时间倒序取样，会引入时序偏差。
2. 任务损失等权重，可能掩盖稀疏任务（如 comment）学习不足。
3. 负样本主要来自“未触发标签”，显式 hard negative 策略较弱。
4. 在线打分将 4 任务简单平均，未体现业务权重差异。
5. 训练在 CPU 默认路径，数据量增大时训练时间可能明显上升。

---

## 11. 可落地优化建议（按优先级）

1. 改成时间感知切分（例如按事件时间分 train/valid/test）并监控线上回放指标。
2. 为四任务引入可配置权重（例如基于样本比例或业务价值）。
3. 增加显式负样本挖掘（曝光未交互、低分评分、dislike）提升排序区分度。
4. 在训练中记录每 epoch 的 loss/AUC 曲线，支持早停与最佳 checkpoint 保存。
5. 在线融合分数由“等权平均”升级为可配置加权或学习式融合。
6. 增加特征快照与数据质量监控（空值率、分布漂移、标签漂移）。

---

## 12. 一句话总结

该 MMOE 实现已经具备“可训练、可评估、可上线、可回放”的完整闭环，当前主要瓶颈不在模型结构本身，而在数据切分策略、任务权重设计和负样本建模策略。
