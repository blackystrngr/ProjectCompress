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
async function fetchTasks() {
    const container = document.getElementById('tasksContainer');
    if (!container) return;
    try {
        const resp = await fetch('/get_tasks');
        if (!resp.ok) throw new Error(`HTTP ${resp.status} - ${resp.statusText}`);
        const data = await resp.json();
        console.log('fetchTasks received:', data);

        // Ensure data is an array
        if (!Array.isArray(data)) {
            console.error('Invalid response: not an array', data);
            container.innerHTML = '<div class="empty-state" style="color:#ff8a8a;">⚠️ Invalid response from server.</div>';
            return;
        }

        // **Client‑side filter** – ignore any task that is done, error, or cancelled
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
            if (task.status === 'uploading') statusText = `📤 Uploading (${progress}%)`;
            else if (task.status === 'waiting_colab') statusText = `⏳ Waiting for Colab`;
            else if (task.status === 'downloading') statusText = `📥 Downloading (${progress}%)`;
            else if (task.status === 'done') statusText = `✅ Done`;
            else if (task.status === 'error') statusText = `❌ Error`;
            else if (task.status === 'cancelled') statusText = `⛔ Cancelled`;
            else if (task.status === 'detecting_scenes') statusText = `🎬 Detecting scenes (${progress}%)`;
            else if (task.status === 'clipping') statusText = `✂️ Clipping (${progress}%)`;
            else if (task.status === 'merging') statusText = `🔗 Merging (${progress}%)`;
            else if (task.status === 'ytdlp_extract') statusText = `🔍 Extracting (${progress}%)`;
            else if (task.status === 'fetching') statusText = `🌐 Fetching (${progress}%)`;
            else if (task.status === 'searching') statusText = `🔎 Searching (${progress}%)`;
            else if (task.status === 'testing') statusText = `🧪 Testing (${progress}%)`;

            const taskIdShort = task.task_id.substring(0, 8);
            html += `<div class="task-card" data-task-id="${task.task_id}">
                        <div class="task-header">
                            <span class="task-id">${taskIdShort}</span>
                            <span class="task-status">${statusText}</span>
                        </div>
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

        // Attach cancel button listeners
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

async function cancelTask(taskId) {
    if (!taskId) return showToast('No task ID', true);
    try {
        const resp = await fetch(`/cancel/${taskId}`, { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        showToast(`Cancelling task ${taskId.substring(0,8)}`);
        fetchTasks(); // refresh immediately
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
        panes.forEach(pane => pane.classList.remove('active'));
        const activeBtn = document.querySelector(`.tab-btn[data-tab="${tabId}"]`);
        if (activeBtn) activeBtn.classList.add('active');
        const activePane = document.getElementById(`${tabId}-tab`);
        if (activePane) activePane.classList.add('active');
        // Optional refresh for tabs that need it
        if (tabId === 'local' && typeof loadDirectory === 'function') loadDirectory();
        if (tabId === 'drive' && typeof loadDriveFiles === 'function') loadDriveFiles();
    }
    tabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            const tabId = btn.getAttribute('data-tab');
            switchTab(tabId);
        });
    });
    const activeTab = document.querySelector('.tab-btn.active')?.getAttribute('data-tab');
    if (activeTab) switchTab(activeTab);
    else if (tabBtns.length) switchTab(tabBtns[0].getAttribute('data-tab'));
}

// ==================== Start polling ====================
document.addEventListener('DOMContentLoaded', () => {
    initTabs();
    fetchTasks();
    setInterval(fetchTasks, 2000);
});

// Expose globally so features can manually refresh
window.fetchTasks = fetchTasks;
