#!/bin/bash
###############################################################################
# ProjectCompress — Full Installation Script (v2)
#
# Installs:
#   • System packages (ffmpeg, curl, git, tesseract, poppler, etc.)
#   • Node.js 20.x     (for bgutil POT provider)
#   • Deno             (JS runtime for yt-dlp EJS)
#   • yt-dlp + yt-dlp-ejs + curl_cffi
#   • bgutil-ytdlp-pot-provider (as systemd service)
#   • Playwright + Chromium (for URL cracker)
#   • All Python dependencies from requirements.txt
#   • Basic Auth password file (/root/.password)
#   • Placeholder config files (cookies.txt, telegram_creds.json)
#   • Optional: python3-libtorrent for torrent support
#
# Tested on: Ubuntu 22.04 / 24.04, Debian 12
#
# Usage:
#   chmod +x setup.sh
#   sudo ./setup.sh
###############################################################################

set -euo pipefail

# ============================================================
# Colors + helpers
# ============================================================
C_RESET='\033[0m'
C_BLUE='\033[1;34m'
C_GREEN='\033[1;32m'
C_YELLOW='\033[1;33m'
C_RED='\033[1;31m'
C_CYAN='\033[1;36m'

log()   { echo -e "${C_BLUE}[INFO]${C_RESET}  $*"; }
ok()    { echo -e "${C_GREEN}[ OK ]${C_RESET}  $*"; }
warn()  { echo -e "${C_YELLOW}[WARN]${C_RESET}  $*"; }
err()   { echo -e "${C_RED}[FAIL]${C_RESET}  $*"; }
step()  { echo; echo -e "${C_CYAN}▶ $*${C_RESET}"; }

# ============================================================
# Sanity checks
# ============================================================
if [[ $EUID -ne 0 ]]; then
    err "Please run as root: sudo $0"
    exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POT_DIR="/opt/bgutil-ytdlp-pot-provider"
POT_VERSION="2.0.0"
PIP_FLAGS="--break-system-packages --ignore-installed"

if [[ ! -f "$PROJECT_DIR/app.py" ]]; then
    err "app.py not found in $PROJECT_DIR — is this the ProjectCompress directory?"
    exit 1
fi

if [[ ! -f "$PROJECT_DIR/requirements.txt" ]]; then
    err "requirements.txt not found — cannot install Python dependencies"
    exit 1
fi

log "Project directory: $PROJECT_DIR"

# Detect OS
if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    OS_NAME="${NAME:-Unknown}"
    OS_VERSION="${VERSION_ID:-}"
    log "Detected: $OS_NAME $OS_VERSION"
else
    warn "Could not detect OS from /etc/os-release — assuming Debian-based"
fi

export DEBIAN_FRONTEND=noninteractive

###############################################################################
# 1. System packages
###############################################################################
step "1/10  Installing base system packages"

apt-get update -y

apt-get install -y \
    wget curl git tar xz-utils unzip \
    python3 python3-pip python3-dev python3-venv \
    build-essential libssl-dev libffi-dev \
    ca-certificates gnupg lsb-release software-properties-common \
    tesseract-ocr tesseract-ocr-eng poppler-utils \
    ffmpeg || true

# ffmpeg may not be available on all distros via apt — we'll install static build if missing
ok "Base packages installed"

###############################################################################
# 2. ffmpeg (prefer static build from BtbN — more up to date than distro)
###############################################################################
step "2/10  Installing ffmpeg"

if command -v ffmpeg &>/dev/null && command -v ffprobe &>/dev/null; then
    FFMPEG_VER="$(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}' || echo unknown)"
    ok "ffmpeg already installed ($FFMPEG_VER) — skipping static install"
else
    log "Downloading static ffmpeg build from BtbN…"
    cd /tmp
    rm -rf ffmpeg-master-latest-linux64-gpl*

    if wget -q --show-progress \
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"; then
        tar -xf ffmpeg-master-latest-linux64-gpl.tar.xz
        install -m 755 ffmpeg-master-latest-linux64-gpl/ffmpeg  /usr/local/bin/
        install -m 755 ffmpeg-master-latest-linux64-gpl/ffplay  /usr/local/bin/
        install -m 755 ffmpeg-master-latest-linux64-gpl/ffprobe /usr/local/bin/
        rm -rf ffmpeg-master-latest-linux64-gpl*
        ok "Static ffmpeg installed"
    else
        err "Could not download static ffmpeg — install manually with: sudo apt install ffmpeg"
        exit 1
    fi
fi

# Ensure /usr/local/bin is in PATH for this session
export PATH="/usr/local/bin:$PATH"

