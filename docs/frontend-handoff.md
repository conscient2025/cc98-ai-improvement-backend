# 前端交接文档

这份文档给前端同学用，目标是让插件或页面能尽快接上后端。

## 结论

后端现在只负责 Watch 订阅新帖提醒，不负责历史搜索，也不负责正式 AI 搜索。

- AI 搜索：建议在浏览器插件里完成，使用用户自己的 CC98 Token 和 LLM API Key。
- 订阅新帖提醒：由后端完成，包括订阅管理、扫描 CC98 最新帖、匹配、通知、通知历史。
- `/api/research` 已关闭，会返回 410，前端不要再接这个功能。

## 给前端同学的重点提醒

前端仓库现在是浏览器插件。后端部署到服务器后，插件必须把 Watch 后端地址指向服务器地址，否则默认会继续连本地：

```text
http://127.0.0.1:8000
```

前端可以二选一：

- 在插件设置页的「Watch 后端地址」里让用户填写服务器地址，例如 `https://watch.example.com`。
- 打包给用户前，把前端默认后端地址改成服务器地址，减少用户手动配置步骤。

注意：

- 部署地址不要带最后的 `/`，推荐写成 `https://watch.example.com`。
- 如果是 Chrome/Edge 扩展，访问新的后端域名时需要浏览器授权该域名权限；现有前端设置页已经有申请权限逻辑。
- 后端已经做了登录鉴权，前端注释里如果还有“JWT 暂未校验”之类说法，请同步删掉。
- 前端不要再相信或依赖 `user_id=demo_user`。用户身份以后端 token 为准。

## 用户侧完整流程

用户实际使用订阅提醒时，应该是这个顺序：

1. 安装浏览器插件。
2. 打开插件设置页，确认 Watch 后端地址是部署后的服务器地址。
3. 打开 CC98 页面，进入插件面板。
4. 在「设置」里填写浙大邮箱，点击发送验证码。
5. 去邮箱收 6 位验证码，填回插件完成登录。
6. 在「订阅」里添加关键词订阅。
7. 在「设置」里配置钉钉或邮箱通知渠道，并点击测试。
8. 等后端定时扫描新帖，或测试阶段由管理员手动触发扫描。

只用 AI 搜索时不需要登录后端；只有订阅提醒需要后端邮箱验证码登录。

## 本地启动

```powershell
pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn app.main:app --port 8000 --reload
```

接口文档：

```text
http://127.0.0.1:8000/docs
```

本地 demo 建议 `.env` 保持：

```text
WATCH_FORCE_MOCK_TOPICS=true
MATCHER_FORCE_RULES=true
ENABLE_SCHEDULER=false
```

这样即使没有 CC98 公共账号，也能创建订阅、手动扫描、看到通知记录。

## 推荐接新版接口

### 登录和鉴权

先请求邮箱验证码：

```http
POST /api/v1/auth/request-code
Content-Type: application/json
```

```json
{ "email": "student@zju.edu.cn" }
```

这个接口后端已经实现。生产环境下，只要后端 `.env` 配了 SMTP 且 `AUTH_EMAIL_DELIVERY=true`，验证码会真实发到用户邮箱；本地开发环境可以用 `AUTH_DEV_PRINT_CODE=true` 让接口返回 `dev_code`，方便调试。

再提交验证码换 token：

```http
POST /api/v1/auth/verify-code
Content-Type: application/json
```

```json
{ "email": "student@zju.edu.cn", "code": "验证码" }
```

返回里的 `access_token` 要保存下来。之后所有用户相关接口都要带：

```http
Authorization: Bearer <access_token>
```

注意：

- 请求体或 URL 里的 `user_id` 只为兼容旧前端保留，后端不会再信任它。真正操作的是 token 对应的当前用户。
- 前端保存 token 后，调用订阅、通知渠道、通知历史接口都要带 `Authorization`。
- Swagger 手动测试时，Authorize 弹窗里填裸 token；前端代码里则需要拼成 `Bearer <access_token>`。
- 不要把用户自己的 CC98 Token 或 LLM API Key 传给后端。后端的登录 token 只用于识别产品用户。

### 健康检查

```http
GET /api/v1/health
```

用于判断后端是否在线。

### 创建订阅

```http
POST /api/v1/subscriptions
Content-Type: application/json
Authorization: Bearer <access_token>
```

```json
{
  "name": "微积分/微甲/vjf 历年卷 资料",
  "description": "",
  "board_id": null
}
```

`user_id` 可以不传；即使传了，后端也会使用 token 对应的用户 ID。

返回重点字段：

```json
{
  "id": 1,
  "user_id": "usr_xxx",
  "name": "微积分/微甲/vjf 历年卷 资料",
  "description": "",
  "topic": "微积分/微甲/vjf 历年卷 资料",
  "status": "enabled",
  "active": true
}
```

前端显示建议：

- `name` 作为订阅标题。
- `description` 作为订阅说明。
- `status=enabled` 表示启用，`status=paused` 表示暂停。
- `active` 是给旧前端兼容用的布尔值。

订阅关键词规则：

- 空格分隔表示 AND，每一段都必须命中。例如 `计算机学院 保研` 表示帖子里同时出现这两个词才提醒。
- 斜杠 `/` 表示同义词 OR，同一段里命中任意一个即可。例如 `微积分/微甲/vjf 历年卷 资料` 表示必须命中 `历年卷` 和 `资料`，同时还要命中 `微积分`、`微甲`、`vjf` 之一。
- 前端可以用占位提示文案：`例：微积分/微甲/vjf 历年卷 资料`。
- 单字关键词太容易误报，后端会过滤或拒绝无效关键词；前端可以提示用户至少写 2 个字以上的有效词。

### 获取订阅列表

