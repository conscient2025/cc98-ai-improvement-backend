# 测试交接文档

## 自动化测试

```powershell
python -m pytest -q -p no:cacheprovider --basetemp .test-tmp
```

当前测试覆盖登录鉴权、订阅表达式、订阅总数限制、扫描游标、通知唯一性、批量投递、渠道状态、通知列表限频和 SQLite 旧表迁移。

## 订阅表达式

应接受：

```text
C++ 后端/服务端 实习
C# 校招
.NET/Node.js 开发
```

应拒绝：

```text
空字符串
猫
/实习
实习/
实习//校招
实习／校招
超过 255 字符的表达式
++
```

确认只有空白表示 AND、半角 `/` 表示 OR，其他标点按字面量保留。`实习，校招` 应当是一个完整关键词，不应等价于 `实习 校招`。

## 订阅数量

1. 创建 10 条订阅，其中部分设为 paused。
2. 第 11 条创建必须返回 400。
3. 暂停已有订阅不会释放名额。
4. 恢复暂停订阅应成功。
5. 删除一条后可以再创建一条。

## 通知列表

- 用户接口必须携带 Bearer token，且只能读取自己的数据。
- 同一用户 60 秒内第二次成功读取返回 429 和 `Retry-After`。
- 不同用户互不影响。
- 每个用户最多返回最近 100 条。
- 响应不包含 `is_read`、`delivery_status`、`dispatch_pending` 等字段。

## 通知渠道

- 邮箱和 DingTalk 均启用时，每轮都各尝试一次。
- 一个渠道成功、另一个失败时，通知不重新入队。
- 失败渠道写入 `last_dispatch_status=failed` 和脱敏后的 `last_dispatch_error`。
- 测试接口使用请求中的临时配置，调用后渠道列表不应新增或修改记录。
- 用户请求的通知间隔小于扫描间隔时，后端返回有效扫描间隔。
- 首次保存渠道时缺少 `config` 应返回 400；已有渠道可只提交需要修改的配置字段，未提交的密钥必须保留。
- PATCH 启用或停用渠道应立即生效，并且只能修改当前登录用户自己的渠道。

## SQLite 迁移

部署前备份数据库。启动新版后确认：

- `subscriptions` 只有 `expression`，旧 `name`、`description`、`board_id` 已移除。
- 旧 description 非空时迁移为 expression，否则使用旧 name。
- 旧逗号、顿号、分号先转换为空格，以保留旧 AND 语义。
- `notifications.is_read` 已移除，待投递状态保持不变。
- 通知列表限频状态迁移到 `notification_list_rate_limit_states`。
- 渠道正式状态可从 `last_dispatch_status` 和 `last_dispatch_error` 读取。

迁移遇到不合法或重复的旧订阅表达式时必须失败并报告 ID，不允许截断或静默删除。

## 生产烟测

1. `GET /api/v1/health` 返回 200。
2. 无 token 访问 `/api/v1/subscriptions` 返回 401。
3. 旧 `/api/subscriptions` 返回 404。
4. `systemctl is-active cc98-backend.service` 返回 active。
5. 检查服务日志没有迁移异常或连续扫描失败。

管理扫描接口 `/api/v1/tasks/scan` 需要 `X-Admin-Token`，不要在日志或测试输出中暴露密钥。
