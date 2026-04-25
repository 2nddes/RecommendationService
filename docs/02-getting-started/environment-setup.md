# Environment Setup

## 文档目标

本文档面向第一次在本地或测试环境启动该项目的人，目标是让你在开始碰 API 之前，把以下前置条件一次性准备完整：

- Python 环境
- MySQL 数据库
- Redis 缓存
- 模型与索引文件
- 外部 RAG provider
- 日志与数据目录

如果你只做了一半准备就直接运行 `python app.py`，这个项目大概率不会处于真正可用状态，因为它启动时会同步 warmup 多个子系统。

## 1. 最低环境要求

### 1.1 Python

建议使用 Python 3.11 或相近的现代 3.x 版本。

原因：

- 依赖包含 Flask 3、SQLAlchemy 2、Torch、XGBoost、Polars 等较新的库。
- 较老 Python 版本更容易遇到二进制轮子缺失或依赖安装失败。

### 1.2 MySQL

建议使用 MySQL 8.x。

原因：

- [all.sql](../../all.sql) 的建表脚本来自 MySQL 8 环境。
- 搜索链路使用了全文索引与多种 SQL 语法。
- 任务系统、RAG embedding 存储与训练数据拉取都依赖主库。

### 1.3 Redis

建议使用 Redis 5.x 或更高版本。

原因：

- 用户推荐缓存、搜索缓存、热门榜缓存、Tag 倒排召回都依赖 Redis。
- 关闭 Redis 虽然不一定导致服务完全起不来，但会让一部分核心能力退化。

### 1.4 第三方模型服务

RAG 需要两个外部能力：

- embedding API
- chat completion API

当前 [config.json](../../config.json) 配置的是 OpenAI-compatible 风格 provider。如果这两个接口不可访问，RAG 能力和部分相似片检索会失效。

## 2. 仓库内与运行有关的关键文件

### 必须关注的文件

- [config.json](../../config.json)：运行时配置
- [all.sql](../../all.sql)：数据库结构初始化
- [requirements.txt](../../requirements.txt)：依赖安装清单
- [app.py](../../app.py)：启动入口

### 启动前必须确认的目录

- `logs/`：日志输出目录
- `data/`：运行时数据目录
- `data/artifacts/`：历史产物目录
- `data/models/`：按当前配置约定的活跃模型目录

## 3. 推荐的 Windows 本地环境准备方式

### 3.1 创建虚拟环境

推荐在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

如果你的 PowerShell 限制脚本执行，需要先允许当前会话执行本地脚本，或者改用 CMD / IDE 自带终端。(推测)

### 3.2 安装 Python 依赖

```powershell
pip install -r requirements.txt
```

### 3.3 关于 hnswlib 的额外说明

代码中明确使用了 `hnswlib`，例如：

- [app/reco/recall/two_tower/runtime.py](../../app/reco/recall/two_tower/runtime.py)
- [app/reco/recall/two_tower/indexing.py](../../app/reco/recall/two_tower/indexing.py)

但 [requirements.txt](../../requirements.txt) 当前未显式声明它。因此如果运行时报 `ModuleNotFoundError: hnswlib`，需要单独安装：

```powershell
pip install hnswlib
```

如果某些平台没有合适的预编译轮子，可能还需要编译环境，这也是为什么建议尽量在与你目标环境接近的 Python 版本上安装。

## 4. 数据库准备

### 4.1 创建数据库

当前配置默认使用数据库名：

- `movie_rec`

你需要先在 MySQL 中创建它，或者把 [config.json](../../config.json) 里的 `core.mysql_dsn` 改成你实际使用的数据库名。

### 4.2 导入表结构

使用 [all.sql](../../all.sql) 导入表结构。示例命令如下：

```powershell
mysql -u root -p movie_rec < all.sql
```

如果你使用 Navicat、DBeaver、DataGrip 等 GUI 工具，确保导入的是完整 SQL，而不只是部分表。

### 4.3 为什么数据库导入不是可选步骤

这个项目不是“没有数据也能完整运行”的 demo。即使你只想看接口返回，服务也会在启动时尝试：

- 初始化推荐运行时
- 初始化 RAG 服务
- 预计算缓存
- 查询任务表状态

因此缺失关键表时，warmup 或后续接口很容易直接失败。

## 5. Redis 准备

### 5.1 最小要求

