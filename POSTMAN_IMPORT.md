# Postman 导入说明

本仓库已提供可直接导入的 Postman collection：`RecommendationService.postman_collection.json`。

## 1. 导入步骤

1. 打开 Postman。
2. 选择 `Import`。
3. 选择仓库根目录下的 `RecommendationService.postman_collection.json`。
4. 导入后进入 collection 级别的 `Variables`，按实际环境修改 `baseUrl`。

默认变量：

- `baseUrl`：`http://localhost:5000/api/v1`
- `userId`：推荐用户 ID 示例
- `movieId`：推荐电影 ID 示例
- `taskId`：任务 ID 示例
- `queryText`：RAG 查询示例
- `searchText`：搜索词示例
- `limit` / `offset`：列表接口默认分页变量

## 2. baseUrl 说明

如果你是本地直接运行 Flask 服务，默认值通常可用：

```text
http://localhost:5000/api/v1
```

如果服务部署在网关之后，请把 `baseUrl` 改成网关暴露地址，例如：

```text
https://example.internal/recommendation/api/v1
```

## 3. 各类接口的调用前提

### Health

- `/health` 和 `/health/runtime` 通常可直接调用。

### Recommend

- `/recommend/user` 的参数形态受服务端 `user_reco_delivery_mode` 控制。
- collection 默认使用 `paged` 形态；若服务端是 `pop` 模式，请把 `page/page_size` 改成 `n`。

### Search

- `/search` 依赖 MySQL 和可用的 `MYSQL_DSN`。

### RAG

- `/recommend/rag/stream` 依赖 RAG 配置与下游模型服务。
- collection 已预设 `Accept: text/event-stream` 和 `Content-Type: application/json`。

### Admin

- `/admin/train` 只负责入队，必须有独立训练 worker 才会真正执行。
- `/admin/rag/rebuild` 也依赖独立 worker 消费任务。
- `/admin/rag/enqueue` 是同步刷新单电影 embedding。
- 当前代码未内建管理接口鉴权，若线上环境通过网关鉴权，请在 Postman 中补相应 Header。

## 4. 关于 SSE 的额外说明

`POST /recommend/rag/stream` 是 SSE 接口。Postman 可以发起请求，但不同版本的 Postman 对流式分块展示支持不一致，可能会缓冲后再一次性显示。

如果你需要验证真实的逐块流式输出，建议同时使用命令行工具做补充验证，例如：

```bash
curl -N -H "Accept: text/event-stream" -H "Content-Type: application/json" \
  -d '{"query":"想看高分悬疑推理电影","n":8}' \
  http://localhost:5000/api/v1/recommend/rag/stream
```

## 5. 推荐的调试顺序

1. 先调用 `/health` 与 `/health/runtime`。
2. 再调用 `/recommend/trending` 与 `/search`，确认基础数据链路正常。
3. 然后调用 `/admin/status`，确认运行时和产物路径。
4. 最后再调 `/admin/train`、`/admin/rag/rebuild` 和 `/recommend/rag/stream` 这类依赖更多后台组件的接口。