#!/bin/bash
###############################################################################
# ProjectCompress – Full Installation Script
#
# Installs:
#   - System packages (ffmpeg, wget, curl, git, python3-pip, etc.)
#   - Node.js 20.x  (needed for bgutil POT provider)
#   - yt-dlp (latest) + curl_cffi (for --impersonate chrome)
#   - bgutil-ytdlp-pot-provider (bypasses YouTube "Sign in to confirm" errors)
#   - Registers the POT provider as a systemd service
#
# Uses --break-system-packages for pip (Debian 12+ / Ubuntu 24.04+).
#
# Usage:
#   chmod +x install.sh
#   sudo ./install.sh
###############################################################################

set -euo pipefail

# ---------- helpers ----------
log()   { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()    { echo -e "\033[1;32m[ OK ]\033[0m  $*"; }
warn()  { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
err()   { echo -e "\033[1;31m[FAIL]\033[0m  $*"; }

# ---------- must run as root ----------
if [[ $EUID -ne 0 ]]; then
    err "Please run this script as root:  sudo $0"
    exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POT_DIR="/opt/bgutil-ytdlp-pot-provider"
POT_VERSION="2.0.0"

log "Project directory: $PROJECT_DIR"

# pip flags for Debian 12+ / Ubuntu 24.04+
PIP_FLAGS="--break-system-packages --ignore-installed"

###############################################################################
# 1. System packages
###############################################################################
log "Updating apt and installing base packages..."
apt-get update -y
apt-get install -y \
    wget curl git tar xz-utils \
    python3 python3-pip python3-dev \
    build-essential libssl-dev libffi-dev \
    ca-certificates gnupg lsb-release \
    software-properties-common

ok "Base packages installed."

###############################################################################
# 2. ffmpeg (static build – latest)
###############################################################################
if command -v ffmpeg &>/dev/null && command -v ffprobe &>/dev/null; then
    ok "ffmpeg already installed: $(which ffmpeg)"
else
    log "Installing ffmpeg (static build)..."
    cd /tmp
    wget -q --show-progress \
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
    tar -xf ffmpeg-master-latest-linux64-gpl.tar.xz
    mv ffmpeg-master-latest-linux64-gpl/ffmpeg  /usr/local/bin/
    mv ffmpeg-master-latest-linux64-gpl/ffplay  /usr/local/bin/
    mv ffmpeg-master-latest-linux64-gpl/ffprobe /usr/local/bin/
    rm -rf ffmpeg-master-latest-linux64-gpl ffmpeg-master-latest-linux64-gpl.tar.xz
    ok "ffmpeg installed to /usr/local/bin"
fi

export PATH="/usr/local/bin:$PATH"

###############################################################################
# 3. Node.js 20.x (required for the POT provider)
###############################################################################
if command -v node &>/dev/null; then
    NODE_VER=$(node --version | sed 's/v//;s/\..*//')
    if [[ "$NODE_VER" -ge 18 ]]; then
        ok "Node.js already installed: $(node --version)"
    else
        warn "Node.js version too old ($NODE_VER) – installing 20.x..."
        curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
        apt-get install -y nodejs
    fi
else
    log "Installing Node.js 20.x..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs
    ok "Node.js installed: $(node --version)"
fi

###############################################################################
# 4. Python packages – yt-dlp, curl_cffi, POT plugin, project deps
###############################################################################
log "Upgrading pip..."
python3 -m pip install $PIP_FLAGS --upgrade pip

log "Installing yt-dlp (latest), curl_cffi, and POT provider plugin..."
python3 -m pip install $PIP_FLAGS --upgrade \
    yt-dlp \
    curl_cffi \
    bgutil-ytdlp-pot-provider

if [[ -f "$PROJECT_DIR/requirements.txt" ]]; then
    log "Installing project requirements..."
    python3 -m pip install $PIP_FLAGS --upgrade -r "$PROJECT_DIR/requirements.txt"
fi
ok "Python packages installed."

###############################################################################
# 5. bgutil-ytdlp-pot-provider – server side
###############################################################################
if [[ -d "$POT_DIR" ]]; then
    warn "Existing POT provider found at $POT_DIR – updating..."
    cd "$POT_DIR"
    git fetch --all
    git checkout "$POT_VERSION"
    git pull --ff-only || true
else
    log "Cloning bgutil-ytdlp-pot-provider v$POT_VERSION..."
    git clone --single-branch --branch "$POT_VERSION" \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git "$POT_DIR"
fi

cd "$POT_DIR/server"
log "Installing Node dependencies for POT server..."
npm ci --silent

log "Building POT server (TypeScript)..."
npx tsc

ok "POT provider built."

###############################################################################
# 6. systemd service for the POT provider
###############################################################################
SERVICE_FILE="/etc/systemd/system/bgutil-provider.service"

log "Creating systemd service: $SERVICE_FILE"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=BgUtils POT Provider for yt-dlp
After=network.target

[Service]
Type=simple
WorkingDirectory=$POT_DIR/server
ExecStart=$(which node) build/main.js
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable bgutil-provider
systemctl restart bgutil-provider

sleep 2
if systemctl is-active --quiet bgutil-provider; then
    ok "POT provider service is running."
else
    err "POT provider service failed to start. Check: journalctl -u bgutil-provider -n 50"
    exit 1
fi

###############################################################################
# 7. Verify POT server responds on port 4416
###############################################################################
log "Testing POT provider on http://127.0.0.1:4416/ping ..."
for i in 1 2 3 4 5; do
    if curl -sf http://127.0.0.1:4416/ping >/dev/null; then
        ok "POT provider is responding."
        break
    fi
    sleep 2
    if [[ $i -eq 5 ]]; then
        warn "POT provider not responding after 10s – check: journalctl -u bgutil-provider -n 50"
    fi
done

###############################################################################
# 8. Create empty cookies.txt if not present
###############################################################################
COOKIES_FILE="$PROJECT_DIR/cookies.txt"
if [[ ! -f "$COOKIES_FILE" ]]; then
    cat > "$COOKIES_FILE" <<'EOF'
# Netscape HTTP Cookie File
# Export cookies from your browser (Chrome/Firefox extension "Get cookies.txt LOCALLY")
# Log in to YouTube first, then export for youtube.com and paste them here.
EOF
    warn "Created empty cookies.txt – please export your YouTube cookies into it."
else
    ok "cookies.txt already exists."
fi

###############################################################################
# 9. Ensure /usr/local/bin is globally in PATH
###############################################################################
PROFILE_FILE="/etc/profile.d/projectcompress.sh"
if [[ ! -f "$PROFILE_FILE" ]]; then
    echo 'export PATH="/usr/local/bin:$PATH"' > "$PROFILE_FILE"
    chmod +x "$PROFILE_FILE"
    ok "Added /usr/local/bin to /etc/profile.d/projectcompress.sh"
fi

###############################################################################
# 10. Final verification
###############################################################################
echo
log "Verifying installations..."
echo "  ffmpeg     : $(which ffmpeg || echo MISSING)"
echo "  ffprobe    : $(which ffprobe || echo MISSING)"
echo "  yt-dlp     : $(yt-dlp --version 2>/dev/null || echo MISSING)"
echo "  node       : $(node --version 2>/dev/null || echo MISSING)"
echo "  POT server : $(systemctl is-active bgutil-provider)"
echo

log "Checking yt-dlp plugins..."
yt-dlp --verbose --simulate "https://www.youtube.com/watch?v=PGKT7SbZcHU" 2>&1 | grep -i "pot\|plugin" | head -5 || true

echo
ok "==========================================="
ok "  Installation complete!"
ok "==========================================="
echo
echo "Next steps:"
echo "  1. Export YouTube cookies into:  $COOKIES_FILE"
echo "  2. Start your app:"
echo "       cd $PROJECT_DIR"
echo "       python3 app.py"
echo
echo "To check POT provider status:  systemctl status bgutil-provider"
echo "To view its logs:              journalctl -u bgutil-provider -f"
