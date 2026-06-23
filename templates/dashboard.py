"""Dashboard HTML template — pure data-to-HTML rendering.

No imports from ui.py, core/, or models/. Accepts all data as keyword arguments.
"""

CSS = """<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: system-ui, sans-serif; background: #111827; color: #e5e7eb; padding: 2rem; }
h1 { font-size: 1.5rem; margin-bottom: 1.5rem; color: #f9fafb; }
.flash { padding: 0.75rem 1rem; border-radius: 0.375rem; margin-bottom: 1rem; font-weight: 600; }
.flash.success { background: #065f46; color: #6ee7b7; }
.flash.warning { background: #78350f; color: #fcd34d; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 1rem; margin-bottom: 2rem; }
.card { background: #1f2937; border-radius: 0.5rem; padding: 1.25rem; border-left: 4px solid; }
.card.success { border-color: #10b981; }
.card.running { border-color: #f59e0b; }
.card.failed { border-color: #ef4444; }
.card.unknown { border-color: #6b7280; }
.card h2 { font-size: 1rem; margin-bottom: 0.75rem; }
.status-badge { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 999px; font-size: 0.75rem; font-weight: 600; }
.status-badge.running { background: #f59e0b; color: #000; }
.status-badge.success { background: #10b981; color: #000; }
.status-badge.failed { background: #ef4444; color: #fff; }
.status-badge.unknown { background: #6b7280; color: #fff; }
.stats { display: flex; gap: 1rem; margin-bottom: 1rem; flex-wrap: wrap; }
.stat { background: #1f2937; border-radius: 0.5rem; padding: 1rem; flex: 1; min-width: 120px; text-align: center; }
.stat .num { font-size: 2rem; font-weight: 700; color: #60a5fa; }
.stat .label { font-size: 0.75rem; color: #9ca3af; margin-top: 0.25rem; }
button { padding: 0.5rem 1rem; border: none; border-radius: 0.375rem; font-weight: 600; cursor: pointer; margin-right: 0.5rem; }
.btn-trigger { background: #2563eb; color: #fff; }
.btn-trigger:hover:not(:disabled) { background: #1d4ed8; }
.btn-trigger:disabled { background: #374151; color: #6b7280; cursor: not-allowed; }
table { width: 100%; border-collapse: collapse; font-size: 0.8rem; margin-top: 1.5rem; }
th { text-align: left; color: #9ca3af; padding: 0.5rem 0.75rem; border-bottom: 1px solid #374151; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; }
td { padding: 0.5rem 0.75rem; border-bottom: 1px solid #1f2937; }
tr:hover { background: #1f2937; }
.tag { display: inline-block; padding: 0.1rem 0.4rem; border-radius: 999px; font-size: 0.65rem; font-weight: 600; }
.tag.cloud { background: #1e3a5f; color: #93c5fd; }
.tag.lan { background: #14532d; color: #86efac; }
.tag.success { background: #065f46; color: #6ee7b7; }
.tag.partial { background: #78350f; color: #fcd34d; }
.tag.failed { background: #7f1d1d; color: #fca5a5; }
.success-ago { color: #6ee7b7; }
.warn-ago { color: #fcd34d; }
.critical-ago { color: #fca5a5; }
.schedule-line { font-size: 0.75rem; color: #9ca3af; margin-top: 0.3rem; }
.info { color: #9ca3af; font-size: 0.8rem; margin-top: 1.5rem; }
.metrics-row { background-color: #1f2937; }
.metrics-row td { padding: 0; border-bottom: none; }
.metrics-container { padding: 0; max-height: 0; overflow: hidden; transition: max-height 0.3s ease-out, padding 0.3s ease-out; }
.metrics-container.open { padding: 1rem; max-height: 200px; border-bottom: 1px solid #374151; }
.metrics-badge { display: inline-block; padding: 0.25rem 0.75rem; background-color: #374151; color: #e5e7eb; border-radius: 9999px; font-size: 0.75rem; margin-right: 0.5rem; margin-bottom: 0.5rem; border: 1px solid #4b5563; }
.metrics-badge.added { color: #34d399; }
.metrics-badge.modified { color: #60a5fa; }
.metrics-badge.removed { color: #f87171; }
.metrics-badge.verified { color: #34d399; }
.metrics-badge.size { color: #a78bfa; }
tr.expandable { cursor: pointer; transition: background 0.2s; }
tr.expandable:hover { background: #374151; }
</style>"""

