#!/bin/bash
###############################################################################
# ProjectCompress – Full Installation Script (with Deno + EJS)
#
# Installs:
#   - System packages (ffmpeg, wget, curl, git, python3-pip, etc.)
#   - Node.js 20.x   (for bgutil POT provider)
#   - Deno           (JavaScript runtime for yt-dlp EJS)
#   - yt-dlp + yt-dlp-ejs + curl_cffi
#   - bgutil-ytdlp-pot-provider (YouTube PO Token provider)
#
# Usage:
#   chmod +x install.sh
#   sudo ./install.sh
###############################################################################

set -euo pipefail

log()   { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()    { echo -e "\033[1;32m[ OK ]\033[0m  $*"; }
warn()  { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
err()   { echo -e "\033[1;31m[FAIL]\033[0m  $*"; }

if [[ $EUID -ne 0 ]]; then
    err "Please run as root: sudo $0"
    exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POT_DIR="/opt/bgutil-ytdlp-pot-provider"
POT_VERSION="2.0.0"
PIP_FLAGS="--break-system-packages --ignore-installed"

log "Project directory: $PROJECT_DIR"

###############################################################################
# 1. System packages
###############################################################################
log "Installing base packages..."
apt-get update -y
apt-get install -y \
    wget curl git tar xz-utils unzip \
    python3 python3-pip python3-dev \
    build-essential libssl-dev libffi-dev \
    ca-certificates gnupg lsb-release software-properties-common

ok "Base packages installed."

###############################################################################
# 2. ffmpeg (static build)
###############################################################################
if command -v ffmpeg &>/dev/null && command -v ffprobe &>/dev/null; then
    ok "ffmpeg already installed: $(which ffmpeg)"
else
    log "Installing ffmpeg..."
    cd /tmp
    wget -q --show-progress \
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
    tar -xf ffmpeg-master-latest-linux64-gpl.tar.xz
    mv ffmpeg-master-latest-linux64-gpl/ffmpeg  /usr/local/bin/
    mv ffmpeg-master-latest-linux64-gpl/ffplay  /usr/local/bin/
    mv ffmpeg-master-latest-linux64-gpl/ffprobe /usr/local/bin/
    rm -rf ffmpeg-master-latest-linux64-gpl*
    ok "ffmpeg installed."
fi
export PATH="/usr/local/bin:$PATH"

###############################################################################
# 3. Node.js 20.x
###############################################################################
if command -v node &>/dev/null && [[ "$(node --version | sed 's/v//;s/\..*//')" -ge 18 ]]; then
    ok "Node.js already installed: $(node --version)"
else
    log "Installing Node.js 20.x..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs
    ok "Node.js installed: $(node --version)"
fi

###############################################################################
# 4. Deno – JavaScript runtime for yt-dlp EJS
###############################################################################
if command -v deno &>/dev/null; then
    ok "Deno already installed: $(deno --version | head -1)"
else
    log "Installing Deno..."
    curl -fsSL https://deno.land/install.sh | sh -s -- -y
    # Move to /usr/local/bin so it's globally available
    if [[ -f "$HOME/.deno/bin/deno" ]]; then
        mv "$HOME/.deno/bin/deno" /usr/local/bin/deno
    fi
    ok "Deno installed: $(deno --version | head -1)"
fi

###############################################################################
# 5. Python packages – yt-dlp + yt-dlp-ejs + curl_cffi + POT plugin
###############################################################################
log "Upgrading pip..."
python3 -m pip install $PIP_FLAGS --upgrade pip

log "Installing yt-dlp, yt-dlp-ejs, curl_cffi, POT provider plugin..."
python3 -m pip install $PIP_FLAGS --upgrade \
    yt-dlp \
    yt-dlp-ejs \
    curl_cffi \
    bgutil-ytdlp-pot-provider

if [[ -f "$PROJECT_DIR/requirements.txt" ]]; then
    log "Installing project requirements..."
    python3 -m pip install $PIP_FLAGS --upgrade -r "$PROJECT_DIR/requirements.txt"
fi
ok "Python packages installed."

###############################################################################
# 6. bgutil POT provider – server side
###############################################################################
if [[ -d "$POT_DIR" ]]; then
    warn "Existing POT provider found – updating..."
    cd "$POT_DIR"
    git fetch --all
    git checkout "$POT_VERSION" || true
    git pull --ff-only || true
else
    log "Cloning bgutil-ytdlp-pot-provider v$POT_VERSION..."
    git clone --single-branch --branch "$POT_VERSION" \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git "$POT_DIR"
fi

cd "$POT_DIR/server"
log "Building POT server..."
npm ci --silent
npx tsc
ok "POT provider built."

###############################################################################
# 7. systemd service for POT provider
###############################################################################
SERVICE_FILE="/etc/systemd/system/bgutil-provider.service"
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
    ok "POT provider running."
else
    err "POT provider failed. Check: journalctl -u bgutil-provider -n 50"
    exit 1
fi

###############################################################################
# 8. Verify POT on port 4416
###############################################################################
log "Testing POT provider..."
for i in 1 2 3 4 5; do
    if curl -sf http://127.0.0.1:4416/ping >/dev/null; then
        ok "POT provider responding."
        break
    fi
    sleep 2
done

###############################################################################
# 9. cookies.txt placeholder
###############################################################################
COOKIES_FILE="$PROJECT_DIR/cookies.txt"
if [[ ! -f "$COOKIES_FILE" ]]; then
    cat > "$COOKIES_FILE" <<'EOF'
# Netscape HTTP Cookie File
# Export YouTube cookies from your browser (extension "Get cookies.txt LOCALLY")
# and paste them here.
EOF
    warn "Created cookies.txt – please export your YouTube cookies into it."
fi

###############################################################################
# 10. Global PATH
###############################################################################
echo 'export PATH="/usr/local/bin:$PATH"' > /etc/profile.d/projectcompress.sh
chmod +x /etc/profile.d/projectcompress.sh

###############################################################################
# 11. Final verification
###############################################################################
echo
log "Verifying..."
echo "  ffmpeg     : $(which ffmpeg || echo MISSING)"
echo "  ffprobe    : $(which ffprobe || echo MISSING)"
echo "  yt-dlp     : $(yt-dlp --version 2>/dev/null || echo MISSING)"
echo "  deno       : $(deno --version 2>/dev/null | head -1 || echo MISSING)"
echo "  node       : $(node --version 2>/dev/null || echo MISSING)"
echo "  POT server : $(systemctl is-active bgutil-provider)"
echo

ok "==========================================="
ok "  Installation complete!"
ok "==========================================="
echo
echo "Next steps:"
echo "  1. Export YouTube cookies into: $COOKIES_FILE"
echo "  2. Restart your app: cd $PROJECT_DIR && python3 app.py"
echo
