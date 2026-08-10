import os
import re
import uuid
import random
import threading
import time
import subprocess
import logging
import shutil
import zipfile
from flask import request, jsonify
from tasks import save_task, load_task
from config import UPLOAD_FOLDER

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def check_ffmpeg():
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
        return True
    except:
        logger.warning("ffmpeg not found")
        return False

def get_video_duration(video_path):
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
           '-of', 'default=noprint_wrappers=1:nokey=1', video_path]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise Exception(f"ffprobe failed: {result.stderr}")
    return float(result.stdout.strip())

def has_video_stream(file_path):
    cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=codec_type',
           '-of', 'default=noprint_wrappers=1:nokey=1', file_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == 'video'

def extract_clip_with_fallback(video_path, start_time, clip_duration, output_path, task_id=None, idx=None, total=None):
    # try stream copy first
    cmd_copy = ['ffmpeg', '-ss', str(start_time), '-i', video_path,
                '-t', str(clip_duration),
                '-map', '0:v', '-map', '0:a?',
                '-c', 'copy', '-avoid_negative_ts', 'make_zero', '-copyts',
                '-y', output_path]
    try:
        result = subprocess.run(cmd_copy, capture_output=True, text=True, check=False)
        if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            if has_video_stream(output_path):
                if task_id and idx and total:
                    task = load_task(task_id)
                    if task:
                        task['current_clip'] = idx
                        task['progress'] = 30 + int(50 * idx / total)
                        save_task(task_id, task)
                return
    except:
        pass
    # fallback to re-encode
    cmd_reencode = ['ffmpeg', '-ss', str(start_time), '-i', video_path,
                    '-t', str(clip_duration),
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28',
                    '-c:a', 'aac', '-b:a', '128k',
                    '-movflags', '+faststart',
                    '-avoid_negative_ts', 'make_zero',
                    '-y', output_path]
    result = subprocess.run(cmd_reencode, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise Exception(f"Re-encode failed: {result.stderr}")
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise Exception("Output missing")
    if not has_video_stream(output_path):
        raise Exception("No video stream")
    if task_id and idx and total:
        task = load_task(task_id)
        if task:
            task['current_clip'] = idx
            task['progress'] = 30 + int(50 * idx / total)
            save_task(task_id, task)

def merge_clips(clip_files, output_path, task_id):
    if not clip_files:
        raise Exception("No clips to merge")
    task = load_task(task_id)
    task['status'] = 'merging'
    task['progress'] = 85
    save_task(task_id, task)
    concat_file = os.path.join(os.path.dirname(output_path), f"{task_id}_concat.txt")
    with open(concat_file, 'w') as f:
        for clip in clip_files:
            f.write(f"file '{os.path.abspath(clip)}'\n")
    cmd = ['ffmpeg', '-f', 'concat', '-safe', '0', '-i', concat_file,
           '-c', 'copy', '-y', output_path]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if os.path.exists(concat_file):
        os.remove(concat_file)
    if result.returncode != 0:
        raise Exception(f"Merge error: {result.stderr}")
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise Exception("Merge output missing")
    for clip in clip_files:
        if os.path.exists(clip):
            os.remove(clip)

# ------------------------------------------------------------
# Random Clips
# ------------------------------------------------------------
def process_random_clips(video_path, segment_duration, clip_duration, output_path, task_id):
    total_duration = get_video_duration(video_path)
    task = load_task(task_id)
    task['total_duration'] = total_duration
    task['progress'] = 5
    save_task(task_id, task)

    segments = []
    current = 0
    while current < total_duration:
        seg_end = min(current + segment_duration, total_duration)
        if seg_end - current >= clip_duration:
            max_start = seg_end - clip_duration
            clip_start = random.uniform(current, max_start)
            segments.append((clip_start, clip_start + clip_duration))
        current += segment_duration

    total_clips = len(segments)
    if total_clips == 0:
        raise Exception("No valid segments found")
    task = load_task(task_id)
    task['total_clips'] = total_clips
    task['progress'] = 10
    save_task(task_id, task)

    temp_dir = os.path.join(UPLOAD_FOLDER, f"clips_{task_id}")
    os.makedirs(temp_dir, exist_ok=True)

    clip_files = []
    try:
        for idx, (start, end) in enumerate(segments, 1):
            if load_task(task_id).get('cancelled', False):
                raise Exception("Cancelled")
            clip_path = os.path.join(temp_dir, f"clip_{idx:03d}.mp4")
            extract_clip_with_fallback(video_path, start, clip_duration, clip_path, task_id, idx, total_clips)
            clip_files.append(clip_path)

        merge_clips(clip_files, output_path, task_id)
    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)

    task = load_task(task_id)
    task['status'] = 'done'
    task['progress'] = 100
    task['output_file'] = os.path.basename(output_path)
    save_task(task_id, task)