JS = """<script>
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

async function updateStatus() {
    try {
        const response = await fetch('/status');
        if (!response.ok) return;
        const data = await response.json();

        function timeAgo(isoStr) {
            if (!isoStr) return null;
            var then = new Date(isoStr);
            if (isNaN(then.getTime())) return null;
            var diff = Math.floor((Date.now() - then) / 1000);
            if (diff < 0) diff = 0;
            if (diff < 60) return 'just now';
            if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
            if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
            if (diff < 172800) return '1d ago';
            return Math.floor(diff / 86400) + 'd ago';
        }

        function agoClass(diffDays) {
            if (diffDays < 1) return 'success-ago';
            if (diffDays < 2) return 'warn-ago';
            return 'critical-ago';
        }

        function showLastSuccess(mode, lastSuccess) {
            var el = document.getElementById(mode + '-last-success');
            if (!el) return;
            if (!lastSuccess) {
                el.innerHTML = '<span class="critical-ago">\u26A0\uFE0F No successful backup yet</span>';
                return;
            }
            var ago = timeAgo(lastSuccess);
            if (!ago) {
                el.innerHTML = '<span class="critical-ago">\u26A0\uFE0F No successful backup yet</span>';
                return;
            }
            var then = new Date(lastSuccess);
            var now = new Date();
            var diffDays = (now - then) / 86400000;
            if (isNaN(diffDays)) {
                el.innerHTML = '<span class="critical-ago">\u26A0\uFE0F No successful backup yet</span>';
                return;
            }
            var cls = agoClass(diffDays);
            var icon = diffDays < 1 ? '\u2705' : (diffDays < 2 ? '\u26A0\uFE0F' : '\u274C');
            el.innerHTML = '<span class="' + cls + '">' + icon + ' Last success: ' + ago + '</span>';
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
            if (data.cloud.last_run) { let desc = data.cloud.last_run.status + ' (' + data.cloud.last_run.files + ' changed)'; if (data.cloud.last_run.error) { desc += ' — ' + data.cloud.last_run.error.substring(0, 60); } descCloud.innerText = desc; lastCloud.innerText = 'Last: ' + data.cloud.last_run_formatted; }
            const cloudClass = isCloudRunning ? 'running' : ((data.cloud.last_run && data.cloud.last_run.status.endsWith('_COMPLETE')) ? 'success' : 'failed');
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
            if (data.lan.last_run) { let desc = data.lan.last_run.status + ' (' + data.lan.last_run.files + ' changed)'; if (data.lan.last_run.error) { desc += ' — ' + data.lan.last_run.error.substring(0, 60); } descLan.innerText = desc; lastLan.innerText = 'Last: ' + data.lan.last_run_formatted; }
            const lanClass = isLanRunning ? 'running' : ((data.lan.last_run && data.lan.last_run.status.endsWith('_COMPLETE')) ? 'success' : 'failed');
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
            data.recent_runs.forEach((r, idx) => {
                const escapeHtml = function(text) { return (text || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); };
                const safeMode = ['cloud', 'lan'].includes(r.mode) ? r.mode : 'unknown';
                const modeTag = '<span class="tag ' + safeMode + '">' + escapeHtml(safeMode.toUpperCase()) + '</span>';
                let sTag = ''; if (r.status === 'CLOUD_NO_CHANGES_COMPLETE') { sTag = '<span class="tag success">NO CHANGES</span>'; } else if (r.status.endsWith('_COMPLETE')) { sTag = '<span class="tag success">OK</span>'; } else if (r.status.includes('_PARTIAL')) { sTag = '<span class="tag partial">PARTIAL</span>'; } else if (r.status.includes('_FAILED')) { sTag = '<span class="tag failed">FAILED</span>'; } else { sTag = '<span class="tag">' + escapeHtml(r.status.substring(0, 10)) + '</span>'; }
                const errCell = r.error ? '<td style="color:#fca5a5;max-width:200px;overflow:hidden;text-overflow:ellipsis">' + escapeHtml(r.error.substring(0, 60)) + '</td>' : '<td>-</td>';
                
                let expandableClass = '';
                let extraRowHtml = '';
                if (r.extended_metrics) {
                    try {
                        const metrics = JSON.parse(r.extended_metrics);
                        expandableClass = ' class="expandable" onclick="document.getElementById(\\'metrics-container-' + idx + '\\').classList.toggle(\\'open\\')"';
                        let badges = '';
                        if (r.mode === 'lan') {
                            badges += '<span class="metrics-badge added">➕ Added: ' + (metrics.added || 0) + '</span>';
                            badges += '<span class="metrics-badge modified">✏️ Modified: ' + (metrics.modified || 0) + '</span>';
                            badges += '<span class="metrics-badge removed">🗑️ Pruned: ' + (metrics.removed || 0) + '</span>';
                            badges += '<span class="metrics-badge">📦 Active Destination Files: ' + (metrics.total_files || 0) + '</span>';
                        } else if (r.mode === 'cloud') {
                            badges += '<span class="metrics-badge verified">' + (metrics.verified ? '✅ Verification Passed' : '❌ Verification Failed') + '</span>';
                            badges += '<span class="metrics-badge">☁️ Total Tracked Files: ' + (metrics.total_files || 0) + '</span>';
                            badges += '<span class="metrics-badge size">💾 Space Consumed: ' + (metrics.total_size_gb || 0).toFixed(3) + ' GB</span>';
                        }
                        const isOpen = openRows.has(idx) ? ' open' : '';
                        extraRowHtml = '<tr class="metrics-row"><td colspan="6"><div id="metrics-container-' + idx + '" class="metrics-container' + isOpen + '">' + badges + '</div></td></tr>';
                    } catch(e) { }
                }

                rowsHtml += '<tr' + expandableClass + '><td>' + escapeHtml(r.started_at) + '</td><td>' + modeTag + '</td><td>' + sTag + '</td><td>' + escapeHtml(String(r.files)) + '</td><td>' + escapeHtml(String(r.duration)) + '</td>' + errCell + '</tr>\\n';
                rowsHtml += extraRowHtml;
            });
            tbody.innerHTML = rowsHtml;
        }
    } catch (err) { console.error('Failed to update status dynamically:', err); }
}
setInterval(updateStatus, 2000);
window.addEventListener('DOMContentLoaded', updateStatus);
</script>"""

