# CC98 AI+ 技术文档

> 当前实现；最后核对：2026-09-03；前端版本：0.2.0（`48fb211`）；后端基线：`27e1764`

本文档是当前项目唯一维护中的技术说明，统一记录系统边界、后端实现、前后端接口、部署、测试和已知限制。日期化文档仅用于保留历史设计背景，不作为当前实现依据。

## 1. 产品与系统边界

CC98 AI+ 包含两个相互解耦的功能：

1. **AI Search**：浏览器扩展使用用户当前的 CC98 登录状态搜索历史讨论，并调用用户自己配置的 LLM 生成带来源的回答。
2. **Watch**：产品后端持续扫描 CC98 新帖，按用户订阅表达式匹配，保存通知历史，并按需发送邮箱或钉钉提醒。

关键边界：

- 用户个人 CC98 Token 和 LLM API Key 只用于浏览器侧 AI Search，不上传产品后端。
- Watch 使用独立的后端公共 CC98 服务账号，不代表任何用户。
- 产品账号只负责订阅、通知和渠道管理，与用户的 CC98 身份无关。
- AI Search 不依赖产品后端；Watch 不依赖用户浏览器持续在线。

```text
Chrome Extension
  ├─ AI Search
  │   ├─ 用户 CC98 Token ─────────────► CC98 API
  │   └─ 用户 LLM API Key ────────────► 用户选择的 LLM API
  │
  └─ Watch UI
      └─ 产品 Bearer Token ───────────► FastAPI Backend
                                          ├─ Product Auth
                                          ├─ Subscription / Notification API
                                          ├─ APScheduler / Watch Scan
                                          ├─ SQLite / SQLAlchemy
                                          ├─ 公共 CC98 服务账号 ─────► CC98 API
                                          ├─ SMTP
                                          └─ DingTalk
```

生产环境中扩展必须通过 HTTPS 域名访问后端。代码当前默认的公网 HTTP IP 只适合开发联调。

## 2. 三套身份和数据归属

| 身份或数据 | 保存位置 | 用途 |
| --- | --- | --- |
| 产品账号 access token | 浏览器 `chrome.storage.local` | 调用 Watch 用户接口 |
| 用户 CC98 Token | 浏览器 `chrome.storage.session` | AI Search 调用 CC98 API |
| 用户 LLM API Key | 浏览器 `chrome.storage.local` | AI Search 调用用户选择的模型 |
| 后端公共 CC98 账号 | 服务器环境变量、进程内 Token 状态 | Watch 扫描新帖 |
| 用户、订阅、渠道、通知历史 | 后端数据库 | Watch 业务数据 |
| AI Search 问题、进度和结果 | 浏览器本地 | 跨标签页恢复和展示 |

`chrome.storage.local` 是扩展隔离的持久化存储，但不是操作系统级加密保险箱。隐私说明不能声称 LLM Key 或产品 Token 已加密。

## 3. 前端与后端职责

前端是 Chrome Manifest V3 扩展。Content Script 负责页面侧栏和 AI Search；Extension Service Worker 负责跨域 fetch 代理、徽章和打开设置页。

AI Search 当前流程：

```text
用户问题
  → LLM 生成最多 N 个搜索 Query
  → 串行、限速调用 CC98 搜索接口
  → 按 topic id 去重
  → 标题关键词重叠和热度启发式排序
  → 读取 Top 主题的前若干楼层并逐楼截断
  → LLM 生成 Markdown 总结
  → 展示总结和原帖链接
```

当前没有独立的 embedding 或第二轮语义重排。搜索状态保存在扩展本地并跨标签页同步；发起标签页关闭后，其他标签页会在 25 秒无心跳时判定搜索中断。

Watch 前端只调用本文档第 11 节的 `/api/v1` 接口：

- 不发送或信任 `user_id`；
- 不调用管理员扫描接口；
- 通知列表只在用户主动打开或手动刷新时读取；
- 60 秒冷却期内显示本地缓存；
- 收到 401 后清除本地产品登录信息；
- 收到 429 后遵守 `Retry-After`。

## 4. 后端技术栈与进程模型

- Python + FastAPI
- SQLAlchemy ORM
- SQLite（默认）
- APScheduler 后台调度
- httpx 调用 CC98、钉钉和可选 LLM Matcher
- smtplib 发送验证码和邮件通知

应用导入 `app.main` 时加载 `.env`、创建数据表并运行自定义兼容迁移。FastAPI 启动事件根据 `ENABLE_SCHEDULER` 启动内置 APScheduler。

