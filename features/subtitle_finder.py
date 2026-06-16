import os
import re
import uuid
import threading
import time
import logging
import requests
from flask import request, jsonify, Response
from tasks import save_task, load_task
from config import UPLOAD_FOLDER

logger = logging.getLogger(__name__)
OPENSUBTITLES_API_KEY = os.environ.get('OPENSUBTITLES_API_KEY', '')
OPENSUBTITLES_API_URL = "https://api.opensubtitles.com/api/v1"

LANGUAGES = {
    'eng': 'English', 'spa': 'Spanish', 'fra': 'French', 'deu': 'German',
    'ita': 'Italian', 'por': 'Portuguese', 'rus': 'Russian', 'jpn': 'Japanese',
    'kor': 'Korean', 'zho': 'Chinese', 'ara': 'Arabic', 'hin': 'Hindi',
    'tur': 'Turkish', 'nld': 'Dutch', 'pol': 'Polish', 'swe': 'Swedish',
    'nor': 'Norwegian', 'dan': 'Danish', 'fin': 'Finnish', 'heb': 'Hebrew'
}

def search_opensubtitles(query, language_code):
    if not OPENSUBTITLES_API_KEY:
        return None
    headers = {'Api-Key': OPENSUBTITLES_API_KEY, 'Content-Type': 'application/json', 'User-Agent': 'MediaCompressor/1.0'}
    queries_to_try = [
        query,
        re.sub(r'\(\d{4}\)', '', query).strip(),
        re.sub(r'[^\w\s]', '', query).strip(),
        ' '.join(query.split()[:3])
    ]
    queries_to_try = list(dict.fromkeys(queries_to_try))
    for q in queries_to_try:
        if not q:
            continue
        params = {'query': q, 'languages': language_code, 'type': 'movie', 'limit': 30}
        try:
            resp = requests.get(f"{OPENSUBTITLES_API_URL}/subtitles", headers=headers, params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('data'):
                    return data['data']
            elif resp.status_code == 401:
                return None
        except Exception as e:
            logger.warning(f"OpenSubtitles search error: {e}")
            continue
    return None

def search_fallback_opensubtitles_legacy(query, language_code):
    url = f"https://rest.opensubtitles.org/search/query-{requests.utils.quote(query)}/sublanguageid-{language_code}"
    headers = {'User-Agent': 'MediaCompressor/1.0'}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            results = resp.json()
            subtitles = []
            for item in results:
                subtitles.append({
                    'id': item.get('IDSubtitleFile'),
                    'file_id': item.get('IDSubtitleFile'),
                    'file_name': item.get('SubFileName', f"subtitle_{item.get('IDSubtitle')}.srt"),
                    'language': item.get('ISO639', language_code),
                    'download_count': item.get('SubDownloadsCnt', 0),
                    'ratings': item.get('SubRating', 0),
                    'size_mb': round(int(item.get('SubSize', 0)) / (1024*1024), 2),
                    'url': item.get('SubDownloadLink', '')
                })
            return subtitles
    except Exception as e:
        logger.warning(f"Fallback subtitle search error: {e}")
    return None

def search_subtitles_task(query, language_code, task_id):
    task = load_task(task_id)
    task['status'] = 'searching'
    save_task(task_id, task)

    data = search_opensubtitles(query, language_code)
    subtitles = []
    if data:
        for item in data:
            attributes = item.get('attributes', {})
            for file_info in attributes.get('files', []):
                subtitles.append({
                    'id': item.get('id'),
                    'file_id': file_info.get('file_id'),
                    'file_name': file_info.get('file_name'),
                    'language': attributes.get('language', language_code),
                    'download_count': attributes.get('download_count', 0),
                    'ratings': attributes.get('ratings', 0),
                    'size_mb': round(file_info.get('size', 0) / (1024*1024), 2),
                    'url': file_info.get('url')
                })
    if not subtitles:
        fallback = search_fallback_opensubtitles_legacy(query, language_code)
        if fallback:
            subtitles = fallback
    task = load_task(task_id)
    task['status'] = 'search_done'
    task['subtitles'] = subtitles[:30]
    save_task(task_id, task)

def register_routes(app):
    @app.route('/subtitle/search', methods=['POST'])
    def subtitle_search_endpoint():
        query = request.form.get('query', '').strip()
        language = request.form.get('language', 'eng')
        if not query:
            return jsonify({'error': 'Movie name required'}), 400
        task_id = str(uuid.uuid4())
        task_data = {
            'task_id': task_id, 'status': 'queued', 'created_at': time.time(),
            'cancelled': False, 'query': query, 'language': language
        }
        save_task(task_id, task_data)
        threading.Thread(target=search_subtitles_task, args=(query, language, task_id), daemon=True).start()
        return jsonify({'task_id': task_id})

    @app.route('/subtitle/download_direct', methods=['GET'])
    def subtitle_download_direct():
        url = request.args.get('url')
        filename = request.args.get('filename')
        if not url or not filename:
            return jsonify({'error': 'Missing url or filename'}), 400
        if not url.startswith(('http://', 'https://')):
            return jsonify({'error': 'Invalid URL'}), 400
        filename = os.path.basename(filename)
        if not filename.lower().endswith(('.srt', '.vtt', '.ass', '.ssa')):
            filename += '.srt'
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            resp = requests.get(url, headers=headers, stream=True, timeout=30)
            resp.raise_for_status()
            def generate():
                for chunk in resp.iter_content(chunk_size=8192):
                    yield chunk
            return Response(generate(), mimetype='text/plain',
                            headers={'Content-Disposition': f'attachment; filename="{filename}"'})
        except Exception as e:
            logger.error(f"Subtitle download error: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/subtitle/languages')
    def subtitle_languages():
        return jsonify(LANGUAGES)