###############################################################################
# 3. Node.js 20.x (for POT provider)
###############################################################################
step "3/10  Installing Node.js 20.x"

NODE_OK=false
if command -v node &>/dev/null; then
    NODE_MAJOR="$(node --version | sed 's/v//;s/\..*//')"
    if [[ "$NODE_MAJOR" -ge 18 ]] 2>/dev/null; then
        ok "Node.js already installed: $(node --version)"
        NODE_OK=true
    fi
fi

if [[ "$NODE_OK" == "false" ]]; then
    log "Installing Node.js 20.x from NodeSource…"
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs
    ok "Node.js installed: $(node --version)"
fi

###############################################################################
# 4. Deno (JS runtime for yt-dlp EJS challenges)
###############################################################################
step "4/10  Installing Deno"

if command -v deno &>/dev/null; then
    ok "Deno already installed: $(deno --version | head -1)"
else
    log "Installing Deno…"
    curl -fsSL https://deno.land/install.sh | sh -s -- -y
    if [[ -f "$HOME/.deno/bin/deno" ]]; then
        mv "$HOME/.deno/bin/deno" /usr/local/bin/deno
        chmod +x /usr/local/bin/deno
    fi
    ok "Deno installed: $(deno --version 2>/dev/null | head -1 || echo 'version unknown')"
fi

###############################################################################
# 5. Python packages
###############################################################################
step "5/10  Installing Python packages"

log "Upgrading pip…"
python3 -m pip install $PIP_FLAGS --upgrade pip

log "Installing yt-dlp, yt-dlp-ejs, curl_cffi, POT provider plugin…"
python3 -m pip install $PIP_FLAGS --upgrade \
    yt-dlp \
    yt-dlp-ejs \
    curl_cffi \
    bgutil-ytdlp-pot-provider

log "Installing project requirements from requirements.txt…"
python3 -m pip install $PIP_FLAGS --upgrade -r "$PROJECT_DIR/requirements.txt"

# Optional: torrent support
if apt-cache show python3-libtorrent &>/dev/null; then
    log "Installing python3-libtorrent (for torrent downloads)…"
    apt-get install -y python3-libtorrent || warn "python3-libtorrent failed — torrent features disabled"
else
    warn "python3-libtorrent not available — torrent features disabled (install manually if needed)"
fi

ok "Python packages installed"

###############################################################################
# 6. Playwright (for URL cracker / network sniffer)
###############################################################################
step "6/10  Installing Playwright + Chromium"

log "Downloading Chromium for Playwright (this can take a few minutes)…"
if python3 -m playwright install chromium 2>&1 | tail -5; then
    ok "Playwright Chromium installed"
else
    warn "Playwright install failed — URL cracker's network sniffing will be unavailable"
    warn "Fix later with: playwright install chromium"
fi

# Playwright system deps (needed on minimal VPS images)
if command -v playwright &>/dev/null; then
    log "Installing Playwright system dependencies…"
    playwright install-deps chromium 2>&1 | tail -3 || warn "Could not install Playwright system deps"
fi

###############################################################################
# 7. bgutil POT provider
###############################################################################
step "7/10  Installing bgutil-ytdlp-pot-provider"

if [[ -d "$POT_DIR" ]]; then
    log "Existing POT provider found — updating…"
    cd "$POT_DIR"
    git fetch --all --tags
    git checkout "$POT_VERSION" 2>/dev/null || true
    git pull --ff-only 2>/dev/null || true
else
    log "Cloning bgutil-ytdlp-pot-provider v$POT_VERSION…"
    git clone --single-branch --branch "$POT_VERSION" \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git "$POT_DIR"
fi

cd "$POT_DIR/server"
log "Building POT server…"
npm ci --silent 2>&1 | tail -3 || npm install --silent
npx tsc
ok "POT provider built"

###############################################################################
# 8. systemd service for POT provider
###############################################################################
step "8/10  Registering POT provider systemd service"

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
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable bgutil-provider
systemctl restart bgutil-provider
sleep 2

if systemctl is-active --quiet bgutil-provider; then
    ok "POT provider running on port 4416"
else
    err "POT provider failed to start"
    err "Diagnose with: journalctl -u bgutil-provider -n 50 --no-pager"
    exit 1
fi

# Verify port is responding
POT_OK=false
for i in 1 2 3 4 5; do
    if curl -sf http://127.0.0.1:4416/ping >/dev/null 2>&1; then
        POT_OK=true
        break
    fi
    sleep 2
done

if [[ "$POT_OK" == "true" ]]; then
    ok "POT provider responding to /ping"
