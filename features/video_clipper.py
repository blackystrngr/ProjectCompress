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

def log_info(m):    logger.info(m)
def log_warning(m): logger.warning(m)


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
    result = subprocess.run(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise Exception(f"ffprobe failed: {result.stderr}")
    return float(result.stdout.strip())


def has_video_stream(file_path):
    cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
           '-show_entries', 'stream=codec_type',
           '-of', 'default=noprint_wrappers=1:nokey=1', file_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == 'video'


def extract_clip_with_fallback(video_path, start_time, clip_duration,
                               output_path, task_id=None, idx=None, total=None):
    cmd_copy = ['ffmpeg', '-ss', str(start_time), '-i', video_path,
                '-t', str(clip_duration),
                '-map', '0:v', '-map', '0:a?',
                '-c', 'copy', '-avoid_negative_ts', 'make_zero',
                '-copyts', '-y', output_path]
    try:
        result = subprocess.run(cmd_copy, capture_output=True, text=True, check=False)
        if (result.returncode == 0 and os.path.exists(output_path)
                and os.path.getsize(output_path) > 0
                and has_video_stream(output_path)):
            if task_id and idx and total:
                task = load_task(task_id)
                if task:
                    task['current_clip'] = idx
                    task['progress'] = 30 + int(50 * idx / total)
                    save_task(task_id, task)
            return
    except Exception:
        pass

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
    concat_file = os.path.join(os.path.dirname(output_path),
                               f"{task_id}_concat.txt")
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
def process_random_clips(video_path, segment_duration, clip_duration,
                         output_path, task_id):
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
            extract_clip_with_fallback(video_path, start, clip_duration,
                                       clip_path, task_id, idx, total_clips)
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
def process_summarizer(video_path, target_duration_sec, clip_duration_sec,
                       output_path, task_id):
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
            extract_clip_with_fallback(video_path, start, clip_duration_sec,
                                       clip_path, task_id, idx, len(starts))
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
# Frame Extractor
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
        cmd = ['ffmpeg', '-i', video_path,
               '-vf', f"fps=1/{interval_sec}",
               '-q:v', '2', '-y', pattern]
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
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

        frame_files = sorted([f for f in os.listdir(temp_dir)
                              if f.endswith(f'.{output_format}')])
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
# Trim / Crop from timestamp range
# ------------------------------------------------------------
def _parse_timestamp(val):
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    if s.startswith('-'):
        return None
    parts = s.split(':')
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return None
    return None


def process_trim_task(video_path, start_sec, end_sec, output_path,
                      mode, task_id):
    task = load_task(task_id)
    if not task:
        return
    task['status'] = 'trimming'
    task['progress'] = 0
    task['mode'] = mode
    task['start_sec'] = start_sec
    task['end_sec'] = end_sec
    task['duration'] = end_sec - start_sec
    save_task(task_id, task)

    try:
        total_duration = get_video_duration(video_path)
        if total_duration is None:
            raise Exception("Cannot read source duration")
        if start_sec < 0:
            raise Exception("start must be >= 0")
        if end_sec <= start_sec:
            raise Exception("end must be greater than start")
        if start_sec >= total_duration:
            raise Exception(f"start ({start_sec:.2f}s) is past the end "
                            f"of video ({total_duration:.2f}s)")
        if end_sec > total_duration:
            log_warning(f"end clamped {end_sec:.2f}s → {total_duration:.2f}s")
            end_sec = total_duration

        trim_dur = end_sec - start_sec
        log_info(f"Trim: {video_path} [{start_sec:.3f}s → {end_sec:.3f}s] "
                 f"({trim_dur:.2f}s) mode={mode}")

        task = load_task(task_id)
        task['total_duration_source'] = total_duration
        task['duration'] = trim_dur
        save_task(task_id, task)

        if mode == 'copy':
            cmd = ['ffmpeg', '-ss', f'{start_sec:.3f}', '-i', video_path,
                   '-t', f'{trim_dur:.3f}',
                   '-map', '0:v:0', '-map', '0:a:0?',
                   '-c', 'copy',
                   '-avoid_negative_ts', 'make_zero',
                   '-reset_timestamps', '1',
                   '-movflags', '+faststart',
                   '-y', output_path]
        else:
            cmd = ['ffmpeg', '-i', video_path,
                   '-ss', f'{start_sec:.3f}', '-t', f'{trim_dur:.3f}',
                   '-map', '0:v:0', '-map', '0:a:0?',
                   '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '20',
                   '-pix_fmt', 'yuv420p',
                   '-c:a', 'aac', '-b:a', '128k', '-ac', '2',
                   '-movflags', '+faststart',
                   '-avoid_negative_ts', 'make_zero',
                   '-y', output_path]

        log_info(f"ffmpeg: {' '.join(cmd)}")
        pct_re = re.compile(r'out_time_us=(\d+)')
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                universal_newlines=True, bufsize=1)
        last_update = 0
        for line in proc.stdout:
            line = line.strip()
            m = pct_re.search(line)
            if m and trim_dur > 0:
                cur_sec = int(m.group(1)) / 1_000_000
                pct = min(99, int(100 * cur_sec / trim_dur))
                now = time.time()
                if now - last_update >= 0.5:
                    task = load_task(task_id)
                    if task:
                        task['progress'] = pct
                        save_task(task_id, task)
                    last_update = now
            t = load_task(task_id)
            if t and t.get('cancelled', False):
                try:
                    proc.kill()
                except Exception:
                    pass
                raise Exception("Cancelled by user")
        proc.wait()
        if proc.returncode != 0:
            raise Exception(f"ffmpeg exited with code {proc.returncode}")
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise Exception("Trim output is empty")
        if not has_video_stream(output_path):
            raise Exception("Trim output has no video stream")

        actual_dur = get_video_duration(output_path)
        out_size = os.path.getsize(output_path)
        task = load_task(task_id)
        if task:
            task['status'] = 'done'
            task['progress'] = 100
            task['output_file'] = os.path.basename(output_path)
            task['output_size'] = out_size
            task['output_duration'] = actual_dur
            save_task(task_id, task)
        log_info(f"Trim done: {os.path.basename(output_path)} "
                 f"({out_size / 1024 / 1024:.2f} MB)")

    except Exception as e:
        logger.exception(f"Trim failed for {task_id}")
        task = load_task(task_id)
        if task:
            task['status'] = 'error'
            task['error_msg'] = str(e)
            save_task(task_id, task)
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except Exception:
            pass


