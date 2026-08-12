#!/bin/bash
# install ffmpeg
wget https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz
tar -xf ffmpeg-master-latest-linux64-gpl.tar.xz
cd ffmpeg-master-latest-linux64-gpl/bin
sudo mv ffmpeg ffplay ffprobe /usr/local/bin/
cd ..
rm -rf ffmpeg-master-latest-linux64-gpl*

# install pip packages
pip install -r requirements.txt --break-system-packages --ignore-installed