else
    warn "POT provider not responding to /ping yet — may take longer on first start"
fi

###############################################################################
# 9. Auth, config placeholders, directories
###############################################################################
step "9/10  Setting up auth and config placeholders"

# ---- Auth password file ----
PASSWORD_FILE="/root/.password"
if [[ ! -f "$PASSWORD_FILE" ]]; then
    echo
    echo -e "${C_CYAN}── Basic Auth Setup ──${C_RESET}"
    echo "ProjectCompress requires HTTP Basic Auth on every route."
    echo "The password will be stored at: $PASSWORD_FILE"
    echo
    read -r -s -p "Enter a password (leave blank to generate a random one): " USER_PW
    echo
    if [[ -z "$USER_PW" ]]; then
        USER_PW="$(openssl rand -base64 24 2>/dev/null || head -c 24 /dev/urandom | base64 | tr -d '\n')"
        echo -e "${C_YELLOW}Generated password:${C_RESET} $USER_PW"
        echo -e "${C_YELLOW}Save this somewhere safe — you'll need it to log in.${C_RESET}"
    fi
    printf '%s' "$USER_PW" > "$PASSWORD_FILE"
    chmod 600 "$PASSWORD_FILE"
    chown root:root "$PASSWORD_FILE"
    ok "Password saved to $PASSWORD_FILE"
    echo
    warn "Username can be anything (Basic Auth ignores it) — the password is what matters"
else
    ok "Password file already exists: $PASSWORD_FILE"
fi

# ---- cookies.txt placeholder ----
COOKIES_FILE="$PROJECT_DIR/cookies.txt"
if [[ ! -f "$COOKIES_FILE" ]]; then
    cat > "$COOKIES_FILE" <<'EOF'
# Netscape HTTP Cookie File
# Export YouTube cookies from your browser using the extension
# "Get cookies.txt LOCALLY" (Chrome) or "cookies.txt" (Firefox),
# then paste the content here (replacing this comment).
#
# The server will read this file when downloading from YouTube and other
# sites that require login / age verification.
EOF
    chmod 644 "$COOKIES_FILE"
    ok "Created placeholder cookies.txt"
else
    ok "cookies.txt already exists"
fi

# ---- telegram_creds.json placeholder ----
TELEGRAM_CREDS="$PROJECT_DIR/telegram_creds.json"
if [[ ! -f "$TELEGRAM_CREDS" ]]; then
    cat > "$TELEGRAM_CREDS" <<'EOF'
{
  "api_id": 0,
  "api_hash": "REPLACE_WITH_YOUR_API_HASH"
}
EOF
    chmod 600 "$TELEGRAM_CREDS"
    warn "Created placeholder telegram_creds.json — edit it with real credentials"
    warn "  Get them from: https://my.telegram.org/apps"
else
    ok "telegram_creds.json already exists"
fi

# ---- ensure project directories ----
mkdir -p "$PROJECT_DIR"/downloads
mkdir -p "$PROJECT_DIR"/downloads/.thumbnails
mkdir -p "$PROJECT_DIR"/tasks
mkdir -p "$PROJECT_DIR"/proxy_cache
mkdir -p "$PROJECT_DIR"/workflows
ok "Runtime directories ensured"

# ---- .env template ----
ENV_FILE="$PROJECT_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    cat > "$ENV_FILE" <<'EOF'
# ─────────────────────────────────────────────────────────────
# ProjectCompress — environment variables
#
# All values here are OPTIONAL. Settings saved via the in-app
# Settings panel (settings.json) take priority over these.
# ─────────────────────────────────────────────────────────────

# Flask session secret — change to a long random string
SECRET_KEY=change-me-to-a-long-random-string

# HTTP proxy (optional)
# PROXY_URL=http://user:pass@proxy.example.com:8080

# Google Drive folder ID (optional — only for Colab integration)
# DRIVE_FOLDER_ID=1abc...xyz

# Token for the browser cookie-uploader extension
# Generate with: openssl rand -hex 32
COOKIE_UPLOAD_TOKEN=change-me-to-a-long-random-hex

# Telegram logger — fallback if not set in settings.json
# TG_LOG_ENABLED=false
# TG_LOG_CHANNEL=
# TG_LOG_BOT_TOKEN=
# TG_LOG_LEVEL=WARNING

# Custom auth password location
# (defaults to /root/.password if unset)
# AUTH_PASSWORD_FILE=/etc/projectcompress.password
EOF
    chmod 600 "$ENV_FILE"
    ok "Created .env template"
else
    ok ".env already exists"
fi

