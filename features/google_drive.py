# features/google_drive.py
import os
import glob
import json
import uuid
import threading
import time
import logging
from flask import request, jsonify
from werkzeug.utils import secure_filename
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from googleapiclient.errors import HttpError
from tasks import save_task, load_task
from config import UPLOAD_FOLDER

logger = logging.getLogger(__name__)

MY_DRIVE_ROOT = 'root'
COLAB_FOLDER_NAME = "Colab_Processing"
COLAB_OUTPUT_SUBFOLDER = "Processed"

# ============================================================
# GLOBAL DRIVE I/O LOCK (reentrant)
# ------------------------------------------------------------
# googleapiclient + httplib2 are not thread-safe. Any concurrent
# call to service.*.execute() can corrupt the heap or crash inside
# OpenSSL. All Drive calls — list, get, upload, download — are
# serialized through this reentrant lock. Reentrant so a call site
# that already holds it can call helper functions that also lock.
# ============================================================
_drive_io_lock = threading.RLock()


def _locked(fn):
    """Run fn() while holding the global Drive lock."""
    with _drive_io_lock:
        return fn()


# ============================================================
# Token discovery + validity
# ============================================================
def discover_accounts():
    out = {}
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in sorted(glob.glob(os.path.join(base, 'token*.json'))):
        name = os.path.splitext(os.path.basename(path))[0]
        if name == 'token':
            key = 'default'
        elif name.startswith('token_'):
            key = name[len('token_'):]
        else:
            continue
        if _token_file_valid(path):
            out[key] = path
    return out