启用调度器时必须保持 **一个应用进程、一个服务副本**。当前没有分布式锁，多 Uvicorn worker 或多副本可能并发扫描并重复调用外部通知渠道。

### 4.1 模块职责

| 模块 | 职责 |
| --- | --- |
| `app/main.py` | FastAPI、路由、鉴权依赖、CORS 和健康接口 |
| `app/auth.py` | 邮箱验证、验证码、产品 access token |
| `app/models.py` | 数据模型、唯一约束和索引 |
| `app/database.py` | Engine、Session、建表和 SQLite 兼容迁移 |
| `app/tasks.py` | APScheduler 启停和扫描任务入口 |
| `app/watch.py` | 分页、游标、匹配、通知创建和投递编排 |
| `app/cc98_auth.py` | 公共 CC98 账号登录、刷新和内存状态 |
| `app/cc98_client.py` | CC98 API、请求限速、403 退避和数据标准化 |
| `app/matcher.py` | 表达式解析、规则匹配和可选 LLM 复核 |
| `app/notifiers.py` | 邮箱、钉钉、批量消息和脱敏 |
| `app/notification_frequency.py` | 用户级提醒周期 |
| `app/schemas.py` | Pydantic 请求和响应模型 |
| `app/utils.py` | UTC、JSON、HMAC、验证码和 ID 工具 |

## 5. 产品认证

### 5.1 邮箱验证码

`POST /api/v1/auth/request-code` 只接受 `ZJU_EMAIL_DOMAINS` 中的域名。验证码默认 6 位，数据库只保存基于服务端 Secret 的 HMAC 摘要，默认 10 分钟过期。

生产环境必须设置：

```dotenv
AUTH_DEV_PRINT_CODE=false
AUTH_EMAIL_DELIVERY=true
JWT_SECRET=<强随机值>
SMTP_HOST=<SMTP 服务地址>
SMTP_USERNAME=<SMTP 用户名>
SMTP_PASSWORD=<SMTP 授权码>
SMTP_FROM=<发件地址>
```

当前验证码请求没有应用内的单邮箱发送频率或来源 IP 限制，公网入口必须额外限频。验证码校验只使用最新一条未消费记录，错误尝试超过 5 次返回 429。

### 5.2 产品 access token

当前令牌格式为：

```text
base64url(JSON payload).HMAC-SHA256-prefix
```

Payload 包含 `sub`、`email` 和 `exp`。签名使用 `JWT_SECRET`。虽然变量名和旧材料使用“JWT”，当前令牌不是标准三段式 JWT；前端必须把它当作不透明字符串。

受保护接口依次校验 Bearer Token、签名、过期时间、用户是否存在以及 `status=active`。当前没有服务端登出、令牌撤销表或密钥轮换机制；前端登出只清除本地令牌。

## 6. 数据模型

| 表 | 主键/唯一约束 | 用途 |
| --- | --- | --- |
| `users` | `id`；`email` 唯一 | 产品账号 |
| `email_verification_codes` | `id` | 验证码摘要、过期、消费和尝试次数 |
| `subscriptions` | `id`；`(user_id, expression)` 唯一 | 订阅表达式和 enabled/paused 状态 |
| `notification_channels` | `id`；`(user_id, provider)` 唯一 | 邮箱/钉钉配置和最近投递状态 |
| `notification_preferences` | `user_id` | 用户统一提醒间隔和上次投递开始时间 |
| `cc98_topics` | `topic_id` | 已抓取主题的轻量快照和原始 JSON |
| `notifications` | `id`；`(user_id, topic_id)` 唯一 | 通知历史和短期待投递队列 |
| `notification_list_rate_limit_states` | `user_id` | 通知列表最近成功读取时间 |
| `system_cursors` | `source` | 全站扫描高水位 |
| `worker_statuses` | `name` | Worker 状态、错误和指标 |

当前没有数据库外键，关联完整性由应用逻辑维护。通知渠道的 `config_json` 当前明文存储，只有 API 响应和错误文本会脱敏。

## 7. 订阅表达式和匹配

公开语法：

- 空白表示 AND；
- 半角 `/` 表示 OR；
- 其他标点是关键词正文。

例如：

```text
C++ 后端/服务端 实习
```

表示必须命中 `C++` 和 `实习`，并命中 `后端` 或 `服务端` 中至少一个。

校验规则：

- 规范化后不能为空且最多 255 个字符；
- 每个关键词至少 2 个 Unicode 字符；
- 每个关键词至少包含字母、数字或中文等 Unicode letter/number；
- `/` 两侧不能为空，不接受全角 `／`；
- 英文匹配不区分大小写；
- 用户总订阅数受 `SUBSCRIPTION_LIMIT` 限制，暂停订阅也计数。

