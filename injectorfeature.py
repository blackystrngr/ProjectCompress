#!/usr/bin/env python3
"""
Add verbose progress tracking (0-100%) to all feature modules.
Run this script from your project root (where features/ and static/ are).
"""

import os
import re
import shutil

def update_task_creation_in_file(filepath):
    """Add 'progress': 0 to every new task_data dict."""
    with open(filepath, 'r') as f:
        content = f.read()

    # Pattern: task_data = { ... }
    pattern = r'(task_data\s*=\s*\{)([^}]*)(\})'
    def replacer(match):
        before = match.group(1)
        body = match.group(2)
        after = match.group(3)
        if "'progress':" not in body and '"progress":' not in body:
            # Insert progress at the end of the dict body
            # Clean trailing comma or whitespace
            body = body.rstrip()
            if body.endswith(','):
                body = body[:-1]
            body += ",\n            'progress': 0"
        return before + body + after

    new_content = re.sub(pattern, replacer, content, flags=re.DOTALL)

    if new_content != content:
        with open(filepath, 'w') as f:
            f.write(new_content)
        print(f"  ✓ Added progress field in {filepath}")
        return True
    return False

def add_progress_updates_in_telegram(filepath):
    """Add progress updates in telegram.py download loop."""
    with open(filepath, 'r') as f:
        content = f.read()

    # Look for the download_selected_async function
    # Insert progress = 0 at start, and increment in loop
    lines = content.splitlines()
    modified = False
    new_lines = []
    in_download_loop = False
    total_msgs = 0
    for line in lines:
        if 'async def download_selected_async' in line:
            in_download_loop = True
        if in_download_loop and 'for idx, msg_id in enumerate(message_ids, 1):' in line:
            # Add progress line after task creation in loop
            new_lines.append(line)
            new_lines.append('            # Update progress')
            new_lines.append('            t = load_task(download_task_id)')
            new_lines.append('            if t:')
            new_lines.append('                t["progress"] = int(100 * idx / total)')
            new_lines.append('                save_task(download_task_id, t)')
            modified = True
            continue
        new_lines.append(line)
    if modified:
        with open(filepath, 'w') as f:
            f.write('\n'.join(new_lines))
        print(f"  ✓ Added progress updates in {filepath}")
    return modified

def add_progress_updates_in_drive(filepath):
    """Add progress to drive download loop."""
    with open(filepath, 'r') as f:
        content = f.read()

    # Look for while not done loop inside run_download
    lines = content.splitlines()
    new_lines = []
    in_download_loop = False
    for line in lines:
        if 'while not done:' in line:
            in_download_loop = True
        if in_download_loop and 'status, done = downloader.next_chunk()' in line:
            new_lines.append(line)
            new_lines.append('                    pct = int(status.progress() * 100)')
            new_lines.append('                    task = load_task(task_id)')
            new_lines.append('                    if task:')
            new_lines.append('                        task["progress"] = pct')
            new_lines.append('                        save_task(task_id, task)')
            modified = True
            continue
        new_lines.append(line)
    if modified:
        with open(filepath, 'w') as f:
            f.write('\n'.join(new_lines))
        print(f"  ✓ Added progress updates in {filepath}")
    return modified

def update_app_js_for_progress():
    """Modify app.js to display task.progress field."""
    js_path = os.path.join('static', 'js', 'app.js')
    if not os.path.exists(js_path):
        print("  ! app.js not found – skipping JS update")
        return

    with open(js_path, 'r') as f:
        content = f.read()

    # Find the line where progress is calculated
    pattern = r'(let progress = task\.download_progress \|\| task\.upload_progress \|\| 0;)'
    replacement = r'let progress = task.download_progress || task.upload_progress || task.test_progress || task.progress || 0;'
    new_content = re.sub(pattern, replacement, content)

    # Add more descriptive status texts for new statuses
    status_map = {
        r"task\.status === 'ytdlp_extract'": "`🔍 Extracting (${progress}%)`",
        r"task\.status === 'fetching'": "`🌐 Fetching page (${progress}%)`",
        r"task\.status === 'searching'": "`🔎 Searching (${progress}%)`",
    }
    for old, new in status_map.items():
        # This is a simplistic replace; manual editing may be better.
        # We'll just print a suggestion.
        pass

    if new_content != content:
        with open(js_path, 'w') as f:
            f.write(new_content)
        print("  ✓ Updated app.js to include custom progress fields")
    else:
        print("  ! No changes needed in app.js")

def main():
    print("Starting verbose progress injection...\n")

    # 1. Update all feature files to include 'progress': 0 in task creation
    features_dir = 'features'
    modified_files = []
    for filename in os.listdir(features_dir):
        if filename.endswith('.py') and filename != '__init__.py':
            path = os.path.join(features_dir, filename)
            if update_task_creation_in_file(path):
                modified_files.append(filename)

    # 2. Specialised updates for Telegram and Drive (loops)
    tg_path = os.path.join(features_dir, 'telegram.py')
    if os.path.exists(tg_path):
        add_progress_updates_in_telegram(tg_path)

    drive_path = os.path.join(features_dir, 'google_drive.py')
    if os.path.exists(drive_path):
        add_progress_updates_in_drive(drive_path)

    # 3. Update app.js to display all progress fields
    update_app_js_for_progress()

    print("\n✅ Done! Please review changes in the following files:")
    for f in modified_files:
        print(f"  - features/{f}")
    print("\n📝 Note: For subtitle_finder, proxy_fetcher, and video_extractor,\n   you may need to manually add 'progress' updates inside long loops.")
    print("   The script added the field in task creation. You can now set task['progress'] = X\n   in your background threads (0-100).")

if __name__ == '__main__':
    main()
