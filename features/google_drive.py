import os
import glob
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
from config import UPLOAD_FOLDER, DRIVE_FOLDER_ID, BASE_DIR

logger = logging.getLogger(__name__)


# ============================================================
# Multi-account token discovery + service cache
# ============================================================
def discover_accounts():
    """
    Return dict {account_name: token_path} by scanning token*.json
    in the project root.
      - token.json       → account name "default"
      - token_alice.json → account name "alice"
      - token_bob.json   → account name "bob"
    """
    out = {}
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in sorted(glob.glob(os.path.join(base, 'token*.json'))):
        name = os.path.splitext(os.path.basename(path))[0]
        if name == 'token':
            out['default'] = path
        elif name.startswith('token_'):
            out[name[len('token_'):]] = path
    return out


_service_cache = {}          # account_name -> service object
_email_cache = {}            # account_name -> email
_quota_cache = {}            # account_name -> (limit, usage)
_cache_lock = threading.Lock()


def _load_credentials(token_path):
    creds = Credentials.from_authorized_user_file(
        token_path, ['https://www.googleapis.com/auth/drive'])
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            try:
                with open(token_path, 'w') as f:
                    f.write(creds.to_json())
                logger.info(f"Refreshed token: {os.path.basename(token_path)}")
            except Exception as e:
                logger.warning(f"Could not persist refreshed token: {e}")
        else:
            raise Exception(f"Token invalid and cannot be refreshed: {token_path}")
    return creds


def get_drive_service(account='default'):
    """
    Return a Drive service for the given account name.
    Cached per account. Raises if account not found.
    """
    with _cache_lock:
        if account in _service_cache:
            return _service_cache[account]

    accounts = discover_accounts()
    if account not in accounts:
        raise Exception(
            f"Drive account '{account}' not found. "
            f"Available: {sorted(accounts.keys())}. "
            f"Add token_<name>.json to the project root."
        )

    token_path = accounts[account]
    creds = _load_credentials(token_path)
    service = build('drive', 'v3', credentials=creds, cache_discovery=False)

    # Fetch email once for display
    try:
        about = service.about().get(fields='user,storageQuota').execute()
        email = about.get('user', {}).get('emailAddress', 'unknown')
        q = about.get('storageQuota', {})
        _quota_cache[account] = (
            int(q.get('limit', 0) or 0),
            int(q.get('usage', 0) or 0),
        )
    except Exception:
        email = 'unknown'

    with _cache_lock:
        _service_cache[account] = service
        _email_cache[account] = email

    logger.info(f"Drive service loaded for account '{account}' ({email})")
    return service


def _request_account():
    """Read account name from query string or form. Defaults to 'default'."""
    acct = request.args.get('account') or request.form.get('account')
    return acct if acct else 'default'


def _format_size(size_bytes):
    try:
        size = int(size_bytes)
    except Exception:
        return "—"
    if size < 1024:    return f"{size} B"
    if size < 1024**2: return f"{size / 1024:.1f} KB"
    if size < 1024**3: return f"{size / (1024**2):.1f} MB"
    return f"{size / (1024**3):.2f} GB"


def _get_breadcrumbs(service, folder_id, root_id):
    if folder_id == root_id:
        return [{'id': root_id, 'name': 'My Drive / Root'}]
    crumbs = []
    current = folder_id
    visited = set()
    while current and current not in visited and len(crumbs) < 50:
        visited.add(current)
        try:
            meta = service.files().get(
                fileId=current, fields='id,name,parents').execute()
        except HttpError:
            break
        crumbs.insert(0, {'id': meta['id'], 'name': meta.get('name', 'Unknown')})
        parents = meta.get('parents', [])
        current = parents[0] if parents else None
    crumbs.insert(0, {'id': root_id, 'name': 'Root'})
    return crumbs


