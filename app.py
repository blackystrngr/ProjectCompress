import subprocess
import shutil
import sys
import os


def ensure_ffmpeg():
    """Install ffmpeg if missing (static build to /usr/local/bin)."""
    if shutil.which('ffmpeg') and shutil.which('ffprobe'):
        print("✅ ffmpeg already installed")
        return True
    print("⚠️ ffmpeg not found – installing...")
    try:
        url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
        subprocess.run(['wget', '-q', '--show-progress', url], check=True)
        subprocess.run(['tar', '-xf', 'ffmpeg-master-latest-linux64-gpl.tar.xz'], check=True)
        subprocess.run(['sudo', 'mv', 'ffmpeg-master-latest-linux64-gpl/bin/ffmpeg', '/usr/local/bin/'], check=True)
        subprocess.run(['sudo', 'mv', 'ffmpeg-master-latest-linux64-gpl/bin/ffplay', '/usr/local/bin/'], check=True)
        subprocess.run(['sudo', 'mv', 'ffmpeg-master-latest-linux64-gpl/bin/ffprobe', '/usr/local/bin/'], check=True)
        subprocess.run(['rm', '-rf', 'ffmpeg-master-latest-linux64-gpl', 'ffmpeg-master-latest-linux64-gpl.tar.xz'], check=True)
        print("✅ ffmpeg installed")
        return True
    except Exception as e:
        print(f"❌ ffmpeg install failed: {e}\nPlease install manually: sudo apt install ffmpeg")
        return False

def ensure_pip_packages():
    """Install all packages from requirements.txt with --ignore-installed."""
    req_file = 'requirements.txt'
    if not os.path.exists(req_file):
        print("⚠️ requirements.txt not found – skipping pip install")
        return
    print("📦 Installing/upgrading Python packages (--ignore-installed)...")
    try:
        # Use --ignore-installed to override system packages
        subprocess.run(
            [sys.executable, '-m', 'pip', 'install', '--ignore-installed','--break-system-packages', '--upgrade', '-r', req_file],
            check=True
        )
        print("✅ All Python packages installed/upgraded")
    except subprocess.CalledProcessError as e:
        print(f"❌ Pip install failed: {e}")
        print("Try running manually: pip3 install --ignore-installed -r requirements.txt")
