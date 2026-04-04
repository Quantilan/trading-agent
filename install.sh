#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Quantilan Trading Agent — VPS Installer
#
#  Usage (fresh Ubuntu/Debian VPS):
#    curl -fsSL https://raw.githubusercontent.com/Quantilan/trading-agent/main/install.sh | bash
#
#  Or if you already have the repo:
#    bash install.sh
# ─────────────────────────────────────────────────────────────────────────────
set -e

REPO_URL="https://github.com/Quantilan/trading-agent.git"
INSTALL_DIR="$HOME/trading-agent"

_ok()  { echo "  ✅  $*"; }
_info(){ echo "  ℹ️   $*"; }
_warn(){ echo "  ⚠️   $*"; }
_err() { echo "  ❌  $*"; exit 1; }
_sep() { echo "────────────────────────────────────────────────────────"; }

echo
echo "  ╔══════════════════════════════════════════════════════╗"
echo "  ║       Quantilan Trading Agent — Installer            ║"
echo "  ╚══════════════════════════════════════════════════════╝"
echo
_sep

# ── 1. Docker ────────────────────────────────────────────────────────────────
if command -v docker &>/dev/null; then
    _ok "Docker already installed ($(docker --version | cut -d' ' -f3 | tr -d ','))"
else
    _info "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    # Add current user to docker group so we can run without sudo
    if id -nG "$USER" | grep -qw docker; then
        :
    else
        sudo usermod -aG docker "$USER"
        _warn "Added $USER to docker group. Log out and back in if docker commands fail."
    fi
    _ok "Docker installed"
fi
_sep

# ── 2. Clone or update repo ───────────────────────────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
    _info "Repo already exists at $INSTALL_DIR — pulling latest..."
    git -C "$INSTALL_DIR" pull --ff-only
    _ok "Repo updated"
else
    _info "Cloning repo to $INSTALL_DIR ..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    _ok "Repo cloned"
fi
cd "$INSTALL_DIR"
_sep

# ── 3. Setup (.env + state file) ─────────────────────────────────────────────
if [ ! -f .env ]; then
    cp .env.example .env
    _ok ".env created from .env.example"
else
    _info ".env already exists — keeping it"
fi
touch agent_state.json
mkdir -p logs
_sep

# ── 4. Build Docker image ─────────────────────────────────────────────────────
_info "Building Docker image (this takes ~2 min on first run)..."
docker compose build
_ok "Image built"
_sep

# ── 5. Done — print next steps ────────────────────────────────────────────────
VPS_IP=$(curl -s --max-time 3 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
SSH_USER=$(whoami)

echo
echo "  ╔══════════════════════════════════════════════════════╗"
echo "  ║                  Installation done!                  ║"
echo "  ╚══════════════════════════════════════════════════════╝"
echo
echo "  📂 Location: $INSTALL_DIR"
echo
echo "  Next steps:"
echo
echo "  1. Open an SSH tunnel from your local machine:"
echo
echo "       ssh -L 8080:localhost:8080 ${SSH_USER}@${VPS_IP}"
echo
echo "  2. Start the Setup GUI on the VPS:"
echo
echo "       cd $INSTALL_DIR && make gui"
echo
echo "  3. Open in your browser:  http://localhost:8080"
echo "     Configure exchange, Telegram, signal source — then Start Agent."
echo
echo "  4. Once configured, start the agent in background:"
echo
echo "       make start"
echo
echo "  Useful commands:"
echo "    make logs     — watch live output"
echo "    make stop     — stop agent"
echo "    make restart  — restart after .env change"
echo "    make status   — container status"
echo
_sep
