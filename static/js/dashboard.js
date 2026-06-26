function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.style.background = type === 'success' ? 'rgba(16, 185, 129, 0.95)' : (type === 'error' ? 'rgba(239, 68, 68, 0.95)' : 'rgba(30, 41, 59, 0.95)');
    toast.style.color = '#fff';
    toast.style.padding = '0.85rem 1.25rem';
    toast.style.borderRadius = '12px';
    toast.style.boxShadow = '0 10px 15px -3px rgba(0, 0, 0, 0.3), 0 4px 6px -4px rgba(0, 0, 0, 0.3)';
    toast.style.backdropFilter = 'blur(10px)';
    toast.style.border = '1px solid rgba(255,255,255,0.1)';
    toast.style.fontFamily = "'Plus Jakarta Sans', sans-serif";
    toast.style.fontSize = '0.85rem';
    toast.style.fontWeight = '500';
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(-10px)';
    toast.style.transition = 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)';
    toast.innerText = message;
    container.appendChild(toast);
    setTimeout(() => { toast.style.opacity = '1'; toast.style.transform = 'translateY(0)'; }, 50);
    setTimeout(() => { toast.style.opacity = '0'; toast.style.transform = 'translateY(-10px)'; setTimeout(() => { toast.remove(); }, 300); }, 5000);
}

async function triggerBackup(event, mode) {
    event.preventDefault();
    const confirmMsg = mode === 'cloud' ? 'Start cloud backup now?' : 'Start LAN backup now? Includes WoL + sync + shutdown.';
    if (!confirm(confirmMsg)) return;
    const btn = document.getElementById('btn-' + mode);
    if (!btn) return;
    btn.style.pointerEvents = 'none'; btn.style.opacity = '0.5'; btn.innerText = 'Starting...';
    try {
        const response = await fetch('/trigger/' + mode, { method: 'POST' });
        const data = await response.json();
        if (response.ok) { showToast(data.detail || 'Backup started successfully!', 'success'); }
        else { showToast(data.detail || 'Failed to start backup.', 'error'); btn.style.pointerEvents = ''; btn.style.opacity = ''; btn.innerText = 'Run ' + mode.charAt(0).toUpperCase() + mode.slice(1) + ' Backup'; }
    } catch (err) { showToast('Network connection error: ' + err.message, 'error'); btn.style.pointerEvents = ''; btn.style.opacity = ''; btn.innerText = 'Run ' + mode.charAt(0).toUpperCase() + mode.slice(1) + ' Backup'; }
    await updateStatus();
}

function statusDescription(status, files, filesFailed) {
    if (status === 'CLOUD_NO_CHANGES_COMPLETE' || status === 'LAN_NO_CHANGES_COMPLETE') return 'Backup Complete — no changes detected';
    if (status.endsWith('_COMPLETE')) {
        if (filesFailed > 0) return 'Backup Complete — ' + files + ' files backed up, ' + filesFailed + ' could not be copied';
        return 'Backup Complete — ' + files + ' files backed up';
    }
    if (status.includes('_PARTIAL')) return 'Backup Partial — ' + files + ' files backed up, ' + filesFailed + ' could not be copied';
    if (status.includes('_FAILED')) return 'Backup Failed — see issue below';
    return status;
}

function formatFullDate(isoStr) {
    if (!isoStr) return '-';
    var d = new Date(isoStr);
    if (isNaN(d.getTime())) return isoStr;
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    var hours = d.getHours(), ampm = hours >= 12 ? 'PM' : 'AM';
    hours = hours % 12; if (hours === 0) hours = 12;
    var mins = d.getMinutes(); if (mins < 10) mins = '0' + mins;
    return d.getDate() + ' ' + months[d.getMonth()] + ' ' + d.getFullYear() + ' at ' + hours + ':' + mins + ' ' + ampm;
}

