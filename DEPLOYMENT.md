# Great Waters — Production Deployment & Infrastructure Runbook

**Target OS:** Ubuntu 22.04 LTS / Debian 12 / macOS Server  
**Backend Stack:** Python 3.11+, Django 5, Gunicorn 22+, Nginx, PostgreSQL 15+  
**Process Manager:** Systemd  

---

## 1. Architecture Overview

```
[ Internet / Mobile App / Web Browser ]
                  │
                  ▼ (Port 443 HTTPS / TLS)
          [ Nginx Reverse Proxy ]
         ┌────────┴────────┐
         ▼                 ▼
   /static/ & /media/   Gunicorn WSGI (Unix Socket)
   (Static Assets)         │
                           ▼
                    [ Django Application ]
                           │
             ┌─────────────┴─────────────┐
             ▼                           ▼
     [ PostgreSQL 15 ]          [ Firebase FCM & SMTP ]
```

---

## 2. Environment Configuration (`.env`)

Never commit production `.env` files to Git. Create `/srv/great_waters/.env` modeled after `.env.example`:

```bash
# Core Django
DJANGO_SECRET_KEY=replace_with_a_64_character_crypto_random_key
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=portal.greatwaters.com,api.greatwaters.com,127.0.0.1

# Database (PostgreSQL)
DB_NAME=great_waters_db
DB_USER=great_waters_user
DB_PASSWORD=StrongDatabasePasswordHere
DB_HOST=127.0.0.1
DB_PORT=5432

# CORS & CSRF
CORS_ALLOWED_ORIGINS=https://portal.greatwaters.com,https://api.greatwaters.com
CSRF_TRUSTED_ORIGINS=https://portal.greatwaters.com,https://api.greatwaters.com

# JWT Configuration
JWT_ACCESS_TOKEN_LIFETIME_MINUTES=15
JWT_REFRESH_TOKEN_LIFETIME_DAYS=30

# Email (SMTP)
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.sendgrid.net
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=apikey
EMAIL_HOST_PASSWORD=SendgridApiKeyHere
DEFAULT_FROM_EMAIL=Great Waters Notifications <notifications@greatwaters.com>

# Firebase Push Notifications (FCM)
FCM_SERVER_KEY_PATH=/srv/great_waters/config/firebase_credentials.json

# Storage / Media
MEDIA_ROOT=/srv/great_waters/media
STATIC_ROOT=/srv/great_waters/static
```

---

## 3. Systemd Service Configuration

### Gunicorn Service (`/etc/systemd/system/great_waters.service`)
```ini
[Unit]
Description=Great Waters Django Gunicorn Service
After=network.target postgresql.service

[Service]
User=www-data
Group=www-data
WorkingDirectory=/srv/great_waters
EnvironmentFile=/srv/great_waters/.env
ExecStart=/srv/great_waters/venv/bin/gunicorn \
    --workers 4 \
    --threads 2 \
    --bind unix:/run/great_waters/gunicorn.sock \
    --timeout 60 \
    --access-logfile /var/log/great_waters/access.log \
    --error-logfile /var/log/great_waters/error.log \
    config.wsgi:application

Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## 4. Nginx Configuration (`/etc/nginx/sites-available/great_waters.conf`)

```nginx
server {
    listen 80;
    server_name portal.greatwaters.com api.greatwaters.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name portal.greatwaters.com api.greatwaters.com;

    ssl_certificate /etc/letsencrypt/live/portal.greatwaters.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/portal.greatwaters.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    client_max_body_size 25M;

    # Static Assets
    location /static/ {
        alias /srv/great_waters/static/;
        expires 30d;
        add_header Cache-Control "public, no-transform";
    }

    # Uploaded Receipts & Proofs (Secured)
    location /media/ {
        alias /srv/great_waters/media/;
        expires 7d;
        add_header Cache-Control "private";
    }

    # Django Application Proxy
    location / {
        proxy_pass http://unix:/run/great_waters/gunicorn.sock;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

---

## 5. Automated Backups & Scheduled Jobs

### Cron Configuration (`/etc/cron.d/great_waters_tasks`)
```cron
# 1. Daily PostgreSQL Backup at 01:00 UTC
0 1 * * * postgres pg_dump -Fc great_waters_db > /srv/backups/great_waters_$(date +\%Y\%m\%d_\%H\%M).dump && find /srv/backups/ -name "*.dump" -mtime +30 -delete

# 2. Monthly Earned-Leave Accrual on 1st of every month at 02:00 UTC
0 2 1 * * www-data /srv/great_waters/venv/bin/python /srv/great_waters/manage.py process_monthly_leave_accrual >> /var/log/great_waters/accrual.log 2>&1

# 3. Notification & Pending Approval Reminders daily at 09:00 UTC
0 9 * * 1-5 www-data /srv/great_waters/venv/bin/python /srv/great_waters/manage.py send_pending_approval_reminders >> /var/log/great_waters/reminders.log 2>&1
```

---

## 6. Disaster Recovery & Database Restore

```bash
# Restore from backup dump:
pg_restore --clean --if-exists -U great_waters_user -d great_waters_db /srv/backups/great_waters_YYYYMMDD_HHMM.dump
```

---

## Scheduled jobs (cron)

Run these from the project root with the virtualenv active (example crontab, server time):

```cron
# Earned-leave accrual on the 1st of every month (safe to re-run)
5 0 1 * *   cd /srv/great_waters && venv/bin/python manage.py process_monthly_leave_accrual
# Year-start carry-forward cap and yearly entitlement grants (safe to re-run)
15 0 1 1 *  cd /srv/great_waters && venv/bin/python manage.py process_yearly_leave_reset
# Remind approvers about requests pending longer than APPROVAL_REMINDER_DAYS
0 9 * * *   cd /srv/great_waters && venv/bin/python manage.py send_pending_reminders
```

## New environment settings

See `.env.example`: `SESSION_IDLE_TIMEOUT_MINUTES`, `PORTAL_BASE_URL` (used in e-mail links), `LEAVE_WEEKEND_DAYS`,
`APPROVAL_REMINDER_DAYS`, SMTP settings (`EMAIL_HOST`, …) for password-reset and approval e-mails, and
`EXPO_ACCESS_TOKEN` / `PUSH_NOTIFICATIONS_ENABLED` for mobile push via the Expo push service.
Set `DJANGO_DEBUG=False` in production — this also hides the demo quick-login buttons on the sign-in page.