启用订阅按 ID 升序参与匹配。同一用户有多条订阅命中同一帖子时，第一条命中的订阅胜出，只生成一条通知。

默认 `MATCHER_FORCE_RULES=true`，只对标题和可选 `content` 执行包含判断。正常 `/topic/new` 数据主要只有标题，因此 Watch 当前主要按标题匹配。

设置 `MATCHER_FORCE_RULES=false` 且配置 `AI_API_KEY` 后，规则命中的候选才调用 OpenAI-compatible `/chat/completions` 复核；LLM 异常或 JSON 不合法时退回规则结果。当前是逐订阅/逐主题调用，不是批量 Matcher Job。

## 8. CC98 服务账号与新帖扫描

Watch 使用独立公共账号。认证优先级：

1. 有效的内存 access token；
2. refresh token；
3. 用户名和密码重新登录。

登录返回的新 refresh token 只保存在进程内存，不会自动写回 `.env`。CC98 请求默认 `CC98_TRUST_ENV=false`，避免意外使用系统代理。

扫描游标键为：

```text
cc98_watch:last_topic_id
```

### 8.1 首次运行

- 默认 `WATCH_INITIAL_CURSOR_MODE=baseline`：将当前新帖列表头设为游标，不通知历史帖子。
- `backfill`：在 `MAX_NEW_POST_PAGES` 范围内处理历史帖子，只应在明确需要时使用。
- `CC98_INITIAL_TOPIC_ID` 仅在数据库没有游标时用于恢复已知高水位。

### 8.2 正常扫描

1. 以 `NEW_POST_PAGE_SIZE` 分页，代码限制最大为 20。
2. 第一页最大数值 topic id 是候选新游标，游标不会倒退。
3. 只收集 `topic_id > old_cursor` 的帖子。
4. 单轮通过 `seen_topic_ids` 去除分页重叠。
5. 遇到任意 `topic_id <= old_cursor`、不足一页、mock 数据或最大页数时停止。
6. 达到最大页数仍未越过旧游标时标记 `cursor_gap` 并保留旧游标。
7. 主题、通知和游标先提交，再处理外部通知。

`/topic/new` 有独立请求间隔和有限 403 指数退避。生产环境未配置公共账号或请求失败时扫描直接失败；只有开发环境允许 mock 回退。

## 9. 通知历史和外部投递

### 9.1 创建通知

- `(user_id, topic_id)` 是最终去重边界。
- 用户存在至少一个启用渠道时，新通知设置 `dispatch_pending=true`。
- 没有启用渠道时仍保存通知历史，但不进入外部提醒队列。
- 用户以后启用渠道不会补发旧历史。

### 9.2 用户统一提醒周期

所有渠道共享同一用户周期：

```text
effective_interval = max(SCAN_INTERVAL_MINUTES, notify_interval_minutes)
```

到期判断使用用户级 `last_dispatch_started_at`，默认允许 5 秒调度抖动。

### 9.3 至多一次投递

到期后，后端先把当前用户全部待处理通知设为非 pending、写入 `dispatch_processed_at` 并提交，再按 `NOTIFICATION_BATCH_SIZE` 调用全部启用渠道。

因此：

- 外部发送失败不重新入队；
- 出队提交后进程崩溃可能漏发；
- 前端通知历史是兜底；
- 渠道只记录最近一次正式发送状态，不记录逐通知投递审计。

同一轮扫描内，相同 provider 和相同目的地会按 topic id 去重；只有发送成功后才登记目的地已发送集合。该集合不会跨进程或跨扫描保留。

## 10. 接口边界

HTTP 接口的请求、响应、鉴权和错误码集中维护在 [后端接口说明](interfaces.md)。本文件只说明系统结构与运行机制，不重复接口字段。

## 11. 环境变量

`.env.example` 是配置字段参考，以下为生产重点：