def _token_file_valid(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return False
        if {'client_id', 'client_secret', 'refresh_token'}.issubset(data.keys()):
            return True
        if {'type', 'private_key', 'client_email'}.issubset(data.keys()):
            return True
        return False
    except Exception:
        return False


_service_cache = {}
_email_cache = {}
_quota_cache = {}
_colab_folder_cache = {}
_cache_lock = threading.Lock()


def _load_credentials(token_path):
    # Network call when refresh is needed — must run under the
    # Drive lock to avoid concurrent refresh on the same creds.
    with _drive_io_lock:
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
    with _cache_lock:
        if account in _service_cache:
            return _service_cache[account]

    accounts = discover_accounts()
    if account not in accounts:
        raise Exception(
            f"Drive account '{account}' not found or invalid. "
            f"Available: {sorted(accounts.keys())}."
        )

    token_path = accounts[account]

    with _drive_io_lock:
        # Re-check inside the lock in case another thread just built it
        with _cache_lock:
            if account in _service_cache:
                return _service_cache[account]

        creds = _load_credentials(token_path)
        service = build('drive', 'v3', credentials=creds, cache_discovery=False)

        try:
            about = service.about().get(fields='user,storageQuota').execute()
            email = about.get('user', {}).get('emailAddress', 'unknown')
            q = about.get('storageQuota', {})
            _quota_cache[account] = (
                int(q.get('limit', 0) or 0),
                int(q.get('usage', 0) or 0),
            )
        except Exception as e:
            logger.warning(f"Quota fetch failed for {account}: {e}")
            email = 'unknown'

        with _cache_lock:
            _service_cache[account] = service
            _email_cache[account] = email

    logger.info(f"Drive service loaded for account '{account}' ({email})")
    return service


def get_account_email(account='default'):
    with _cache_lock:
        if account in _email_cache:
            return _email_cache[account]
    try:
        get_drive_service(account)
    except Exception:
        return 'unknown'
    with _cache_lock:
        return _email_cache.get(account, 'unknown')


def _request_account():
    requested = (request.args.get('account')
                 or request.form.get('account')
                 or '').strip()

    accounts = discover_accounts()
    if not accounts:
        raise Exception("No valid Drive accounts found.")

    if requested and requested in accounts:
        return requested

    last_err = None
    for name in sorted(accounts.keys()):
        try:
            get_drive_service(name)
            return name
        except Exception as e:
            last_err = e
            continue

    raise Exception(f"No Drive account could be loaded. Last error: {last_err}")


def _format_size(size_bytes):
    try:
        size = int(size_bytes)
    except Exception:
        return "—"
    if size < 1024:    return f"{size} B"
    if size < 1024**2: return f"{size / 1024:.1f} KB"
    if size < 1024**3: return f"{size / (1024**2):.1f} MB"
    return f"{size / (1024**3):.2f} GB"


def _get_breadcrumbs(service, folder_id):
    if folder_id == MY_DRIVE_ROOT:
        return [{'id': MY_DRIVE_ROOT, 'name': 'My Drive'}]

    crumbs = []
    current = folder_id
    visited = set()

    while current and current not in visited and len(crumbs) < 50:
        visited.add(current)
        try:
            meta = _locked(lambda: service.files().get(
                fileId=current, fields='id,name,parents').execute())
        except HttpError:
            break
        crumbs.insert(0, {'id': meta['id'], 'name': meta.get('name', 'Unknown')})
        parents = meta.get('parents', [])
        current = parents[0] if parents else None

    crumbs.insert(0, {'id': MY_DRIVE_ROOT, 'name': 'My Drive'})
    return crumbs


def _list_folder(service, folder_id):
    query = f"'{folder_id}' in parents and trashed = false"
    fields = ("files(id, name, size, modifiedTime, mimeType, "
              "parents, iconLink, webViewLink)")
    results = _locked(lambda: service.files().list(
        q=query, fields=fields, orderBy="folder, name", pageSize=1000
    ).execute())

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
    if folder_id != MY_DRIVE_ROOT:
        try:
            meta = _locked(lambda: service.files().get(
                fileId=folder_id, fields='parents').execute())
            parents = meta.get('parents', [])
            parent_id = parents[0] if parents else MY_DRIVE_ROOT
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


def _find_or_create_folder(service, name, parent_id=None):
    try:
        escaped = name.replace("'", "\\'")
        q = (f"name = '{escaped}' and "
             f"mimeType = 'application/vnd.google-apps.folder' and "
             f"trashed = false")
        if parent_id:
            q += f" and '{parent_id}' in parents"

        res = _locked(lambda: service.files().list(
            q=q, fields="files(id,name)",
            pageSize=10,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute())

        if res.get('files'):
            return res['files'][0]['id']

        body = {'name': name, 'mimeType': 'application/vnd.google-apps.folder'}
        if parent_id:
            body['parents'] = [parent_id]
        folder = _locked(lambda: service.files().create(
            body=body, fields='id',
            supportsAllDrives=True,
        ).execute())
        logger.info(f"Created Drive folder '{name}' (id={folder['id']})")
        return folder['id']
    except Exception as e:
        logger.error(f"Folder resolve failed for '{name}': {e}")
        return None


def get_colab_folder_id(account='default'):
    with _cache_lock:
        if account in _colab_folder_cache:
            return _colab_folder_cache[account]

    try:
        svc = get_drive_service(account)
        fid = _find_or_create_folder(svc, COLAB_FOLDER_NAME)
        if fid:
            with _cache_lock:
                _colab_folder_cache[account] = fid
        return fid
    except Exception as e:
        logger.error(f"Could not resolve Colab folder for {account}: {e}")
        return None


# ============================================================
# Colab upload job — lock only around actual Drive calls
# ============================================================
def process_colab(task_id, input_path, original_filename, account):
    logger.info(f"Colab task {task_id}: input={input_path} account={account}")
    try:
        task = load_task(task_id)
        task['status'] = 'uploading'
        task['upload_progress'] = 0
        task['account'] = account
        save_task(task_id, task)

        service = get_drive_service(account)
        colab_folder = get_colab_folder_id(account)
        if not colab_folder:
            raise Exception(f"Cannot resolve Colab_Processing for {account}")

        # ---- Upload the source file (lock held for the whole upload) ----
        with _drive_io_lock:
            file_metadata = {'name': original_filename, 'parents': [colab_folder]}
            media = MediaFileUpload(input_path, resumable=True,
                                    chunksize=10 * 1024 * 1024)
            request = service.files().create(
                body=file_metadata, media_body=media, fields='id')
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    pct = int(status.progress() * 100)
                    t = load_task(task_id)
                    if t:
                        t['upload_progress'] = pct
                        save_task(task_id, t)

        task = load_task(task_id)
        task['status'] = 'waiting_colab'
        task['upload_progress'] = 100
        task['download_progress'] = 0
        save_task(task_id, task)

        base, ext = os.path.splitext(original_filename)
        output_name = f"{base}_av1{ext}"
        local_output = os.path.join(UPLOAD_FOLDER,
                                     f"{task_id}_temp_colab{ext}")
        start_time = time.time()
        timeout = 7200

        while time.time() - start_time < timeout:
            time.sleep(15)

            # Poll for output (short lock)
            query = (f"'{colab_folder}' in parents and name = '{output_name}' "
                     f"and trashed = false")
            results = _locked(lambda: service.files().list(
                q=query, fields="files(id, name)").execute())
            files = results.get('files', [])

            if files:
                file_id = files[0]['id']
                # Download output + delete it, both under the lock
                with _drive_io_lock:
                    request = service.files().get_media(fileId=file_id)
                    with open(local_output, 'wb') as f:
                        downloader = MediaIoBaseDownload(
                            f, request, chunksize=10 * 1024 * 1024)
                        done = False
                        while not done:
                            status, done = downloader.next_chunk()
                            if status:
                                pct = int(status.progress() * 100)
                                t = load_task(task_id)
                                if t:
                                    t['download_progress'] = pct
                                    save_task(task_id, t)
                    service.files().delete(fileId=file_id).execute()

                final_name = _get_unique_filename(output_name)
                final_path = os.path.join(UPLOAD_FOLDER, final_name)
                os.rename(local_output, final_path)
                task = load_task(task_id)
                task['status'] = 'done'
                task['output_file'] = final_name
                save_task(task_id, task)
                return

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
        accounts = discover_accounts()
        out = []
        for name in sorted(accounts.keys()):
            entry = {'name': name, 'email': None, 'free_bytes': None}
            try:
                get_drive_service(name)
            except Exception as e:
                logger.warning(f"Skipping broken Drive account '{name}': {e}")
                continue
            entry['email'] = _email_cache.get(name)
            limit, usage = _quota_cache.get(name, (0, 0))
            if limit > 0:
                entry['free_bytes'] = max(0, limit - usage)
            out.append(entry)

        return jsonify({
            'accounts': out,
            'default': out[0]['name'] if out else None,
        })

    @app.route('/drive/list')
    def drive_list():
        try:
            account = _request_account()
        except Exception as e:
            return jsonify({'error': str(e)}), 500

        folder_id = request.args.get('folder_id') or MY_DRIVE_ROOT

        try:
            service = get_drive_service(account)

            if folder_id != MY_DRIVE_ROOT:
                try:
                    meta = _locked(lambda: service.files().get(
                        fileId=folder_id,
                        fields='id,name,mimeType'
                    ).execute())
                except HttpError as e:
                    if e.resp.status == 404:
                        return jsonify({'error': 'Folder not found'}), 404
                    raise
                if meta.get('mimeType') != 'application/vnd.google-apps.folder':
                    return jsonify({'error': 'Not a folder'}), 400
                folder_name = meta.get('name', 'Folder')
            else:
                folder_name = 'My Drive'

            listing = _list_folder(service, folder_id)
            breadcrumbs = _get_breadcrumbs(service, folder_id)

            colab_id = None
            try:
                colab_id = get_colab_folder_id(account)
            except Exception:
                pass

            return jsonify({
                'account': account,
                'account_email': get_account_email(account),
                'current_folder_id': folder_id,
                'current_folder_name': folder_name,
                'parent_id': listing['parent_id'],
                'breadcrumbs': breadcrumbs,
                'folders': listing['folders'],
                'files': listing['files'],
                'is_root': folder_id == MY_DRIVE_ROOT,
                'colab_folder_id': colab_id,
            })

        except Exception as e:
            logger.error(f"Drive list error [{account}]: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/drive/download_to_server', methods=['POST'])
    def drive_download_to_server():
        try:
            account = _request_account()
        except Exception as e:
            return jsonify({'error': str(e)}), 500

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
                final_name = _get_unique_filename(file_name)
                temp_path = os.path.join(UPLOAD_FOLDER,
                                          f"{task_id}_temp_{final_name}")

                with _drive_io_lock:
                    request = service.files().get_media(fileId=file_id)
                    with open(temp_path, 'wb') as f:
                        downloader = MediaIoBaseDownload(
                            f, request, chunksize=10 * 1024 * 1024)
                        done = False
                        while not done:
                            status, done = downloader.next_chunk()
                            if status:
                                pct = int(status.progress() * 100)
                                t = load_task(task_id)
                                if t:
                                    t['download_progress'] = pct
                                    save_task(task_id, t)

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
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass

        threading.Thread(target=run, daemon=True).start()
        return jsonify({'task_id': task_id, 'account': account})

    @app.route('/drive/delete/<file_id>', methods=['DELETE'])
    def drive_delete(file_id):
        try:
            account = _request_account()
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        try:
            service = get_drive_service(account)
            _locked(lambda: service.files().delete(fileId=file_id).execute())
            return jsonify({'success': True, 'account': account})
        except Exception as e:
            logger.error(f"Drive delete error [{account}]: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/drive/upload', methods=['POST'])
    def drive_upload():
        try:
            account = _request_account()
        except Exception as e:
            return jsonify({'error': str(e)}), 500

        if 'file' not in request.files:
            return jsonify({'error': 'No file'}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'Empty filename'}), 400

        safe_name = secure_filename(file.filename)
        if not safe_name:
            return jsonify({'error': 'Invalid filename'}), 400

        target_folder = get_colab_folder_id(account)
        if not target_folder:
            return jsonify({'error':
                f'Cannot resolve/create Colab_Processing for '
                f'{account}'}), 500

        temp_path = os.path.join(UPLOAD_FOLDER,
                                  f"_upload_{uuid.uuid4().hex}_{safe_name}")
        try:
            file.save(temp_path)
        except Exception as e:
            return jsonify({'error': f'Save failed: {e}'}), 500

        task_id = str(uuid.uuid4())
        task_data = {
            'task_id': task_id, 'status': 'queued', 'upload_progress': 0,
            'created_at': time.time(), 'cancelled': False,
            'account': account, 'target_folder': target_folder,
            'target_folder_name': COLAB_FOLDER_NAME,
        }
        save_task(task_id, task_data)

        def run():
            task = load_task(task_id)
            task['status'] = 'uploading'
            save_task(task_id, task)
            try:
                service = get_drive_service(account)

                with _drive_io_lock:
                    file_metadata = {
                        'name': safe_name,
                        'parents': [target_folder],
                    }
                    media = MediaFileUpload(temp_path, resumable=True,
                                            chunksize=10 * 1024 * 1024)
                    request = service.files().create(
                        body=file_metadata, media_body=media,
                        fields='id,name,size')
                    response = None
                    while response is None:
                        status, response = request.next_chunk()
                        if status:
                            pct = int(status.progress() * 100)
                            t = load_task(task_id)
                            if t:
                                t['upload_progress'] = pct
                                save_task(task_id, t)

                task = load_task(task_id)
                task['status'] = 'done'
                task['upload_progress'] = 100
                task['drive_file_id'] = response.get('id')
                task['drive_file_name'] = response.get('name')
                task['drive_folder_id'] = target_folder
                save_task(task_id, task)
            except Exception as e:
                logger.exception("Drive upload failed")
                task = load_task(task_id)
                task['status'] = 'error'
                task['error_msg'] = str(e)
                save_task(task_id, task)
            finally:
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except Exception:
                    pass

        threading.Thread(target=run, daemon=True).start()
        return jsonify({
            'task_id': task_id,
            'account': account,
            'target_folder': target_folder,
            'target_folder_name': COLAB_FOLDER_NAME,
        })

    @app.route('/colab_process', methods=['POST'])
    def colab_process():
        try:
            account = _request_account()
        except Exception as e:
            logger.warning(f"colab_process: cannot resolve account: {e}")
            return jsonify({'error': str(e)}), 500

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
