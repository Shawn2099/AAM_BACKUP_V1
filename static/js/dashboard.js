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
        var desc0 = document.getElementById('desc-' + mode);
        if (desc0 && /^Fetching/.test(desc0.innerText)) desc0.innerText = 'Status unavailable - cannot reach the server.';
        if (card) card.className = 'dest tone-idle';
        if (badge) { badge.innerText = 'Unknown'; badge.className = 'pill tone-idle'; }
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
        if (badge) { badge.innerText = 'Unknown'; badge.className = 'pill tone-idle'; }
        if (desc) desc.innerText = 'Status unavailable — session may have expired. Sign in again.';
        if (card) card.className = 'dest tone-idle';
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
// Status category / hero tone -> the three tones the stylesheet knows (plus idle for "no news").
var _TONE = { 'success': 'ok', 'no-changes': 'ok', 'ok': 'ok', 'partial': 'warn', 'running': 'warn', 'warn': 'warn',
              'failed': 'bad', 'blocked': 'bad', 'bad': 'bad', 'skipped': 'idle', 'unknown': 'idle', 'idle': 'idle' };
function toneOf(category) { return _TONE[category] || 'idle'; }
var _GLYPH = { ok: 'M30 52l14 14 27-30', bad: 'M50 30v26M50 69v.01', warn: 'M34 50h.01M50 50h.01M66 50h.01',
               idle: 'M41 41c0-10 18-10 18 0 0 8-9 8-9 17M50 70v.01' };
function setHero(tone, line, sub) {
    var hero = document.getElementById('hero');
    if (!hero) return;
    var t = toneOf(tone);
    var wasOk = hero.classList.contains('tone-ok');
    hero.className = 'band tone-' + t;
    var glyph = document.getElementById('seal-glyph');
    if (glyph) {
        glyph.setAttribute('d', _GLYPH[t]);
        // the check mark draws itself once, when the page first says "safe"
        glyph.setAttribute('class', 'glyph' + (t === 'ok' && !wasOk ? ' draw' : ''));
    }
    document.getElementById('hero-line').textContent = line;
    document.getElementById('hero-sub').textContent = sub;
    var action = document.getElementById('hero-action');
    if (action) {
        var decision = document.querySelector('.approval-card');
        action.hidden = t !== 'bad';
        action.textContent = decision ? 'Review the decision' : 'See what happened';
        action.setAttribute('href', decision ? '#approvals-container' : '#history');
    }
}
function renderHero(data) {
    var legs = [['cloud', 'The cloud backup'], ['lan', 'The office NAS backup']].filter(function(l) {
        return data[l[0]] && data[l[0]].enabled !== false;
    });
    if (!legs.length) { setHero('unknown', 'Backups are switched off', 'Both backups are disabled in the settings.'); return; }
    var attention = data.attention || [];
    var hasRed = attention.some(function(i) { return i.level === 'red'; });
    var problems = [], running = [], unknown = [], partial = [], waiting = [];
    legs.forEach(function(l) {
        var d = data[l[0]];
        var cat = d.last_run ? d.last_run.status_category : 'unknown';
        if (d.running) running.push(l[1]);
        else if (d.run_state === 'unknown') unknown.push(l[1]);
        else if (!d.last_run) waiting.push(l[1]);
        else if (cat === 'partial') partial.push(l[1]);
        else if (_OK_CATEGORIES.indexOf(cat) < 0) problems.push(l[1] + ': ' + (d.last_run ? d.last_run.status_label : 'no backup yet'));
    });
    if (waiting.length === legs.length && !running.length && !hasRed && !attention.length) {
        setHero('idle', 'Waiting for your first backup', 'Nothing has been backed up yet. The first backup runs at its scheduled time - there is nothing to do now.');
        return;
    }
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
            if (!data[l[0]].last_run) return l[1] + ' has not run yet';
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
                badgeCloud.innerText = 'Disabled'; badgeCloud.className = 'pill tone-idle';
                cardCloud.className = 'dest tone-idle';
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
                else { descCloud.innerText = 'No backup has run yet. The first one starts at the scheduled time.'; lastCloud.innerText = 'Last backup: none yet'; }
                if (isCloudRunning) { descCloud.innerText = 'Copying files to the cloud now.'; }
                const cloudClass = isCloudRunning ? 'running' : (cloudUnknown ? 'unknown' : (data.cloud.last_run ? safeCategory(data.cloud.last_run.status_category) : 'unknown'));
                cardCloud.className = 'dest tone-' + toneOf(cloudClass); badgeCloud.className = 'pill tone-' + toneOf(cloudClass);
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
                badgeLan.innerText = 'Disabled'; badgeLan.className = 'pill tone-idle';
                cardLan.className = 'dest tone-idle';
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
                else { descLan.innerText = 'No backup has run yet. The first one starts at the scheduled time.'; lastLan.innerText = 'Last backup: none yet'; }
                if (isLanRunning) { descLan.innerText = 'Copying files to the office NAS now.'; }
                const lanClass = isLanRunning ? 'running' : (lanUnknown ? 'unknown' : (data.lan.last_run ? safeCategory(data.lan.last_run.status_category) : 'unknown'));
                cardLan.className = 'dest tone-' + toneOf(lanClass); badgeLan.className = 'pill tone-' + toneOf(lanClass);
                showLastSuccess('lan', data.lan.last_success);
            }
        }
        if (data.health && !data.health.error) { document.getElementById('health-info').innerText = 'Source: ' + data.health.source_free_gb + ' GB free | FY: ' + data.fy_prefix; }
        renderLegVisuals(data);
        renderHistory(data.recent_runs);
    } catch (err) {
        setServerUnreachable(true, 'no answer');
        var now = Date.now(); if (now - _lastStatusErr > 30000) { console.error('Failed to update status dynamically:', err); _lastStatusErr = now; }
    }
}
setInterval(updateStatus, 2000);
window.addEventListener('DOMContentLoaded', updateStatus);

