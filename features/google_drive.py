import os
import uuid
import threading
import time
import logging
from flask import request, jsonify
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from googleapiclient.errors import HttpError
from tasks import save_task, load_task
from config import UPLOAD_FOLDER, DRIVE_FOLDER_ID, TOKEN_FILE

logger = logging.getLogger(__name__)

def get_drive_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, ['https://www.googleapis.com/auth/drive'])
        except Exception as e:
            logger.error(f"Failed to load token.json: {e}")
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

def process_colab(task_id, input_path, original_filename):
    logger.info(f"Colab task {task_id}: input={input_path}")
    try:
        task = load_task(task_id)
        task['status'] = 'uploading'
        task['upload_progress'] = 0
        save_task(task_id, task)

        service = get_drive_service()
        file_metadata = {'name': original_filename, 'parents': [DRIVE_FOLDER_ID]}
        media = MediaFileUpload(input_path, resumable=True, chunksize=10*1024*1024)
        request = service.files().create(body=file_metadata, media_body=media, fields='id')
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                task = load_task(task_id)
                if task:
                    task['upload_progress'] = pct
                    save_task(task_id, task)

        task = load_task(task_id)
        task['status'] = 'waiting_colab'
        task['upload_progress'] = 100
        task['download_progress'] = 0
        save_task(task_id, task)

        base, ext = os.path.splitext(original_filename)
        output_name = f"{base}_av1{ext}"
        local_output = os.path.join(UPLOAD_FOLDER, f"{task_id}_temp_colab{ext}")
        start_time = time.time()
        timeout = 7200
        while time.time() - start_time < timeout:
            query = f"'{DRIVE_FOLDER_ID}' in parents and name = '{output_name}' and trashed = false"
            results = service.files().list(q=query, fields="files(id, name)").execute()
            files = results.get('files', [])
            if files:
                file_id = files[0]['id']
                request = service.files().get_media(fileId=file_id)
                with open(local_output, 'wb') as f:
                    downloader = MediaIoBaseDownload(f, request, chunksize=10*1024*1024)
                    done = False
                    while not done:
                        status, done = downloader.next_chunk()
                        if status:
                            pct = int(status.progress() * 100)
                            task = load_task(task_id)
                            if task:
                                task['download_progress'] = pct
                                save_task(task_id, task)
                service.files().delete(fileId=file_id).execute()
                final_name = _get_unique_filename(output_name)
                final_path = os.path.join(UPLOAD_FOLDER, final_name)
                os.rename(local_output, final_path)
                task = load_task(task_id)
                task['status'] = 'done'
                task['output_file'] = final_name
                save_task(task_id, task)
                return
            time.sleep(15)
        raise TimeoutError("Timeout waiting for Colab output")
    except Exception as e:
        logger.error(f"Colab error for task {task_id}: {e}")
        task = load_task(task_id)
        task['status'] = 'error'
        task['error_msg'] = str(e)
        save_task(task_id, task)

def _get_unique_filename(filename):
    base, ext = os.path.splitext(filename)
    counter = 1
    new_name = filename
    while os.path.exists(os.path.join(UPLOAD_FOLDER, new_name)):
        new_name = f"{base}_{counter}{ext}"
        counter += 1
    return new_name

def register_routes(app):
    @app.route('/drive/list')
    def drive_list():
        try:
            service = get_drive_service()
            results = service.files().list(
                q=f"'{DRIVE_FOLDER_ID}' in parents and trashed = false",
                fields="files(id, name, size, modifiedTime, mimeType)",
                orderBy="name"
            ).execute()
            files = results.get('files', [])
            for f in files:
                size = int(f.get('size', 0))
                if size < 1024:
                    size_str = f"{size} B"
                elif size < 1024*1024:
                    size_str = f"{size/1024:.1f} KB"
                elif size < 1024*1024*1024:
                    size_str = f"{size/(1024*1024):.1f} MB"
                else:
                    size_str = f"{size/(1024*1024*1024):.2f} GB"
                f['size_str'] = size_str
            return jsonify(files)
        except Exception as e:
            logger.error(f"Drive list error: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/drive/download_to_server', methods=['POST'])
    def drive_download_to_server():
        file_id = request.form.get('file_id')
        file_name = request.form.get('file_name')
        if not file_id or not file_name:
            return jsonify({'error': 'Missing file_id or file_name'}), 400
        task_id = str(uuid.uuid4())
        task_data = {'task_id': task_id, 'status': 'queued', 'download_progress': 0,
                     'created_at': time.time(), 'cancelled': False}
        save_task(task_id, task_data)
        def run():
            task = load_task(task_id)
            task['status'] = 'downloading'
            save_task(task_id, task)
            temp_path = None
            try:
                service = get_drive_service()
                request = service.files().get_media(fileId=file_id)
                final_name = _get_unique_filename(file_name)
                temp_path = os.path.join(UPLOAD_FOLDER, f"{task_id}_temp_{final_name}")
                with open(temp_path, 'wb') as f:
                    downloader = MediaIoBaseDownload(f, request, chunksize=10*1024*1024)
                    done = False
                    while not done:
                        status, done = downloader.next_chunk()
                        if status:
                            pct = int(status.progress() * 100)
                            task = load_task(task_id)
                            if task:
                                task['download_progress'] = pct
                                save_task(task_id, task)
                final_path = os.path.join(UPLOAD_FOLDER, final_name)
                os.rename(temp_path, final_path)
                task = load_task(task_id)
                task['status'] = 'done'
                task['output_file'] = final_name
                save_task(task_id, task)
            except Exception as e:
                logger.error(f"Drive download error: {e}")
                task = load_task(task_id)
                task['status'] = 'error'
                task['error_msg'] = str(e)
                save_task(task_id, task)
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)
        threading.Thread(target=run, daemon=True).start()
        return jsonify({'task_id': task_id})

    @app.route('/drive/delete/<file_id>', methods=['DELETE'])
    def drive_delete(file_id):
        try:
            service = get_drive_service()
            service.files().delete(fileId=file_id).execute()
            return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Drive delete error: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/colab_process', methods=['POST'])
    def colab_process():
        try:
            file_path = request.form.get('file_path')
            filename = request.form.get('filename')
            if file_path:
                if '..' in file_path or file_path.startswith('/'):
                    return jsonify({'error': 'Invalid path'}), 400
                full_path = os.path.join(UPLOAD_FOLDER, file_path)
                original_filename = os.path.basename(file_path)
            elif filename:
                full_path = os.path.join(UPLOAD_FOLDER, filename)
                original_filename = filename
            else:
                return jsonify({'error': 'Missing file_path or filename'}), 400
            if not os.path.exists(full_path):
                return jsonify({'error': 'File not found'}), 404
            if os.path.isdir(full_path):
                return jsonify({'error': 'Cannot process a directory'}), 400

            task_id = str(uuid.uuid4())
            task_data = {'task_id': task_id, 'status': 'queued', 'upload_progress': 0,
                         'download_progress': 0, 'created_at': time.time(), 'cancelled': False}
            save_task(task_id, task_data)
            threading.Thread(target=process_colab, args=(task_id, full_path, original_filename), daemon=True).start()
            return jsonify({'task_id': task_id})
        except Exception as e:
            logger.exception("Colab process error")
            return jsonify({'error': str(e)}), 500
