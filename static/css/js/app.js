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
    return str.replace(/[&<>]/g, function(m) {
        if (m === '&') return '&amp;';
        if (m === '<') return '&lt;';
        if (m === '>') return '&gt;';
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
    const units = ['B/s', 'KB/s', 'MB/s'];
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
            let progress = task.download_progress || task.upload_progress || task.progress || 0;
            let statusText = task.status;
            let speed = task.download_speed || 0;
            let total = task.total_size || 0;
            let downloaded = task.downloaded_size || 0;
            if (task.status === 'uploading') statusText = `📤 Uploading (${progress}%)`;
            else if (task.status === 'waiting_colab') statusText = `⏳ Waiting for Colab`;
            else if (task.status === 'downloading') {
                let speedDisplay = speed > 0 ? ` (${formatSpeed(speed * 1024)})` : '';
                let sizeDisplay = total > 0 ? ` ${formatBytes(downloaded)} / ${formatBytes(total)}` : ` ${formatBytes(downloaded)}`;
                statusText = `📥 Downloading${sizeDisplay}${speedDisplay}`;
            } else if (task.status === 'done') statusText = `✅ Done`;
            else if (task.status === 'error') statusText = `❌ Error`;
            else if (task.status === 'cancelled') statusText = `⛔ Cancelled`;
            else if (task.status === 'detecting_scenes') statusText = `🎬 Detecting scenes (${progress}%)`;
            else if (task.status === 'clipping') statusText = `✂️ Clipping (${progress}%)`;
            else if (task.status === 'merging') statusText = `🔗 Merging (${progress}%)`;
            else if (task.status === 'ytdlp_extract') statusText = `🔍 Extracting (${progress}%)`;
            else if (task.status === 'fetching') statusText = `🌐 Fetching (${progress}%)`;
            else if (task.status === 'searching') statusText = `🔎 Searching (${progress}%)`;
            else if (task.status === 'testing') statusText = `🧪 Testing (${progress}%)`;
            else statusText = task.status;
            const taskIdShort = task.task_id.substring(0, 8);
            const fileName = task.output_file || 'downloading...';
            html += `<div class="task-card" data-task-id="${task.task_id}">
                        <div class="task-header">
                            <span class="task-id">${taskIdShort}</span>
                            <span class="task-status">${statusText}</span>
                        </div>
                        <div style="font-size:0.8rem; margin-bottom:0.2rem;">${escapeHtml(fileName)}</div>
                        <div class="progress-bar"><div class="progress-fill" style="width: ${progress}%"></div></div>
                        <div style="display: flex; justify-content: flex-end; gap: 0.5rem;">
                            ${(task.status !== 'done' && task.status !== 'error' && task.status !== 'cancelled') ?
                                `<button class="cancel-task-btn" data-task-id="${task.task_id}" style="background:#3a2e2e; padding:0.2rem 0.8rem;"><i class="fas fa-ban"></i> Cancel</button>` : ''}
                            ${task.status === 'done' && task.output_file ?
                                `<a href="/download/${task.output_file}" style="background:#2b8c5e; padding:0.2rem 0.8rem; text-decoration:none; color:white; border-radius:2rem;"><i class="fas fa-download"></i> Result</a>` : ''}
                        </div>
                        ${task.error_msg ? `<div style="font-size:0.7rem; color:#ff8a8a; margin-top:0.3rem;">${escapeHtml(task.error_msg)}</div>` : ''}
                    </div>`;
        });
        container.innerHTML = html;
        document.querySelectorAll('.cancel-task-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
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
            showToast(`Cancelling task ${taskId.substring(0,8)}`);
        } catch (err) {
            showToast(err.message, true);
        }
    }

    // ==================== Tab Switching (unchanged) ====================
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

    // fetchTasks() is called by several feature pages right after they kick
    // off a task (telegram send, colab process, extracted-url download,
    // magnet download, etc). It needs to pull the current list from the
    // server and render it - if it's just aliased to renderTasks(), calling
    // it with no arguments wipes the pane to "No active tasks" until the
    // next SSE push happens to arrive.
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
    console.log('app.js loaded with SSE (no polling)');
})();
