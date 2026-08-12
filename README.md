# CC98 AI Improvement Backend

This repository contains the backend for the CC98 AI product.

The backend owns product accounts, Watch subscriptions, notification channels,
CC98 service-account access, polling, matching, notification history, and health
state. AI Search should stay in the browser extension because it uses the user's
own CC98 token and LLM API key.

## Quick Start

```powershell
pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn app.main:app --port 8000 --reload
```

Open:

```text
http://127.0.0.1:8000/docs
```

For local demos, keep:

```text
MATCHER_FORCE_RULES=true
WATCH_FORCE_MOCK_TOPICS=true
ENABLE_SCHEDULER=false
```

Real Watch polling requires a CC98 service account:

```text
CC98_SERVICE_USERNAME=
CC98_SERVICE_PASSWORD=
```

The backend logs in through `https://openid.cc98.org/connect/token`, refreshes
tokens when possible, and uses that service token only for Watch.

Run checks:

```powershell
python -m compileall app tests
python -m pytest -q
```

## API Shape

New versioned APIs live under `/api/v1/*`.

Legacy compatibility APIs are also kept:

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

## Boundaries

- Do not upload user CC98 tokens to this backend.
- Do not upload user LLM API keys to this backend.
- Do not commit `.env`, database files, tokens, passwords, or webhook secrets.
- Treat CC98 titles/content as untrusted input before passing anything to a model.
