import os
import re
import json
import uuid
import threading
import time
import logging
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import request, jsonify
from tasks import save_task, load_task
from config import PROXY_CACHE_DIR

logger = logging.getLogger(__name__)

TEST_URL = "https://httpbin.org/ip"
TEST_TIMEOUT = 5

def parse_simple_list(text, proxy_type):
    proxies = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        line = line.split('#')[0].strip()
        match = re.match(r'^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d{1,5})$', line)
        if match:
            ip, port = match.groups()
            proxies.append((ip, port, proxy_type))
        else:
            parts = line.split()
            if len(parts) >= 2 and re.match(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', parts[0]):
                proxies.append((parts[0], parts[1], proxy_type))
    return proxies

SOURCES = [
    ("HTTP (TheSpeedX)", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt", lambda t: parse_simple_list(t, "http")),
    ("SOCKS4 (TheSpeedX)", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt", lambda t: parse_simple_list(t, "socks4")),
    ("SOCKS5 (TheSpeedX)", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt", lambda t: parse_simple_list(t, "socks5")),
    ("HTTP (ProxyScrape)", "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=all", lambda t: parse_simple_list(t, "http")),
    ("SOCKS4 (ProxyScrape)", "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks4&timeout=5000&country=all", lambda t: parse_simple_list(t, "socks4")),
    ("SOCKS5 (ProxyScrape)", "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=5000&country=all", lambda t: parse_simple_list(t, "socks5")),
]

def fetch_proxies_from_sources():
    all_proxies = []
    for name, url, parser in SOURCES:
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                proxies = parser(resp.text)
                all_proxies.extend(proxies)
                logger.info(f"Fetched {len(proxies)} from {name}")
        except Exception as e:
            logger.warning(f"Error fetching {name}: {e}")
    unique = {}
    for ip, port, ptype in all_proxies:
        key = f"{ip}:{port}:{ptype}"
        if key not in unique:
            unique[key] = (ip, port, ptype)
    return list(unique.values())

def test_proxy(ip, port, proxy_type, task_id=None, index=None, total=None):
    if proxy_type in ('http', 'https'):
        proxy_url = f"http://{ip}:{port}"
        proxies = {'http': proxy_url, 'https': proxy_url}
    elif proxy_type == 'socks4':
        proxy_url = f"socks4://{ip}:{port}"
        proxies = {'http': proxy_url, 'https': proxy_url}
    elif proxy_type == 'socks5':
        proxy_url = f"socks5://{ip}:{port}"
        proxies = {'http': proxy_url, 'https': proxy_url}
    else:
        return None
    try:
        start = time.time()
        resp = requests.get(TEST_URL, proxies=proxies, timeout=TEST_TIMEOUT)
        latency = int((time.time() - start) * 1000)
        if resp.status_code == 200:
            if task_id and index and total:
                task = load_task(task_id)
                if task:
                    task['test_progress'] = int(100 * index / total)
                    save_task(task_id, task)
            return {'ip': ip, 'port': port, 'type': proxy_type, 'latency_ms': latency}
    except:
        pass
    return None

def run_proxy_fetch_task(task_id):
    task = load_task(task_id)
    task['status'] = 'fetching'
    save_task(task_id, task)

    raw_proxies = fetch_proxies_from_sources()
    if not raw_proxies:
        task = load_task(task_id)
        task['status'] = 'error'
        task['error_msg'] = 'No proxies fetched'
        save_task(task_id, task)
        return

    result_file = os.path.join(PROXY_CACHE_DIR, f"{task_id}_results.json")
    with open(result_file, 'w') as f:
        json.dump([], f)

    task = load_task(task_id)
    task['status'] = 'testing'
    task['total_proxies'] = len(raw_proxies)
    task['tested'] = 0
    task['working_proxies'] = 0
    save_task(task_id, task)

    working = []
    with ThreadPoolExecutor(max_workers=30) as executor:
        futures = {executor.submit(test_proxy, ip, port, ptype, task_id, idx, len(raw_proxies)): (ip, port, ptype)
                   for idx, (ip, port, ptype) in enumerate(raw_proxies, 1)}
        for future in as_completed(futures):
            result = future.result()
            if result:
                working.append(result)
                with open(result_file, 'w') as f:
                    json.dump(working, f)
                task = load_task(task_id)
                if task:
                    task['working_proxies'] = len(working)
                    save_task(task_id, task)
            task = load_task(task_id)
            if task:
                task['tested'] = task.get('tested', 0) + 1
                save_task(task_id, task)

    with open(result_file, 'w') as f:
        json.dump(working, f)
    task = load_task(task_id)
    task['status'] = 'done'
    task['working_proxies'] = len(working)
    task['result_file'] = result_file
    save_task(task_id, task)

def register_routes(app):
    @app.route('/proxy/fetch', methods=['POST'])
    def start_proxy_fetch():
        task_id = str(uuid.uuid4())
        task_data = {
            'task_id': task_id, 'status': 'queued', 'created_at': time.time(), 'cancelled': False,
            'fetch_progress': 0, 'test_progress': 0, 'total_proxies': 0, 'tested': 0, 'working_proxies': 0
        }
        save_task(task_id, task_data)
        threading.Thread(target=run_proxy_fetch_task, args=(task_id,), daemon=True).start()
        return jsonify({'task_id': task_id})

    @app.route('/proxy/results/<task_id>')
    def get_proxy_results(task_id):
        task = load_task(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        result_file = os.path.join(PROXY_CACHE_DIR, f"{task_id}_results.json")
        proxies = []
        if os.path.exists(result_file):
            with open(result_file, 'r') as f:
                proxies = json.load(f)
        return jsonify({
            'status': task.get('status'),
            'working_proxies': len(proxies),
            'proxies': proxies,
            'tested': task.get('tested', 0),
            'total': task.get('total_proxies', 0)
        })

    @app.route('/proxy/test_single', methods=['POST'])
    def test_single_proxy():
        data = request.get_json()
        ip = data.get('ip')
        port = data.get('port')
        proxy_type = data.get('type')
        if not all([ip, port, proxy_type]):
            return jsonify({'error': 'Missing ip/port/type'}), 400
        result = test_proxy(ip, port, proxy_type)
        if result:
            return jsonify({'success': True, 'latency_ms': result['latency_ms']})
        else:
            return jsonify({'success': False}), 200
