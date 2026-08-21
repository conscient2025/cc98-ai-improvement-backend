# CC98 AI 优化项目后端

这是 CC98 AI 优化项目的后端仓库。

后端主要负责“订阅新帖提醒”这一块：产品账号、订阅管理、通知渠道、CC98 服务账号访问、定时扫描新帖、帖子匹配、通知历史和健康状态。

后端不再做历史搜索/历史查阅功能。正式的 AI 搜索如果后续要做，应该放在浏览器插件里完成，因为它需要使用用户自己的 CC98 Token 和 LLM API Key，这些敏感信息不应该上传到后端。

扫描频率和通知频率是分开的：后端按 `SCAN_INTERVAL_MINUTES` 固定扫描新帖，用户可以在通知渠道里选择更慢的聚合推送频率。通知频率不会快于扫描频率，例如扫描每 10 分钟一次时，用户最快也是 10 分钟收到一次，也可以选择 60 分钟收到一次。

## 快速启动

```powershell
pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn app.main:app --port 8000 --reload
```

启动后打开：

```text
http://127.0.0.1:8000/docs
```

本地演示建议 `.env` 保持：

```text
MATCHER_FORCE_RULES=true
WATCH_FORCE_MOCK_TOPICS=true
ENABLE_SCHEDULER=false
SCAN_INTERVAL_MINUTES=10
AUTH_DEV_PRINT_CODE=true
AUTH_EMAIL_DELIVERY=false
```

这样即使暂时没有 CC98 公共账号，也能用 mock 数据跑完整流程。

## 登录鉴权

用户先用浙大邮箱验证码登录，拿到 `access_token` 后，前端调用订阅、通知渠道、通知历史等用户接口时都要带：

```http
Authorization: Bearer <access_token>
```

后端会从 token 里识别当前用户，不再信任请求体或查询参数里的 `user_id`。为了兼容旧前端，接口里暂时还保留 `user_id` 字段，但它不会决定实际操作哪个用户。

手动扫描和管理健康检查属于后台接口，需要单独的管理员密钥：

```http
X-Admin-Token: <ADMIN_API_TOKEN>
```

生产环境必须配置强随机的 `JWT_SECRET` 和 `ADMIN_API_TOKEN`，不要使用 `.env.example` 里的示例值。

## 真实 CC98 新帖扫描

真实订阅扫描需要配置一个后端公共 CC98 服务账号。扫描对象是 CC98 顶部入口里的“新帖”列表，也就是全站多个版块汇总出来的最新帖子，不是某一个固定版块。

```text
CC98_SERVICE_USERNAME=
CC98_SERVICE_PASSWORD=
```

后端会通过 `https://openid.cc98.org/connect/token` 登录和刷新 token。这个 token 只用于 Watch 订阅新帖扫描，不用于 AI 搜索。扫描时会读取 CC98 全站新帖列表，再和订阅关键词/说明做匹配。

## 网易邮箱发订阅推送

如果用户选择用邮箱接收订阅帖子推送，可以用网易邮箱作为后端发件邮箱。需要先在网易邮箱里开启 SMTP，并获取“授权码”。后端使用授权码登录 SMTP，不要使用邮箱登录密码。

`.env` 示例：

```text
SMTP_HOST=smtp.163.com
SMTP_PORT=465
SMTP_USE_SSL=true
SMTP_USERNAME=你的网易邮箱
SMTP_PASSWORD=网易邮箱授权码
SMTP_FROM=你的网易邮箱
```

验证码邮件也复用这套 SMTP 配置。生产环境建议：

```text
AUTH_DEV_PRINT_CODE=false
AUTH_EMAIL_DELIVERY=true
```

这样验证码只会发到用户邮箱，不会在接口返回或日志中直接暴露。

如果使用 126 邮箱，通常把 `SMTP_HOST` 改成：

```text
SMTP_HOST=smtp.126.com
```

## 本地检查

```powershell
python -m compileall app tests
python -m pytest -q
```

如果 Windows 临时目录权限异常，可以这样跑测试：

```powershell
python -m pytest -q -p no:cacheprovider --basetemp .test-tmp
```

## 接口说明

新版接口统一放在 `/api/v1/*` 下。

交接文档：

- 前端同学：`docs/frontend-handoff.md`
- 测试同学：`docs/test-handoff.md`
- 接口总览：`interfaces.md`

为了兼容之前仓库里的前端，也保留了旧接口：

```text
POST   /api/subscribe
GET    /api/subscriptions
DELETE /api/subscribe/{id}
GET    /api/notifications
GET    /api/notification-settings
PUT    /api/notification-settings
POST   /api/notification-settings/test
POST   /api/tasks/scan
```

## 安全边界

- 不要把用户自己的 CC98 Token 上传到后端。
- 不要把用户自己的 LLM API Key 上传到后端。
- 不要提交 `.env`、数据库文件、token、密码、webhook secret。
- 生产环境不要使用 `CORS_ORIGINS=*`，应改成实际前端来源。
- 生产环境建议设置 `APP_ENV=production`、`ENABLE_PUBLIC_DOCS=false`、`WATCH_FORCE_MOCK_TOPICS=false`。
- 生产环境 CC98 拉取失败时不会自动回退 mock 数据，应通过 `/api/v1/admin/health` 或日志排查服务账号/RVPN/网络问题。
- CC98 帖子标题和正文都属于不可信输入，传给模型前必须做边界控制和结构校验。
