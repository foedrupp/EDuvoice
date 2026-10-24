# EduVoice 500 — mobile-first MVP

Bilingual Khmer/English browser application for polls with a hard maximum of **500 accepted responses per session**. Students join by QR code; no installation required. Lecturer and projector pages update over WebSockets; student phones use lightweight HTTPS requests instead of maintaining 500 WebSocket connections.

## Run locally

```bash
python -m pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 on the lecturer's computer. To scan QR codes from phones, open the site using an IP or hostname reachable by those phones. Use HTTPS for internet deployment and browser UUID support.

## Capacity and security notes

- SQLite WAL mode and a serialized `BEGIN IMMEDIATE` transaction enforce at most 500 **accepted submissions** per session, including simultaneous submissions. This is **not** a verified 500-concurrent-user load-test guarantee.
- One response per browser-generated identifier, not a verified unique person; clearing browser storage or switching browsers can bypass this restriction.
- Anonymous mode stores no name but does store a random browser identifier for duplicate checks. Infrastructure may still process network metadata.
- Prototype host access uses a bearer token in the query string; protect it and do not use this version for sensitive assessments.
- For institutional use: HTTPS, lecturer authentication, server-side rate limiting, reverse proxy, retention/deletion policy, backups, monitoring and realistic 500-device load tests.
- For multiple app workers/instances, migrate to PostgreSQL and a shared event broker; the included WebSocket registry is process-local.
