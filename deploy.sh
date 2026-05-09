#!/bin/bash
# Bet Tracking API — VPS Deployment Script
set -e

echo "=========================================="
echo "Bet Tracking API — VPS Deployment"
echo "=========================================="

SERVICE_NAME="${SERVICE_NAME:-bet-tracking-api}"
SERVICE_DIR="${SERVICE_DIR:-/home/ubuntu/services/bet-tracking-api}"
VENV_DIR="$SERVICE_DIR/venv"
REPO_URL="${REPO_URL:-https://github.com/joypciu/bet-tracking-api.git}"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
API_PORT="${API_PORT:-5002}"
LOCK_FILE="/tmp/${SERVICE_NAME}.deploy.lock"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
print_success() { echo -e "${GREEN}✓ $1${NC}"; }
print_error()   { echo -e "${RED}✗ $1${NC}"; }
print_info()    { echo -e "${YELLOW}→ $1${NC}"; }

if [ -f "$LOCK_FILE" ]; then
    print_error "Another deployment is running ($LOCK_FILE exists)"
    exit 1
fi
trap 'rm -f "$LOCK_FILE"' EXIT
touch "$LOCK_FILE"

if [ "$USER" != "ubuntu" ]; then
    print_error "Must run as ubuntu user"
    exit 1
fi

print_info "Service: $SERVICE_NAME  Dir: $SERVICE_DIR  Port: $API_PORT"

# ── Clone or update repo ──────────────────────────────────────────────────────
if [ ! -d "$SERVICE_DIR" ]; then
    mkdir -p "$SERVICE_DIR"
fi
cd "$SERVICE_DIR"

if [ ! -d ".git" ]; then
    print_info "First deploy — cloning repository..."
    git clone "$REPO_URL" .
    print_success "Repository cloned"
else
    print_info "Updating repository..."
    git fetch origin "$DEPLOY_BRANCH"
    git checkout -f "$DEPLOY_BRANCH"
    git reset --hard "origin/$DEPLOY_BRANCH"
    print_success "Repository updated"
fi

# ── Migrate existing bets.db from cache-api if this is first deploy ──────────
CACHE_API_DB="/home/ubuntu/services/cache-api/bets.db"
LOCAL_DB="$SERVICE_DIR/bets.db"
if [ ! -f "$LOCAL_DB" ] && [ -f "$CACHE_API_DB" ]; then
    print_info "First deploy: copying existing bets.db from cache-api..."
    cp "$CACHE_API_DB" "$LOCAL_DB"
    print_success "bets.db migrated (${CACHE_API_DB} → ${LOCAL_DB})"
fi

# ── Python virtualenv + deps ──────────────────────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    print_info "Creating Python virtual environment..."
    python3 -m venv venv
    print_success "venv created"
fi

source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt --upgrade -q
print_success "Dependencies installed"

# ── .env ──────────────────────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        print_info ".env created from template — edit BET_API_TOKEN before use"
    else
        print_error ".env.example not found"
    fi
else
    print_success ".env already exists"
fi

# ── Systemd service unit ──────────────────────────────────────────────────────
cat > "/tmp/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Bet Tracking API
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=${SERVICE_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${VENV_DIR}/bin/python -m uvicorn main:app --host 0.0.0.0 --port ${API_PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo cp "/tmp/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo systemctl daemon-reload
print_success "Systemd unit installed"

# ── Port safety check ─────────────────────────────────────────────────────────
sudo systemctl stop "$SERVICE_NAME" 2>/dev/null || true
sleep 2

port_line="$(sudo ss -ltnp "sport = :${API_PORT}" 2>/dev/null | awk 'NR>1 && /LISTEN/{print; exit}')"
if [ -n "$port_line" ]; then
    port_pid="$(echo "$port_line" | sed -n 's/.*pid=\([0-9]\+\).*/\1/p')"
    owner_unit=""
    if [ -n "$port_pid" ] && [ -r "/proc/${port_pid}/cgroup" ]; then
        owner_unit="$(grep -oE '[^/]+\.service' "/proc/${port_pid}/cgroup" | head -n 1 || true)"
    fi
    if [ "$owner_unit" = "${SERVICE_NAME}.service" ]; then
        sleep 2
    else
        print_error "Port ${API_PORT} is occupied by ${owner_unit:-unknown process}. Aborting."
        exit 1
    fi
fi

# ── Start service ─────────────────────────────────────────────────────────────
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"
sleep 3

if sudo systemctl is-active --quiet "$SERVICE_NAME"; then
    print_success "$SERVICE_NAME is running on port $API_PORT"
else
    print_error "$SERVICE_NAME failed to start"
    sudo journalctl -u "$SERVICE_NAME" -n 30 --no-pager
    exit 1
fi

# ── Health check ──────────────────────────────────────────────────────────────
if curl -fsS "http://localhost:${API_PORT}/" > /dev/null; then
    print_success "API responding on port ${API_PORT}"
else
    print_error "API not responding on port ${API_PORT}"
fi

echo ""
echo "=========================================="
print_success "Deployment complete!"
echo "  Logs:    sudo journalctl -u $SERVICE_NAME -f"
echo "  Status:  sudo systemctl status $SERVICE_NAME"
echo "  Health:  curl http://localhost:${API_PORT}/"
echo "=========================================="
