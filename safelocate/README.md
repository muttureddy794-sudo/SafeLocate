# SafeLocate - consent-based live location sharing

Flask + MySQL + vanilla JS + Leaflet/OpenStreetMap. Share your live GPS position with trusted contacts, with expiry and a one-tap stop.

## Privacy by design
| Rule | How it is enforced |
|---|---|
| No secret tracking | GPS starts only after a user tap **and** the browser's permission prompt. A visible "You are sharing live" state is shown whenever it is on. |
| No phone-number lookup | Phone numbers are never collected. Contacts are invited by email and must **accept**. |
| Time-limited | Each share expires (15 min to 24 h). Expired access is cut off on the next request. |
| Instant stop | "Stop sharing" deletes the stored point; "Stop everything now" also revokes every invite and share. |
| Data minimisation | Only the latest point is stored, never a history. `flask purge` removes stale rows. |
| Account security | Password hashing (scrypt), email OTP on registration **and every login**, OTP stored as HMAC, 5 attempts max, rate limits, CSRF tokens, secure cookies, CSP/HSTS headers, no user enumeration on register/invite. |

## Run locally
```bash
python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env     # set APP_ENV=development, MAIL_DEV_MODE=true, SECRET_KEY, DATABASE_URL
mysql -u root -p -e "CREATE DATABASE safelocate CHARACTER SET utf8mb4; CREATE USER 'safelocate'@'localhost' IDENTIFIED BY 'password'; GRANT ALL ON safelocate.* TO 'safelocate'@'localhost';"
flask --app app init-db   # or run schema.sql
flask --app app run
```
With `MAIL_DEV_MODE=true` the OTP is printed in the terminal. **Never enable it in production.**
Geolocation works on `localhost` and on HTTPS only. To test on a phone, deploy or use an HTTPS tunnel.

## Environment variables
| Variable | Purpose |
|---|---|
| `APP_ENV` | `production` (default) or `development` |
| `SECRET_KEY` | Required in production. `python -c "import secrets;print(secrets.token_hex(32))"` |
| `DATABASE_URL` | `mysql+pymysql://user:pass@host:3306/db` (`mysql://...` is auto-converted) |
| `DB_SSL_CA` | Optional CA file path for managed MySQL that requires TLS |
| `TRUST_PROXY` | `true` behind a hosting proxy (correct client IP and HTTPS detection) |
| `MAIL_DEV_MODE`, `SMTP_*`, `MAIL_FROM` | Email delivery for OTPs and invite notices (Brevo, SendGrid, Mailgun, Amazon SES or Gmail app password all work) |

## Deploy (Railway, always-on, MySQL included)
1. Push this folder to a GitHub repo (`.env` is git-ignored).
2. At railway.app: **New Project -> Deploy from GitHub repo** and select the repo. The `Procfile` is detected automatically.
3. In the same project: **New -> Database -> MySQL**.
4. On the web service, open **Variables** and add:
   - `DATABASE_URL` = `${{MySQL.MYSQL_URL}}` (Railway reference variable)
   - `SECRET_KEY` = a long random value
   - `APP_ENV` = `production`, `TRUST_PROXY` = `true`, `MAIL_DEV_MODE` = `false`
   - your `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `MAIL_FROM`
5. **Settings -> Networking -> Generate Domain** (HTTPS is automatic). Add your own domain here if you have one.
6. Deploy. The start command runs `flask init-db` (idempotent) and then Gunicorn, so tables are created on first boot.
7. Set **Settings -> Healthcheck Path** to `/healthz`.
8. Optional: add a Railway cron service running `flask --app app purge` daily.

### Alternative: Render (web) + any managed MySQL
Render has no managed MySQL, so create one at Railway, Aiven or a similar provider and paste its URL into `DATABASE_URL` (set `DB_SSL_CA` if TLS is required).
Create a **Web Service** from the repo with Build `pip install -r requirements.txt` and Start the `Procfile` command, and choose a **paid instance** (free instances sleep, which breaks always-on sharing). Set the same variables and a health check path of `/healthz`.

### Alternative: VPS
Ubuntu + MySQL + `gunicorn app:app` behind Nginx with a Let's Encrypt certificate, run under systemd. Pass the same env vars and keep `TRUST_PROXY=true`.

Hosting plans and prices change, so confirm current always-on options before choosing.

## Scaling and hardening notes
- Rate limiting uses in-memory storage, which is per worker. For several instances, point Flask-Limiter at Redis.
- Browsers throttle GPS when a tab is in the background or the screen is locked. The app requests a screen wake lock, but true background tracking needs a native or PWA wrapper with user-visible notifications.
- Contacts see the owner's name and last update time. Owners see exactly who has access and when it expires.
- Before going public, add a privacy policy, account deletion, and abuse reporting, and consider a notification email whenever a share starts.

## Layout
```
app.py            Flask app, models, auth, API, CLI (init-db, purge)
schema.sql        Optional manual DB setup
templates/        auth pages + dashboard
static/           style.css, app.js (Leaflet map, GPS watch, polling)
Procfile          Gunicorn start command
```
