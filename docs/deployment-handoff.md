# 后端部署交接文档

本文档给组长/部署同学使用，说明后端如何迁到云服务器、`.env` 需要填什么、上线后如何验证。

## 现在后端负责什么

当前后端只负责“订阅新帖提醒”：

- 用户用浙大邮箱验证码登录。
- 用户创建订阅关键词。
- 后端定时扫描 CC98 全站“新帖”列表。
- 命中订阅后，通过用户选择的通知渠道推送。
- 目前通知渠道支持钉钉和邮箱。

后端不做历史搜索，也不接收用户自己的 CC98 Token 或 LLM API Key。AI 搜索如后续保留，应该放在浏览器插件侧。

## 推荐部署方式

服务器上直接拉 GitHub 仓库：

```bash
git clone https://github.com/firefelixfu026/cc98-ai-improvement-backend.git
cd cc98-ai-improvement-backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

然后编辑 `.env`。不要把 `.env` 提交到 GitHub。

如果服务器已经有仓库，更新代码：

```bash
cd cc98-ai-improvement-backend
git pull
source .venv/bin/activate
pip install -r requirements.txt
```

## 生产环境 .env 示例

以下是上线建议配置。尖括号里的内容由组长替换成真实值。

```text
# App
APP_ENV=production
DATABASE_URL=sqlite:///./cc98_watch.db
CORS_ORIGINS=<前端插件或管理页来源，例如 https://example.com>
ENABLE_PUBLIC_DOCS=false
ENABLE_SCHEDULER=true
SCAN_INTERVAL_MINUTES=10
SUBSCRIPTION_LIMIT=10
ADMIN_API_TOKEN=<强随机管理员密钥>

# Product auth
JWT_SECRET=<强随机 JWT 密钥>
SESSION_EXPIRE_HOURS=168
EMAIL_CODE_EXPIRE_MINUTES=10
ZJU_EMAIL_DOMAINS=zju.edu.cn,intl.zju.edu.cn
AUTH_DEV_PRINT_CODE=false
AUTH_EMAIL_DELIVERY=true

# Email notification channel, NetEase 163 SMTP
SMTP_HOST=smtp.163.com
SMTP_PORT=465
SMTP_USE_SSL=true
SMTP_USERNAME=cc98aiimprove@163.com
SMTP_PASSWORD=<网易邮箱 SMTP 授权码，不是邮箱登录密码>
SMTP_FROM=cc98aiimprove@163.com
SMTP_TIMEOUT=10

# CC98 service account for Watch only
CC98_API_BASE_URL=https://api.cc98.org
CC98_OPENID_BASE_URL=https://openid.cc98.org
CC98_CLIENT_ID=9a1fd200-8687-44b1-4c20-08d50a96e5cd
CC98_CLIENT_SECRET=8b53f727-08e2-4509-8857-e34bf92b27f2
CC98_SERVICE_USERNAME=<公共 CC98 账号>
CC98_SERVICE_PASSWORD=<公共 CC98 密码>
CC98_SERVICE_REFRESH_TOKEN=
CC98_TIMEOUT=10
CC98_TRUST_ENV=false
CC98_SEARCH_MIN_INTERVAL_SECONDS=1.2
CC98_SEARCH_RETRY_ATTEMPTS=2

# Matcher
AI_API_KEY=
AI_BASE_URL=https://api.siliconflow.cn/v1
AI_LLM_MODEL=Qwen/Qwen2.5-14B-Instruct
AI_LLM_PROVIDER=openai
MATCHER_FORCE_RULES=true
WATCH_FORCE_MOCK_TOPICS=false
MATCHER_BATCH_TOPIC_LIMIT=30
MATCHER_BATCH_SUBSCRIPTION_LIMIT=50

# Notifications
DINGTALK_TIMEOUT=10
```

## 关键配置解释

`APP_ENV=production`

生产模式。真实 CC98 拉取失败时不会自动回退 mock，方便及时发现服务账号或网络问题。

`ENABLE_PUBLIC_DOCS=false`

关闭公网 `/docs`。本地开发可以开，公网部署建议关。

`ENABLE_SCHEDULER=true`

开启自动扫描。不填或为 `false` 时，只能手动调 `/api/v1/tasks/scan`。

`SCAN_INTERVAL_MINUTES=10`

扫描 CC98 新帖的间隔。测试时可以临时设为 `1`，上线建议 `10`。

`ADMIN_API_TOKEN`

后台接口密钥。手动扫描和后台健康检查要带：

```http
X-Admin-Token: <ADMIN_API_TOKEN>
```

`JWT_SECRET`

用户登录 token 的签名密钥。必须使用强随机值，不能用示例值。

`AUTH_DEV_PRINT_CODE=false`

生产环境不要把验证码直接返回给前端或打印到日志。

`AUTH_EMAIL_DELIVERY=true`

生产环境验证码通过 SMTP 发到用户邮箱。

`SMTP_PASSWORD`

网易邮箱 SMTP 授权码，不是邮箱登录密码。账号需要先在网易邮箱设置里开启 IMAP/SMTP。

`CC98_SERVICE_USERNAME` / `CC98_SERVICE_PASSWORD`

后端公共 CC98 账号。它只用于扫描 CC98 全站新帖，不代表用户发帖，也不接收用户自己的 CC98 Token。

`CC98_CLIENT_ID` / `CC98_CLIENT_SECRET`

CC98 OpenID 客户端信息，当前代码默认使用已有可用值。它们和公共 CC98 账号配合，用于向 `openid.cc98.org` 换取访问 `api.cc98.org` 的 token。

`CC98_TRUST_ENV=false`

访问 CC98 时不读取服务器系统代理环境变量。我们本地测试发现系统代理可能导致 TLS 握手超时，所以默认保持 `false`。只有服务器必须通过代理访问 CC98 时才改成 `true`。

`MATCHER_FORCE_RULES=true`

当前匹配采用规则逻辑，不调用 LLM。订阅语法：

```text
空格 = AND
/ = 同义词 OR
```

例如：

```text
微积分/微甲/vjf 历年卷 资料
```

表示必须出现“历年卷”和“资料”，同时“微积分/微甲/vjf”中任意一个出现即可。

`WATCH_FORCE_MOCK_TOPICS=false`

上线必须为 `false`，否则扫描的是 mock 帖子，不是真实 CC98。

## 启动命令

开发/临时启动：

```bash
source .venv/bin/activate
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

