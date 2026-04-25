# Documentation Index

本文档入口页的目标不是重复 [README.md](../README.md) 的内容，而是把项目的知识按读者路径重新组织，让不同角色在最短路径内找到自己需要的材料。

## 这套文档为什么存在

这个项目已经不是一个“只有几个 API 的轻量 Flask 服务”。它同时包含：

- 在线推荐流水线
- 条件搜索
- RAG 检索与流式回答
- 模型训练与产物热刷新
- RAG 重建任务
- Redis 缓存与倒排索引预热
- 运行时健康检查与后台 worker

如果这些内容继续全部堆在一个 README 中，会出现三个问题：

1. 新接手的人很难知道应该先看哪里。
2. 运维、开发、算法、测试会在同一份文档里互相干扰。
3. 一旦 README 同时承担门面、架构说明、接口手册、运维手册，后续几乎无法稳定维护。

因此这套 docs 的设计原则是：

- README 只做门面和导航。
- docs 承担详细说明。
- 先按读者路径组织，再按主题深入。
- 每篇文档有明确边界，避免重复维护。

## 你应该从哪里开始读

### 路径 A：第一次接手项目，需要先跑起来

推荐顺序：

1. [../README.md](../README.md)
2. [02-getting-started/environment-setup.md](02-getting-started/environment-setup.md)
3. [02-getting-started/configuration-guide.md](02-getting-started/configuration-guide.md)
4. [02-getting-started/first-run-checklist.md](02-getting-started/first-run-checklist.md)
5. [02-getting-started/local-debugging-guide.md](02-getting-started/local-debugging-guide.md)

这条路径的目标不是“理解所有代码”，而是：

- 把数据库和缓存准备好
- 把配置调通
- 成功启动服务
- 看到健康检查为可用
- 成功请求一次推荐、搜索和管理接口

### 路径 B：需要快速看懂系统是怎么工作的

推荐顺序：

1. [01-overview/project-overview.md](01-overview/project-overview.md)
2. [01-overview/architecture-overview.md](01-overview/architecture-overview.md)
3. [01-overview/repository-map.md](01-overview/repository-map.md)

这条路径的目标是帮助你回答几个核心问题：

- 这个项目到底负责什么，不负责什么
- 在线请求如何流转
- 为什么它是“单体 + 内嵌 worker”的结构
- 推荐、搜索、RAG、任务系统分别在哪些文件里

### 路径 C：你负责交付、运行或排障

推荐顺序：

1. [02-getting-started/configuration-guide.md](02-getting-started/configuration-guide.md)
2. [02-getting-started/first-run-checklist.md](02-getting-started/first-run-checklist.md)
3. [02-getting-started/local-debugging-guide.md](02-getting-started/local-debugging-guide.md)
4. [01-overview/architecture-overview.md](01-overview/architecture-overview.md)

这条路径目前先服务于“首轮交付”和“本地 / 测试环境运行”，后续会继续补充更完整的训练 runbook、RAG 重建 runbook 和监控文档。

## 当前已落地的文档

### 01-overview

- [01-overview/project-overview.md](01-overview/project-overview.md)
  - 解释项目定位、业务边界、核心能力、当前系统状态。
- [01-overview/architecture-overview.md](01-overview/architecture-overview.md)
  - 解释高层架构、启动链路、运行时组件、请求路径和主要状态。
- [01-overview/repository-map.md](01-overview/repository-map.md)
  - 解释仓库结构、关键目录、关键文件与推荐阅读顺序。

### 02-getting-started

- [02-getting-started/environment-setup.md](02-getting-started/environment-setup.md)
  - 环境准备、依赖安装、数据库导入、模型与索引准备。
- [02-getting-started/configuration-guide.md](02-getting-started/configuration-guide.md)
  - 逐段解释 `config.json` 中各字段的含义与风险。
- [02-getting-started/first-run-checklist.md](02-getting-started/first-run-checklist.md)
  - 从零跑通服务的顺序化操作清单。
- [02-getting-started/local-debugging-guide.md](02-getting-started/local-debugging-guide.md)
  - 常见启动与运行问题的排查手册。

## 当前推荐的文档使用方式

### 如果你要把仓库交给别人

不要只发 [README.md](../README.md)。

至少应一起交付：

- [02-getting-started/environment-setup.md](02-getting-started/environment-setup.md)
- [02-getting-started/configuration-guide.md](02-getting-started/configuration-guide.md)
- [02-getting-started/first-run-checklist.md](02-getting-started/first-run-checklist.md)
- [01-overview/architecture-overview.md](01-overview/architecture-overview.md)

因为交付接收方最先关心的是：

- 要装什么
- 要配什么
- 怎么验证跑起来了
- 跑起来以后出了问题该看哪里

### 如果你要开始改代码

不要先从 `all.sql` 或某个模型训练文件开始读。

建议顺序是：

1. 先看 [01-overview/architecture-overview.md](01-overview/architecture-overview.md)
2. 再看 [01-overview/repository-map.md](01-overview/repository-map.md)
3. 然后定位你要改的链路对应的入口文件

## 本轮未完成但已规划的文档

以下文档尚未在本轮落地，但已经是确定的下一批目标：

- 推荐主链路说明
- 搜索链路说明
- RAG 详细实现说明
- 完整 REST / SSE 接口参考
- 数据契约与缓存契约
- 训练任务与 RAG 重建 runbook
- 健康检查与监控说明
- 扩展指南与文档规范

## 维护约定

为了避免文档再次退化成单篇超长 README，建议在后续维护中遵守以下约定：

1. 根 README 只保留项目门面和入口导航。
2. 详细实现放在 docs 中，不在 README 重复写长篇正文。
3. 任何新增的运维流程、接口或关键配置，都优先落到对应专题文档。
4. 如果一个主题已经在 docs 中有独立文档，README 只写 3 到 6 行摘要并链接过去。

## 下一步建议

如果你准备继续实现下一批文档，推荐顺序是：

1. 推荐主链路说明
2. 搜索链路说明
3. RAG 详细说明
4. REST / SSE 接口参考
5. 数据与缓存契约
6. 训练与运行手册