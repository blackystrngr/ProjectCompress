import os
import uuid
import threading
import time
import json
import logging
from flask import request, jsonify
from werkzeug.utils import secure_filename
from tasks import save_task, load_task
from config import UPLOAD_FOLDER, DRIVE_FOLDER_ID, TOKEN_FILE
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# Google Drive helpers
# ------------------------------------------------------------
def get_drive_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, ['https://www.googleapis.com/auth/drive'])
        except Exception as e:
            logger.error(f"Failed to read token.json: {e}")
            raise
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(TOKEN_FILE, 'w') as token:
                    token.write(creds.to_json())
                logger.info("Token refreshed")
            except Exception as e:
                logger.error(f"Token refresh failed: {e}")
                raise
        else:
            raise Exception("token.json missing or invalid. Please re-authorize.")
    return build('drive', 'v3', credentials=creds)

def upload_file_to_drive(local_path, drive_folder_id, remote_name=None):
    service = get_drive_service()
    name = remote_name or os.path.basename(local_path)
    file_metadata = {'name': name, 'parents': [drive_folder_id]}
    media = MediaFileUpload(local_path, resumable=True)
    try:
        file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        logger.info(f"Uploaded {name} (ID: {file['id']})")
        return file['id']
    except HttpError as e:
        logger.error(f"Drive upload HTTP error: {e}")
        raise
    except Exception as e:
        logger.error(f"Drive upload error: {e}")
        raise

