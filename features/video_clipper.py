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
    except Exception:
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


def get_duration_clip(clip_path):
    """Same as get_video_duration but returns None on failure."""
    try:
        return get_video_duration(clip_path)
    except Exception:
        return None


# ------------------------------------------------------------
# Stream-copy extraction with audio re-encode for sync
# ------------------------------------------------------------
def extract_clip(video_path, start_time, clip_duration, output_path,
                 task_id=None, idx=None, total=None):
    """
    Extract a single clip:
      - video: stream copy
      - audio: re-encoded with aresample=async=1 so A/V start
        at the same timestamp (fixes accumulated drift on concat)
    """
    cmd = [
        'ffmpeg',
        '-ss', f'{start_time:.3f}',
        '-i', video_path,
        '-t', f'{clip_duration:.3f}',
        '-map', '0:v:0', '-map', '0:a:0?',
        '-c:v', 'copy',
        '-c:a', 'aac', '-b:a', '128k', '-ar', '48000', '-ac', '2',
        '-af', 'aresample=async=1',
        '-avoid_negative_ts', 'make_zero',
        '-reset_timestamps', '1',
        '-shortest',
        '-y', output_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if (result.returncode != 0
            or not os.path.exists(output_path)
            or os.path.getsize(output_path) == 0
            or not has_video_stream(output_path)):
        # Fallback to re-encode (rarely needed but safer)
        cmd_re = [
            'ffmpeg',
            '-i', video_path,
            '-ss', f'{start_time:.3f}',
            '-t', f'{clip_duration:.3f}',
            '-map', '0:v:0', '-map', '0:a:0?',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '20',
            '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '128k', '-ac', '2',
            '-af', 'aresample=async=1',
            '-movflags', '+faststart',
            '-avoid_negative_ts', 'make_zero',
            '-y', output_path
        ]
        result = subprocess.run(cmd_re, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise Exception(f"Re-encode fallback failed: {result.stderr[-300:]}")
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise Exception("Re-encode produced empty output")
        if not has_video_stream(output_path):
            raise Exception("Re-encode has no video stream")

    if task_id and idx and total:
        task = load_task(task_id)
        if task:
            task['current_clip'] = idx
            task['progress'] = 5 + int(40 * idx / total)
            save_task(task_id, task)


def _write_concat_list_with_durations(clip_files, clip_durations, concat_list):
    """
    Write a concat list with explicit `duration` directives.

    Why: ffmpeg's concat demuxer relies on each clip's container duration
    to advance to the next file. Stream-copied clips often report 0 or -1,
    which causes the demuxer to hit EOF early (encoder stops at ~26s of a
    14-min timeline). The `duration` line forces the correct advance.

    The final repeat of the last file is a concat-demuxer quirk that
    ensures the last clip's audio tail is flushed.
    """
    with open(concat_list, 'w') as f:
        for c, d in zip(clip_files, clip_durations):
            f.write(f"file '{os.path.abspath(c)}'\n")
            f.write(f"duration {d:.3f}\n")
        if clip_files:
            f.write(f"file '{os.path.abspath(clip_files[-1])}'\n")


def merge_clips(clip_files, output_path, task_id):
    """Stream-copy concat of clips into one file. No re-encode."""
    if not clip_files:
        raise Exception("No clips to merge")
    task = load_task(task_id)
    task['status'] = 'merging'
    task['progress'] = 90
    save_task(task_id, task)

    concat_file = os.path.join(os.path.dirname(output_path),
                               f"{task_id}_concat.txt")
    with open(concat_file, 'w') as f:
        for clip in clip_files:
            f.write(f"file '{os.path.abspath(clip)}'\n")

    cmd = ['ffmpeg', '-f', 'concat', '-safe', '0', '-i', concat_file,
           '-fflags', '+genpts',
           '-c', 'copy', '-y', output_path]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if os.path.exists(concat_file):
        os.remove(concat_file)
    if result.returncode != 0:
        raise Exception(f"Merge error: {result.stderr[-300:]}")
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise Exception("Merge output missing")


# ------------------------------------------------------------
# Unified Clips pipeline (extract + optional compress)
# ------------------------------------------------------------
ALLOWED_CODECS = {'x265', 'av1'}
ALLOWED_RESOLUTIONS = {360, 480, 720, 1080}
ALLOWED_X265_PRESETS = {
    'ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium'
}
ALLOWED_AV1_PRESETS = {8, 9, 10, 11, 12, 13}


def _run_progress_ffmpeg(cmd, total_duration, task_id,
                         progress_lo, progress_hi):
    """
    Run an ffmpeg command, parse `out_time_us=` for progress, update task.
    Progress is mapped linearly into [progress_lo, progress_hi].
    Returns True on success, raises on failure.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        bufsize=1,
        start_new_session=True,
    )
    pct_re = re.compile(r'out_time_us=(\d+)')
    last_update = 0
    last_stderr = []

    for line in proc.stderr:
        line = line.strip()
        last_stderr.append(line)
        if len(last_stderr) > 40:
            last_stderr.pop(0)

        m = pct_re.search(line)
        if m and total_duration > 0:
            cur = int(m.group(1)) / 1_000_000
            local_pct = min(100, 100 * cur / total_duration)
            overall = progress_lo + int((progress_hi - progress_lo) * local_pct / 100)
            now = time.time()
            if now - last_update >= 0.5:
                t = load_task(task_id)
                if t:
                    t['progress'] = min(progress_hi, overall)
                    save_task(task_id, t)
                last_update = now

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
        log_warning("ffmpeg tail:")
        for l in last_stderr[-12:]:
            log_warning(f"  {l}")
        raise Exception(f"ffmpeg exited with code {proc.returncode}")


def process_clips_task(video_path, base, task_id,
                       segment_duration, clip_duration, tail_seconds,
                       compress, codec, resolution, crf, preset):
    """
    Unified pipeline:
      1. Extract stream-copied clips with audio-sync handling.
      2. If compress:
           Write concat list with durations → encode directly to H.265/AV1.
         Else:
           Stream-copy concat merge (fast).
    """
    task = load_task(task_id)
    if not task:
        return
    task['status'] = 'extracting'
    task['progress'] = 0
    task['mode'] = 'clips_compress' if compress else 'clips_only'
    task['compress'] = compress
    if compress:
        task['codec'] = codec
        task['resolution'] = resolution
        task['crf'] = crf
        task['preset'] = preset
    save_task(task_id, task)

    # Build output filename
    if compress:
        tag = f"_clips_{resolution}p_{codec}"
    else:
        tag = "_clips"
    out_name = f"{base}{tag}.mp4"
    i = 1
    while os.path.exists(os.path.join(UPLOAD_FOLDER, out_name)):
        out_name = f"{base}{tag}_{i}.mp4"
        i += 1
    out_path = os.path.join(UPLOAD_FOLDER, out_name)

    task = load_task(task_id)
    task['output_file'] = out_name
    save_task(task_id, task)

    temp_dir = os.path.join(UPLOAD_FOLDER, f"clips_{task_id}")
    os.makedirs(temp_dir, exist_ok=True)

    try:
        total_duration = get_video_duration(video_path)
        if not total_duration or total_duration <= 0:
            raise Exception("Cannot read source duration")

        task = load_task(task_id)
        task['total_duration'] = total_duration
        save_task(task_id, task)

        # Build segments (clip windows + tail chunks)
        tail_start = max(0, total_duration - tail_seconds)
        clip_end = tail_start if tail_seconds > 0 else total_duration

        segments = []
        current = 0.0
        while current < clip_end - clip_duration:
            seg_end = min(current + segment_duration, clip_end)
            latest = seg_end - clip_duration
            if latest > current:
                start = random.uniform(current, latest)
                segments.append((start, 'clip'))
            current += segment_duration

        # Tail chunks
        if tail_seconds > 0 and tail_start < total_duration:
            ct = tail_start
            while ct < total_duration - 0.5:
                ce = min(ct + 30.0, total_duration)
                if ce - ct >= 2.0:
                    segments.append((ct, 'tail'))
                ct = ce

        total_segments = len(segments)
        if total_segments == 0:
            raise Exception("No valid segments found")

        task = load_task(task_id)
        task['total_clips'] = total_segments
        save_task(task_id, task)

        clip_files = []
        clip_durations = []

        for idx, (start, kind) in enumerate(segments, 1):
            if load_task(task_id).get('cancelled', False):
                raise Exception("Cancelled")
            clip_path = os.path.join(temp_dir, f"clip_{idx:05d}.mp4")

            if kind == 'tail':
                # Tail chunks are larger; the last one may be truncated
                dur = min(30.0, total_duration - start)
            else:
                dur = clip_duration

            extract_clip(video_path, start, dur, clip_path,
                         task_id, idx, total_segments)
            d = get_duration_clip(clip_path)
            if d is None or d < 0.1:
                # Skip unreadable clip
                try:
                    os.remove(clip_path)
                except Exception:
                    pass
                continue
            clip_files.append(clip_path)
            clip_durations.append(d)

        if not clip_files:
            raise Exception("No clips extracted successfully")

        task = load_task(task_id)
        task['extracted_clips'] = len(clip_files)
        task['progress'] = 50
        save_task(task_id, task)

        total_clip_dur = sum(clip_durations)

        if compress:
            # ---- ENCODE DIRECTLY FROM CONCAT LIST ----
            concat_list = os.path.join(temp_dir, "concat.txt")
            _write_concat_list_with_durations(clip_files, clip_durations,
                                              concat_list)

            task = load_task(task_id)
            task['status'] = 'compressing'
            task['progress'] = 50
            save_task(task_id, task)

            vf = f"scale=-2:{resolution}:flags=lanczos,fps=30"

            if codec == 'x265':
                cmd = [
                    'ffmpeg', '-y',
                    '-progress', 'pipe:2', '-stats_period', '0.5',
                    '-f', 'concat', '-safe', '0', '-i', concat_list,
                    '-fflags', '+genpts',
                    '-map', '0:v:0', '-map', '0:a:0?',
                    '-vf', vf,
                    '-c:v', 'libx265',
                    '-preset', preset,
                    '-crf', str(crf),
                    '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-b:a', '128k', '-ac', '2',
                    '-af', 'aresample=async=1:first_pts=0',
                    '-movflags', '+faststart',
                    out_path
                ]
            else:  # av1
                cmd = [
                    'ffmpeg', '-y',
                    '-progress', 'pipe:2', '-stats_period', '0.5',
                    '-f', 'concat', '-safe', '0', '-i', concat_list,
                    '-fflags', '+genpts',
                    '-map', '0:v:0', '-map', '0:a:0?',
                    '-vf', vf,
                    '-c:v', 'libsvtav1',
                    '-preset', str(preset),
                    '-crf', str(crf),
                    '-pix_fmt', 'yuv420p10le',
                    '-svtav1-params',
                    'lp=2:film-grain=6:tune=0:scd=1:keyint=240:aq-mode=2',
                    '-c:a', 'libopus', '-b:a', '128k', '-ac', '2',
                    '-af', 'aresample=async=1:first_pts=0',
                    '-movflags', '+faststart',
                    out_path
                ]

            log_info(f"Encode: codec={codec}, preset={preset}, crf={crf}, "
                     f"res={resolution}p, clips={len(clip_files)}, "
                     f"total_dur={total_clip_dur:.1f}s")

            _run_progress_ffmpeg(cmd, total_clip_dur, task_id, 50, 99)
        else:
            # ---- MERGE ONLY ----
            merge_clips(clip_files, out_path, task_id)

        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise Exception("Output file missing or empty")
        if not has_video_stream(out_path):
            raise Exception("Output has no video stream")

        out_size = os.path.getsize(out_path)
        try:
            in_size = os.path.getsize(video_path)
            savings = (1 - out_size / in_size) * 100
        except Exception:
            savings = 0
        actual_dur = get_video_duration(out_path)

        task = load_task(task_id)
        if task:
            task['status'] = 'done'
            task['progress'] = 100
            task['output_file'] = os.path.basename(out_path)
            task['output_size'] = out_size
            task['output_duration'] = actual_dur
            task['savings_pct'] = round(savings, 1)
            save_task(task_id, task)

    except Exception as e:
        logger.exception(f"Clips pipeline failed for {task_id}")
        task = load_task(task_id)
        if task:
            task['status'] = 'error'
            task['error_msg'] = str(e)
            save_task(task_id, task)
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ------------------------------------------------------------
# AI Summarizer (unchanged, still uses stream copy merge)
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
            extract_clip(video_path, start, clip_duration_sec,
                         clip_path, task_id, idx, len(starts))
            clip_files.append(clip_path)

        merge_clips(clip_files, output_path, task_id)
    finally:
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
# Trim / Crop
# ------------------------------------------------------------
def _parse_timestamp(val):
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.startswith('-'):
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
    save_task(task_id, task)

    try:
        total_duration = get_video_duration(video_path)
        if start_sec < 0:
            raise Exception("start must be >= 0")
        if end_sec <= start_sec:
            raise Exception("end must be greater than start")
        if start_sec >= total_duration:
            raise Exception(f"start ({start_sec:.2f}s) is past the end of video")
        if end_sec > total_duration:
            end_sec = total_duration

        trim_dur = end_sec - start_sec

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

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                universal_newlines=True, bufsize=1)
        pct_re = re.compile(r'out_time_us=(\d+)')
        last_update = 0
        for line in proc.stdout:
            m = pct_re.search(line.strip())
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
# Routes
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

    # ---- Unified clips endpoint (extract, optional compress) ----
    @app.route('/clipper/clips', methods=['POST'])
    def clipper_clips():
        if not check_ffmpeg():
            return jsonify({'error': 'ffmpeg not installed'}), 500

        video_file = request.form.get('video_file')
        if not video_file:
            return jsonify({'error': 'Video file required'}), 400
        video_path = os.path.join(UPLOAD_FOLDER, video_file)
        if not os.path.exists(video_path):
            return jsonify({'error': 'Video not found'}), 404

        try:
            segment_duration = int(request.form.get('segment_duration', 10))
            clip_duration = int(request.form.get('clip_duration', 5))
            tail_seconds = int(request.form.get('tail_seconds', 0))
        except ValueError:
            return jsonify({'error': 'Invalid duration values'}), 400

        if segment_duration < 1 or clip_duration < 1:
            return jsonify({'error': 'Durations must be >= 1'}), 400
        if clip_duration > segment_duration:
            return jsonify({'error': 'Clip must be <= segment'}), 400
        if tail_seconds < 0:
            tail_seconds = 0

        compress = request.form.get('compress', 'false').lower() in (
            '1', 'true', 'yes', 'on'
        )

        if compress:
            codec = request.form.get('codec', 'x265').lower()
            if codec not in ALLOWED_CODECS:
                return jsonify({'error':
                    f'Invalid codec. Allowed: {sorted(ALLOWED_CODECS)}'}), 400

            try:
                resolution = int(request.form.get('resolution', 720))
                crf = int(request.form.get('crf', 27))
            except ValueError:
                return jsonify({'error': 'Invalid resolution or CRF'}), 400

            if resolution not in ALLOWED_RESOLUTIONS:
                return jsonify({'error':
                    f'Invalid resolution. Allowed: {sorted(ALLOWED_RESOLUTIONS)}'}), 400

            if codec == 'x265':
                if crf < 10 or crf > 45:
                    return jsonify({'error': 'x265 CRF must be 10–45'}), 400
                preset = request.form.get('preset', 'veryfast').lower()
                if preset not in ALLOWED_X265_PRESETS:
                    return jsonify({'error':
                        f'Invalid x265 preset. Allowed: '
                        f'{sorted(ALLOWED_X265_PRESETS)}'}), 400
            else:  # av1
                if crf < 15 or crf > 45:
                    return jsonify({'error': 'AV1 CRF must be 15–45'}), 400
                try:
                    preset = int(request.form.get('preset', 12))
                except ValueError:
                    return jsonify({'error': 'Invalid AV1 preset'}), 400
                if preset not in ALLOWED_AV1_PRESETS:
                    return jsonify({'error':
                        f'Invalid AV1 preset. Allowed: '
                        f'{sorted(ALLOWED_AV1_PRESETS)}'}), 400
        else:
            codec = resolution = crf = preset = None

        base = os.path.splitext(os.path.basename(video_file))[0]
        task_id = str(uuid.uuid4())

        task_data = {
            'task_id': task_id,
            'status': 'queued',
            'progress': 0,
            'created_at': time.time(),
            'cancelled': False,
            'video_file': video_file,
            'segment_duration': segment_duration,
            'clip_duration': clip_duration,
            'tail_seconds': tail_seconds,
            'compress': compress,
        }
        if compress:
            task_data.update({
                'codec': codec,
                'resolution': resolution,
                'crf': crf,
                'preset': preset,
            })
        save_task(task_id, task_data)

        def run():
            try:
                process_clips_task(
                    video_path, base, task_id,
                    segment_duration, clip_duration, tail_seconds,
                    compress, codec, resolution, crf, preset
                )
            except Exception as e:
                logger.exception("Clips pipeline error")
                t = load_task(task_id)
                if t:
                    t['status'] = 'error'
                    t['error_msg'] = str(e)
                    save_task(task_id, t)

        threading.Thread(target=run, daemon=True).start()
        return jsonify({
            'task_id': task_id,
            'compress': compress,
            'codec': codec,
            'resolution': resolution,
        })

    # ---- AI Summarizer ----
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

    # ---- Frame extractor ----
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

    # ---- Trim ----
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
        if start_sec is None or end_sec is None:
            return jsonify({'error': 'Invalid start or end time'}), 400
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
