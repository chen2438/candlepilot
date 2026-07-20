#!/usr/bin/env bash
set -euo pipefail

# CandlePilot one-shot installer for Ubuntu 24.04, Debian 12, or Debian 13.
# The backend remains loopback-only. Nginx exposes an authenticated HTTPS
# console using a self-signed certificate whose SAN is the VPS IP address.

APP_USER="${CANDLEPILOT_APP_USER:-candlepilot}"
APP_DIR="${CANDLEPILOT_APP_DIR:-/opt/candlepilot}"
REPO_URL="${CANDLEPILOT_REPO_URL:-https://github.com/chen2438/candlepilot.git}"
BRANCH="${CANDLEPILOT_BRANCH:-main}"
PUBLIC_PORT="${CANDLEPILOT_PUBLIC_PORT:-8443}"
NODE_VERSION="${CANDLEPILOT_NODE_VERSION:-24.18.0}"
UV_VERSION="${CANDLEPILOT_UV_VERSION:-0.11.15}"
MANAGED_PYTHON_VERSION="${CANDLEPILOT_MANAGED_PYTHON_VERSION:-3.12.13}"

fail() {
  echo "CandlePilot installer: $*" >&2
  exit 1
}

prompt_value() {
  local variable_name="$1"
  local prompt="$2"
  local secret="${3:-false}"
  local current="${!variable_name:-}"
  if [[ -z "$current" ]]; then
    if [[ "$secret" == "true" ]]; then
      read -r -s -p "$prompt" current </dev/tty
      echo >/dev/tty
    else
      read -r -p "$prompt" current </dev/tty
    fi
  fi
  [[ -n "$current" ]] || fail "$variable_name cannot be empty"
  printf -v "$variable_name" '%s' "$current"
}

[[ "${EUID}" -eq 0 ]] || fail "run as root (for example: sudo bash scripts/install_vps.sh)"
[[ -r /etc/os-release ]] || fail "cannot identify the operating system"
# shellcheck disable=SC1091
source /etc/os-release
case "${ID:-}:${VERSION_ID:-}" in
  ubuntu:24.04)
    PLATFORM_NAME="Ubuntu 24.04"
    PYTHON_BIN="/usr/bin/python3.12"
    PYTHON_PACKAGES=(python3.12 python3.12-venv)
    MANAGED_PYTHON=false
    ;;
  debian:12)
    PLATFORM_NAME="Debian 12"
    PYTHON_BIN=""
    PYTHON_PACKAGES=(python3)
    MANAGED_PYTHON=true
    ;;
  debian:13)
    PLATFORM_NAME="Debian 13"
    PYTHON_BIN="/usr/bin/python3"
    PYTHON_PACKAGES=(python3 python3-venv)
    MANAGED_PYTHON=false
    ;;
  *)
    fail "this installer supports Ubuntu 24.04, Debian 12, and Debian 13 only"
    ;;
esac
[[ "$PUBLIC_PORT" =~ ^[0-9]+$ ]] && (( PUBLIC_PORT >= 1024 && PUBLIC_PORT <= 65535 )) \
  || fail "CANDLEPILOT_PUBLIC_PORT must be between 1024 and 65535"
[[ ! -e "$APP_DIR" ]] || fail "$APP_DIR already exists; refusing to overwrite an installation"

PUBLIC_IP="${CANDLEPILOT_PUBLIC_IP:-}"
if [[ -z "$PUBLIC_IP" ]]; then
  DETECTED_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')"
  if [[ -n "$DETECTED_IP" ]] && ! python3 - "$DETECTED_IP" <<'PY'
import ipaddress
import sys

raise SystemExit(0 if ipaddress.ip_address(sys.argv[1]).is_global else 1)
PY
  then
    DETECTED_IP=""
  fi
  read -r -p "Public IPv4 address${DETECTED_IP:+ [$DETECTED_IP]}: " PUBLIC_IP </dev/tty
  PUBLIC_IP="${PUBLIC_IP:-$DETECTED_IP}"