建议用 systemd 托管，示例：

```ini
[Unit]
Description=CC98 AI Improvement Backend
After=network.target

[Service]
WorkingDirectory=/path/to/cc98-ai-improvement-backend
EnvironmentFile=/path/to/cc98-ai-improvement-backend/.env
ExecStart=/path/to/cc98-ai-improvement-backend/.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable cc98-ai-backend
sudo systemctl start cc98-ai-backend
sudo systemctl status cc98-ai-backend
```

## 上线后验收

如果生产环境关闭了 `/docs`，可以用 curl 测。

健康检查：

```bash
curl -s http://127.0.0.1:8000/api/v1/health
```

应看到：

```json
{
  "status": "ok"
}
```

后台健康检查：

```bash
curl -s \
  -H "X-Admin-Token: <ADMIN_API_TOKEN>" \
  http://127.0.0.1:8000/api/v1/admin/health
```

重点看：

- `cc98_service_account.reachable` 应为 `true`。
- `workers.watch_scan.status` 应为 `ok`。
- `workers.watch_scan.last_success_at` 应随自动扫描更新。

手动触发扫描：

```bash
curl -s -X POST \
  -H "X-Admin-Token: <ADMIN_API_TOKEN>" \
  http://127.0.0.1:8000/api/v1/tasks/scan
```

真实扫描时应看到：

```json
{
  "source": "cc98_new_posts"
}
```

如果返回 `source: "mock"`，说明没有进入真实扫描。检查：

- `WATCH_FORCE_MOCK_TOPICS=false`
- `CC98_SERVICE_USERNAME` / `CC98_SERVICE_PASSWORD` 是否填写
- 服务器是否能访问 `https://openid.cc98.org` 和 `https://api.cc98.org`
- `APP_ENV` 是否还是 development，导致失败后回退 mock

## 前端/测试同学使用说明

用户接口都需要：

```http
Authorization: Bearer <access_token>
```

用户先通过：

```text
POST /api/v1/auth/request-code
POST /api/v1/auth/verify-code
```

登录拿 token。

常用接口：

```text
GET    /api/v1/health
POST   /api/v1/auth/request-code
POST   /api/v1/auth/verify-code
POST   /api/v1/subscriptions
GET    /api/v1/subscriptions
PATCH  /api/v1/subscriptions/{id}
DELETE /api/v1/subscriptions/{id}
PUT    /api/v1/notification-channels
GET    /api/v1/notification-channels
POST   /api/v1/notification-channels/test
GET    /api/v1/notifications
POST   /api/v1/tasks/scan
GET    /api/v1/admin/health
```

注意：请求体或 URL 里的 `user_id` 只是兼容旧前端，后端实际以 token 中的用户为准。

## 已通过的关键测试

本地已验证：

- 真实 CC98 新帖扫描可用。
- 定时自动扫描可用。
- 钉钉推送可用。
- 网易邮箱 SMTP 推送可用。
- 同一轮扫描会把匹配帖子合并成一条消息。
- 重复订阅会被拦截。
- 同一通知目的地不会重复收到同一个帖子。

自动测试：

```bash
python -m pytest
```

当前通过数：`21 passed`。

## 注意事项

- 不要提交 `.env`、`cc98_watch.db`、邮箱授权码、CC98 密码、钉钉 webhook secret。
- SQLite 可以支撑演示和轻量使用；如果后续用户变多，建议迁移 PostgreSQL。
- 公网部署时建议用 Nginx 反向代理到 `127.0.0.1:8000`，并配置 HTTPS。
- 如果服务器暴露公网，不要开放 `/docs`，不要使用弱 `ADMIN_API_TOKEN`。
- 本地反复测试可能留下多个测试用户和通知渠道，导致重复推送。生产库从空库开始即可。