确保 Redis 实例可被 [config.json](../../config.json) 中的地址访问。

当前默认配置是：

- host: `127.0.0.1`
- port: `6379`
- db: `0`

### 5.2 为什么建议先确保 Redis 可用

Redis 在本项目中承担：

- 用户推荐缓存
- 搜索缓存
- 热门榜缓存
- 特征缓存
- Tag 倒排召回缓存
- 推荐构建锁
- RAG 的部分短期缓存

如果 Redis 不可用，你可能会看到：

- 某些接口延迟变高
- 某些功能直接退化或返回空
- 启动预热不完整

## 6. 模型与索引文件准备

### 6.1 先理解“活跃路径”与“历史产物”的区别

当前仓库里存在两类相关目录：

- `data/artifacts/...`：历史训练产物
- `data/models/...`：配置期望的活跃模型路径

同时仓库结构中还出现了 `models/` 目录。这意味着你不能只看到“有模型文件存在”就认为启动一定没问题。

真正决定运行时是否能加载成功的是：

- [config.json](../../config.json) 中的路径是否存在

### 6.2 当前配置要求存在的活跃文件

按照 [config.json](../../config.json)，至少应确认这些路径可用：

- `data/models/two_tower_latest.pt`
- `data/models/mmoe_latest.pt`
- `data/models/xgb_latest.json`
- `data/two_tower_items.hnsw`
- `data/two_tower_vectors.db`

### 6.3 如果仓库里只有 artifacts，没有 active models 怎么办

你有两个选择：

1. 把 `config.json` 改成直接指向你要用的现有产物。
2. 按当前配置约定，把需要的文件放到 `data/models/` 与对应索引路径。

不要忽略这个问题。warmup 阶段会直接尝试加载这些模型与索引。

## 7. RAG provider 准备

### 7.1 embedding 与 llm 都要可用

RAG 依赖两组配置：

- `rag.embedding_*`
- `rag.llm_*`

仅 embedding 可用不够，聊天接口还需要 llm API 可用。

### 7.2 配置安全问题

当前仓库中的 [config.json](../../config.json) 包含明文 API Key，这在真实交付中不应被保留。建议：

- 本地开发可临时使用测试 key
- 交付版本改为环境变量或外部密钥注入

### 7.3 如果你暂时不需要 RAG

请注意：当前启动 warmup 会初始化 RAG 服务。因此“暂时不调 RAG 接口”并不自动等价于“可以完全忽略 RAG 配置”。

## 8. 日志与运行目录准备

### 8.1 日志文件

当前默认日志路径来自配置：

- `logs/app1.log`

日志系统会优先写文件，而不是只打控制台，因此请确认：

- `logs/` 目录可创建
- 当前进程有写权限

### 8.2 data 目录

确认以下目录有读写权限：

- `data/`
- `data/artifacts/`
- `data/models/`

尤其在 Windows 上，如果仓库位于权限较严格的目录，可能导致索引、状态文件或日志无法写入。

## 9. 推荐的准备顺序

不要并发地“边启动边修环境”。建议严格按顺序做：

1. 安装 Python 与创建虚拟环境。
2. 安装 requirements。
3. 补装 hnswlib（如果缺）。
4. 创建 MySQL 数据库并导入 [all.sql](../../all.sql)。
5. 启动 Redis 并确认端口可访问。
6. 确认 [config.json](../../config.json) 中 MySQL、Redis、模型路径、RAG provider 配置正确。
7. 确认活跃模型与索引文件存在。
8. 再运行 `python app.py`。

## 10. 启动前检查清单

在执行 `python app.py` 之前，至少确认以下事项：

- Python 虚拟环境已经激活。
- `pip install -r requirements.txt` 已成功完成。
- `hnswlib` 如果缺失，已单独安装。
- MySQL 可连接，且数据库中有导入后的表结构。
- Redis 可连接。
- [config.json](../../config.json) 中没有错误的路径或错误的账号密码。
- `data/models` 和索引文件路径下存在实际文件。
- 日志目录可写。
- RAG provider 配置指向的是可访问的测试环境或正式环境。

## 11. 准备完成后去哪里

准备完成后，不建议直接自由探索接口。请继续按顺序阅读：

1. [configuration-guide.md](configuration-guide.md)
2. [first-run-checklist.md](first-run-checklist.md)

这样你能在“启动之后如何验证系统真正可用”这个问题上少走很多弯路。