REPORT_JS = """<script>
function generateReport(period) {
    window.location.href = '/report/' + period;
}

async function emailReport(period) {
    const label = period === 'weekly' ? 'Weekly' : 'Monthly';
    if (!confirm('Send the ' + label + ' Backup Report now via email to all configured recipients?')) return;
    const btn = document.getElementById('btn-email-' + period);
    if (btn) { btn.style.pointerEvents = 'none'; btn.style.opacity = '0.5'; btn.innerText = 'Sending...'; }
    try {
        const response = await fetch('/trigger/report/' + period + '/email', { method: 'POST' });
        const data = await response.json();
        if (response.ok) {
            showToast(data.detail || 'Report emailed successfully!', 'success');
        } else if (response.status === 404) {
            showToast(data.detail || 'No runs found for this period.', 'info');
        } else {
            showToast(data.detail || 'Failed to send email.', 'error');
        }
    } catch (err) {
        showToast('Network error: ' + err.message, 'error');
    } finally {
        if (btn) { btn.style.pointerEvents = ''; btn.style.opacity = ''; btn.innerText = '\u2709\ufe0f Email ' + label + ' Report'; }
    }
}
</script>"""


def render_dashboard(
    *,
    lan_files: int = 0,
    cloud_files: int = 0,
    fy_prefix: str = "",
    cloud_class: str = "unknown",
    cloud_running: str = "Unknown",
    cloud_run: str = "Unknown",
    cloud_last: str = "No data",
    cloud_btn: str = "",
    lan_class: str = "unknown",
    lan_running: str = "Unknown",
    lan_run: str = "Unknown",
    lan_last: str = "No data",
    lan_btn: str = "",
    health_info: str = "Unavailable",
    flash_html: str = "",
    history_rows: str = "",
    auth_enabled: bool = False,
    cloud_schedule: str = "",
    lan_schedule: str = "",
    cloud_last_success: str | None = None,
    lan_last_success: str | None = None,
) -> str:
    """Render the dashboard HTML. Pure function — no I/O, no imports."""
    logout_link = " · <a href='/logout' style='color:#60a5fa'>Logout</a>" if auth_enabled else ""
    no_runs_row = '<tr><td colspan="6" style="color:#6b7280">No runs recorded yet</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AAM Backup Dashboard</title>
{CSS}
</head>
<body>
<div id="toast-container" style="position: fixed; top: 1.5rem; right: 1.5rem; z-index: 10000; display: flex; flex-direction: column; gap: 0.75rem; max-width: 350px;"></div>
<div class="container">
<h1>AAM Backup Dashboard</h1>
<div class="subtitle">Secure Automated Sequential Multi-Destination Backups</div>
<div id="flash-container">{flash_html}</div>
<div class="stats">
    <div class="stat"><div class="num" id="stat-lan-files">{lan_files:,}</div><div class="label">LAN Files</div></div>
    <div class="stat"><div class="num" id="stat-cloud-files">{cloud_files:,}</div><div class="label">Cloud Files</div></div>
    <div class="stat"><div class="num" id="stat-fy-prefix">{fy_prefix}</div><div class="label">FY Prefix</div></div>
