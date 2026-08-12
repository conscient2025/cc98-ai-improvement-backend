# CC98 AI 优化项目后端

这是 CC98 AI 优化项目的后端仓库。

后端主要负责“订阅提醒”这一块：产品账号、订阅管理、通知渠道、CC98 服务账号访问、定时扫描、帖子匹配、通知历史和健康状态。

正式的 AI 搜索建议放在浏览器插件里完成，因为它需要使用用户自己的 CC98 Token 和 LLM API Key，这些敏感信息不应该上传到后端。

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
```

这样即使暂时没有 CC98 公共账号，也能用 mock 数据跑完整流程。

## 真实 CC98 扫描

真实订阅扫描需要配置一个后端公共 CC98 服务账号：

```text
CC98_SERVICE_USERNAME=
CC98_SERVICE_PASSWORD=
```

后端会通过 `https://openid.cc98.org/connect/token` 登录和刷新 token。这个 token 只用于 Watch 订阅扫描，不用于 AI 搜索。

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
- CC98 帖子标题和正文都属于不可信输入，传给模型前必须做边界控制和结构校验。