var _lastStatusErr = 0;
async function updateStatus() {
    try {
        const response = await fetch('/status');
        if (!response.ok) return;
        _lastStatusErr = 0;
        const data = await response.json();

        function agoClass(diffDays) {
            if (diffDays < 1) return 'success-ago';
            if (diffDays < 2) return 'warn-ago';
            return 'critical-ago';
        }

        function showLastSuccess(mode, lastSuccess) {
            var el = document.getElementById(mode + '-last-success');
            if (!el) return;
            if (!lastSuccess) {
                el.textContent = '! No successful backup yet';
                el.className = 'critical-ago';
                return;
            }
            var then = new Date(lastSuccess);
            var now = new Date();
            var diffDays = (now - then) / 86400000;
            if (isNaN(diffDays)) {
                el.textContent = '! No successful backup yet';
                el.className = 'critical-ago';
                return;
            }
            var cls = agoClass(diffDays);
            var prefix = diffDays < 1 ? '[OK]' : (diffDays < 2 ? '[WARN]' : '[ERR]');
            var fullDate = formatFullDate(lastSuccess);
            el.textContent = prefix + ' Last success: ' + fullDate;
            el.className = cls;
        }

        document.getElementById('stat-lan-files').innerText = Number(data.manifest.lan_files).toLocaleString();
        document.getElementById('stat-cloud-files').innerText = Number(data.manifest.cloud_files).toLocaleString();
        document.getElementById('stat-fy-prefix').innerText = data.fy_prefix;

        const cardCloud = document.getElementById('card-cloud');
        const badgeCloud = document.getElementById('badge-cloud');
        const descCloud = document.getElementById('desc-cloud');
        const lastCloud = document.getElementById('last-cloud');
        const btnCloud = document.getElementById('btn-cloud');
        if (cardCloud && badgeCloud && descCloud && lastCloud && btnCloud) {
            const isCloudRunning = data.cloud.running;
            badgeCloud.innerText = isCloudRunning ? 'Running' : 'Idle';
            if (isCloudRunning) { btnCloud.setAttribute('disabled', 'disabled'); btnCloud.style.pointerEvents = 'none'; btnCloud.style.opacity = '0.5'; btnCloud.innerText = 'Running...'; }
            else if (btnCloud.innerText !== 'Starting...') { btnCloud.removeAttribute('disabled'); btnCloud.style.pointerEvents = ''; btnCloud.style.opacity = ''; btnCloud.innerText = 'Run Cloud Backup'; }
            if (data.cloud.last_run) { descCloud.innerText = statusDescription(data.cloud.last_run.status, data.cloud.last_run.files, data.cloud.last_run.files_failed || 0); lastCloud.innerText = 'Last backup: ' + data.cloud.last_run_formatted; }
            const cloudClass = isCloudRunning ? 'running' : (data.cloud.last_run ? (data.cloud.last_run.status.endsWith('_COMPLETE') ? 'success' : 'failed') : 'unknown');
            cardCloud.className = 'card ' + cloudClass; badgeCloud.className = 'status-badge ' + cloudClass;
            showLastSuccess('cloud', data.cloud.last_success);
        }
        const cardLan = document.getElementById('card-lan');
        const badgeLan = document.getElementById('badge-lan');
        const descLan = document.getElementById('desc-lan');
        const lastLan = document.getElementById('last-lan');
        const btnLan = document.getElementById('btn-lan');
        if (cardLan && badgeLan && descLan && lastLan && btnLan) {
            const isLanRunning = data.lan.running;
            badgeLan.innerText = isLanRunning ? 'Running' : 'Idle';
            if (isLanRunning) { btnLan.setAttribute('disabled', 'disabled'); btnLan.style.pointerEvents = 'none'; btnLan.style.opacity = '0.5'; btnLan.innerText = 'Running...'; }
            else if (btnLan.innerText !== 'Starting...') { btnLan.removeAttribute('disabled'); btnLan.style.pointerEvents = ''; btnLan.style.opacity = ''; btnLan.innerText = 'Run LAN Backup'; }
            if (data.lan.last_run) { descLan.innerText = statusDescription(data.lan.last_run.status, data.lan.last_run.files, data.lan.last_run.files_failed || 0); lastLan.innerText = 'Last backup: ' + data.lan.last_run_formatted; }
            const lanClass = isLanRunning ? 'running' : (data.lan.last_run ? (data.lan.last_run.status.endsWith('_COMPLETE') ? 'success' : 'failed') : 'unknown');
            cardLan.className = 'card ' + lanClass; badgeLan.className = 'status-badge ' + lanClass;
            showLastSuccess('lan', data.lan.last_success);
        }
        if (data.health && !data.health.error) { document.getElementById('health-info').innerText = 'Source: ' + data.health.source_free_gb + ' GB free | FY: ' + data.fy_prefix; }
        const tbody = document.getElementById('history-tbody');
        if (tbody && data.recent_runs && data.recent_runs.length > 0) {
            const openRows = new Set();
            for (let i = 0; i < data.recent_runs.length; i++) {
                const el = document.getElementById('metrics-container-' + i);
                if (el && el.classList.contains('open')) openRows.add(i);
            }
            let rowsHtml = '';
            const escapeHtml = function(text) { return (text == null ? '' : String(text)).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); };
            data.recent_runs.forEach((r, idx) => {
                const safeMode = ['cloud', 'lan'].includes(r.mode) ? r.mode : 'unknown';
                const modeTag = '<span class="tag ' + safeMode + '">' + escapeHtml(safeMode.toUpperCase()) + '</span>';
                let sTag = ''; if (r.status === 'CLOUD_NO_CHANGES_COMPLETE' || r.status === 'LAN_NO_CHANGES_COMPLETE') { sTag = '<span class="tag no-changes">No Changes</span>'; } else if (r.status.endsWith('_COMPLETE') || r.status === 'SUCCESS') { sTag = '<span class="tag success">Complete</span>'; } else if (r.status.includes('_PARTIAL')) { sTag = '<span class="tag partial">Partial</span>'; } else if (r.status.includes('_FAILED')) { sTag = '<span class="tag failed">Failed</span>'; } else if (r.status.includes('_SKIPPED')) { sTag = '<span class="tag no-changes">Skipped</span>'; } else { sTag = '<span class="tag">' + escapeHtml(r.status.substring(0, 10)) + '</span>'; }
                const errCell = r.error ? '<td style="color:#fca5a5;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + escapeHtml(r.error) + '">' + escapeHtml(r.error) + '</td>' : '<td>-</td>';
                
                let expandableClass = '';
                let expandIcon = '';
                let extraRowHtml = '';
                if (r.extended_metrics) {
                    try {
                        const metrics = JSON.parse(r.extended_metrics);
                        const iconCls = openRows.has(idx) ? ' expand-icon open' : ' expand-icon';
                        expandIcon = '<span class="' + iconCls + '">\u25B8</span>';
                        expandableClass = ' class="expandable" onclick="document.getElementById(\'metrics-container-' + idx + '\').classList.toggle(\'open\'); this.querySelector(\'.expand-icon\').classList.toggle(\'open\')"';
                        let badges = '';
                        if (r.mode === 'lan') {
                            badges += '<span class="metrics-badge added">Added: ' + (metrics.added || 0) + '</span>';
                            badges += '<span class="metrics-badge modified">Modified: ' + (metrics.modified || 0) + '</span>';
                            badges += '<span class="metrics-badge removed">Pruned: ' + (metrics.removed || 0) + '</span>';
                            badges += '<span class="metrics-badge">Active Destination Files: ' + (metrics.total_files || 0) + '</span>';
                        } else if (r.mode === 'cloud') {
                            badges += '<span class="metrics-badge verified">' + (metrics.verified ? 'Verification Passed' : 'Verification Failed') + '</span>';
                            badges += '<span class="metrics-badge">Total Tracked Files: ' + (metrics.total_files || 0) + '</span>';
                            badges += '<span class="metrics-badge size">Space Consumed: ' + (metrics.total_size_gb || 0).toFixed(3) + ' GB</span>';
                        }
                        const isOpen = openRows.has(idx) ? ' open' : '';
                        extraRowHtml = '<tr class="metrics-row"><td colspan="6"><div id="metrics-container-' + idx + '" class="metrics-container' + isOpen + '">' + badges + '</div></td></tr>';
                    } catch(e) { }
                }

                rowsHtml += '<tr' + expandableClass + '><td>' + expandIcon + escapeHtml(r.started_at) + '</td><td>' + modeTag + '</td><td>' + sTag + '</td><td>' + escapeHtml(String(r.files)) + '</td><td>' + escapeHtml(String(r.duration)) + '</td>' + errCell + '</tr>\n';
                rowsHtml += extraRowHtml;
            });
            tbody.innerHTML = rowsHtml;
        } else if (tbody && data.recent_runs && data.recent_runs.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" style="color:#6b7280; text-align: center;">No backup runs recorded yet</td></tr>';
        }
    } catch (err) { var now = Date.now(); if (now - _lastStatusErr > 30000) { console.error('Failed to update status dynamically:', err); _lastStatusErr = now; } }
}
setInterval(updateStatus, 2000);
window.addEventListener('DOMContentLoaded', updateStatus);
