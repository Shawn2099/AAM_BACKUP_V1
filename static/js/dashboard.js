function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = 'toast ' + type;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => { toast.classList.add('shown'); }, 50);
    setTimeout(() => { toast.classList.remove('shown'); setTimeout(() => { toast.remove(); }, 300); }, 5000);
}

async function triggerBackup(event, mode) {
    event.preventDefault();
    const btnInfo = document.getElementById('btn-' + mode);
    var lanSteps = [];
    if (btnInfo && btnInfo.dataset.wol === 'yes') lanSteps.push('wakes the NAS');
    lanSteps.push('copies the changes');
    if (btnInfo && btnInfo.dataset.shutdown === 'yes') lanSteps.push('switches the NAS off afterwards');
    const confirmMsg = mode === 'cloud' ? 'Start the online backup now?'
        : 'Start the office NAS backup now? It ' + lanSteps.join(', ') + '.';
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

// Every client-facing time is IST, whatever timezone the viewing PC is set to
// (getHours() would use the browser's own zone).
function formatFullDate(isoStr) {
    if (!isoStr) return '-';
    var d = new Date(isoStr);
    if (isNaN(d.getTime())) return isoStr;
    var p = {};
    new Intl.DateTimeFormat('en-IN', { timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short',
        year: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true })
        .formatToParts(d).forEach(function(x) { p[x.type] = x.value; });
    return p.day + ' ' + p.month + ' ' + p.year + ' at ' + p.hour + ':' + p.minute + ' ' +
        String(p.dayPeriod || '').toUpperCase() + ' IST';
}

var _lastStatusErr = 0;
// Audit 2026-10-18: a dashboard that cannot reach the server must say so - the
// cards would otherwise keep showing the last (possibly green) state.
var _lastOk = null;
function setServerUnreachable(down, why) {
    var banner = document.getElementById('server-banner');
    if (!down) {
        _lastOk = new Date();
        if (banner) banner.style.display = 'none';
        var lu = document.getElementById('last-updated');
        if (lu) lu.textContent = 'Last updated: ' + formatFullDate(_lastOk.toISOString());
        return;
    }
    if (banner) {
        banner.style.display = 'block';
        banner.textContent = 'Cannot reach the backup server since ' +
            (_lastOk ? formatFullDate(_lastOk.toISOString()) : 'this page opened') +
            ' (' + why + '). The information below may be out of date.';
    }
    setHero('unknown', 'Backup status is unavailable', 'This page cannot reach the backup server right now.');
    ['cloud', 'lan'].forEach(function(mode) {
        var card = document.getElementById('card-' + mode);
        var badge = document.getElementById('badge-' + mode);
        if (card) card.className = 'card unknown';
        if (badge) { badge.innerText = 'Unknown'; badge.className = 'status-badge unknown'; }
    });
}
function setSessionExpired(expired) {
    var banner = document.getElementById('session-banner');
    if (banner) banner.style.display = expired ? 'block' : 'none';
    if (!expired) return;
    setHero('unknown', 'Sign in again to see your backups', 'Your session has expired.');
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
    var when = integrity.checked_at ? ' (checked ' + formatFullDate(integrity.checked_at) + ')' : '';
    if (s === 'VERIFIED') return 'Integrity: VERIFIED' + when;
    if (s === 'VERIFICATION_FAILED') return 'Integrity: VERIFICATION FAILED' + when + ' — see audit detail';
    // WP7: the audit could not reach a verdict (network/TLS/DNS/process) - not divergence
    if (s === 'AUDIT_INCOMPLETE') return 'Integrity: AUDIT COULD NOT RUN' + when + (integrity.detail ? ' — ' + integrity.detail : '');
    return 'Integrity: NOT VERIFIED — no audit recorded';
}
// Plan WP9: what needs attention, computed by the server (core/attention.py).
function renderAttention(items) {
    var box = document.getElementById('attention-banner');
    if (!box) return;
    if (!items || !items.length) { box.innerHTML = ''; return; }
    box.innerHTML = items.map(function(i) {
        var red = i.level === 'red';
        return '<div class="banner ' + (red ? 'red' : 'amber') + '">' + escHtml(i.message) + '</div>';
    }).join('');
}

// The one sentence at the top: what a partner needs to know without reading the cards.
// Built only from what the server already decided (status categories, attention items).
var _OK_CATEGORIES = ['success', 'no-changes'];
function agoText(iso) {
    var ms = Date.now() - new Date(iso).getTime();
    if (isNaN(ms) || ms < 0) return 'recently';
    var h = Math.floor(ms / 3600000);
    if (h < 1) return 'less than an hour ago';
    if (h < 48) return h + (h === 1 ? ' hour ago' : ' hours ago');
    return Math.floor(h / 24) + ' days ago';
}
function setHero(tone, line, sub) {
    var hero = document.getElementById('hero');
    if (!hero) return;
    hero.setAttribute('data-tone', tone);
    document.getElementById('hero-line').textContent = line;
    document.getElementById('hero-sub').textContent = sub;
}
function renderHero(data) {
    var legs = [['cloud', 'The cloud backup'], ['lan', 'The office NAS backup']].filter(function(l) {
        return data[l[0]] && data[l[0]].enabled !== false;
    });
    if (!legs.length) { setHero('unknown', 'Backups are switched off', 'Both backups are disabled in the settings.'); return; }
    var attention = data.attention || [];
    var hasRed = attention.some(function(i) { return i.level === 'red'; });
    var problems = [], running = [], unknown = [], partial = [];
    legs.forEach(function(l) {
        var d = data[l[0]];
        var cat = d.last_run ? d.last_run.status_category : 'unknown';
        if (d.running) running.push(l[1]);
        else if (d.run_state === 'unknown') unknown.push(l[1]);
        else if (cat === 'partial') partial.push(l[1]);
        else if (_OK_CATEGORIES.indexOf(cat) < 0) problems.push(l[1] + ': ' + (d.last_run ? d.last_run.status_label : 'no backup yet'));
    });
    if (problems.length || hasRed) {
        setHero('bad', 'Your backups need attention', problems.length ? problems.join('. ') + '.' : 'Read the notice below.');
    } else if (unknown.length) {
        setHero('unknown', 'Backup status is unavailable', 'The server could not say what is running. This page tries again every few seconds.');
    } else if (partial.length) {
        setHero('warn', 'A backup finished only partly', partial.join(' and ') + ' copied some of the files; the rest follows on the next run.');
    } else if (running.length) {
        setHero('warn', 'A backup is running now', running.join(' and ') + ' is copying files at the moment.');
    } else if (attention.length) {
        setHero('warn', 'The latest backups succeeded, with a warning', 'Read the notice below.');
    } else {
        var parts = legs.map(function(l) {
            var ls = data[l[0]].last_success;
            return l[1] + ' last succeeded ' + (ls ? agoText(ls) : 'never');
        });
        setHero('ok', 'Everything is backed up', parts.join('. ') + '.');
    }
}
async function updateStatus() {
    try {
        const response = await fetch('/status');
        if (response.status === 401) { setSessionExpired(true); return; }
        if (!response.ok) { setServerUnreachable(true, 'the server answered ' + response.status); return; }
        setSessionExpired(false);
        setServerUnreachable(false);
        _lastStatusErr = 0;
        const data = await response.json();
        renderAttention(data.attention);
        renderHero(data);

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
            var prefix = diffDays < 1 ? '' : (diffDays < 2 ? 'Late - ' : 'Overdue - ');
            var fullDate = formatFullDate(lastSuccess);
            el.textContent = prefix + 'Last success: ' + fullDate;
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
                badgeCloud.innerText = isCloudRunning ? 'Running' : (cloudUnknown ? 'Unknown' : (data.cloud.last_run ? data.cloud.last_run.status_label : 'No backup yet'));
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
                badgeLan.innerText = isLanRunning ? 'Running' : (lanUnknown ? 'Unknown' : (data.lan.last_run ? data.lan.last_run.status_label : 'No backup yet'));
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
                const safeMode = ['cloud', 'lan', 'rollover'].includes(r.mode) ? r.mode : 'unknown';
                const modeTag = '<span class="tag ' + safeMode + '">' + escapeHtml(safeMode.toUpperCase()) + '</span>';
                const sTag = '<span class="tag ' + safeCategory(r.status_category) + '" title="' + escapeHtml(r.status) + '">' + escapeHtml(r.status_label || r.status) + '</span>';
                // plain title + fault code first (audit 2026-10-18); the raw error stays in the tooltip
                const errText = r.fault_title ? r.fault_title + ' (' + r.fault_code + ')' : r.error;
                const errCell = errText ? '<td class="err-cell" title="' + escapeHtml(r.error || errText) + '">' + escapeHtml(errText) + '</td>' : '<td>-</td>';
                
                let expandableClass = '';
                let expandIcon = '';
                let extraRowHtml = '';
                if (r.extended_metrics) {
                    try {
                        const metrics = JSON.parse(r.extended_metrics);
                        let badges = '';
                        if (r.mode === 'lan' && 'added' in metrics) {
                            badges += '<span class="metrics-badge added">Added: ' + (metrics.added || 0) + '</span>';
                            badges += '<span class="metrics-badge modified">Modified: ' + (metrics.modified || 0) + '</span>';
                            badges += '<span class="metrics-badge removed">Pruned: ' + (metrics.removed || 0) + '</span>';
                            badges += '<span class="metrics-badge">Active Destination Files: ' + (metrics.total_files || 0) + '</span>';
                        } else if (r.mode === 'cloud' && 'total_files' in metrics) {
                            // WP7: the daily check compares SIZES only - it is a size check, not content verification
                            var sizeOk = ('size_check_passed' in metrics) ? metrics.size_check_passed : metrics.verified;
                            badges += '<span class="metrics-badge verified">' + (sizeOk === undefined ? 'Size check unknown' : (sizeOk ? 'Size check passed' : 'Size check failed')) + '</span>';
                            badges += '<span class="metrics-badge">Total Tracked Files: ' + (metrics.total_files || 0) + '</span>';
                            badges += '<span class="metrics-badge size">Space Consumed: ' + (metrics.total_size_gb || 0).toFixed(3) + ' GB</span>';
                        }
                        // only runs that measured something are expandable (a blocked run has no counts)
                        if (badges) {
                            const iconCls = openRows.has(idx) ? ' expand-icon open' : ' expand-icon';
                            expandIcon = '<span class="' + iconCls + '">▸</span>';
                            expandableClass = ' class="expandable" onclick="document.getElementById(\'metrics-container-' + idx + '\').classList.toggle(\'open\'); this.querySelector(\'.expand-icon\').classList.toggle(\'open\')"';
                            const isOpen = openRows.has(idx) ? ' open' : '';
                            extraRowHtml = '<tr class="metrics-row"><td colspan="6"><div id="metrics-container-' + idx + '" class="metrics-container' + isOpen + '">' + badges + '</div></td></tr>';
                        }
                    } catch(e) { }
                }

                rowsHtml += '<tr' + expandableClass + '><td>' + expandIcon + escapeHtml(r.started_at) + '</td><td>' + modeTag + '</td><td>' + sTag + '</td><td>' + escapeHtml(String(r.files)) + '</td><td>' + escapeHtml(String(r.duration)) + '</td>' + errCell + '</tr>\n';
                rowsHtml += extraRowHtml;
            });
            tbody.innerHTML = rowsHtml;
        } else if (tbody && data.recent_runs && data.recent_runs.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" class="empty">No backup runs recorded yet. The first scheduled backup will appear here.</td></tr>';
        }
    } catch (err) {
        setServerUnreachable(true, 'no answer');
        var now = Date.now(); if (now - _lastStatusErr > 30000) { console.error('Failed to update status dynamically:', err); _lastStatusErr = now; }
    }
}
setInterval(updateStatus, 2000);
window.addEventListener('DOMContentLoaded', updateStatus);