# ---- PATH persistence ----
echo 'export PATH="/usr/local/bin:$PATH"' > /etc/profile.d/projectcompress.sh
chmod +x /etc/profile.d/projectcompress.sh
ok "Global PATH set for /usr/local/bin"

###############################################################################
# 10. Final verification
###############################################################################
step "10/10  Verifying installation"

echo
echo -e "${C_CYAN}── Installed Components ──${C_RESET}"

check_cmd() {
    local name="$1"
    local cmd="$2"
    local out
    out=$(eval "$cmd" 2>/dev/null | head -1) || out=""
    if [[ -n "$out" ]]; then
        printf "  ${C_GREEN}✓${C_RESET} %-14s %s\n" "$name" "$out"
    else
        printf "  ${C_RED}✗${C_RESET} %-14s ${C_RED}MISSING${C_RESET}\n" "$name"
    fi
}

check_cmd "ffmpeg"     "which ffmpeg && ffmpeg -version"
check_cmd "ffprobe"    "which ffprobe"
check_cmd "yt-dlp"     "yt-dlp --version"
check_cmd "deno"       "deno --version"
check_cmd "node"       "node --version"
check_cmd "npm"        "npm --version"
check_cmd "python3"    "python3 --version"
check_cmd "playwright" "python3 -m playwright --version"
check_cmd "tesseract"  "tesseract --version"
check_cmd "pdftoppm"   "pdftoppm -v"

echo
echo -e "${C_CYAN}── POT Provider ──${C_RESET}"
POT_STATE=$(systemctl is-active bgutil-provider 2>/dev/null || echo unknown)
if [[ "$POT_STATE" == "active" ]]; then
    printf "  ${C_GREEN}✓${C_RESET} %-14s running\n" "bgutil-provider"
else
    printf "  ${C_RED}✗${C_RESET} %-14s %s\n" "bgutil-provider" "$POT_STATE"
fi

echo
echo -e "${C_CYAN}── Project Files ──${C_RESET}"
for f in app.py requirements.txt cookies.txt telegram_creds.json .env; do
    if [[ -f "$PROJECT_DIR/$f" ]]; then
        printf "  ${C_GREEN}✓${C_RESET} %s\n" "$f"
    else
        printf "  ${C_RED}✗${C_RESET} %s\n" "$f"
    fi
done

if [[ -f "/root/.password" ]]; then
    printf "  ${C_GREEN}✓${C_RESET} /root/.password (auth)\n"
else
    printf "  ${C_RED}✗${C_RESET} /root/.password\n"
fi

###############################################################################
# Done
###############################################################################
echo
echo -e "${C_GREEN}═══════════════════════════════════════════════════${C_RESET}"
echo -e "${C_GREEN}  ✅ Installation complete!${C_RESET}"
echo -e "${C_GREEN}═══════════════════════════════════════════════════${C_RESET}"
echo
echo -e "${C_CYAN}Next steps:${C_RESET}"
echo
echo "  1. Edit $PROJECT_DIR/.env if you want to change defaults:"
echo "       nano $PROJECT_DIR/.env"
echo
echo "  2. (Optional) Add YouTube cookies for age-restricted content:"
echo "       nano $PROJECT_DIR/cookies.txt"
echo "     Export from browser extension 'Get cookies.txt LOCALLY'"
echo
echo "  3. (Optional) Add Telegram API credentials:"
echo "       nano $PROJECT_DIR/telegram_creds.json"
echo "     Get them from https://my.telegram.org/apps"
echo
echo "  4. (Optional) Add Google Drive token for Drive features:"
echo "       python3 -c \"from google_auth_oauthlib.flow import InstalledAppFlow;\""
echo "       (see README.md for the full command)"
echo
echo -e "  ${C_YELLOW}5. Run the app:${C_RESET}"
echo "       cd $PROJECT_DIR && python3 app.py"
echo
echo -e "     Or install as systemd service (recommended for production):"
echo "       See README.md → 'Running' → 'Systemd'"
echo
echo -e "  ${C_YELLOW}6. Open in browser:${C_RESET}"
echo "       http://YOUR_SERVER_IP:5000"
echo -e "       (you'll be prompted for Basic Auth — password is in /root/.password)"
echo
echo -e "${C_CYAN}Diagnostic commands:${C_RESET}"
echo "  journalctl -u bgutil-provider -n 30 --no-pager    # POT provider logs"
echo "  curl http://localhost:5000/settings/debug          # Settings health (needs auth)"
echo "  curl http://localhost:5000/telegram/log/status     # Telegram logger state"
echo
echo -e "${C_GREEN}Installation log saved to: /tmp/projectcompress-install.log${C_RESET}"
echo