fi
[[ -n "$PUBLIC_IP" ]] || fail "CANDLEPILOT_PUBLIC_IP cannot be empty"
python3 - "$PUBLIC_IP" <<'PY' || fail "CANDLEPILOT_PUBLIC_IP must be a public IPv4 address"
import ipaddress
import sys

address = ipaddress.ip_address(sys.argv[1])
if address.version != 4 or not address.is_global:
    raise SystemExit(1)
PY

ADMIN_USERNAME="${CANDLEPILOT_ADMIN_USERNAME:-operator}"
ADMIN_PASSWORD="${CANDLEPILOT_ADMIN_PASSWORD:-}"
BINANCE_KEY="${BINANCE_TESTNET_API_KEY:-}"
BINANCE_SECRET="${BINANCE_TESTNET_API_SECRET:-}"
prompt_value ADMIN_USERNAME "Administrator username [operator]: "
prompt_value ADMIN_PASSWORD "Administrator password (minimum 12 characters): " true
prompt_value BINANCE_KEY "Binance Demo API key: " true
prompt_value BINANCE_SECRET "Binance Demo API secret: " true
[[ "$ADMIN_USERNAME" =~ ^[A-Za-z0-9_.@-]{3,64}$ ]] || fail "administrator username is invalid"
(( ${#ADMIN_PASSWORD} >= 12 )) || fail "administrator password must contain at least 12 characters"
[[ "$ADMIN_PASSWORD" != *$'\n'* && "$ADMIN_PASSWORD" != *$'\r'* ]] \
  || fail "administrator password must be a single-line value"
[[ "$BINANCE_KEY" != *$'\n'* && "$BINANCE_KEY" != *$'\r'* \
  && "$BINANCE_SECRET" != *$'\n'* && "$BINANCE_SECRET" != *$'\r'* ]] \
  || fail "credentials must be single-line values"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl git nginx openssl sqlite3 xz-utils "${PYTHON_PACKAGES[@]}"

case "$(uname -m)" in
  x86_64)
    NODE_ARCH="x64"
    UV_TARGET="x86_64-unknown-linux-gnu"
    ;;
  aarch64|arm64)
    NODE_ARCH="arm64"
    UV_TARGET="aarch64-unknown-linux-gnu"
    ;;
  *) fail "unsupported CPU architecture: $(uname -m)" ;;
esac
NODE_ARCHIVE="node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz"
NODE_ROOT="/opt/node-v${NODE_VERSION}-linux-${NODE_ARCH}"
if [[ ! -x "$NODE_ROOT/bin/node" ]]; then
  TEMP_DIR="$(mktemp -d)"
  trap 'rm -rf "$TEMP_DIR"' EXIT
  curl --fail --show-error --location --proto '=https' --tlsv1.2 \
    "https://nodejs.org/dist/v${NODE_VERSION}/${NODE_ARCHIVE}" -o "$TEMP_DIR/$NODE_ARCHIVE"
  curl --fail --show-error --location --proto '=https' --tlsv1.2 \
    "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt" -o "$TEMP_DIR/SHASUMS256.txt"
  (cd "$TEMP_DIR" && grep "  ${NODE_ARCHIVE}$" SHASUMS256.txt | sha256sum --check --status) \
    || fail "Node.js checksum verification failed"
  tar -xJf "$TEMP_DIR/$NODE_ARCHIVE" -C /opt
fi
ln -sfn "$NODE_ROOT/bin/node" /usr/local/bin/node
ln -sfn "$NODE_ROOT/bin/npm" /usr/local/bin/npm
ln -sfn "$NODE_ROOT/bin/npx" /usr/local/bin/npx
npm install --global --prefix /usr/local pnpm@10 @openai/codex@latest