def download_file_from_drive(file_id, local_path):
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id)
    try:
        with open(local_path, 'wb') as f:
            downloader = MediaIoBaseDownload(f, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    logger.debug(f"Downloaded {status.progress()*100:.1f}%")
        logger.info(f"Downloaded {file_id} to {local_path}")
        return True
    except HttpError as e:
        logger.error(f"Drive download HTTP error: {e}")
        raise
    except Exception as e:
        logger.error(f"Drive download error: {e}")
        raise

def delete_drive_file(file_id):
    if not file_id:
        return
    try:
        service = get_drive_service()
        service.files().delete(fileId=file_id).execute()
        logger.info(f"Deleted Drive file {file_id}")
    except Exception as e:
        logger.warning(f"Could not delete Drive file {file_id}: {e}")

# ------------------------------------------------------------
# Configuration – replace with your actual folder IDs
# ------------------------------------------------------------
FACE_SWAP_JOBS_FOLDER_ID = "13pGHK5L8X0pL2z5n5P4bhNLlhP1rQVXs"       # e.g., "1abc..."
FACE_SWAP_OUTPUT_FOLDER_ID = "1h1JKgaxsO3H8MYY0yJOMIbPpqKjku301"   # e.g., "1xyz..."

# ------------------------------------------------------------
# Background face swap task
# ------------------------------------------------------------
def process_face_swap(task_id, video_path, photo_path):
    photo_drive_id = None
    video_drive_id = None
    job_local = None
    try:
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video not found: {video_path}")
        if not os.path.exists(photo_path):
            raise FileNotFoundError(f"Photo not found: {photo_path}")

        photo_remote = f"source_{task_id}_{os.path.basename(photo_path)}"
        video_remote = f"target_{task_id}_{os.path.basename(video_path)}"
        photo_drive_id = upload_file_to_drive(photo_path, DRIVE_FOLDER_ID, photo_remote)
        video_drive_id = upload_file_to_drive(video_path, DRIVE_FOLDER_ID, video_remote)

        job = {
            'task_id': task_id,
            'source_file': photo_remote,
            'target_file': video_remote,
            'output_filename': f"{task_id}_swapped.mp4"
        }
        job_local = os.path.join(UPLOAD_FOLDER, f"{task_id}_job.json")
        with open(job_local, 'w') as f:
            json.dump(job, f)

        upload_file_to_drive(job_local, FACE_SWAP_JOBS_FOLDER_ID, f"{task_id}_job.json")
        os.remove(job_local)
        job_local = None

        status_file_name = f"{task_id}_status.json"
        output_file_name = f"{task_id}_swapped.mp4"
        timeout = 1800  # 30 minutes
        start_time = time.time()
        output_file_id = None
        status_file_id = None
        result = None

        service = get_drive_service()
        while time.time() - start_time < timeout:
            query = f"'{FACE_SWAP_OUTPUT_FOLDER_ID}' in parents and trashed = false"
            try:
                results = service.files().list(q=query, fields="files(id, name)").execute()
                files = results.get('files', [])
                for f in files:
                    if f['name'] == status_file_name:
                        status_file_id = f['id']
                    if f['name'] == output_file_name:
                        output_file_id = f['id']
            except HttpError as e:
                logger.warning(f"Polling error: {e}")
                time.sleep(5)
                continue

            if status_file_id and output_file_id:
                status_local = os.path.join(UPLOAD_FOLDER, f"{task_id}_status.json")
                download_file_from_drive(status_file_id, status_local)
                with open(status_local, 'r') as sf:
                    result = json.load(sf)
                os.remove(status_local)

                if result.get('status') == 'done':
                    output_local = os.path.join(UPLOAD_FOLDER, output_file_name)
                    download_file_from_drive(output_file_id, output_local)
                    task = load_task(task_id)
                    if task:
                        task['status'] = 'done'
                        task['output_file'] = output_file_name
                        task['progress'] = 100
                        save_task(task_id, task)
                    delete_drive_file(photo_drive_id)
                    delete_drive_file(video_drive_id)
                    delete_drive_file(status_file_id)
                    delete_drive_file(output_file_id)
                    return
                elif result.get('status') == 'error':
                    raise Exception(result.get('error_msg', 'Face swap worker reported an error'))
            time.sleep(5)

        raise TimeoutError("Face swap timed out after 30 minutes")

    except Exception as e:
        logger.exception(f"Face swap task {task_id} failed")
        task = load_task(task_id)
        if task:
            task['status'] = 'error'
            task['error_msg'] = str(e)
            save_task(task_id, task)
    finally:
        if job_local and os.path.exists(job_local):
            os.remove(job_local)
        if os.path.exists(photo_path):
            os.remove(photo_path)
        if photo_drive_id:
            delete_drive_file(photo_drive_id)
        if video_drive_id:
            delete_drive_file(video_drive_id)

# ------------------------------------------------------------
# Flask routes
# ------------------------------------------------------------
def register_routes(app):
    @app.route('/face_swap/list_videos', methods=['GET'])
    def face_swap_list_videos():
        """List video files in the downloads folder."""
        try:
            videos = []
            for f in os.listdir(UPLOAD_FOLDER):
                if f.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv')):
                    videos.append({'name': f})
            logger.info(f"Face swap video list: {len(videos)} videos found")
            return jsonify(videos)
        except Exception as e:
            logger.error(f"Failed to list videos for face swap: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/face_swap/start', methods=['POST'])
    def face_swap_start():
        try:
            video_path = request.form.get('video_path')
            if not video_path:
                return jsonify({'error': 'No video path provided'}), 400
            # Prevent path traversal
            if '..' in video_path or video_path.startswith('/'):
                return jsonify({'error': 'Invalid video path'}), 400
            full_video_path = os.path.join(UPLOAD_FOLDER, video_path)
            if not os.path.exists(full_video_path):
                return jsonify({'error': 'Video file not found'}), 404

            if 'photo' not in request.files:
                return jsonify({'error': 'No photo uploaded'}), 400
            photo = request.files['photo']
            if photo.filename == '':
                return jsonify({'error': 'Empty photo'}), 400

            allowed_photo_types = {'image/jpeg', 'image/png', 'image/jpg'}
            if photo.mimetype not in allowed_photo_types:
                return jsonify({'error': 'Photo must be JPEG or PNG'}), 400

            # Save photo temporarily
            photo_filename = secure_filename(photo.filename)
            temp_photo_path = os.path.join(UPLOAD_FOLDER, f"temp_face_{uuid.uuid4().hex}_{photo_filename}")
            photo.save(temp_photo_path)

            task_id = str(uuid.uuid4())
            task_data = {
                'task_id': task_id,
                'status': 'queued',
                'progress': 0,
                'created_at': time.time(),
                'cancelled': False,
                'type': 'face_swap'
            }
            save_task(task_id, task_data)

            # Start background thread
            threading.Thread(target=process_face_swap, args=(task_id, full_video_path, temp_photo_path), daemon=True).start()
            return jsonify({'task_id': task_id})

        except Exception as e:
            logger.exception("Error in /face_swap/start")
            return jsonify({'error': str(e)}), 500
