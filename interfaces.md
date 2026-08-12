# CC98 AI Improvement Backend API

Backend scope: product account, Watch subscriptions, notification channels,
notification history, CC98 service-account polling, matching workers, and health.

AI Search should run in the browser plugin with the user's own CC98 token and
LLM API key. The backend keeps `/api/research` only as a compatibility stub.

## Health

- `GET /api/health`
- `GET /api/v1/health`
- `GET /api/v1/admin/health`

`/api/v1/admin/health` reports database-side worker status, CC98 service-account
probe status, and the Watch cursor.

## Auth

Product auth is separated from CC98 identity.

- `POST /api/v1/auth/request-code`

```json
{ "email": "student@zju.edu.cn" }
```

- `POST /api/v1/auth/verify-code`

```json
{ "email": "student@zju.edu.cn", "code": "123456" }
```

MVP returns `dev_code` when `AUTH_DEV_PRINT_CODE=true`. Replace this with SMTP
before public deployment.

## Subscriptions

- `POST /api/v1/subscriptions`
- `GET /api/v1/subscriptions?user_id=demo_user`
- `PATCH /api/v1/subscriptions/{id}`
- `DELETE /api/v1/subscriptions/{id}`

Create body:

```json
{
  "user_id": "demo_user",
  "name": "实习招聘",
  "description": "算法、后端、暑期实习相关新帖",
  "board_id": null
}
```

Rules:

- `status` is `enabled` or `paused`.
- Default max enabled subscriptions is 10 per user.
- Legacy `topic` is accepted and mapped to `name`.

Legacy compatibility:

- `POST /api/subscribe`
- `GET /api/subscriptions`
- `GET /api/admin/subscriptions`
- `DELETE /api/subscribe/{id}`

## Notification Channels

- `GET /api/v1/notification-channels?user_id=demo_user`
- `PUT /api/v1/notification-channels`
- `POST /api/v1/notification-channels/test`

Example DingTalk config:

```json
{
  "user_id": "demo_user",
  "provider": "dingtalk",
  "enabled": true,
  "config": {
    "webhook": "https://oapi.dingtalk.com/robot/send?access_token=xxx",
    "secret": "SECxxx"
  }
}
```

Legacy compatibility:

- `GET /api/notification-settings`
- `PUT /api/notification-settings`
- `POST /api/notification-settings/test`

## Notifications

- `GET /api/v1/notifications?user_id=demo_user`
- `GET /api/notifications?user_id=demo_user`

Notification uniqueness is `(user_id, subscription_id, topic_id)`, so repeated
scans do not create duplicate reminders for the same matched post.

## Watch Scan

- `POST /api/v1/tasks/scan`
- `POST /api/tasks/scan`

The scan worker:

1. Loads enabled subscriptions.
2. Uses CC98 service account to search candidate topics, or mock topics when no
   service account is configured.
3. Persists CC98 topic snapshots.
4. Matches topic against subscription.
5. Creates unique notifications.
6. Sends through enabled notification channels.
7. Updates worker health and cursor.

Important env vars:

- `CC98_SERVICE_USERNAME`
- `CC98_SERVICE_PASSWORD`
- `CC98_SERVICE_REFRESH_TOKEN`
- `WATCH_FORCE_MOCK_TOPICS`
- `MATCHER_FORCE_RULES`
- `ENABLE_SCHEDULER`
