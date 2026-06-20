// ==================== Toast ====================
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

// ==================== Task Management ====================
let pollInterval = null;
let isPolling = false;
const POLL_INTERVAL_MS = 5000; // 5 seconds

async function fetchTasks() {
    const container = document.getElementById('tasksContainer');
    if (!container) return;
    try {
        const resp = await fetch('/get_tasks');
        if (!resp.ok) throw new Error(`HTTP ${resp.status} - ${resp.statusText}`);
        const data = await resp.json();
        console.log('fetchTasks received:', data);

        if (!Array.isArray(data)) {
            console.error('Invalid response: not an array', data);
            container.innerHTML = '<div class="empty-state" style="color:#ff8a8a;">⚠️ Invalid response from server.</div>';
            return;
        }

        const terminalStatuses = ['done', 'error', 'cancelled', 'search_done', 'scan_done'];
        const activeTasks = data.filter(t =>
            t && t.task_id &&
            !terminalStatuses.includes(t.status)
        );

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
    } catch (err) {
        console.error('fetchTasks error:', err);
        container.innerHTML = `<div class="empty-state" style="color:#ff8a8a;">⚠️ Failed to load tasks. ${err.message}</div>`;
    }
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

async function cancelTask(taskId) {
    if (!taskId) return showToast('No task ID', true);
    try {
        const resp = await fetch(`/cancel/${taskId}`, { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        showToast(`Cancelling task ${taskId.substring(0,8)}`);
        fetchTasks();
    } catch (err) {
        showToast(err.message, true);
    }
}

// ==================== Tab Switching (Fixed) ====================
function initTabs() {
    const tabBtns = document.querySelectorAll('.tab-btn');
    const panes = document.querySelectorAll('.tab-pane');
    if (!tabBtns.length) {
        console.warn('No tab buttons found');
        return;
    }

    function switchTab(tabId) {
        console.log('Switching to tab:', tabId);
        // Update button states
        tabBtns.forEach(btn => btn.classList.remove('active'));
        const activeBtn = document.querySelector(`.tab-btn[data-tab="${tabId}"]`);
        if (activeBtn) activeBtn.classList.add('active');

        // Update pane states
        panes.forEach(pane => pane.classList.remove('active'));
        const activePane = document.getElementById(`${tabId}-tab`);
        if (activePane) activePane.classList.add('active');

        // Optional: refresh tab-specific data (with error handling)
        try {
            if (tabId === 'local' && typeof loadDirectory === 'function') {
                loadDirectory();
            }
            if (tabId === 'drive' && typeof loadDriveFiles === 'function') {
                loadDriveFiles();
            }
        } catch (e) {
            console.warn('Error refreshing tab data:', e);
        }
    }

    // Remove any old listeners and attach new ones (prevent duplicates)
    tabBtns.forEach(btn => {
        btn.removeEventListener('click', btn._listener);
        const listener = function() {
            const tabId = this.getAttribute('data-tab');
            switchTab(tabId);
        };
        btn._listener = listener;
        btn.addEventListener('click', listener);
    });

    // Activate the initially active tab
    const activeTab = document.querySelector('.tab-btn.active')?.getAttribute('data-tab');
    if (activeTab) {
        switchTab(activeTab);
    } else if (tabBtns.length) {
        // fallback: activate first tab
        const firstTab = tabBtns[0].getAttribute('data-tab');
        switchTab(firstTab);
    }
}

// ==================== Polling with Visibility API ====================
function startPolling() {
    if (isPolling) return;
    isPolling = true;
    console.log('Task polling started (every 5s)');
    fetchTasks();
    pollInterval = setInterval(fetchTasks, POLL_INTERVAL_MS);
}

function stopPolling() {
    if (pollInterval) {
        clearInterval(pollInterval);
        pollInterval = null;
        isPolling = false;
        console.log('Task polling stopped (tab hidden)');
    }
}

function handleVisibilityChange() {
    if (document.hidden) {
        stopPolling();
    } else {
        startPolling();
        // Also re‑initialise tabs in case they were broken (optional)
        // initTabs(); // not needed, but safe
    }
}

// ==================== Initialization ====================
document.addEventListener('DOMContentLoaded', function() {
    initTabs();

    // Start polling only if tab is visible
    if (!document.hidden) {
        startPolling();
    }
    document.addEventListener('visibilitychange', handleVisibilityChange);
});

// Expose fetchTasks globally so features can refresh manually
window.fetchTasks = fetchTasks;