```dotenv
# App
APP_ENV=production
APP_LOG_LEVEL=INFO
DATABASE_URL=sqlite:///./cc98_watch.db
CORS_ORIGINS=https://<受控网页来源>
ENABLE_PUBLIC_DOCS=false
ENABLE_SCHEDULER=true
SCAN_INTERVAL_MINUTES=10
SUBSCRIPTION_LIMIT=10
ADMIN_API_TOKEN=<强随机值>

# Product auth
JWT_SECRET=<强随机值>
SESSION_EXPIRE_HOURS=168
EMAIL_CODE_EXPIRE_MINUTES=10
ZJU_EMAIL_DOMAINS=zju.edu.cn,intl.zju.edu.cn
AUTH_DEV_PRINT_CODE=false
AUTH_EMAIL_DELIVERY=true

# SMTP
SMTP_HOST=smtp.163.com
SMTP_PORT=465
SMTP_USE_SSL=true
SMTP_USERNAME=<SMTP 用户名>
SMTP_PASSWORD=<SMTP 授权码>
SMTP_FROM=<发件地址>
SMTP_TIMEOUT=10

# CC98 Watch account
CC98_API_BASE_URL=https://api.cc98.org
CC98_OPENID_BASE_URL=https://openid.cc98.org
CC98_CLIENT_ID=<客户端 ID>
CC98_CLIENT_SECRET=<客户端 Secret>
CC98_SERVICE_USERNAME=<公共账号>
CC98_SERVICE_PASSWORD=<公共密码>
CC98_SERVICE_REFRESH_TOKEN=
CC98_TIMEOUT=10
CC98_TRUST_ENV=false
CC98_NEW_POSTS_MIN_INTERVAL_SECONDS=2
CC98_NEW_POSTS_RETRY_ATTEMPTS=2
NEW_POST_PAGE_SIZE=20
MAX_NEW_POST_PAGES=10
CC98_INITIAL_TOPIC_ID=
WATCH_INITIAL_CURSOR_MODE=baseline
WATCH_FORCE_MOCK_TOPICS=false

# Matcher
MATCHER_FORCE_RULES=true
AI_API_KEY=
AI_BASE_URL=https://api.siliconflow.cn/v1
AI_LLM_MODEL=Qwen/Qwen2.5-14B-Instruct

# Notifications
DINGTALK_TIMEOUT=10
NOTIFICATION_BATCH_SIZE=20
NOTIFICATION_DUE_GRACE_SECONDS=5
NOTIFICATION_READ_RATE_LIMIT_SECONDS=60
```

当前代码不读取旧配置中的 `AI_LLM_PROVIDER`、`MATCHER_BATCH_TOPIC_LIMIT` 和 `MATCHER_BATCH_SUBSCRIPTION_LIMIT`。`CC98_SEARCH_*` 仅供后端尚未公开使用的搜索客户端方法读取，不参与 Watch 新帖扫描。

## 12. 数据库升级

启动时会自动执行兼容迁移：

- 旧订阅 `name/description` 转为 `expression`，非法或重复数据会让启动失败并报告 ID；
- 旧通知表重建为 `(user_id, topic_id)` 唯一结构；
- 移除旧 `subscription_id`、`is_read` 等字段；
- 补充渠道运行状态并清理重复渠道；
- 迁移通知列表限频状态；
- 建立用户级通知偏好。

生产升级必须：停止服务和扫描、备份数据库、验证备份哈希和 `PRAGMA integrity_check`、在副本上试跑迁移，再启动正式服务。当前没有 downgrade；代码回滚时必须同时恢复对应数据库备份。

## 13. 生产部署

### 13.1 当前服务器状态

以下状态于 2026-09-03 通过只读 SSH 检查确认：

| 项目 | 当前值 |
| --- | --- |
| 服务器 | `ubuntu@122.51.57.222:22` |
| systemd unit | `cc98-backend.service`，loaded、enabled、active/running |
| 工作目录 | `/opt/cc98-ai-improvement-backend` |
| 运行用户 | `ubuntu` |
| 启动命令 | `.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000` |
| 重启策略 | `Restart=always`，间隔 5 秒；本次启动后重启计数为 0 |
| 本次启动时间 | 2026-09-03 20:31:31 CST |
| 后端健康检查 | 服务器本机和外部访问 `122.51.57.222:8000/api/v1/health` 均返回 200 |
| 调度状态 | 已启用，扫描周期 10 分钟 |
| Nginx | 已安装、enabled 且 active，但没有后端 API 反向代理 |
| 文件权限 | `.env` 及其备份为 `0600`；主数据库和两份数据库备份为 `0644` |

Nginx 当前监听 80/443，只把 `conscient.hk` 的站点请求代理到 `127.0.0.1:3000`。访问 Nginx 的 `/api/v1/health` 返回 404；后端 8000 端口可以从公网直接访问。因此，“通过 Nginx/HTTPS 暴露后端 API”是待实施目标，不是当前事实。

数据库包含用户邮箱及明文通知渠道 Secret 时，`0644` 权限不合适，应收紧为仅服务账号可读写，并为备份建立独立目录、权限和保留策略。

### 13.2 安装与更新

首次安装：