</div>
<div class="grid">
    <div class="card {cloud_class}" id="card-cloud">
        <h2>Cloud Backup <span class="status-badge {cloud_class}" id="badge-cloud">{cloud_running}</span></h2>
        <p id="desc-cloud">{cloud_run}</p>
        <p style="font-size:0.75rem;color:#9ca3af;margin-top:0.5rem" id="last-cloud">Last: {cloud_last}</p>
        <p class="schedule-line">Next: {cloud_schedule or 'Not configured'}</p>
        <p style="font-size:0.75rem" id="cloud-last-success"></p>
        <form style="margin-top:1rem" onsubmit="triggerBackup(event, 'cloud')">
            <button class="btn-trigger btn-cloud" id="btn-cloud" {cloud_btn}>Run Cloud Backup</button>
        </form>
    </div>
    <div class="card {lan_class}" id="card-lan">
        <h2>LAN Backup <span class="status-badge {lan_class}" id="badge-lan">{lan_running}</span></h2>
        <p id="desc-lan">{lan_run}</p>
        <p style="font-size:0.75rem;color:#9ca3af;margin-top:0.5rem" id="last-lan">Last: {lan_last}</p>
        <p class="schedule-line">Next: {lan_schedule or 'Not configured'}</p>
        <p style="font-size:0.75rem" id="lan-last-success"></p>
        <form style="margin-top:1rem" onsubmit="triggerBackup(event, 'lan')">
            <button class="btn-trigger btn-lan" id="btn-lan" {lan_btn}>Run LAN Backup</button>
        </form>
    </div>
</div>
<div style="margin-bottom:1rem;display:flex;gap:0.5rem;flex-wrap:wrap">
    <button class="btn-trigger" onclick="generateReport('weekly')">&#8203;&#128196; Download Weekly Report</button>
    <button class="btn-trigger" onclick="generateReport('monthly')">&#8203;&#128196; Download Monthly Report</button>
    <button class="btn-trigger" id="btn-email-weekly" onclick="emailReport('weekly')" style="background:#0e7490">&#9993;&#65039; Email Weekly Report</button>
    <button class="btn-trigger" id="btn-email-monthly" onclick="emailReport('monthly')" style="background:#0e7490">&#9993;&#65039; Email Monthly Report</button>
</div>
<h2 style="margin-top:2rem;margin-bottom:0.75rem;color:#9ca3af;font-size:0.9rem;">Run History</h2>
<table>
<thead><tr><th>Time</th><th>Pipeline</th><th>Status</th><th>Changed</th><th>Duration</th><th>Error</th></tr></thead>
<tbody id="history-tbody">{history_rows or no_runs_row}</tbody>
</table>
<div class="info">
    <p id="health-info">{health_info}</p>
    <p style="margin-top:0.25rem">AAM Backup Automation V1 — Dynamic Real-Time Updates{logout_link}</p>
</div>
</div>
{JS}
{REPORT_JS}
</body>
</html>"""
