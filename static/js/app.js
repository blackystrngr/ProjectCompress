// ==================== Global Helper Functions ====================
function showToast(message, isError = false) {
    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.innerHTML = `<i class="fas ${isError ? 'fa-exclamation-triangle' : 'fa-check-circle'}"></i> ${escapeHtml(message)}`;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 3000);
}

function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/[&<>"]/g, function(m) {
        if (m === '&') return '&amp;';
        if (m === '<') return '&lt;';
        if (m === '>') return '&gt;';
        if (m === '"') return '&quot;';
        return m;
    });
}

function formatBytes(bytes) {
    if (bytes === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    const val = (bytes / Math.pow(1024, i)).toFixed(i > 0 ? 1 : 0);
    return val + ' ' + units[i];
}

function formatSpeed(bytesPerSec) {
    if (bytesPerSec === 0) return '0 B/s';
    const units = ['B/s', 'KB/s', 'MB/s', 'GB/s'];
    const i = Math.floor(Math.log(bytesPerSec) / Math.log(1024));
    const val = (bytesPerSec / Math.pow(1024, i)).toFixed(i > 0 ? 1 : 0);
    return val + ' ' + units[i];
}

// ==================== Task Management with SSE ====================
(function() {
    let eventSource = null;
    let reconnectAttempts = 0;
    const MAX_RECONNECT_ATTEMPTS = 5;

    function connectSSE() {
        if (eventSource) {
            eventSource.close();
        }
        eventSource = new EventSource('/tasks/stream');

        eventSource.onmessage = function(event) {
            try {
                const tasks = JSON.parse(event.data);
                renderTasks(tasks);
                reconnectAttempts = 0;
            } catch (e) {
                console.error('SSE parse error:', e);
            }
        };

        eventSource.onerror = function(e) {
            console.warn('SSE connection error, reconnecting...', e);
            eventSource.close();
            if (reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
                reconnectAttempts++;
                setTimeout(connectSSE, 2000 * reconnectAttempts);
            } else {
                console.error('SSE connection failed after multiple attempts.');
                const container = document.getElementById('tasksContainer');
                if (container) {
                    container.innerHTML = '<div class="empty-state" style="color:#ff8a8a;">⚠️ Could not connect to task updates. Refresh the page.</div>';
                }
            }
        };
    }

    function renderTasks(tasks) {
        const container = document.getElementById('tasksContainer');
        if (!container) return;

        if (!Array.isArray(tasks) || tasks.length === 0) {
            container.innerHTML = '<div class="empty-state">No active tasks.</div>';
            return;
        }

        const terminalStatuses = ['done', 'error', 'cancelled', 'search_done', 'scan_done'];
        const activeTasks = tasks.filter(t => t && t.task_id && !terminalStatuses.includes(t.status));

        if (activeTasks.length === 0) {
            container.innerHTML = '<div class="empty-state">No active tasks.</div>';
            return;
        }

        let html = '';

        activeTasks.forEach(task => {
            let progress = task.download_progress || task.upload_progress || task.test_progress || task.progress || 0;
            let statusText = task.status;
            let speed = task.download_speed || 0;
            let total = task.total_size || 0;
            let downloaded = task.downloaded_size || 0;

            if (task.status === 'uploading') {
                statusText = `📤 Uploading (${progress}%)`;
            } else if (task.status === 'waiting_colab') {
                statusText = `⏳ Waiting for Colab`;
            } else if (task.status === 'downloading') {
                const speedDisplay = speed > 0 ? ` (${formatSpeed(speed * 1024)})` : '';
                const sizeDisplay = total > 0
                    ? ` ${formatBytes(downloaded)} / ${formatBytes(total)}`
                    : ` ${formatBytes(downloaded)}`;
                statusText = `📥 Downloading${sizeDisplay}${speedDisplay}`;
            } else if (task.status === 'downloading_playlist') {
                const cur = task.current_item || 0;
                const tot = task.total_items || 0;
                const title = task.current_title ? ` "${task.current_title}"` : '';
                const speedDisplay = speed > 0 ? ` (${formatSpeed(speed * 1024)})` : '';
                statusText = `📺 Playlist ${cur}/${tot}${title}${speedDisplay}`;
            } else if (task.status === 'done') {
                statusText = `✅ Done`;
            } else if (task.status === 'error') {
                statusText = `❌ Error`;
            } else if (task.status === 'cancelled') {
                statusText = `⛔ Cancelled`;
            } else if (task.status === 'detecting_scenes') {
                statusText = `🎬 Detecting scenes (${progress}%)`;
            } else if (task.status === 'clipping') {
                statusText = `✂️ Clipping (${progress}%)`;
            } else if (task.status === 'merging') {
                statusText = `🔗 Merging (${progress}%)`;
            } else if (task.status === 'ytdlp_extract') {
                statusText = `🔍 Extracting (${progress}%)`;
            } else if (task.status === 'fetching') {
                statusText = `🌐 Fetching (${progress}%)`;
            } else if (task.status === 'searching') {
                statusText = `🔎 Searching (${progress}%)`;
            } else if (task.status === 'testing') {
                statusText = `🧪 Testing (${progress}%)`;
            } else if (task.status === 'downloading_m3u8') {
                statusText = `📺 M3U8 (${progress}%)`;
            } else if (task.status === 'sending') {
                statusText = `📤 Sending to Telegram (${progress}%)`;
            }

            const taskIdShort = task.task_id.substring(0, 8);
            const fileName = task.output_file || 'downloading...';

            html += `
                <div class="task-card" data-task-id="${task.task_id}">
                    <div class="task-header">
                        <span class="task-id">${taskIdShort}</span>
                        <span class="task-status">${statusText}</span>
                    </div>
                    <div style="font-size:0.8rem; margin-bottom:0.2rem;">${escapeHtml(fileName)}</div>
                    <div class="progress-bar">
                        <div class="progress-fill" style="width: ${progress}%"></div>
                    </div>
                    <div style="display:flex; justify-content:flex-end; gap:0.5rem;">
                        ${(task.status !== 'done' && task.status !== 'error' && task.status !== 'cancelled')
                            ? `<button class="cancel-task-btn" data-task-id="${task.task_id}"
                                       style="background:#3a2e2e; padding:0.2rem 0.8rem;">
                                    <i class="fas fa-ban"></i> Cancel
                               </button>`
                            : ''}
                        ${task.status === 'done' && task.output_file
                            ? `<a href="/download_file?path=${encodeURIComponent(task.output_file)}"
                                  style="background:#2b8c5e; padding:0.2rem 0.8rem; text-decoration:none; color:white; border-radius:2rem;">
                                    <i class="fas fa-download"></i> Result
                               </a>`
                            : ''}
                    </div>
                    ${task.error_msg
                        ? `<div style="font-size:0.7rem; color:#ff8a8a; margin-top:0.3rem;">${escapeHtml(task.error_msg)}</div>`
                        : ''}
                </div>
            `;
        });

        container.innerHTML = html;

        document.querySelectorAll('.cancel-task-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const taskId = btn.getAttribute('data-task-id');
                if (taskId) cancelTask(taskId);
            });
        });
    }

    async function cancelTask(taskId) {
        if (!taskId) return showToast('No task ID', true);
        try {
            const resp = await fetch(`/cancel/${taskId}`, { method: 'POST' });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
            showToast(`Cancelling task ${taskId.substring(0, 8)}`);
        } catch (err) {
            showToast(err.message, true);
        }
    }

    // ==================== Tab Switching ====================
    function initTabs() {
        const tabBtns = document.querySelectorAll('.tab-btn');
        const panes = document.querySelectorAll('.tab-pane');
        if (!tabBtns.length) return;

        function switchTab(tabId) {
            tabBtns.forEach(btn => btn.classList.remove('active'));
            const activeBtn = document.querySelector(`.tab-btn[data-tab="${tabId}"]`);
            if (activeBtn) activeBtn.classList.add('active');

            panes.forEach(pane => pane.classList.remove('active'));
            const activePane = document.getElementById(`${tabId}-tab`);
            if (activePane) activePane.classList.add('active');

            try {
                if (tabId === 'local' && typeof loadDirectory === 'function') loadDirectory();
                if (tabId === 'drive' && typeof loadDriveFiles === 'function') loadDriveFiles();
            } catch (e) { /* ignore */ }
        }

        tabBtns.forEach(btn => {
            btn.removeEventListener('click', btn._listener);
            const listener = function() {
                const tabId = this.getAttribute('data-tab');
                switchTab(tabId);
            };
            btn._listener = listener;
            btn.addEventListener('click', listener);
        });

        const activeTab = document.querySelector('.tab-btn.active')?.getAttribute('data-tab');
        if (activeTab) switchTab(activeTab);
        else if (tabBtns.length) switchTab(tabBtns[0].getAttribute('data-tab'));
    }

    // ==================== Initialization ====================
    document.addEventListener('DOMContentLoaded', function() {
        initTabs();
        connectSSE();
    });

    // fetchTasks() – used by feature pages to refresh the task list
    async function fetchTasks() {
        try {
            const resp = await fetch('/get_tasks');
            const tasks = await resp.json();
            renderTasks(tasks);
        } catch (e) {
            console.error('fetchTasks error:', e);
        }
    }
    window.fetchTasks = fetchTasks;

    // ==================== Persistent Task Polling ====================
    function startPollingForTask(taskId, statusElementId, resultElementId, progressKey = 'progress') {
        if (!taskId) return;

        const statusEl = document.getElementById(statusElementId);
        const resultEl = document.getElementById(resultElementId);
        if (!statusEl || !resultEl) return;

        let interval = setInterval(async () => {
            try {
                const resp = await fetch(`/progress/${taskId}`);
                const task = await resp.json();

                if (task.error) {
                    clearInterval(interval);
                    statusEl.innerHTML = `<span style="color:#ff8a8a;">Error: ${escapeHtml(task.error)}</span>`;
                    return;
                }

                const progress = task[progressKey] || 0;

                if (task.status === 'done') {
                    clearInterval(interval);
                    statusEl.innerHTML = `✅ Done! <a href="/download/${task.output_file}" target="_blank">Download</a>`;
                    resultEl.innerHTML = `<div class="empty-state">✅ Ready: <a href="/download/${task.output_file}">${escapeHtml(task.output_file)}</a></div>`;
                    localStorage.removeItem(`task_${taskId}`);
                } else if (task.status === 'error') {
                    clearInterval(interval);
                    statusEl.innerHTML = `<span style="color:#ff8a8a;">❌ Error: ${escapeHtml(task.error_msg)}</span>`;
                    resultEl.innerHTML = '<div class="empty-state">Failed.</div>';
                    localStorage.removeItem(`task_${taskId}`);
                } else if (task.status === 'cancelled') {
                    clearInterval(interval);
                    statusEl.innerHTML = `⛔ Cancelled`;
                    resultEl.innerHTML = '<div class="empty-state">Cancelled.</div>';
                    localStorage.removeItem(`task_${taskId}`);
                } else {
                    statusEl.innerHTML = `<i class="fas fa-spinner fa-pulse"></i> ${escapeHtml(task.status)} (${progress}%)`;
                    resultEl.innerHTML = `<div class="empty-state">Processing (${progress}%)...</div>`;
                }
            } catch (err) {
                console.error(err);
            }
        }, 2000);

        return interval;
    }

    function storeTaskId(feature, taskId) {
        localStorage.setItem(`${feature}_task_id`, taskId);
    }

    // Expose globally
    window.startPollingForTask = startPollingForTask;
    window.storeTaskId = storeTaskId;

    // ==================== Resume polling on page load ====================
    document.addEventListener('DOMContentLoaded', function() {
        const features = ['url', 'random_clips', 'summary', 'frame_extract', 'face_swap', 'ocr'];

        features.forEach(feature => {
            const taskId = localStorage.getItem(`${feature}_task_id`);
            if (!taskId) return;

            fetch(`/progress/${taskId}`)
                .then(resp => resp.json())
                .then(task => {
                    if (task.status && !['done', 'error', 'cancelled'].includes(task.status)) {
                        // Map feature → status/result element IDs
                        const mapping = {
                            'url':           { status: 'urlStatus',    result: 'urlResult',    progress: 'download_progress' },
                            'random_clips':  { status: 'randomStatus', result: 'randomResult', progress: 'progress' },
                            'summary':       { status: 'summaryStatus', result: 'summaryResult', progress: 'progress' },
                            'frame_extract': { status: 'frameStatus',  result: 'frameResult',  progress: 'progress' },
                            'face_swap':     { status: 'swapStatus',   result: 'swapResult',   progress: 'progress' },
                            'ocr':           { status: 'ocrStatus',    result: 'ocrResult',    progress: 'progress' }
                        };
                        const map = mapping[feature];
                        if (map) {
                            startPollingForTask(taskId, map.status, map.result, map.progress);
                        }
                    } else {
                        localStorage.removeItem(`${feature}_task_id`);
                    }
                })
                .catch(() => localStorage.removeItem(`${feature}_task_id`));
        });
    });

    console.log('app.js loaded with SSE (no polling)');
})();