if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash --user-group "$APP_USER"
fi
install -d -o "$APP_USER" -g "$APP_USER" "$APP_DIR"
runuser -u "$APP_USER" -- git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$APP_DIR"
if [[ "$MANAGED_PYTHON" == true ]]; then
  UV_ARCHIVE="uv-${UV_TARGET}.tar.gz"
  UV_ROOT="$APP_DIR/.installer"
  install -d -o "$APP_USER" -g "$APP_USER" "$UV_ROOT/bin"
  TEMP_DIR="$(mktemp -d)"
  trap 'rm -rf "$TEMP_DIR"' EXIT
  curl --fail --show-error --location --proto '=https' --tlsv1.2 \
    "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${UV_ARCHIVE}" \
    -o "$TEMP_DIR/$UV_ARCHIVE"
  curl --fail --show-error --location --proto '=https' --tlsv1.2 \
    "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${UV_ARCHIVE}.sha256" \
    -o "$TEMP_DIR/${UV_ARCHIVE}.sha256"
  (cd "$TEMP_DIR" && sha256sum --check --status "${UV_ARCHIVE}.sha256") \
    || fail "uv checksum verification failed"
  tar -xzf "$TEMP_DIR/$UV_ARCHIVE" -C "$TEMP_DIR"
  install -o "$APP_USER" -g "$APP_USER" -m 0755 \
    "$TEMP_DIR/uv-${UV_TARGET}/uv" "$UV_ROOT/bin/uv"
  runuser -u "$APP_USER" -- env \
    HOME="/home/$APP_USER" \
    UV_CACHE_DIR="$APP_DIR/.cache/uv" \
    UV_PYTHON_INSTALL_DIR="$APP_DIR/.python" \
    "$UV_ROOT/bin/uv" --directory "$APP_DIR" --no-config \
      python install --no-bin "$MANAGED_PYTHON_VERSION"
  runuser -u "$APP_USER" -- env \
    HOME="/home/$APP_USER" \
    UV_CACHE_DIR="$APP_DIR/.cache/uv" \
    UV_PYTHON_INSTALL_DIR="$APP_DIR/.python" \
    "$UV_ROOT/bin/uv" --directory "$APP_DIR" --no-config \
      venv --seed --managed-python \
      --python "$MANAGED_PYTHON_VERSION" "$APP_DIR/.venv"
fi
if [[ "$MANAGED_PYTHON" == false ]]; then
  runuser -u "$APP_USER" -- "$PYTHON_BIN" -m venv "$APP_DIR/.venv"
fi
runuser -u "$APP_USER" -- "$APP_DIR/.venv/bin/pip" install --disable-pip-version-check -r "$APP_DIR/requirements.lock"
runuser -u "$APP_USER" -- "$APP_DIR/.venv/bin/pip" install --disable-pip-version-check -e "$APP_DIR" --no-deps
runuser -u "$APP_USER" -- env HOME="/home/$APP_USER" PATH="/usr/local/bin:/usr/bin:/bin" \
  pnpm --dir "$APP_DIR/frontend" install --frozen-lockfile
runuser -u "$APP_USER" -- env HOME="/home/$APP_USER" PATH="/usr/local/bin:/usr/bin:/bin" \
  pnpm --dir "$APP_DIR/frontend" run build

PASSWORD_HASH="$(printf '%s\n' "$ADMIN_PASSWORD" | runuser -u "$APP_USER" -- "$APP_DIR/.venv/bin/python" -m candlepilot.auth --password-stdin)"
SESSION_SECRET="$(openssl rand -base64 48 | tr -d '\n')"
unset ADMIN_PASSWORD