```bash
git clone https://github.com/firefelixfu026/cc98-ai-improvement-backend.git
cd cc98-ai-improvement-backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

更新生产实例前先停止扫描并备份数据库，在代码和依赖更新后运行测试，再重启和检查服务：

```bash
sudo systemctl stop cc98-backend
# 备份并校验 SQLite 数据库
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest -q -p no:cacheprovider
sudo systemctl start cc98-backend
systemctl status cc98-backend --no-pager
curl --fail http://127.0.0.1:8000/api/v1/health
```

实际代码同步方式和数据库备份路径应由运维流程确定，不应在数据库未备份时直接更新。

### 13.3 公网入口目标

正式上线前应让 Uvicorn 改为只监听 `127.0.0.1:8000`，再由 Nginx、Caddy 或可信 CDN 提供 HTTPS。入口还需要：

- 将 `/api/` 明确代理到 `127.0.0.1:8000`，并验证健康检查和全部 CORS 来源；
- 对验证码请求、验证码校验、通知列表和管理接口分别限频；
- 只信任已知代理写入的 `X-Forwarded-For`；
- 管理接口额外限制来源；
- 生产关闭 `/docs`、`/redoc` 和 `/openapi.json`；
- 前端默认地址和 Manifest 权限同步切换到 HTTPS 域名；
- 在云防火墙和主机防火墙中关闭公网 8000 端口。

启用 `ENABLE_SCHEDULER=true` 时严禁使用多个 Uvicorn worker，或同时运行多个启用调度的服务副本。

## 14. 上线验收和故障处理

自动化测试：

```bash
python -m pytest -q -p no:cacheprovider
```

2026-09-03 当前基线为 `42 passed`，同时有 7 条弃用警告，主要涉及 FastAPI `on_event`、Starlette TestClient/httpx 和 `datetime.utcnow()`。

验收顺序：

1. `GET /api/v1/health` 返回 200，扫描周期和限频正确；
2. 无 Token 访问订阅接口返回 401；
3. 生产验证码真实到达且响应没有 `dev_code`；
4. 管理健康接口能读取 worker 和游标；
5. 首次 baseline 扫描返回 `baseline_created=true`；
6. 后续扫描返回 `source=cc98_new_posts`、`status=ok`、`cursor_gap=false`；
7. 邮箱和钉钉测试消息成功；
8. 通知历史按用户隔离，60 秒内重复读取返回 429；
9. 日志不包含 Token、密码或完整 Webhook。

`cursor_gap` 处理：暂停自动调度，保存旧游标和日志，评估新增量，临时增加 `MAX_NEW_POST_PAGES` 后手动重试；仍无法越过时，由维护者决定是否接受缺口并重新建立基线，不能无记录地直接推进游标。

外部通知失败不会补发。修复配置后只发送测试消息，原匹配帖子仍可在通知历史查看。

## 15. 已知限制与上线前事项

- 当前服务器由 Uvicorn 直接监听 `0.0.0.0:8000`，Nginx 没有后端 API 反代；必须迁移到受控 HTTPS 入口并关闭公网 8000。
- 服务器主数据库和数据库备份权限为 `0644`，而数据库含邮箱和明文渠道 Secret；必须改为仅服务账号可访问。
- 配置备份和数据库备份散落在代码目录，缺少独立备份位置、加密、保留周期和恢复演练。
- 前端登录校验只接受 `@zju.edu.cn`，后端默认还接受 `@intl.zju.edu.cn`，需要统一。
- 验证码没有应用内单邮箱/IP 发送限频。
- 通知渠道 Secret 在数据库明文保存。
- 产品 access token 不是标准 JWT，没有服务端撤销能力。
- APScheduler 不支持多进程或多副本安全运行。
- SQLite 自定义迁移没有版本号、锁或自动回滚。
- 通知历史没有保留和清理任务，会持续增长。
- ZJU-Connect 健康状态固定为 unknown，无法区分隧道、账号和 CC98 服务故障。
- requirements 未锁版本，生产环境不可完全复现。
- 前端 Markdown 渲染当前没有独立 HTML sanitizer，LLM 输出必须在正式发布前进行安全过滤。
- 仓库中曾出现固定 CC98 client credential；必须确认其是否可公开，否则应轮换并只通过环境变量注入。

## 16. 历史文档

- `2026-08-11-product-architecture-draft.md`：早期产品和架构讨论。
- `2026-08-21-backend-prelaunch-review.md`：早期后端上线前审查。
- `2026-09-02-scan-notification-redesign.md`：扫描、通知和前端刷新重构设计记录。