def _list_folder(service, folder_id):
    query = f"'{folder_id}' in parents and trashed = false"
    fields = ("files(id, name, size, modifiedTime, mimeType, "
              "parents, iconLink, webViewLink)")
    results = service.files().list(
        q=query, fields=fields, orderBy="folder, name", pageSize=1000
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

    parent_id = None
    if folder_id != DRIVE_FOLDER_ID:
        try:
            meta = service.files().get(fileId=folder_id, fields='parents').execute()
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


# ============================================================
# Colab upload job (Google Drive -> Drive av1 output)
# ============================================================
def process_colab(task_id, input_path, original_filename, account='default'):
    logger.info(f"Colab task {task_id}: input={input_path} account={account}")
    try:
        task = load_task(task_id)
        task['status'] = 'uploading'
        task['upload_progress'] = 0
        save_task(task_id, task)

        service = get_drive_service(account)
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
            query = (f"'{DRIVE_FOLDER_ID}' in parents and name = '{output_name}' "
                     f"and trashed = false")
            results = service.files().list(q=query, fields="files(id, name)").execute()
            files = results.get('files', [])
            if files:
                file_id = files[0]['id']
                request = service.files().get_media(fileId=file_id)
                with open(local_output, 'wb') as f:
                    downloader = MediaIoBaseDownload(f, request,
                                                     chunksize=10 * 1024 * 1024)
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
        if task:
            task['status'] = 'error'
            task['error_msg'] = str(e)
            save_task(task_id, task)


# ============================================================
# Routes
# ============================================================
def register_routes(app):

    @app.route('/drive/accounts')
    def drive_accounts():
        """
        List all available token_*.json accounts with email + quota.
        Optional ?probe=1 to fetch email for each (slower).
        """
        accounts = discover_accounts()
        probe = request.args.get('probe', '0') == '1'
        out = []
        for name in sorted(accounts.keys()):
            entry = {'name': name, 'email': None, 'free_bytes': None}
            if probe:
                try:
                    svc = get_drive_service(name)
                    entry['email'] = _email_cache.get(name)
                    limit, usage = _quota_cache.get(name, (0, 0))
                    if limit > 0:
                        entry['free_bytes'] = max(0, limit - usage)
                except Exception as e:
                    entry['error'] = str(e)
            out.append(entry)
        return jsonify({'accounts': out, 'default': out[0]['name'] if out else None})

    @app.route('/drive/list')
    def drive_list():
        account = _request_account()
        folder_id = request.args.get('folder_id', DRIVE_FOLDER_ID)

        if not folder_id or len(folder_id) < 10:
            return jsonify({'error': 'Invalid folder_id'}), 400

        try:
            service = get_drive_service(account)
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

            listing = _list_folder(service, folder_id)
            breadcrumbs = _get_breadcrumbs(service, folder_id, DRIVE_FOLDER_ID)

            return jsonify({
                'account': account,
                'account_email': _email_cache.get(account, 'unknown'),
                'current_folder_id': folder_id,
                'current_folder_name': meta.get('name', 'Root'),
                'parent_id': listing['parent_id'],
                'breadcrumbs': breadcrumbs,
                'folders': listing['folders'],
                'files': listing['files'],
                'is_root': folder_id == DRIVE_FOLDER_ID,
            })

        except Exception as e:
            logger.error(f"Drive list error [{account}]: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/drive/download_to_server', methods=['POST'])
    def drive_download_to_server():
        account = _request_account()
        file_id = request.form.get('file_id')
        file_name = request.form.get('file_name')
        if not file_id or not file_name:
            return jsonify({'error': 'Missing file_id or file_name'}), 400

        task_id = str(uuid.uuid4())
        task_data = {
            'task_id': task_id, 'status': 'queued', 'download_progress': 0,
            'created_at': time.time(), 'cancelled': False,
            'account': account,
        }
        save_task(task_id, task_data)

        def run():
            task = load_task(task_id)
            task['status'] = 'downloading'
            save_task(task_id, task)
            temp_path = None
            try:
                service = get_drive_service(account)
                request = service.files().get_media(fileId=file_id)
                final_name = _get_unique_filename(file_name)
                temp_path = os.path.join(UPLOAD_FOLDER,
                                          f"{task_id}_temp_{final_name}")
                with open(temp_path, 'wb') as f:
                    downloader = MediaIoBaseDownload(f, request,
                                                     chunksize=10 * 1024 * 1024)
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
                logger.error(f"Drive download error [{account}]: {e}")
                task = load_task(task_id)
                task['status'] = 'error'
                task['error_msg'] = str(e)
                save_task(task_id, task)
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)

        threading.Thread(target=run, daemon=True).start()
        return jsonify({'task_id': task_id, 'account': account})

    @app.route('/drive/delete/<file_id>', methods=['DELETE'])
    def drive_delete(file_id):
        account = _request_account()
        try:
            service = get_drive_service(account)
            service.files().delete(fileId=file_id).execute()
            return jsonify({'success': True, 'account': account})
        except Exception as e:
            logger.error(f"Drive delete error [{account}]: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/colab_process', methods=['POST'])
    def colab_process():
        account = _request_account()
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
                'download_progress': 0, 'created_at': time.time(),
                'cancelled': False, 'account': account,
            }
            save_task(task_id, task_data)
            threading.Thread(
                target=process_colab,
                args=(task_id, full_path, original_filename, account),
                daemon=True
            ).start()
            return jsonify({'task_id': task_id, 'account': account})
        except Exception as e:
            logger.exception("Colab process error")
            return jsonify({'error': str(e)}), 500