// ── Where-your-files-go visuals ──────────────────────────────────────────
function legClass(d) {
    if (!d || d.enabled === false) return 'unknown';
    if (d.running) return 'running';
    if (d.run_state === 'unknown') return 'unknown';
    return d.last_run ? safeCategory(d.last_run.status_category) : 'unknown';
}
function safetyText(integrity) {
    if (!integrity || !integrity.status) return 'Safety check: not run yet';
    var when = integrity.checked_at ? ' (checked ' + formatFullDate(integrity.checked_at) + ')' : '';
    if (integrity.status === 'VERIFIED') return 'Safety check: every file matches' + when;
    if (integrity.status === 'VERIFICATION_FAILED') return 'Safety check found differences - open Details';
    if (integrity.status === 'AUDIT_INCOMPLETE') return 'Safety check could not run - open Details';
    return 'Safety check: not run yet';
}
var _NIGHT_LABEL = { ok: 'Done', partial: 'Partly done', bad: 'Needs attention', none: 'No backup' };
function renderStrip(mode, nights) {
    var el = document.getElementById('strip-' + mode);
    if (!el) return;
    el.innerHTML = (nights || []).map(function(n) {
        var state = _NIGHT_LABEL[n.state] ? n.state : 'none';
        return '<i class="' + state + '" title="' + escHtml(n.date + ': ' + _NIGHT_LABEL[state]) + '"></i>';
    }).join('');
}
function renderLegVisuals(data) {
    ['cloud', 'lan'].forEach(function(mode) {
        var d = data[mode];
        var wire = document.getElementById('wire-' + mode);
        if (wire) wire.setAttribute('class', 'wire tone-' + toneOf(legClass(d)));
        renderStrip(mode, d && d.nights);
        var safety = document.getElementById('safety-' + mode);
        if (safety) safety.textContent = safetyText(d && d.integrity);
    });
    updateVerifyUi(data);
    var free = document.getElementById('stat-free');
    if (free && data.health && !data.health.error) free.textContent = data.health.source_free_gb + ' GB';
    var fy = document.getElementById('source-fy');
    if (fy) fy.textContent = data.fy_prefix;
}

// ── Safety check (read-only audit): start it by hand, show that it is running ─
var _VERIFY_NAME = { lan: 'the office copy', cloud: 'the online copy', all: 'both copies' };
async function triggerVerify(mode) {
    var msg = 'Run the safety check on ' + _VERIFY_NAME[mode] + ' now?\n\nIt compares every file with your original and can take several hours. '
        + 'Nothing is changed. Backups wait until it has finished.';
    if (!confirm(msg)) return;
    var btn = document.getElementById('btn-verify-' + mode);
    if (btn) { btn.disabled = true; btn.innerText = 'Starting...'; }
    try {
        const response = await fetch('/trigger/verify/' + mode, { method: 'POST' });
        const data = await response.json();
        showToast(data.detail || (response.ok ? 'Safety check started.' : 'Could not start the safety check.'), response.ok ? 'success' : 'error');
    } catch (err) { showToast('Network connection error: ' + err.message, 'error'); }
    await updateStatus();
}
function updateVerifyUi(data) {
    var busyBackup = !!((data.cloud && data.cloud.running) || (data.lan && data.lan.running));
    var checking = !!data.verify_running;
    ['lan', 'cloud', 'all'].forEach(function(mode) {
        var btn = document.getElementById('btn-verify-' + mode);
        if (!btn) return;
        var leg = data[mode];
        var off = mode !== 'all' && leg && leg.enabled === false;
        btn.disabled = off || checking || busyBackup;
        btn.innerText = checking ? 'Checking...' : (mode === 'all' ? 'Check both copies now' : 'Check now');
        btn.title = off ? 'This copy is switched off in the settings.'
            : (checking ? 'A safety check is running.' : (busyBackup ? 'A backup is running. Try again when it has finished.' : btn.title));
    });
    var note = document.getElementById('verify-note');
    if (note) note.textContent = checking ? 'A safety check is running now. The result appears on each copy when it finishes.'
        : 'Runs by itself every Sunday. You can also run it now.';
    if (checking) ['cloud', 'lan'].forEach(function(mode) {
        var safety = document.getElementById('safety-' + mode);
        if (safety) safety.textContent = 'Safety check: running now...';
    });
}