# ------------------------------------------------------------
# AI Summarizer
# ------------------------------------------------------------
def process_summarizer(video_path, target_duration_sec, clip_duration_sec, output_path, task_id):
    total_duration = get_video_duration(video_path)
    num_clips = max(1, int(target_duration_sec / clip_duration_sec))
    max_clips = int(total_duration / clip_duration_sec)
    if max_clips == 0:
        raise Exception(f"Video too short, need at least {clip_duration_sec}s")
    num_clips = min(num_clips, max_clips)
    step = total_duration / num_clips
    starts = [i * step for i in range(num_clips)]
    starts = [min(s, total_duration - clip_duration_sec) for s in starts]
    starts = sorted(set(starts))
    task = load_task(task_id)
    task['total_clips'] = len(starts)
    save_task(task_id, task)

    temp_dir = os.path.join(UPLOAD_FOLDER, f"clips_{task_id}")
    os.makedirs(temp_dir, exist_ok=True)

    clip_files = []
    try:
        for idx, start in enumerate(starts, 1):
            if load_task(task_id).get('cancelled', False):
                raise Exception("Cancelled")
            clip_path = os.path.join(temp_dir, f"clip_{idx:03d}.mp4")
            extract_clip_with_fallback(video_path, start, clip_duration_sec, clip_path, task_id, idx, len(starts))
            clip_files.append(clip_path)
        merge_clips(clip_files, output_path, task_id)
    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)

    task = load_task(task_id)
    task['status'] = 'done'
    task['progress'] = 100
    task['output_file'] = os.path.basename(output_path)
    save_task(task_id, task)

