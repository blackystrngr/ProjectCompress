# features/google_drive.py
import os
import glob
import json
import time
import uuid
import threading
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
# Thread-local Drive service cache
# ============================================================
_thread_local = threading.local()

_service_generation = 0
_generation_lock = threading.Lock()


def _bump_service_generation():
    global _service_generation
    with _generation_lock:
        _service_generation += 1


def _current_generation():
    with _generation_lock:
        return _service_generation


_cred_lock = threading.Lock()

_email_cache = {}
_quota_cache = {}
_colab_folder_cache = {}
_cache_lock = threading.Lock()


# ============================================================
# Coordinator: processed list + claims
# ============================================================
_processed_cache = {'names': set(), 'ts': 0, 'lock': threading.Lock()}
_processed_cache_ttl = 30

_claims = {}
_claims_lock = threading.Lock()
_claim_ttl = 4 * 3600
_min_output_size = 1024


def _purge_stale_claims():
    now = time.time()
    with _claims_lock:
        stale = [k for k, v in _claims.items() if now - v['ts'] > _claim_ttl]
        for k in stale:
            del _claims[k]


def _snapshot_claims():
    now = time.time()
    with _claims_lock:
        return {
            k: {'account': v['account'], 'age_sec': int(now - v['ts'])}
            for k, v in _claims.items()
        }


# ============================================================
# Token discovery
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


def _load_credentials(token_path):
    with _cred_lock:
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
    """Return a per-thread Drive service for the account."""
    if not hasattr(_thread_local, 'services'):
        _thread_local.services = {}
        _thread_local.generation = -1

    if _thread_local.generation != _current_generation():
        _thread_local.services.clear()
        _thread_local.generation = _current_generation()

    if account in _thread_local.services:
        return _thread_local.services[account]

    accounts = discover_accounts()
    if account not in accounts:
        raise Exception(
            f"Drive account '{account}' not found or invalid. "
            f"Available: {sorted(accounts.keys())}. "
            f"Add a valid token_<name>.json to the project root."
        )

    token_path = accounts[account]
    creds = _load_credentials(token_path)
    service = build('drive', 'v3', credentials=creds, cache_discovery=False)
    _thread_local.services[account] = service

    try:
        about = service.about().get(fields='user,storageQuota').execute()
        email = about.get('user', {}).get('emailAddress', 'unknown')
        q = about.get('storageQuota', {})
        with _cache_lock:
            _email_cache[account] = email
            _quota_cache[account] = (
                int(q.get('limit', 0) or 0),
                int(q.get('usage', 0) or 0),
            )
    except Exception as e:
        logger.warning(f"About fetch failed for {account}: {e}")

    logger.info(f"Drive service built (thread={threading.get_ident()}, account='{account}')")
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


