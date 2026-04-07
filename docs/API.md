# Server Java API 文档（当前实现）

更新时间：2026-04-06  
适用范围：当前仓库 Java 服务对外接口

## 1. 基础信息

- 基础路径：`/api/v1`
- 普通接口响应：`application/json`
- 流式接口响应：`text/event-stream`
- 时间格式：ISO-8601

## 2. 鉴权与权限

### 2.1 Bearer Token

需要登录的接口请携带：

```http
Authorization: Bearer <token>
```

### 2.2 内部密钥（推荐代理接口）

推荐代理相关接口支持/要求：

```http
X-Internal-Secret: <secret>
```

规则：

- 当 `app.recs.internal-secret` 为空时，不强制校验。
- 当 `app.recs.internal-secret` 非空时，缺失或不匹配返回 401。

### 2.3 路由权限（按当前 SecurityConfig）

公开接口：

- `POST /users`
- `POST /auth/tokens`
- `GET /users/**`
- `GET /movies`
- `GET /movies/**`
- `GET /tags`
- `GET /persons/**`
- `GET /comments/*/likes`
- `GET /resources/**`
- 推荐代理：`GET /health`、`GET /health/runtime`、`GET /recommend/**`、`POST /recommend/rag/stream`、`GET /search`、`/admin/train|refresh|tasks|status`

登录后可访问：

- `/users/me/**`
- `DELETE /auth/tokens/current`
- `PUT /movies/*/rating`
- `GET /movies/*/rating`
- `GET /movies/*/tags/dynamic`
- `PUT /comments/*/likes`
- `DELETE /comments/*/likes`
- 其他未显式放行接口（如 `DELETE /comments/{id}`、通知接口等）

管理员接口：

- `/admin/**` 需要 `admin` 或 `super_admin`
- `PATCH /comments/**` 需要 `admin` 或 `super_admin`
- 推荐代理 `POST /admin/train` 等虽在路径上属于 `/admin/**`，但已单独放行，实际依赖内部密钥控制

## 3. 通用约定

### 3.1 游标参数

- `cursor` 可选；首屏不传或传空字符串
- `limit` 默认 `20`
- `limit` 最大 `100`

游标结构：

```json
{
  "next_cursor": "1",
  "limit": 20,
  "items": []
}
```

说明：

- 当 `next_cursor` 为 `null` 时，表示没有下一页。
- 请求下一页时，将上一次响应中的 `next_cursor` 作为 `cursor` 传回。

### 3.2 默认值

- `GET /movies/{id}/similar` 的 `limit` 默认 `20`
- `GET /recommend/user` 的 `n` 默认 `10`
- `GET /recommend/item` 的 `n` 默认 `8`
- `GET /recommend/trending` 的 `n` 默认 `10`
- `GET /recommend/trending` 的 `window` 默认 `weekly`
- `POST /recommend/rag/stream` 的 `n` 默认 `8`


### 3.3 错误响应

普通业务接口错误（ProblemDetail）：

```json
{
  "type": "about:blank",
  "title": "Bad Request",
  "status": 400,
  "detail": "..."
}
```

推荐代理接口错误（统一包装）：

```json
{
  "code": 400,
  "message": "...",
  "data": null
}
```

## 4. 认证接口

### 4.1 发送邮箱验证码

- `POST /auth/email-verification-codes`
- Body:

```json
{
  "email": "user@example.com"
}
```

### 4.2 注册

- `POST /users`
- Body:

```json
{
  "email": "user@example.com",
  "password": "string",
  "verification_code": "123456"
}
```

- 说明：`nickname` 由后端自动生成，用户可后续通过 `PATCH /users/me` 修改。

- Response:

```json
{
  "user_id": 1,
  "email": "user@example.com",
  "nickname": "string",
  "avatar": null,
  "gender": null,
  "bio": null,
  "profession": null
}
```

### 4.3 登录

- `POST /auth/tokens`
- Body:

```json
{
  "email": "user@example.com",
  "password": "string"
}
```

- Response:

```json
{
  "token": "string",
  "expires_at": "2026-04-06T15:00:00"
}
```

### 4.4 退出登录

- `DELETE /auth/tokens/current`
- Header: `Authorization: Bearer <token>`

## 5. 用户接口

### 5.1 获取用户信息

- `GET /users/{id}`
- `GET /users/me`（需登录）

### 5.2 更新当前用户

- `PATCH /users/me`（需登录）
- Body（字段均可选）：

```json
{
  "nickname": "string",
  "avatar": "string",
  "gender": "unknown",
  "bio": "string",
  "profession": "string"
}
```

### 5.3 关注关系

- `PUT /users/me/following/{id}`（需登录）
- `DELETE /users/me/following/{id}`（需登录）
- `GET /users/me/following`（需登录）
- `GET /users/me/followers`（需登录）

Query：`cursor`、`limit`

## 6. 电影接口

### 6.1 列表与详情

- `GET /movies`
- `GET /movies/{id}`

说明：当用户已登录时，访问 `GET /movies/{id}` 会自动记录一次电影详情点击行为到 `user_click`。

`GET /movies` Query：

- `q` 可选
- `status` 可选
- `cursor` 可选
- `limit` 默认 20

