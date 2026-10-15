import uuid
import threading
import time
import logging
import requests
from bs4 import BeautifulSoup
from flask import request, jsonify
from tasks import save_task, load_task

logger = logging.getLogger(__name__)
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

def search_1337x(query):
    results = []
    try:
        url = f"https://1337x.to/search/{query.replace(' ', '+')}/1/"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        rows = soup.select('tbody tr')
        for row in rows[:15]:
            try:
                name_elem = row.select_one('td.coll-1 a.dl-torrent')
                if not name_elem:
                    continue
                title = name_elem.text.strip()
                link = "https://1337x.to" + name_elem['href']
                detail = requests.get(link, headers=HEADERS, timeout=10)
                detail_soup = BeautifulSoup(detail.text, 'html.parser')
                magnet = detail_soup.select_one('a[href^="magnet:?xt="]')
                if not magnet:
                    continue
                size_elem = row.select_one('td.coll-4.size')
                size = size_elem.text.strip() if size_elem else "Unknown"
                seeds = row.select_one('td.coll-2.seeds').text.strip()
                leechs = row.select_one('td.coll-3.leechs').text.strip()
                results.append({
                    'title': title, 'magnet': magnet['href'], 'size': size,
                    'seeders': seeds, 'leechers': leechs, 'source': '1337x'
                })
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"1337x search error: {e}")
    return results

def search_piratebay(query):
    results = []
    try:
        url = f"https://thepiratebay.org/search/{query.replace(' ', '%20')}/0/7/0"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        rows = soup.select('table#searchResult tr')[1:]
        for row in rows:
            try:
                cells = row.find_all('td')
                if len(cells) < 4:
                    continue
                title_cell = cells[1]
                title_link = title_cell.find('a', class_='detLink')
                if not title_link:
                    continue
                title = title_link.text.strip()
                magnet = None
                for a in title_cell.find_all('a'):
                    if a.get('href', '').startswith('magnet:'):
                        magnet = a['href']
                        break
                if not magnet:
                    continue
                size = title_cell.find('font', class_='detDesc')
                size_text = size.text.strip() if size else "Unknown"
                seeds = cells[2].text.strip()
                leechs = cells[3].text.strip()
                results.append({
                    'title': title, 'magnet': magnet, 'size': size_text,
                    'seeders': seeds, 'leechers': leechs, 'source': 'PirateBay'
                })
            except Exception:
                continue
            if len(results) >= 15:
                break
    except Exception as e:
        logger.warning(f"PirateBay search error: {e}")
    return results

def search_zooqle(query):
    results = []
    try:
        url = f"https://zooqle.com/search?q={query.replace(' ', '+')}"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        rows = soup.select('table.table-condensed tbody tr')
        for row in rows[:15]:
            try:
                title_elem = row.select_one('a[itemprop="url"]')
                if not title_elem:
                    continue
                title = title_elem.text.strip()
                magnet_elem = row.select_one('a[title="Magnet link"]')
                if not magnet_elem:
                    continue
                size = row.select_one('td[data-sort-value]').text.strip()
                seeds = row.select_one('td:nth-child(6)').text.strip()
                leechs = row.select_one('td:nth-child(7)').text.strip()
                results.append({
                    'title': title, 'magnet': magnet_elem['href'], 'size': size,
                    'seeders': seeds, 'leechers': leechs, 'source': 'Zooqle'
                })
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"Zooqle search error: {e}")
    return results

def search_torrents_all(query, task_id):
    results = []
    for name, func in [('1337x', search_1337x), ('PirateBay', search_piratebay), ('Zooqle', search_zooqle)]:
        if load_task(task_id).get('cancelled', False):
            break
        try:
            res = func(query)
            if res:
                results.extend(res)
                logger.info(f"{name} returned {len(res)} results")
        except Exception as e:
            logger.warning(f"{name} failed: {e}")
        time.sleep(1)
    seen = set()
    unique = []
    for r in results:
        if r['magnet'] not in seen:
            seen.add(r['magnet'])
            unique.append(r)
    return unique

def register_routes(app):
    @app.route('/torrent_search', methods=['POST'])
    def torrent_search():
        query = request.form.get('query', '').strip()
        if not query:
            return jsonify({'error': 'Search query required'}), 400
        task_id = str(uuid.uuid4())
        task_data = {
            'task_id': task_id, 'status': 'queued', 'created_at': time.time(),
            'cancelled': False, 'query': query, 'results': []
        }
        save_task(task_id, task_data)
        def run():
            try:
                results = search_torrents_all(query, task_id)
                task = load_task(task_id)
                task['results'] = results
                task['status'] = 'search_done'
                task['progress'] = 100
                save_task(task_id, task)
            except Exception as e:
                task = load_task(task_id)
                task['status'] = 'error'
                task['error_msg'] = str(e)
                save_task(task_id, task)
        threading.Thread(target=run, daemon=True).start()
        return jsonify({'task_id': task_id})