def _escape_drive(s):
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _get_breadcrumbs(service, folder_id):
    if folder_id == MY_DRIVE_ROOT:
        return [{'id': MY_DRIVE_ROOT, 'name': 'My Drive'}]

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

    crumbs.insert(0, {'id': MY_DRIVE_ROOT, 'name': 'My Drive'})
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
    if folder_id != MY_DRIVE_ROOT:
        try:
            meta = service.files().get(fileId=folder_id,
                                        fields='parents').execute()
            parents = meta.get('parents', [])
            parent_id = parents[0] if parents else MY_DRIVE_ROOT
        except HttpError:
            pass

    return {'folders': folders, 'files': files, 'parent_id': parent_id}


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
        q = (f"name = '{_escape_drive(name)}' and "
             f"mimeType = 'application/vnd.google-apps.folder' and "
             f"trashed = false")
        if parent_id:
            q += f" and '{parent_id}' in parents"

        res = service.files().list(
            q=q, fields="files(id,name)",
            pageSize=10,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()

        if res.get('files'):
            return res['files'][0]['id']

        body = {'name': name, 'mimeType': 'application/vnd.google-apps.folder'}
        if parent_id:
            body['parents'] = [parent_id]
        folder = service.files().create(
            body=body, fields='id', supportsAllDrives=True,
        ).execute()
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


def get_colab_output_folder_id(account='default'):
    try:
        svc = get_drive_service(account)
        root_id = _find_or_create_folder(svc, COLAB_FOLDER_NAME)
        if not root_id:
            return None
        return _find_or_create_folder(svc, COLAB_OUTPUT_SUBFOLDER, root_id)
    except Exception as e:
        logger.error(f"Output folder resolve failed for {account}: {e}")
        return None


def get_all_processed_outputs(force_refresh=False):
    with _processed_cache['lock']:
        now = time.time()
        if (not force_refresh
                and _processed_cache['names']
                and now - _processed_cache['ts'] < _processed_cache_ttl):
            return _processed_cache['names']

    names = set()
    for account in discover_accounts().keys():
        try:
            svc = get_drive_service(account)
            output_id = get_colab_output_folder_id(account)
            if not output_id:
                continue
            page_token = None
            while True:
                res = svc.files().list(
                    q=f"'{output_id}' in parents and trashed = false",
                    fields="nextPageToken, files(name,size)",
                    pageSize=200, pageToken=page_token,
                ).execute()
                for f in res.get('files', []):
                    if int(f.get('size', 0) or 0) >= _min_output_size:
                        n = f.get('name', '')
                        if n:
                            names.add(n)
                page_token = res.get('nextPageToken')
                if not page_token:
                    break
        except Exception as e:
            logger.warning(f"processed scan failed for {account}: {e}")

    with _processed_cache['lock']:
        _processed_cache['names'] = names
        _processed_cache['ts'] = time.time()
    return names


# ============================================================
# Colab upload job
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
            query = (f"'{colab_folder}' in parents and name = '{output_name}' "
                     f"and trashed = false")
            results = service.files().list(
                q=query, fields="files(id, name)").execute()
            files = results.get('files', [])
            if files:
                file_id = files[0]['id']
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
            with _cache_lock:
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
                    meta = service.files().get(
                        fileId=folder_id, fields='id,name,mimeType'
                    ).execute()
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
                request = service.files().get_media(fileId=file_id)
                final_name = _get_unique_filename(file_name)
                temp_path = os.path.join(UPLOAD_FOLDER,
                                          f"{task_id}_temp_{final_name}")
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
            service.files().delete(fileId=file_id).execute()
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
                f'Cannot resolve/create Colab_Processing for {account}'}), 500

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

    # ---- Coordinator endpoints ----
    @app.route('/processed_list', methods=['GET'])
    def processed_list():
        token = request.headers.get('X-Upload-Token', '')
        expected = os.environ.get('COOKIE_UPLOAD_TOKEN', 'whyyouleftme')
        if not token or token != expected:
            return jsonify({'error': 'Unauthorized'}), 401

        _purge_stale_claims()
        force = request.args.get('force', '0') == '1'
        names = get_all_processed_outputs(force_refresh=force)
        return jsonify({
            'processed': sorted(names),
            'in_progress': _snapshot_claims(),
            'generated_at': time.time(),
        })

    @app.route('/mark_processing/<path:video_name>', methods=['POST'])
    def mark_processing(video_name):
        token = request.headers.get('X-Upload-Token', '')
        expected = os.environ.get('COOKIE_UPLOAD_TOKEN', 'whyyouleftme')
        if not token or token != expected:
            return jsonify({'error': 'Unauthorized'}), 401

        account = request.form.get('account', 'unknown')
        expected_output = request.form.get('expected_output', '')

        _purge_stale_claims()
        now = time.time()

        if expected_output:
            processed = get_all_processed_outputs()
            if expected_output in processed:
                return jsonify({'status': 'already_done', 'output': expected_output})

        with _claims_lock:
            existing = _claims.get(video_name)
            if existing:
                return jsonify({
                    'status': 'in_progress',
                    'by': existing['account'],
                    'age_sec': int(now - existing['ts']),
                })
            _claims[video_name] = {'account': account, 'ts': now}
            return jsonify({'status': 'ok', 'claimed_by': account})

    @app.route('/mark_done/<path:video_name>', methods=['POST'])
    def mark_done(video_name):
        token = request.headers.get('X-Upload-Token', '')
        expected = os.environ.get('COOKIE_UPLOAD_TOKEN', 'whyyouleftme')
        if not token or token != expected:
            return jsonify({'error': 'Unauthorized'}), 401

        with _claims_lock:
            _claims.pop(video_name, None)
        with _processed_cache['lock']:
            _processed_cache['ts'] = 0
        return jsonify({'status': 'ok'})

    @app.route('/mark_failed/<path:video_name>', methods=['POST'])
    def mark_failed(video_name):
        token = request.headers.get('X-Upload-Token', '')
        expected = os.environ.get('COOKIE_UPLOAD_TOKEN', 'whyyouleftme')
        if not token or token != expected:
            return jsonify({'error': 'Unauthorized'}), 401

        with _claims_lock:
            _claims.pop(video_name, None)
        return jsonify({'status': 'ok'})
