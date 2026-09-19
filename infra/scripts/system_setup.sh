#!/usr/bin/env bash
# Idempotent system setup for the local-image-classifier app instance
# (Amazon Linux 2023, arm64). Runs once from EC2 UserData at first boot, and
# is safe to re-run by hand over SSH/SSM if something needs fixing --
# verify exact package/service names with `dnf list postgresql16*` /
# `systemctl list-unit-files | grep postgres` on the live instance first if
# the install step below doesn't match.
#
# Expects these to already be exported in the environment (set by CDK
# UserData): S3_BUCKET, AUTH_USERNAME, AUTH_PASSWORD, AWS_DEFAULT_REGION
set -euo pipefail

: "${S3_BUCKET:?S3_BUCKET must be set}"
: "${AUTH_USERNAME:?AUTH_USERNAME must be set}"
: "${AUTH_PASSWORD:?AUTH_PASSWORD must be set}"
: "${AWS_DEFAULT_REGION:?AWS_DEFAULT_REGION must be set}"

# --- swap (cheap OOM safety net on a 1GB instance) --------------------------
if [ ! -f /swapfile ]; then
    dd if=/dev/zero of=/swapfile bs=1M count=1024
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi

# --- PostgreSQL 16 + pgvector ------------------------------------------------
if ! rpm -q postgresql16-server >/dev/null 2>&1; then
    dnf install -y postgresql16 postgresql16-server postgresql16-devel postgresql16-contrib \
        gcc make git python3 python3-pip
fi

PG_DATA_DIR="/var/lib/pgsql/data"
if [ ! -f "${PG_DATA_DIR}/PG_VERSION" ]; then
    /usr/bin/postgresql-setup --initdb
fi

systemctl enable --now postgresql

until sudo -u postgres psql -tAc "SELECT 1" >/dev/null 2>&1; do
    echo "waiting for postgresql to accept connections..."
    sleep 2
done

if [ ! -f /usr/pgsql-16/lib/vector.so ] && [ ! -f /usr/lib64/pgsql/vector.so ]; then
    rm -rf /tmp/pgvector
    git clone --branch v0.7.4 https://github.com/pgvector/pgvector.git /tmp/pgvector
    (
        cd /tmp/pgvector
        export PG_CONFIG
        PG_CONFIG=$(command -v pg_config)
        make
        make install
    )
fi

# --- app role/database/extension --------------------------------------------
# A fresh random password is generated on every run of this script and is
# always synced into the DB via ALTER ROLE (not just at CREATE ROLE time) so
# that app.env and the actual DB role password can never drift apart, even
# across repeated re-runs.
PGPASS=$(openssl rand -base64 24)

sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='imageapp'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE ROLE imageapp WITH LOGIN;"
sudo -u postgres psql -c "ALTER ROLE imageapp WITH PASSWORD '${PGPASS}';"

sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='imagedb'" | grep -q 1 || \
    sudo -u postgres createdb -O imageapp imagedb

sudo -u postgres psql -d imagedb -c "CREATE EXTENSION IF NOT EXISTS vector;"

# Conservative tuning for a 1GB instance shared with gunicorn.
sudo -u postgres psql -c "ALTER SYSTEM SET listen_addresses = 'localhost';"
sudo -u postgres psql -c "ALTER SYSTEM SET shared_buffers = '128MB';"
sudo -u postgres psql -c "ALTER SYSTEM SET work_mem = '4MB';"
sudo -u postgres psql -c "ALTER SYSTEM SET maintenance_work_mem = '32MB';"
sudo -u postgres psql -c "ALTER SYSTEM SET effective_cache_size = '256MB';"
sudo -u postgres psql -c "ALTER SYSTEM SET max_connections = 20;"
sudo -u postgres psql -c "ALTER SYSTEM SET max_wal_size = '1GB';"
systemctl restart postgresql

# --- app user + venv ---------------------------------------------------------
id imageapp >/dev/null 2>&1 || useradd -r -m -d /opt/imageapp -s /sbin/nologin imageapp
mkdir -p /opt/imageapp/app
if [ ! -d /opt/imageapp/venv ]; then
    python3 -m venv /opt/imageapp/venv
fi
/opt/imageapp/venv/bin/pip install --upgrade pip

# --- env file (rewritten every run so it always matches the current PGPASS) -
cat > /opt/imageapp/app.env <<EOF
DATABASE_URL=postgresql://imageapp:${PGPASS}@localhost:5432/imagedb
AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION}
S3_BUCKET=${S3_BUCKET}
AUTH_USERNAME=${AUTH_USERNAME}
AUTH_PASSWORD=${AUTH_PASSWORD}
EOF
chmod 600 /opt/imageapp/app.env
chown imageapp:imageapp /opt/imageapp/app.env

# --- systemd unit -------------------------------------------------------------
cat > /etc/systemd/system/imageapp.service <<'EOF'
[Unit]
Description=Image Tagger Flask app
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=imageapp
Group=imageapp
WorkingDirectory=/opt/imageapp/app
EnvironmentFile=/opt/imageapp/app.env
ExecStart=/opt/imageapp/venv/bin/gunicorn -w 2 --worker-class gthread --threads 4 -b 0.0.0.0:8000 --timeout 60 app:app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable imageapp
# Not started here: /opt/imageapp/app is still empty at first boot. Run
# deploy_app.ps1 from the local machine to ship app.py/templates, which
# starts (and on later runs, restarts) the service.

echo "system_setup.sh complete."
