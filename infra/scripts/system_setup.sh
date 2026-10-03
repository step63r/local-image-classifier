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
# NOTE: "postgresql16-devel" is NOT a real package name on AL2023 -- it
# resolves (via a Provides: alias) to postgresql16-private-devel, which was
# found in production to install a broken /usr/bin/pg_config symlink
# (-> nonexistent pg_server_config), breaking the pgvector build below.
# postgresql16-server-devel is the real package providing the PGXS headers
# (Makefile.global etc.) needed to build a server extension; request it by
# its real name instead of the ambiguous alias.
if ! rpm -q postgresql16-server >/dev/null 2>&1; then
    dnf install -y postgresql16 postgresql16-server postgresql16-server-devel postgresql16-contrib \
        gcc make git python3 python3-pip
fi

if ! /usr/bin/pg_config --version >/dev/null 2>&1; then
    echo "pg_config is missing or broken -- attempting repair" >&2
    rpm -q postgresql16-private-devel >/dev/null 2>&1 && dnf remove -y postgresql16-private-devel
    dnf install -y postgresql16-server-devel
    /usr/bin/pg_config --version >/dev/null 2>&1 || {
        echo "pg_config still broken after repair attempt -- aborting" >&2
        exit 1
    }
fi

# --- PostgreSQL data volume --------------------------------------------------
# The DB lives on a separate, RETAINed EBS volume so that replacing the
# instance does not take the data with it. Everything here fails closed: if
# the volume is not mounted we abort *before* initdb, otherwise a replaced
# instance would silently start a fresh empty DB on the root disk.
PG_HOME="/var/lib/pgsql"
PG_VOLUME_LABEL="pgdata"

if ! mountpoint -q "${PG_HOME}"; then
    # CfnVolumeAttachment runs after the instance is created, so at first boot
    # the disk may not be there yet. On Nitro instances the device name from
    # CDK is not reliable (it shows up as nvme1n1), so pick the one disk that
    # is not the root disk.
    ROOT_DISK="/dev/$(lsblk -no PKNAME "$(findmnt -no SOURCE /)")"
    DATA_DISK=""
    for _ in $(seq 1 60); do
        mapfile -t CANDIDATES < <(lsblk -dpno NAME,TYPE | awk -v root="${ROOT_DISK}" '$2=="disk" && $1!=root {print $1}')
        if [ "${#CANDIDATES[@]}" -gt 1 ]; then
            echo "expected exactly one non-root disk, found: ${CANDIDATES[*]}" >&2
            exit 1
        fi
        if [ "${#CANDIDATES[@]}" -eq 1 ]; then
            DATA_DISK="${CANDIDATES[0]}"
            break
        fi
        echo "waiting for the PostgreSQL data volume to be attached..."
        sleep 5
    done
    [ -n "${DATA_DISK}" ] || { echo "data volume was not attached in time -- aborting" >&2; exit 1; }

    FS_TYPE=$(blkid -o value -s TYPE "${DATA_DISK}" || true)
    FRESH_FS=0
    if [ -z "${FS_TYPE}" ]; then
        mkfs.xfs -L "${PG_VOLUME_LABEL}" "${DATA_DISK}"
        FRESH_FS=1
    elif [ "$(blkid -o value -s LABEL "${DATA_DISK}" || true)" != "${PG_VOLUME_LABEL}" ]; then
        echo "${DATA_DISK} already has a filesystem that is not labelled ${PG_VOLUME_LABEL} -- refusing to touch it" >&2
        exit 1
    fi

    # nofail so a missing volume does not hang boot; postgresql itself is held
    # back by the RequiresMountsFor drop-in below instead.
    grep -q "^LABEL=${PG_VOLUME_LABEL} " /etc/fstab || \
        echo "LABEL=${PG_VOLUME_LABEL} ${PG_HOME} xfs defaults,nofail 0 2" >> /etc/fstab

    systemctl stop postgresql 2>/dev/null || true
    mkdir -p "${PG_HOME}"

    # Carry over a DB that already exists on the root disk (instance created
    # before the separate volume existed). The original files stay under the
    # mount point as a rollback copy, hidden once the volume is mounted.
    if [ "${FRESH_FS}" -eq 1 ] && [ -f "${PG_HOME}/data/PG_VERSION" ]; then
        mkdir -p /mnt/pgdata-new
        mount "${DATA_DISK}" /mnt/pgdata-new
        cp -a "${PG_HOME}/." /mnt/pgdata-new/
        umount /mnt/pgdata-new
    fi

    mount "${PG_HOME}"
fi

mountpoint -q "${PG_HOME}" || { echo "${PG_HOME} is not a mount point -- aborting before initdb" >&2; exit 1; }
chown postgres:postgres "${PG_HOME}"
chmod 700 "${PG_HOME}"
command -v restorecon >/dev/null 2>&1 && restorecon -R "${PG_HOME}" || true

mkdir -p /etc/systemd/system/postgresql.service.d
cat > /etc/systemd/system/postgresql.service.d/data-volume.conf <<EOF
[Unit]
RequiresMountsFor=${PG_HOME}
EOF
systemctl daemon-reload

PG_DATA_DIR="${PG_HOME}/data"
if [ ! -f "${PG_DATA_DIR}/PG_VERSION" ]; then
    /usr/bin/postgresql-setup --initdb
fi

# AL2023's default pg_hba.conf uses "ident" for TCP connections to
# 127.0.0.1/::1, which rejects password auth entirely (found in production:
# the app connects over TCP via DATABASE_URL, not the local Unix socket, so
# this must be password-based). Idempotent: sed is a no-op once already
# scram-sha-256.
sed -i \
    -e 's/^\(host\s\+all\s\+all\s\+127\.0\.0\.1\/32\s\+\)ident/\1scram-sha-256/' \
    -e 's/^\(host\s\+all\s\+all\s\+::1\/128\s\+\)ident/\1scram-sha-256/' \
    "${PG_DATA_DIR}/pg_hba.conf"

systemctl enable --now postgresql
systemctl reload postgresql

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
chown imageapp:imageapp /opt/imageapp/app
if [ ! -d /opt/imageapp/venv ]; then
    python3 -m venv /opt/imageapp/venv
    chown -R imageapp:imageapp /opt/imageapp/venv
fi
sudo -u imageapp /opt/imageapp/venv/bin/pip install --upgrade pip

# SECRET_KEY signs Flask's session cookie, so unlike PGPASS it must survive
# re-runs of this script unchanged -- regenerating it would silently log
# everyone out. Reuse the existing value from app.env if this instance has
# already been set up once.
if [ -f /opt/imageapp/app.env ] && grep -q '^SECRET_KEY=' /opt/imageapp/app.env; then
    SECRET_KEY=$(grep '^SECRET_KEY=' /opt/imageapp/app.env | cut -d= -f2-)
else
    SECRET_KEY=$(openssl rand -hex 32)
fi

# --- env file (rewritten every run so it always matches the current PGPASS) -
cat > /opt/imageapp/app.env <<EOF
DATABASE_URL=postgresql://imageapp:${PGPASS}@localhost:5432/imagedb
AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION}
S3_BUCKET=${S3_BUCKET}
AUTH_USERNAME=${AUTH_USERNAME}
AUTH_PASSWORD=${AUTH_PASSWORD}
SECRET_KEY=${SECRET_KEY}
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