```http
GET /api/v1/subscriptions
Authorization: Bearer <access_token>
```

新版接口不需要拼 `?user_id=demo_user`。如果旧前端仍然拼了，后端也会忽略它，按 token 里的用户返回数据。

### 修改订阅

```http
PATCH /api/v1/subscriptions/{id}
Content-Type: application/json
Authorization: Bearer <access_token>
```

暂停：

```json
{ "status": "paused" }
```

恢复：

```json
{ "status": "enabled" }
```

### 删除订阅

```http
DELETE /api/v1/subscriptions/{id}
Authorization: Bearer <access_token>
```

### 手动触发新帖扫描

```http
POST /api/v1/tasks/scan
X-Admin-Token: <ADMIN_API_TOKEN>
```

返回示例：

```json
{
  "scanned_subscriptions": 1,
  "fetched_topics": 2,
  "candidate_pairs": 2,
  "matched_pairs": 1,
  "created_notifications": 1,
  "sent_notifications": 0,
  "source": "mock"
}
```

这个接口属于后台/测试接口，正式前端不建议直接暴露“全局扫描”按钮。本地 demo 或测试同学要调用时，需要后端提供 `ADMIN_API_TOKEN`。

正式用户侧不需要自己调用这个接口；服务器开启 `ENABLE_SCHEDULER=true` 后，后端会按 `SCAN_INTERVAL_MINUTES` 自动扫描 CC98 全站新帖。

### 获取通知列表

```http
GET /api/v1/notifications
Authorization: Bearer <access_token>
```

新版接口不需要拼 `?user_id=demo_user`。

通知返回重点字段：

```json
{
  "id": 1,
  "subscription_id": 1,
  "topic_id": "mock-cc98-ai-1",
  "topic_title": "新生军训",
  "topic_url": "https://www.cc98.org/topic/mock-cc98-ai-1",
  "matched_reason": "命中关键词：cc98、ai",
  "delivery_status": "skipped",
  "created_at": "2026-08-12T12:00:00"
}
```

前端显示建议：

- `topic_title` 做标题。
- `topic_url` 点击跳转。
- `matched_reason` 展示为什么提醒。
- `delivery_status=skipped` 表示没有配置通知渠道，不是错误。
- 同一个 `user_id + subscription_id + topic_id` 不会重复生成通知。
- 同一次扫描里命中的多个帖子会聚合成一条 DingTalk/邮件消息发送，不会每个帖子刷一条。

## 通知渠道

获取：

```http
GET /api/v1/notification-channels
Authorization: Bearer <access_token>
```

保存 DingTalk：

```http
PUT /api/v1/notification-channels
Content-Type: application/json
Authorization: Bearer <access_token>
```

```json
{
  "user_id": "demo_user",
  "provider": "dingtalk",
  "enabled": true,
  "notify_interval_minutes": 60,
  "config": {
    "webhook": "https://oapi.dingtalk.com/robot/send?access_token=xxx",
    "secret": "SECxxx"
  }
}
```

测试：

```http
POST /api/v1/notification-channels/test
Authorization: Bearer <access_token>
```

前端注意：

- 后端返回配置时会把 `secret` 脱敏成 `***`。
- 如果用户没有改 secret，前端可以原样传回 `***`，后端会保留旧 secret。
- `notify_interval_minutes` 是用户选择的聚合推送间隔。后端会保证它不小于扫描间隔；例如扫描间隔是 10 分钟，用户传 1 分钟，返回会变成 10 分钟。

保存邮箱通知：

```http
PUT /api/v1/notification-channels
Content-Type: application/json
Authorization: Bearer <access_token>
```

```json
{
  "provider": "email",
  "enabled": true,
  "notify_interval_minutes": 60,
  "config": {
    "to_email": "student@zju.edu.cn",
    "subject_prefix": "CC98 订阅提醒"
  }
}
```

邮箱通知默认使用后端 `.env` 里的 SMTP 配置。`config` 里的收件人字段推荐使用 `to_email`，后端也兼容旧字段 `to`、`email`、`recipient`。

通知渠道返回里会包含：

```json
{
  "notify_interval_minutes": 60,
  "last_sent_at": "2026-08-15T12:00:00"
}
```

前端可以用 `GET /api/v1/health` 里的 `components.scan_interval_minutes` 作为通知频率选择器的最小值。

## 旧接口兼容

旧前端还可以继续用这些接口：

- `POST /api/subscribe`
- `GET /api/subscriptions`
- `DELETE /api/subscribe/{id}`
- `GET /api/notifications`
- `GET /api/notification-settings`
- `PUT /api/notification-settings`
- `POST /api/notification-settings/test`
- `POST /api/tasks/scan`

这些旧接口也已经接入鉴权：用户接口仍然要带 `Authorization`，手动扫描仍然要带 `X-Admin-Token`。

新开发建议优先用 `/api/v1/*`。

## 前端目前不用做的事

- 不要把用户 CC98 Token 传给后端。
- 不要把用户自己的 LLM API Key 传给后端。
- 不要依赖 `/api/research`，后端历史搜索功能已经砍掉。

## 联调最短路径

1. 后端启动，确认 `GET /api/v1/health` 返回 `ok`。
2. 前端调用 `POST /api/v1/auth/request-code` 发送验证码。
3. 前端调用 `POST /api/v1/auth/verify-code` 换取 `access_token`。
4. 前端保存 token，后续用户接口都带 `Authorization: Bearer <access_token>`。
5. 前端创建一个订阅。
6. 前端配置 DingTalk 或邮箱通知渠道，并调用测试接口确认能收到测试消息。
7. 测试阶段由管理员调用 `POST /api/v1/tasks/scan` 手动扫描；正式环境由后端定时任务自动扫描。
8. 前端调用 `GET /api/v1/notifications` 展示通知历史。
