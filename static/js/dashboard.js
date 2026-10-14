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

// Status label/category/description come from the server (core/statuses.py)
// so the dashboard can never disagree with reports or the database.
var _KNOWN_CATEGORIES = ['success', 'no-changes', 'partial', 'failed', 'blocked', 'running', 'skipped', 'unknown'];
function safeCategory(cat) { return _KNOWN_CATEGORIES.indexOf(cat) >= 0 ? cat : 'unknown'; }

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
function setSessionExpired(expired) {
    var banner = document.getElementById('session-banner');
    if (banner) banner.style.display = expired ? 'block' : 'none';
    if (!expired) return;
    ['cloud', 'lan'].forEach(function(mode) {
        var badge = document.getElementById('badge-' + mode);
        var desc = document.getElementById('desc-' + mode);
        var card = document.getElementById('card-' + mode);
        if (badge) { badge.innerText = 'Unknown'; badge.className = 'status-badge unknown'; }
        if (desc) desc.innerText = 'Status unavailable — session may have expired. Sign in again.';
        if (card) card.className = 'card unknown';
    });
}
function integrityText(integrity) {
    if (!integrity || !integrity.status) return 'Integrity: NOT VERIFIED — no audit recorded';
    var s = integrity.status;
    var when = integrity.checked_at ? ' (checked ' + integrity.checked_at + ')' : '';
    if (s === 'VERIFIED') return 'Integrity: VERIFIED' + when;
    if (s === 'VERIFICATION_FAILED') return 'Integrity: VERIFICATION FAILED' + when + ' — see audit detail';
    // WP7: the audit could not reach a verdict (network/TLS/DNS/process) - not divergence
    if (s === 'AUDIT_INCOMPLETE') return 'Integrity: AUDIT COULD NOT RUN' + when + (integrity.detail ? ' — ' + integrity.detail : '');
    return 'Integrity: NOT VERIFIED — no audit recorded';
}
async function updateStatus() {
    try {
        const response = await fetch('/status');
        if (response.status === 401) { setSessionExpired(true); return; }
        if (!response.ok) return;
        setSessionExpired(false);
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
        if (cardCloud && badgeCloud && descCloud && lastCloud) {
            const integCloud = document.getElementById('integrity-cloud');
            if (integCloud) integCloud.innerText = integrityText(data.cloud.integrity);
            if (data.cloud.enabled === false) {
                badgeCloud.innerText = 'Disabled'; badgeCloud.className = 'status-badge unknown';
                cardCloud.className = 'card unknown';
                descCloud.innerText = 'Cloud backup is disabled in configuration.';
                if (btnCloud) { btnCloud.setAttribute('disabled', 'disabled'); btnCloud.innerText = 'Disabled'; }
                showLastSuccess('cloud', data.cloud.last_success);
            } else {
                const isCloudRunning = data.cloud.running;
                const cloudUnknown = data.cloud.run_state === 'unknown';
                badgeCloud.innerText = isCloudRunning ? 'Running' : (cloudUnknown ? 'Unknown' : 'Idle');
                if (btnCloud) {
                    if (isCloudRunning) { btnCloud.setAttribute('disabled', 'disabled'); btnCloud.style.pointerEvents = 'none'; btnCloud.style.opacity = '0.5'; btnCloud.innerText = 'Running...'; }
                    else if (btnCloud.innerText !== 'Starting...') { btnCloud.removeAttribute('disabled'); btnCloud.style.pointerEvents = ''; btnCloud.style.opacity = ''; btnCloud.innerText = 'Run Cloud Backup'; }
                }
                if (data.cloud.last_run) { descCloud.innerText = (cloudUnknown ? 'Status unavailable — ' : '') + data.cloud.last_run.status_description; lastCloud.innerText = 'Last backup: ' + data.cloud.last_run_formatted; }
                else if (cloudUnknown) { descCloud.innerText = 'Status unavailable — could not reach Prefect API.'; }
                const cloudClass = isCloudRunning ? 'running' : (cloudUnknown ? 'unknown' : (data.cloud.last_run ? safeCategory(data.cloud.last_run.status_category) : 'unknown'));
                cardCloud.className = 'card ' + cloudClass; badgeCloud.className = 'status-badge ' + cloudClass;
                showLastSuccess('cloud', data.cloud.last_success);
            }
        }
        const cardLan = document.getElementById('card-lan');
        const badgeLan = document.getElementById('badge-lan');
        const descLan = document.getElementById('desc-lan');
        const lastLan = document.getElementById('last-lan');
        const btnLan = document.getElementById('btn-lan');
        if (cardLan && badgeLan && descLan && lastLan) {
            const integLan = document.getElementById('integrity-lan');
            if (integLan) integLan.innerText = integrityText(data.lan.integrity);
            if (data.lan.enabled === false) {
                badgeLan.innerText = 'Disabled'; badgeLan.className = 'status-badge unknown';
                cardLan.className = 'card unknown';
                descLan.innerText = 'LAN backup is disabled in configuration.';
                if (btnLan) { btnLan.setAttribute('disabled', 'disabled'); btnLan.innerText = 'Disabled'; }
                showLastSuccess('lan', data.lan.last_success);
            } else {
                const isLanRunning = data.lan.running;
                const lanUnknown = data.lan.run_state === 'unknown';
                badgeLan.innerText = isLanRunning ? 'Running' : (lanUnknown ? 'Unknown' : 'Idle');
                if (btnLan) {
                    if (isLanRunning) { btnLan.setAttribute('disabled', 'disabled'); btnLan.style.pointerEvents = 'none'; btnLan.style.opacity = '0.5'; btnLan.innerText = 'Running...'; }
                    else if (btnLan.innerText !== 'Starting...') { btnLan.removeAttribute('disabled'); btnLan.style.pointerEvents = ''; btnLan.style.opacity = ''; btnLan.innerText = 'Run LAN Backup'; }
                }
                if (data.lan.last_run) { descLan.innerText = (lanUnknown ? 'Status unavailable — ' : '') + data.lan.last_run.status_description; lastLan.innerText = 'Last backup: ' + data.lan.last_run_formatted; }
                else if (lanUnknown) { descLan.innerText = 'Status unavailable — could not reach Prefect API.'; }
                const lanClass = isLanRunning ? 'running' : (lanUnknown ? 'unknown' : (data.lan.last_run ? safeCategory(data.lan.last_run.status_category) : 'unknown'));
                cardLan.className = 'card ' + lanClass; badgeLan.className = 'status-badge ' + lanClass;
                showLastSuccess('lan', data.lan.last_success);
            }
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
                const sTag = '<span class="tag ' + safeCategory(r.status_category) + '" title="' + escapeHtml(r.status) + '">' + escapeHtml(r.status_label || r.status) + '</span>';
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
                            // WP7: the daily check compares SIZES only - it is a size check, not content verification
                            var sizeOk = ('size_check_passed' in metrics) ? metrics.size_check_passed : metrics.verified;
                            badges += '<span class="metrics-badge verified">' + (sizeOk === undefined ? 'Size check unknown' : (sizeOk ? 'Size check passed' : 'Size check failed')) + '</span>';
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

// ── Blocked backups waiting for a human decision (plan §6.2) ──────────────
// A guard stopped a backup before anything was copied or deleted. The server
// owns every decision; this only shows the reason/numbers and posts the
// typed confirmation phrase.
function escHtml(text) { return (text == null ? '' : String(text)).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
var _approvalsSig = '';
function approvalCard(a, authEnabled) {
    var leg = a.leg === 'cloud' ? 'Cloud backup' : (a.leg === 'rollover' ? 'FY rollover' : 'LAN backup');
    var reasons = (a.guards || []).map(function(g) { return '<li>' + escHtml(g.reason) + '</li>'; }).join('');
    var id = escHtml(a.approval_id);
    var html = '<div class="approval-card ' + (a.status === 'APPROVED' ? 'approved' : 'pending') + '" style="border:2px solid ' + (a.status === 'APPROVED' ? '#d97706' : '#dc2626') + ';border-radius:0.75rem;padding:1rem 1.25rem;margin-bottom:1rem;background:rgba(220,38,38,0.06)">';
    html += '<h2 style="margin:0 0 0.5rem 0">' + leg + ' blocked — ' + (a.status === 'APPROVED' ? 'approved, not run yet' : 'your decision is needed') + '</h2>';
    html += '<ul style="margin:0 0 0.5rem 1.25rem">' + reasons + '</ul>';
    html += '<p style="font-size:0.8rem;color:#6b7280">' + (a.leg === 'rollover' ? 'Nothing was changed.' : 'Nothing was copied or deleted.') + ' Blocked ' + escHtml(formatFullDate(a.created_at)) + ' (run ' + escHtml(a.blocked_run_id) + ').</p>';
    if (!authEnabled) {
        html += '<p style="font-weight:600">Approving needs dashboard sign-in: set <code>dashboard.auth_enabled: true</code> in config.yaml.</p>';
    } else if (a.status === 'PENDING') {
        html += '<p>To run this <b>once</b> anyway, type <code>' + escHtml(a.phrase) + '</code> and approve. The backup re-checks first and stops again if anything got worse.</p>';
        html += '<input id="phrase-' + id + '" type="text" autocomplete="off" placeholder="' + escHtml(a.phrase) + '" style="padding:0.4rem;margin-right:0.5rem;min-width:16rem">';
        html += '<button class="btn-trigger" style="background:#dc2626" onclick="approvalAction(\'' + id + '\', \'approve\')">Approve this backup once</button> ';
        html += '<button class="btn-trigger" style="background:#4b5563" onclick="approvalAction(\'' + id + '\', \'dismiss\')">Dismiss</button>';
    } else {
        html += '<p>Approved by ' + escHtml(a.approved_by) + ' on ' + escHtml(formatFullDate(a.approved_at)) + (a.expired ? ' — <b>expired</b>.' : '; valid until ' + escHtml(formatFullDate(a.expires_at)) + '.') + '</p>';
        if (!a.expired) html += '<button class="btn-trigger" style="background:#d97706" onclick="approvalAction(\'' + id + '\', \'run\')">Run approved backup</button> ';
        html += '<button class="btn-trigger" style="background:#4b5563" onclick="approvalAction(\'' + id + '\', \'dismiss\')">Dismiss</button>';
    }
    return html + '</div>';
}
async function updateApprovals() {
    try {
        const response = await fetch('/approvals');
        if (!response.ok) return;
        const data = await response.json();
        const sig = JSON.stringify(data);
        if (sig === _approvalsSig) return;          // keep a half-typed phrase
        _approvalsSig = sig;
        var box = document.getElementById('approvals-container');
        if (box) box.innerHTML = (data.approvals || []).map(function(a) { return approvalCard(a, data.auth_enabled); }).join('');
    } catch (err) { console.error('Failed to load approvals:', err); }
}
async function approvalAction(id, action) {
    var opts = { method: 'POST' };
    if (action === 'approve') {
        var input = document.getElementById('phrase-' + id);
        opts.headers = { 'Content-Type': 'application/json' };
        opts.body = JSON.stringify({ phrase: input ? input.value : '' });
    } else if (action === 'dismiss' && !confirm('Dismiss this request? Nothing will run on it; the next scheduled backup checks again.')) {
        return;
    }
    try {
        const response = await fetch('/approvals/' + encodeURIComponent(id) + '/' + action + '?wait=false', opts);
        const data = await response.json();
        showToast(data.detail || (response.ok ? 'Done.' : 'Request failed.'), response.ok ? 'success' : 'error');
    } catch (err) { showToast('Network connection error: ' + err.message, 'error'); }
    _approvalsSig = '';
    await updateApprovals();
    await updateStatus();
}
setInterval(updateApprovals, 10000);
window.addEventListener('DOMContentLoaded', updateApprovals);