// ── Blocked backups waiting for a human decision (plan §6.2) ──────────────
// A guard stopped a backup before anything was copied or deleted. The server
// owns every decision; this only shows the reason/numbers and posts the
// typed confirmation phrase.
function escHtml(text) { return (text == null ? '' : String(text)).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
var _approvalsSig = '';
function approvalCard(a) {
    var leg = a.leg === 'cloud' ? 'Cloud backup' : (a.leg === 'rollover' ? 'FY rollover' : 'LAN backup');
    var reasons = (a.guards || []).map(function(g) { return '<li>' + escHtml(g.reason) + '</li>'; }).join('');
    var id = escHtml(a.approval_id);
    var html = '<div class="approval-card ' + (a.status === 'APPROVED' ? 'approved' : 'pending') + '">';
    html += '<h2>' + leg + ' blocked — ' + (a.status === 'APPROVED' ? 'approved, not run yet' : 'your decision is needed') + '</h2>';
    html += '<ul>' + reasons + '</ul>';
    html += '<p class="fine">' + (a.leg === 'rollover' ? 'Nothing was changed.' : 'Nothing was copied or deleted.') + ' Blocked ' + escHtml(formatFullDate(a.created_at)) + ' (run ' + escHtml(a.blocked_run_id) + ').</p>';
    if (a.status === 'PENDING') {
        html += '<p>To run this <b>once</b> anyway, type <code>' + escHtml(a.phrase) + '</code> and approve. The backup re-checks first and stops again if anything got worse.</p>';
        html += '<input id="phrase-' + id + '" type="text" autocomplete="off" placeholder="' + escHtml(a.phrase) + '"><br>';
        html += '<button class="btn-danger" onclick="approvalAction(\'' + id + '\', \'approve\')">Approve this backup once</button> ';
        html += '<button class="btn-secondary" onclick="approvalAction(\'' + id + '\', \'dismiss\')">Dismiss</button>';
    } else {
        html += '<p>Approved by ' + escHtml(a.approved_by) + ' on ' + escHtml(formatFullDate(a.approved_at)) + (a.expired ? ' — <b>expired</b>.' : '; valid until ' + escHtml(formatFullDate(a.expires_at)) + '.') + '</p>';
        if (!a.expired) html += '<button class="btn-warn" onclick="approvalAction(\'' + id + '\', \'run\')">Run approved backup</button> ';
        html += '<button class="btn-secondary" onclick="approvalAction(\'' + id + '\', \'dismiss\')">Dismiss</button>';
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
        if (box) box.innerHTML = (data.approvals || []).map(function(a) { return approvalCard(a); }).join('');
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