# ------------------------------------------------------------
# x265 CPU compress (NEW)
# ------------------------------------------------------------
ALLOWED_X265_PRESETS = {
    'ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium'
}
ALLOWED_X265_RESOLUTIONS = {360, 480, 720, 1080}


def process_x265_task(video_path, output_path, task_id,
                      resolution, crf, preset):
    """
    Compress the entire video to 720p/1080p/etc. H.265 via libx265.
    - resolution: target height in pixels
    - crf: quality (lower = better/larger, 18–40 sensible)
    - preset: x265 preset name
    """
    task = load_task(task_id)
    if not task:
        return
    task['status'] = 'compressing'
    task['progress'] = 0
    task['resolution'] = resolution
    task['crf'] = crf
    task['preset'] = preset
    save_task(task_id, task)

    try:
        duration = get_video_duration(video_path)
        if duration is None or duration <= 0:
            raise Exception("Cannot read source duration")

        # Build video filter: scale + fps cap
        vf = f"scale=-2:{resolution}:flags=lanczos,fps=30"

        cmd = [
            'ffmpeg', '-y',
            '-progress', 'pipe:2',
            '-stats_period', '0.5',
            '-i', video_path,
            '-map', '0:v:0', '-map', '0:a:0?',
            '-vf', vf,
            '-c:v', 'libx265',
            '-preset', preset,
            '-crf', str(crf),
            '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '128k', '-ac', '2',
            '-af', 'aresample=async=1:first_pts=0',
            '-movflags', '+faststart',
            output_path
        ]

        log_info(f"x265 compress: preset={preset}, crf={crf}, "
                 f"resolution={resolution}p, duration={duration:.1f}s")
        log_info(f"ffmpeg: {' '.join(cmd)}")

        # Run with progress parsing on stderr
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            bufsize=1,
            start_new_session=True,
        )

        cur_sec = 0.0
        last_update = 0
        last_stderr = []
        # Read stderr for progress and errors
        for line in proc.stderr:
            line = line.strip()
            last_stderr.append(line)
            if len(last_stderr) > 40:
                last_stderr.pop(0)

            if line.startswith('out_time_us='):
                try:
                    cur_sec = int(line.split('=', 1)[1]) / 1_000_000
                except Exception:
                    pass
            elif line.startswith('out_time_ms='):
                try:
                    cur_sec = int(line.split('=', 1)[1]) / 1_000_000
                except Exception:
                    pass
            elif line.startswith('progress='):
                if duration > 0:
                    pct = min(99, int(100 * cur_sec / duration))
                    now = time.time()
                    if now - last_update >= 0.5:
                        t = load_task(task_id)
                        if t:
                            t['progress'] = pct
                            save_task(task_id, t)
                        last_update = now
                # Check cancel at each progress tick
                t = load_task(task_id)
                if t and t.get('cancelled', False):
                    try:
                        os.killpg(os.getpgid(proc.pid), 9)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    raise Exception("Cancelled by user")

        proc.wait()

        if proc.returncode != 0:
            log_warning("x265 ffmpeg output tail:")
            for l in last_stderr[-15:]:
                log_warning(f"  {l}")
            raise Exception(f"ffmpeg exited with code {proc.returncode}")

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise Exception("Output is empty")
        if not has_video_stream(output_path):
            raise Exception("Output has no video stream")

        actual_dur = get_video_duration(output_path)
        out_size = os.path.getsize(output_path)
        in_size = os.path.getsize(video_path)
        savings = (1 - out_size / in_size) * 100 if in_size else 0

        task = load_task(task_id)
        if task:
            task['status'] = 'done'
            task['progress'] = 100
            task['output_file'] = os.path.basename(output_path)
            task['output_size'] = out_size
            task['input_size'] = in_size
            task['output_duration'] = actual_dur
            task['savings_pct'] = round(savings, 1)
            save_task(task_id, task)

        log_info(f"x265 done: {os.path.basename(output_path)} "
                 f"({out_size / 1024 / 1024:.2f} MB, "
                 f"{savings:.1f}% smaller)")

    except Exception as e:
        logger.exception(f"x265 compress failed for {task_id}")
        task = load_task(task_id)
        if task:
            task['status'] = 'error'
            task['error_msg'] = str(e)
            save_task(task_id, task)
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except Exception:
            pass


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
                if os.path.isfile(full) and f.lower().endswith(
                        ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.m4v')):
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
            'task_id': task_id, 'status': 'queued', 'progress': 0,
            'created_at': time.time(), 'cancelled': False,
            'video_file': video_file, 'mode': 'random',
            'segment_duration': segment_duration, 'clip_duration': clip_duration
        }
        save_task(task_id, task_data)

        def run():
            try:
                process_random_clips(video_path, segment_duration,
                                     clip_duration, output_path, task_id)
            except Exception as e:
                t = load_task(task_id)
                t['status'] = 'error'
                t['error_msg'] = str(e)
                save_task(task_id, t)
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
            'task_id': task_id, 'status': 'queued', 'progress': 0,
            'created_at': time.time(), 'cancelled': False,
            'video_file': video_file, 'mode': 'summarizer',
            'target_duration': target_duration, 'clip_duration': clip_duration
        }
        save_task(task_id, task_data)

        def run():
            try:
                process_summarizer(video_path, target_duration,
                                   clip_duration, output_path, task_id)
            except Exception as e:
                t = load_task(task_id)
                t['status'] = 'error'
                t['error_msg'] = str(e)
                save_task(task_id, t)
        threading.Thread(target=run, daemon=True).start()
        return jsonify({'task_id': task_id})

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
            'task_id': task_id, 'status': 'queued', 'progress': 0,
            'created_at': time.time(), 'cancelled': False,
            'video_file': video_file, 'interval': interval,
            'format': format_, 'total_frames': 0
        }
        save_task(task_id, task_data)

        def run():
            extract_frames_task(video_path, interval, task_id, format_)
        threading.Thread(target=run, daemon=True).start()
        return jsonify({'task_id': task_id})

    @app.route('/clipper/trim', methods=['POST'])
    def clipper_trim():
        if not check_ffmpeg():
            return jsonify({'error': 'ffmpeg not installed'}), 500
        video_file = request.form.get('video_file')
        start_raw = request.form.get('start', '')
        end_raw = request.form.get('end', '')
        mode = request.form.get('mode', 'copy').lower()
        if mode not in ('copy', 'precise'):
            mode = 'copy'
        if not video_file:
            return jsonify({'error': 'Video file required'}), 400
        video_path = os.path.join(UPLOAD_FOLDER, video_file)
        if not os.path.exists(video_path):
            return jsonify({'error': 'Video not found'}), 404

        start_sec = _parse_timestamp(start_raw)
        end_sec = _parse_timestamp(end_raw)
        if start_sec is None:
            return jsonify({'error': f'Invalid start time: {start_raw!r}'}), 400
        if end_sec is None:
            return jsonify({'error': f'Invalid end time: {end_raw!r}'}), 400
        if end_sec <= start_sec:
            return jsonify({'error': 'end must be after start'}), 400

        base = os.path.splitext(os.path.basename(video_file))[0]
        tag = f"{int(start_sec):05d}_{int(end_sec):05d}"
        out_name = f"{base}_trim_{tag}.mp4"
        i = 1
        while os.path.exists(os.path.join(UPLOAD_FOLDER, out_name)):
            out_name = f"{base}_trim_{tag}_{i}.mp4"
            i += 1
        out_path = os.path.join(UPLOAD_FOLDER, out_name)

        task_id = str(uuid.uuid4())
        task_data = {
            'task_id': task_id, 'status': 'queued', 'progress': 0,
            'created_at': time.time(), 'cancelled': False,
            'video_file': video_file, 'mode': mode,
            'start': start_sec, 'end': end_sec,
            'duration': end_sec - start_sec,
            'output_file': out_name,
        }
        save_task(task_id, task_data)

        def run():
            process_trim_task(video_path, start_sec, end_sec,
                              out_path, mode, task_id)
        threading.Thread(target=run, daemon=True).start()

        return jsonify({
            'task_id': task_id,
            'output_file': out_name,
            'duration': end_sec - start_sec,
            'mode': mode,
        })

    # ---------- x265 CPU compress (NEW) ----------
    @app.route('/clipper/x265_compress', methods=['POST'])
    def clipper_x265_compress():
        if not check_ffmpeg():
            return jsonify({'error': 'ffmpeg not installed'}), 500

        video_file = request.form.get('video_file')
        if not video_file:
            return jsonify({'error': 'Video file required'}), 400

        video_path = os.path.join(UPLOAD_FOLDER, video_file)
        if not os.path.exists(video_path):
            return jsonify({'error': 'Video not found'}), 404

        try:
            resolution = int(request.form.get('resolution', 720))
        except ValueError:
            resolution = 720
        if resolution not in ALLOWED_X265_RESOLUTIONS:
            return jsonify({'error':
                f'Invalid resolution. Allowed: '
                f'{sorted(ALLOWED_X265_RESOLUTIONS)}'}), 400

        try:
            crf = int(request.form.get('crf', 27))
        except ValueError:
            return jsonify({'error': 'Invalid CRF'}), 400
        if crf < 10 or crf > 45:
            return jsonify({'error': 'CRF must be between 10 and 45'}), 400

        preset = request.form.get('preset', 'veryfast').lower()
        if preset not in ALLOWED_X265_PRESETS:
            return jsonify({'error':
                f'Invalid preset. Allowed: '
                f'{sorted(ALLOWED_X265_PRESETS)}'}), 400

        base = os.path.splitext(os.path.basename(video_file))[0]
        out_name = f"{base}_{resolution}p_x265_crf{crf}.mp4"
        i = 1
        while os.path.exists(os.path.join(UPLOAD_FOLDER, out_name)):
            out_name = f"{base}_{resolution}p_x265_crf{crf}_{i}.mp4"
            i += 1
        out_path = os.path.join(UPLOAD_FOLDER, out_name)

        task_id = str(uuid.uuid4())
        task_data = {
            'task_id': task_id, 'status': 'queued', 'progress': 0,
            'created_at': time.time(), 'cancelled': False,
            'video_file': video_file, 'mode': 'x265_compress',
            'resolution': resolution, 'crf': crf, 'preset': preset,
            'output_file': out_name,
        }
        save_task(task_id, task_data)

        def run():
            process_x265_task(video_path, out_path, task_id,
                              resolution, crf, preset)
        threading.Thread(target=run, daemon=True).start()

        return jsonify({
            'task_id': task_id,
            'output_file': out_name,
            'resolution': resolution,
            'crf': crf,
            'preset': preset,
        })
