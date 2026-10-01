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
    """Authenticate and return Drive service."""
    try:
        creds = None
        if os.path.exists(TOKEN_FILE):
            try:
                creds = Credentials.from_authorized_user_file(
                    TOKEN_FILE, ['https://www.googleapis.com/auth/drive']
                )
            except Exception as e:
                logger.error(f"Failed to read token.json: {e}")
                raise Exception("Google Drive token file is corrupt.")

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    with open(TOKEN_FILE, 'w') as token:
                        token.write(creds.to_json())
                    logger.info("Google Drive token refreshed")
                except Exception as e:
                    logger.error(f"Token refresh failed: {e}")
                    raise Exception("Google Drive token expired. Please re-authorize.")
            else:
                raise Exception("token.json missing or invalid. Please re-authorize.")

        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        logger.error(f"Drive service error: {e}")
        raise


def _format_size(size_bytes):
    try:
        size = int(size_bytes)
    except Exception:
        return "—"
    if size < 1024:
        return f"{size} B"
    if size < 1024 ** 2:
        return f"{size / 1024:.1f} KB"
    if size < 1024 ** 3:
        return f"{size / (1024 ** 2):.1f} MB"
    return f"{size / (1024 ** 3):.2f} GB"


def _get_breadcrumbs(service, folder_id, root_id):
    """
    Build breadcrumb trail from root to current folder.
    Returns list of {id, name} from root down to current folder.
    """
    if folder_id == root_id:
        return [{'id': root_id, 'name': 'My Drive / Root'}]

    crumbs = []
    current = folder_id
    visited = set()  # prevent infinite loops from circular parents

    while current and current not in visited and len(crumbs) < 50:
        visited.add(current)
        try:
            meta = service.files().get(
                fileId=current, fields='id,name,parents'
            ).execute()
        except HttpError:
            break

        crumbs.insert(0, {'id': meta['id'], 'name': meta.get('name', 'Unknown')})
        parents = meta.get('parents', [])
        current = parents[0] if parents else None

    # Add root at the top
    crumbs.insert(0, {'id': root_id, 'name': 'Root'})
    return crumbs


def _list_folder(service, folder_id):
    """
    List files AND subfolders in a folder.
    Returns dict with files, folders, parent_id.
    """
    query = f"'{folder_id}' in parents and trashed = false"
    fields = ("files(id, name, size, modifiedTime, mimeType, "
              "parents, iconLink, webViewLink)")

    results = service.files().list(
        q=query,
        fields=fields,
        orderBy="folder, name",
        pageSize=1000
    ).execute()

    all_items = results.get('files', [])

    folders = []
    files = []
    FOLDER_MIME = 'application/vnd.google-apps.folder'

    for item in all_items:
        if item.get('mimeType') == FOLDER_MIME:
            folders.append({
                'id': item['id'],
                'name': item['name'],
                'modifiedTime': item.get('modifiedTime'),
                'is_folder': True,
            })
        else:
            size_raw = item.get('size', 0)
            files.append({
                'id': item['id'],
                'name': item['name'],
                'size': size_raw,
                'size_str': _format_size(size_raw),
                'mimeType': item.get('mimeType'),
                'modifiedTime': item.get('modifiedTime'),
                'is_folder': False,
            })

    # Get parent folder ID (for "up" button)
    parent_id = None
    if folder_id != DRIVE_FOLDER_ID:
        try:
            meta = service.files().get(
                fileId=folder_id, fields='parents'
            ).execute()
            parents = meta.get('parents', [])
            parent_id = parents[0] if parents else None
        except HttpError:
            pass

    return {
        'folders': folders,
        'files': files,
        'parent_id': parent_id,
    }


def _get_unique_filename(filename):
    base, ext = os.path.splitext(filename)
    counter = 1
    new_name = filename
    while os.path.exists(os.path.join(UPLOAD_FOLDER, new_name)):
        new_name = f"{base}_{counter}{ext}"
        counter += 1
    return new_name


def process_colab(task_id, input_path, original_filename):
    logger.info(f"Colab task {task_id}: input={input_path}")
    try:
        task = load_task(task_id)
        task['status'] = 'uploading'
        task['upload_progress'] = 0
        save_task(task_id, task)

        service = get_drive_service()
        file_metadata = {'name': original_filename, 'parents': [DRIVE_FOLDER_ID]}
        media = MediaFileUpload(input_path, resumable=True, chunksize=10 * 1024 * 1024)
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
                    downloader = MediaIoBaseDownload(f, request, chunksize=10 * 1024 * 1024)
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


def register_routes(app):

    @app.route('/drive/list')
    def drive_list():
        """
        List files + folders in a Drive folder.
        Query param: ?folder_id=xxx (defaults to root DRIVE_FOLDER_ID)
        """
        folder_id = request.args.get('folder_id', DRIVE_FOLDER_ID)

        # Validate: must be a folder ID (basic check)
        if not folder_id or len(folder_id) < 10:
            return jsonify({'error': 'Invalid folder_id'}), 400

        try:
            service = get_drive_service()

            # Get the current folder's metadata (to validate it exists)
            try:
                meta = service.files().get(
                    fileId=folder_id,
                    fields='id,name,mimeType,parents'
                ).execute()
            except HttpError as e:
                if e.resp.status == 404:
                    return jsonify({'error': 'Folder not found'}), 404
                raise

            if meta.get('mimeType') != 'application/vnd.google-apps.folder':
                return jsonify({'error': 'Not a folder'}), 400

            # List contents
            listing = _list_folder(service, folder_id)

            # Build breadcrumbs
            breadcrumbs = _get_breadcrumbs(service, folder_id, DRIVE_FOLDER_ID)

            return jsonify({
                'current_folder_id': folder_id,
                'current_folder_name': meta.get('name', 'Root'),
                'parent_id': listing['parent_id'],
                'breadcrumbs': breadcrumbs,
                'folders': listing['folders'],
                'files': listing['files'],
                'is_root': folder_id == DRIVE_FOLDER_ID,
            })

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
        task_data = {
            'task_id': task_id, 'status': 'queued', 'download_progress': 0,
            'created_at': time.time(), 'cancelled': False
        }
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
                    downloader = MediaIoBaseDownload(f, request, chunksize=10 * 1024 * 1024)
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
            task_data = {
                'task_id': task_id, 'status': 'queued', 'upload_progress': 0,
                'download_progress': 0, 'created_at': time.time(), 'cancelled': False
            }
            save_task(task_id, task_data)
            threading.Thread(
                target=process_colab,
                args=(task_id, full_path, original_filename),
                daemon=True
            ).start()
            return jsonify({'task_id': task_id})
        except Exception as e:
            logger.exception("Colab process error")
            return jsonify({'error': str(e)}), 500