# ------------------------------------------------------------
# Frame Extractor (NEW)
# ------------------------------------------------------------
def extract_frames_task(video_path, interval_sec, task_id, output_format='jpg'):
    task = load_task(task_id)
    if not task:
        return

    temp_dir = os.path.join(UPLOAD_FOLDER, f"frames_{task_id}")
    os.makedirs(temp_dir, exist_ok=True)

    try:
        duration = get_video_duration(video_path)
        total_frames = max(1, int(duration // interval_sec))
        task['total_frames'] = total_frames
        task['progress'] = 0
        save_task(task_id, task)

        pattern = os.path.join(temp_dir, f"frame_%04d.{output_format}")
        cmd = [
            'ffmpeg', '-i', video_path,
            '-vf', f"fps=1/{interval_sec}",
            '-q:v', '2',
            '-y', pattern
        ]
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        while True:
            line = process.stderr.readline()
            if not line and process.poll() is not None:
                break
            if 'frame=' in line:
                match = re.search(r'frame=\s*(\d+)', line)
                if match:
                    frame_num = int(match.group(1))
                    pct = min(100, int(100 * frame_num / total_frames))
                    task = load_task(task_id)
                    if task:
                        task['progress'] = pct
                        task['current_frame'] = frame_num
                        save_task(task_id, task)
        process.wait()
        if process.returncode != 0:
            raise Exception(f"ffmpeg failed: {process.stderr.read()}")

        frame_files = sorted([f for f in os.listdir(temp_dir) if f.endswith(f'.{output_format}')])
        if not frame_files:
            raise Exception("No frames extracted")

        zip_filename = f"frames_{os.path.splitext(os.path.basename(video_path))[0]}.zip"
        zip_path = os.path.join(UPLOAD_FOLDER, zip_filename)
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for fname in frame_files:
                fpath = os.path.join(temp_dir, fname)
                zf.write(fpath, arcname=fname)

        shutil.rmtree(temp_dir, ignore_errors=True)

        task = load_task(task_id)
        task['status'] = 'done'
        task['progress'] = 100
        task['output_file'] = zip_filename
        task['total_frames'] = len(frame_files)
        save_task(task_id, task)

    except Exception as e:
        logger.exception(f"Frame extraction failed for {task_id}")
        task = load_task(task_id)
        if task:
            task['status'] = 'error'
            task['error_msg'] = str(e)
            save_task(task_id, task)
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)

# ------------------------------------------------------------
# Flask Routes
# ------------------------------------------------------------
def register_routes(app):
    @app.route('/clipper/list_videos', methods=['GET'])
    def clipper_list_videos():
        videos = []
        try:
            for f in os.listdir(UPLOAD_FOLDER):
                full = os.path.join(UPLOAD_FOLDER, f)
                if os.path.isfile(full) and f.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.m4v')):
                    videos.append({'name': f, 'path': f})
            videos.sort(key=lambda x: x['name'])
            return jsonify(videos)
        except Exception as e:
            logger.exception("Error listing videos")
            return jsonify({'error': str(e)}), 500

    @app.route('/clipper/random', methods=['POST'])
    def clipper_random():
        if not check_ffmpeg():
            return jsonify({'error': 'ffmpeg not installed'}), 500
        video_file = request.form.get('video_file')
        segment_duration = int(request.form.get('segment_duration', 30))
        clip_duration = int(request.form.get('clip_duration', 5))
        if not video_file:
            return jsonify({'error': 'Video file required'}), 400
        video_path = os.path.join(UPLOAD_FOLDER, video_file)
        if not os.path.exists(video_path):
            return jsonify({'error': 'Video not found'}), 404
        if clip_duration > segment_duration:
            return jsonify({'error': 'Clip cannot be longer than segment'}), 400
        task_id = str(uuid.uuid4())
        output_filename = f"random_clips_{os.path.splitext(video_file)[0]}.mp4"
        output_path = os.path.join(UPLOAD_FOLDER, output_filename)
        task_data = {
            'task_id': task_id, 'status': 'queued', 'progress': 0, 'created_at': time.time(),
            'cancelled': False, 'video_file': video_file, 'mode': 'random',
            'segment_duration': segment_duration, 'clip_duration': clip_duration
        }
        save_task(task_id, task_data)
        def run():
            try:
                process_random_clips(video_path, segment_duration, clip_duration, output_path, task_id)
            except Exception as e:
                task = load_task(task_id)
                task['status'] = 'error'
                task['error_msg'] = str(e)
                save_task(task_id, task)
        threading.Thread(target=run, daemon=True).start()
        return jsonify({'task_id': task_id})

    @app.route('/clipper/summarize', methods=['POST'])
    def clipper_summarize():
        if not check_ffmpeg():
            return jsonify({'error': 'ffmpeg not installed'}), 500
        video_file = request.form.get('video_file')
        target_duration = int(request.form.get('target_duration', 30))
        clip_duration = int(request.form.get('clip_duration', 2))
        if not video_file:
            return jsonify({'error': 'Video file required'}), 400
        video_path = os.path.join(UPLOAD_FOLDER, video_file)
        if not os.path.exists(video_path):
            return jsonify({'error': 'Video not found'}), 404
        if clip_duration <= 0 or target_duration <= 0:
            return jsonify({'error': 'Durations must be positive'}), 400
        task_id = str(uuid.uuid4())
        output_filename = f"summary_{os.path.splitext(video_file)[0]}.mp4"
        output_path = os.path.join(UPLOAD_FOLDER, output_filename)
        task_data = {
            'task_id': task_id, 'status': 'queued', 'progress': 0, 'created_at': time.time(),
            'cancelled': False, 'video_file': video_file, 'mode': 'summarizer',
            'target_duration': target_duration, 'clip_duration': clip_duration
        }
        save_task(task_id, task_data)
        def run():
            try:
                process_summarizer(video_path, target_duration, clip_duration, output_path, task_id)
            except Exception as e:
                task = load_task(task_id)
                task['status'] = 'error'
                task['error_msg'] = str(e)
                save_task(task_id, task)
        threading.Thread(target=run, daemon=True).start()
        return jsonify({'task_id': task_id})

    # ---------- NEW: Frame Extractor ----------
    @app.route('/clipper/extract_frames', methods=['POST'])
    def clipper_extract_frames():
        if not check_ffmpeg():
            return jsonify({'error': 'ffmpeg not installed'}), 500
        video_file = request.form.get('video_file')
        interval = float(request.form.get('interval', 5))
        format_ = request.form.get('format', 'jpg')
        if not video_file:
            return jsonify({'error': 'Video file required'}), 400
        video_path = os.path.join(UPLOAD_FOLDER, video_file)
        if not os.path.exists(video_path):
            return jsonify({'error': 'Video not found'}), 404
        if interval <= 0:
            return jsonify({'error': 'Interval must be > 0'}), 400
        task_id = str(uuid.uuid4())
        task_data = {
            'task_id': task_id,
            'status': 'queued',
            'progress': 0,
            'created_at': time.time(),
            'cancelled': False,
            'video_file': video_file,
            'interval': interval,
            'format': format_,
            'total_frames': 0
        }
        save_task(task_id, task_data)

        def run():
            extract_frames_task(video_path, interval, task_id, format_)
        threading.Thread(target=run, daemon=True).start()
        return jsonify({'task_id': task_id})