install -o "$APP_USER" -g "$APP_USER" -m 0700 -d "$APP_DIR/data"
install -o "$APP_USER" -g "$APP_USER" -m 0600 /dev/null "$APP_DIR/.env"
printf '%s\n' \
  'CANDLEPILOT_HOST=127.0.0.1' \
  'CANDLEPILOT_PORT=8000' \
  'CANDLEPILOT_DATABASE_URL=sqlite+aiosqlite:///./candlepilot.db' \
  'CANDLEPILOT_DATA_DIR=./data' \
  'CANDLEPILOT_LLM_TIMEOUT=200' \
  'CANDLEPILOT_MAX_SNAPSHOT_AGE_SECONDS=220' \
  'CANDLEPILOT_CADENCES=15m' \
  'CANDLEPILOT_CANDIDATES_PER_CYCLE=5' \
  'CANDLEPILOT_PROVIDER_CHAIN=local' \
  'CANDLEPILOT_AUTH_ENABLED=true' \
  "CANDLEPILOT_AUTH_USERNAME=$ADMIN_USERNAME" \
  "CANDLEPILOT_AUTH_PASSWORD_HASH=$PASSWORD_HASH" \
  "CANDLEPILOT_AUTH_SESSION_SECRET=$SESSION_SECRET" \
  'CANDLEPILOT_AUTH_SESSION_TTL_SECONDS=43200' \
  'CANDLEPILOT_AUTH_COOKIE_SECURE=true' \
  "BINANCE_TESTNET_API_KEY=$BINANCE_KEY" \
  "BINANCE_TESTNET_API_SECRET=$BINANCE_SECRET" \
  >"$APP_DIR/.env"
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 0600 "$APP_DIR/.env"
unset BINANCE_KEY BINANCE_SECRET PASSWORD_HASH SESSION_SECRET

install -d -m 0700 /etc/candlepilot/tls
openssl req -x509 -nodes -newkey rsa:3072 -sha256 -days 825 \
  -keyout /etc/candlepilot/tls/server.key \
  -out /etc/candlepilot/tls/server.crt \
  -subj "/CN=$PUBLIC_IP" \
  -addext "subjectAltName=IP:$PUBLIC_IP"
chmod 0600 /etc/candlepilot/tls/server.key

cat >/etc/systemd/system/candlepilot.service <<EOF
[Unit]
Description=CandlePilot Binance Demo trading console
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment=HOME=/home/$APP_USER
Environment=PATH=/home/$APP_USER/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$APP_DIR/.venv/bin/candlepilot serve
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=$APP_DIR /home/$APP_USER

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/nginx/sites-available/candlepilot <<EOF
server {
    listen $PUBLIC_PORT ssl;
    listen [::]:$PUBLIC_PORT ssl;
    server_name _;

    ssl_certificate /etc/candlepilot/tls/server.crt;
    ssl_certificate_key /etc/candlepilot/tls/server.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_tickets off;

    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy no-referrer always;
    add_header Content-Security-Policy "default-src 'self'; connect-src 'self' wss:; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'" always;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host \$host:\$server_port;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        client_max_body_size 2m;
    }
}
EOF
ln -sfn /etc/nginx/sites-available/candlepilot /etc/nginx/sites-enabled/candlepilot
rm -f /etc/nginx/sites-enabled/default
nginx -t

systemctl daemon-reload
systemctl enable --now candlepilot
for _ in $(seq 1 30); do
  if curl --silent --fail http://127.0.0.1:8000/api/health/ready >/dev/null; then
    break
  fi
  sleep 1
done
curl --silent --fail http://127.0.0.1:8000/api/health/ready >/dev/null \
  || { journalctl -u candlepilot --no-pager -n 80; fail "backend did not become ready"; }
systemctl enable nginx
systemctl reload-or-restart nginx
for _ in $(seq 1 10); do
  if curl --silent --fail --insecure "https://127.0.0.1:$PUBLIC_PORT/api/health/ready" >/dev/null; then
    break
  fi
  sleep 1
done
curl --silent --fail --insecure "https://127.0.0.1:$PUBLIC_PORT/api/health/ready" >/dev/null \
  || { journalctl -u nginx --no-pager -n 80; fail "HTTPS reverse proxy did not become ready"; }

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
  ufw allow "$PUBLIC_PORT/tcp"
fi

echo
echo "CandlePilot installation completed."
echo "Platform: $PLATFORM_NAME"
echo "URL: https://$PUBLIC_IP:$PUBLIC_PORT"
echo "Username: $ADMIN_USERNAME"
echo "The certificate is self-signed; verify this SHA-256 fingerprint before accepting it:"
openssl x509 -in /etc/candlepilot/tls/server.crt -noout -fingerprint -sha256
echo "Service logs: journalctl -u candlepilot -f"
echo "Optional Codex login: sudo -iu $APP_USER codex login --device-auth"