### 6.2 相似电影

- `GET /movies/{id}/similar`
- Query：`limit` 可选，默认 20
- 说明：Java 服务会调用推荐代理并补充电影摘要字段

### 6.3 评分

- `PUT /movies/{id}/rating`（需登录）
- `GET /movies/{id}/rating`（需登录）

PUT Body：

```json
{
  "rating": 8
}
```

### 6.4 收藏

- `PUT /users/me/collections/movies/{id}`（需登录）
- `DELETE /users/me/collections/movies/{id}`（需登录）

## 7. 标签接口

### 7.1 标签查询与创建

- `GET /tags`
- `GET /tags/static/random`
- `POST /tags`（需登录）

`GET /tags` Query：`q`、`type`、`status`、`cursor`、`limit`

`GET /tags/static/random` Query：`count`（可选，默认 `20`，范围 `1~100`）

`POST /tags` Body：

```json
{
  "tag_name": "string",
  "type": "dynamic"
}
```

### 7.2 电影标签

- `GET /movies/{id}/tags`
- `GET /movies/{id}/tags/dynamic`（需登录）
- `POST /movies/{id}/tags`（需登录）
- `PUT /movies/{movieId}/tags/{tagId}/votes`（需登录）
- `DELETE /movies/{movieId}/tags/{tagId}/votes`（需登录）

### 7.3 我的标签

- `PUT /users/me/collections/tags/{tagId}`（需登录）
- `DELETE /users/me/collections/tags/{tagId}`（需登录）
- `GET /users/me/tags/dynamic`（需登录）
- `DELETE /users/me/tags/dynamic/{tagId}`（需登录）

## 8. 评论接口

- `POST /movies/{id}/comments`（需登录）
- `GET /movies/{id}/comments`
- `DELETE /comments/{id}`（需登录）
- `PATCH /comments/{id}`（管理员）
- `PUT /comments/{id}/likes`（需登录）
- `DELETE /comments/{id}/likes`（需登录）
- `GET /comments/{id}/likes`

`GET /movies/{id}/comments` Query：

- `sort` 默认 `new`
- `cursor` 可选
- `limit` 默认 20

## 9. 通知接口

- `GET /users/me/notifications`（需登录）
- `PATCH /users/me/notifications/{id}`（需登录）
- `PATCH /users/me/notifications`（需登录）

Query：

- `unread_only` 默认 `0`，`1` 表示只看未读
- `cursor`、`limit`

## 10. 人物与静态资源

### 10.1 人物详情

- `GET /persons/{id}`

返回含：人物基础信息 + `works`（作品列表，含角色、标签、主创等聚合字段）。

### 10.2 静态资源

- `GET /resources/static/**`

说明：资源不存在时，会回退返回默认图片。

## 11. 管理员电影接口

- `POST /admin/movies`（管理员）
- `PATCH /admin/movies/{id}`（管理员）

## 12. 推荐代理接口

说明：以下接口由 Java 服务代理到推荐服务，并按统一错误结构返回。

### 12.1 健康检查

- `GET /health`
- `GET /health/runtime`

### 12.2 推荐查询

- `GET /recommend/user`
  - Query：`user_id` 必填，`n` 可选（默认 10）
- `GET /recommend/item`
  - Query：`movie_id` 必填，`n` 可选（默认 8）
- `GET /recommend/trending`
  - Query：`window` 可选（默认 `weekly`）
  - `window` 允许值：`daily`、`weekly`、`monthly`、`half_year`、`one_year`、`all_time`
  - Query：`n` 可选（默认 10）

### 12.3 搜索

- `GET /search`
- Query：
  - `query` 可选，可为空字符串
  - `tag` 可选，支持多值（如 `tag=a&tag=b`）
  - 约束：`query` 和 `tag` 不能同时为空
  - `n` 可选，正整数
  - `offset` 可选，非负整数
  - 其他任意查询参数会原样透传给推荐服务（支持多值参数）

示例：

```http
GET /search?query=%E7%A7%91%E5%B9%BB%20%E8%AF%BA%E5%85%B0&n=20&offset=0&genre=Sci-Fi&year_min=2010&tag=%E6%82%AC%E7%96%91&tag=%E6%8E%A8%E7%90%86
```

### 12.4 RAG 流式推荐

- `POST /recommend/rag/stream`
- Header：`Accept: text/event-stream`
- Body：

```json
{
  "query": "想看高分悬疑推理电影",
  "n": 8,
  "rebuild_index": false
}
```

约束：

- `query` 必填且去空格后不能为空
- `n` 必须为正整数

### 12.5 推荐管理

- `POST /admin/train`
  - Body 必填字段：`component`、`model`
- `POST /admin/refresh`
- `GET /admin/tasks/{task_id}`
- `GET /admin/tasks`
  - Query：
    - `source`：`all|memory|db`
    - `status`：`pending|processing|completed|failed`
    - `limit`、`offset`
- `GET /admin/status`

## 13. 对接建议

- 前端统一调用本服务 `/api/v1`，不要直连推荐微服务。
- 推荐接口优先解析 `code/message/data`。
- 普通业务接口错误优先解析 `ProblemDetail.detail`。