// ── Recent backups: a timeline, one plain line per run ───────────────────
var _MODE_NAME = { cloud: 'Online copy', lan: 'Office copy', rollover: 'New financial year' };
function toggleRun(idx) {
    var li = document.getElementById('run-' + idx);
    if (!li) return;
    li.classList.toggle('open');
    li.querySelector('.run-head').setAttribute('aria-expanded', li.classList.contains('open'));
}
function renderHistory(runs) {
    var list = document.getElementById('history-list');
    if (!list || !runs) return;
    if (!runs.length) {
        list.innerHTML = '<li class="empty">No backups yet. The first scheduled backup will appear here.</li>';
        return;
    }
    var open = {};
    runs.forEach(function(_, i) { var el = document.getElementById('run-' + i); if (el && el.classList.contains('open')) open[i] = true; });
    var html = '';
    runs.forEach(function(r, idx) {
        const safeMode = ['cloud', 'lan', 'rollover'].includes(r.mode) ? r.mode : 'unknown';
        var tone = toneOf(safeCategory(r.status_category));
        // plain title + fault code first (audit 2026-10-18); the raw error stays in the detail
        var errText = r.fault_title ? r.fault_title + ' (' + r.fault_code + ')' : r.error;
        var summary = errText ? errText
            : (String(r.files) + ' files' + (r.duration && r.duration !== '-' ? ', took ' + r.duration : ''));
        var badges = '';
        if (r.extended_metrics) {
            try {
                const metrics = JSON.parse(r.extended_metrics);
                if (r.mode === 'lan' && 'added' in metrics) {
                    badges += '<span class="chip added">Added: ' + (metrics.added || 0) + '</span>';
                    badges += '<span class="chip modified">Changed: ' + (metrics.modified || 0) + '</span>';
                    badges += '<span class="chip removed">Removed: ' + (metrics.removed || 0) + '</span>';
                    badges += '<span class="chip">Files on the NAS: ' + (metrics.total_files || 0) + '</span>';
                } else if (r.mode === 'cloud' && 'total_files' in metrics) {
                    // WP7: the daily check compares SIZES only - it is a size check, not content verification
                    var sizeOk = ('size_check_passed' in metrics) ? metrics.size_check_passed : metrics.verified;
                    badges += '<span class="chip verified">' + (sizeOk === undefined ? 'Size check unknown' : (sizeOk ? 'Size check passed' : 'Size check failed')) + '</span>';
                    badges += '<span class="chip">Files in the cloud: ' + (metrics.total_files || 0) + '</span>';
                    badges += '<span class="chip size">Space used: ' + (metrics.total_size_gb || 0).toFixed(3) + ' GB</span>';
                }
            } catch (e) { }
        }
        var detail = badges + (r.error ? '<p class="run-error">' + escHtml(r.error) + '</p>' : '')
            + '<p class="run-tech">Status: ' + escHtml(r.status) + '</p>';
        html += '<li class="run tone-' + tone + (open[idx] ? ' open' : '') + '" id="run-' + idx + '">'
            + '<button class="run-head" aria-expanded="' + (open[idx] ? 'true' : 'false') + '" onclick="toggleRun(' + idx + ')">'
            + '<span class="run-main"><strong>' + (_MODE_NAME[safeMode] || 'Backup') + '</strong> '
            + '<span class="state-word">' + escHtml(r.status_label || r.status) + '</span>'
            + '<small>' + escHtml(summary) + '</small></span>'
            + '<span class="run-when">' + escHtml(r.started_at) + '</span></button>'
            + '<div class="run-detail"><div>' + detail + '</div></div></li>';
    });
    list.innerHTML = html;
}

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

// ── First-visit guide ─────────────────────────────────────────────────────
function dismissGuide() {
    var g = document.getElementById('guide');
    if (g) g.hidden = true;
    try { localStorage.setItem('aam-guide-dismissed', '1'); } catch (e) { }
}
window.addEventListener('DOMContentLoaded', function() {
    var g = document.getElementById('guide');
    try { if (g && localStorage.getItem('aam-guide-dismissed') === '1') g.hidden = true; } catch (e) { }
});
