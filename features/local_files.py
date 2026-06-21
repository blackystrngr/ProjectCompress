
import os
import re
import shutil
import logging
from flask import request, jsonify, send_file, abort, Response
from config import UPLOAD_FOLDER

logger = logging.getLogger(__name__)

def get_file_icon(filename):
    ext = os.path.splitext(filename)[1].lower()
    if ext in ['.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.m4v']:
        return 'fas fa-video'
    elif ext in ['.mp3', '.wav', '.flac', '.aac', '.ogg']:
        return 'fas fa-music'
    elif ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']:
        return 'fas fa-image'
    elif ext in ['.pdf']:
        return 'fas fa-file-pdf'
    elif ext in ['.zip', '.rar', '.7z', '.tar', '.gz']:
        return 'fas fa-file-archive'
    elif ext in ['.txt', '.md', '.log']:
        return 'fas fa-file-alt'
    else:
        return 'fas fa-file'

def get_file_size_str(size):
    if size < 1024:
        return f"{size} B"
    elif size < 1024*1024:
        return f"{size/1024:.1f} KB"
    elif size < 1024*1024*1024:
        return f"{size/(1024*1024):.1f} MB"
    else:
        return f"{size/(1024*1024*1024):.2f} GB"

def list_directory(path, relative_path=""):
    items = []
    try:
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            if os.path.isdir(full):
                items.append({
                    'name': name,
                    'type': 'directory',
                    'icon': 'fas fa-folder',
                    'path': os.path.join(relative_path, name) if relative_path else name
                })
            else:
                size = os.path.getsize(full)
                items.append({
                    'name': name,
                    'type': 'file',
                    'icon': get_file_icon(name),
                    'size_str': get_file_size_str(size),
                    'size': size,
                    'path': os.path.join(relative_path, name) if relative_path else name
                })
    except Exception as e:
        logger.error(f"Error listing {path}: {e}")
    return items

def get_range_response(file_path, range_header):
    file_size = os.path.getsize(file_path)
    start = 0
    end = file_size - 1
    if range_header:
        match = re.search(r'bytes=(\d+)-(\d*)', range_header)
        if match:
            start = int(match.group(1))
            if match.group(2):
                end = int(match.group(2))
    length = end - start + 1
    with open(file_path, 'rb') as f:
        f.seek(start)
        data = f.read(length)
    return Response(data, 206, mimetype='video/mp4',
                    headers={'Content-Range': f'bytes {start}-{end}/{file_size}'})

def register_routes(app):
    @app.route('/browse', methods=['GET'])
    def browse():
        subpath = request.args.get('path', '')
        if '..' in subpath or subpath.startswith('/'):
            return jsonify({'error': 'Invalid path'}), 400
        full_path = os.path.join(UPLOAD_FOLDER, subpath)
        if not os.path.exists(full_path):
            return jsonify({'error': 'Path not found'}), 404
        if not os.path.isdir(full_path):
            return jsonify({'error': 'Not a directory'}), 400
        items = list_directory(full_path, subpath)
        breadcrumbs = []
        if subpath:
            parts = subpath.split(os.sep)
            current = ""
            for i, part in enumerate(parts):
                current = os.path.join(current, part) if current else part
                breadcrumbs.append({
                    'name': part,
                    'path': current,
                    'is_last': i == len(parts)-1
                })
        return jsonify({
            'current_path': subpath,
            'breadcrumbs': breadcrumbs,
            'items': items
        })

    @app.route('/download_file', methods=['GET'])
    def download_file():
        file_path = request.args.get('path', '')
        if '..' in file_path or file_path.startswith('/'):
            abort(403)
        full = os.path.join(UPLOAD_FOLDER, file_path)
        if not os.path.exists(full) or os.path.isdir(full):
            abort(404)
        return send_file(full, as_attachment=True, download_name=os.path.basename(full))

    @app.route('/delete_file', methods=['DELETE'])
    def delete_file():
        file_path = request.args.get('path', '')
        if '..' in file_path or file_path.startswith('/'):
            return jsonify({'error': 'Invalid path'}), 400
        full = os.path.join(UPLOAD_FOLDER, file_path)
        if not os.path.exists(full):
            return jsonify({'error': 'Not found'}), 404
        try:
            if os.path.isdir(full):
                if not os.listdir(full):
                    os.rmdir(full)
                else:
                    return jsonify({'error': 'Directory not empty'}), 400
            else:
                os.remove(full)
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/delete_folder', methods=['DELETE'])
    def delete_folder():
        folder_path = request.args.get('path', '')
        if '..' in folder_path or folder_path.startswith('/'):
            return jsonify({'error': 'Invalid path'}), 400
        full = os.path.join(UPLOAD_FOLDER, folder_path)
        if not os.path.exists(full) or not os.path.isdir(full):
            return jsonify({'error': 'Folder not found'}), 404
        try:
            shutil.rmtree(full)
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/stream_file', methods=['GET'])
    def stream_file():
        file_path = request.args.get('path', '')
        if '..' in file_path or file_path.startswith('/'):
            abort(403)
        full = os.path.join(UPLOAD_FOLDER, file_path)
        if not os.path.exists(full) or os.path.isdir(full):
            abort(404)
        range_header = request.headers.get('Range')
        if range_header:
            return get_range_response(full, range_header)
        return send_file(full, mimetype='video/mp4')
