// Settings page (owner decision DEC-9). The SERVER decides everything:
// validation, which keys are risky / locked / secret. This page draws an
// easy editor per setting, checks the obvious things instantly, then asks
// the server (/api/settings/validate - the exact check a save makes) after
// every edit. Save stays disabled until the server says the values are valid.

const TZ = 'Asia/Kolkata';
const EMAIL_RE = /^[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}$/;
const DOW = [['MON', 'Monday'], ['TUE', 'Tuesday'], ['WED', 'Wednesday'], ['THU', 'Thursday'],
             ['FRI', 'Friday'], ['SAT', 'Saturday'], ['SUN', 'Sunday']];
const DOW_NUM = { '0': 'SUN', '1': 'MON', '2': 'TUE', '3': 'WED', '4': 'THU', '5': 'FRI', '6': 'SAT', '7': 'SUN' };

let catalog = [];
let fields = {};          // key -> field metadata
let original = {};        // key -> value as loaded
let values = {};          // key -> value being edited
let localErrors = {};     // key -> message (browser check)
let serverOk = true;
let authEnabled = false;
let validateTimer = null;
let validateSeq = 0;
let scheduleTz = TZ;
let currentTab = 'basic';
let isAdmin = false;
let adminMinutes = 30;

function esc(text) {
    return (text == null ? '' : String(text)).replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// Every client-facing time is shown in IST, whatever the viewing PC's timezone.
function fmtIST(iso) {
    if (!iso) return '-';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString('en-IN', { timeZone: TZ, day: 'numeric', month: 'short', year: 'numeric',
        hour: 'numeric', minute: '2-digit', hour12: true }) + ' IST';
}

function showToast(message, type) {
    const colors = { success: '#065f46', error: '#7f1d1d', warning: '#78350f', info: '#1e3a8a' };
    const el = document.createElement('div');
    el.style.cssText = 'background:' + (colors[type] || colors.info) + ';color:#fff;padding:0.75rem 1rem;' +
        'border-radius:0.5rem;font-size:0.85rem;box-shadow:0 4px 12px rgba(0,0,0,.4);white-space:pre-line';
    el.textContent = message;
    document.getElementById('toast-container').appendChild(el);
    setTimeout(() => el.remove(), type === 'error' ? 12000 : 7000);
}

function fid(key) { return key.replace(/[^a-zA-Z0-9_]/g, '_'); }
function disabledAttr(f) { return (f.locked || !authEnabled) ? ' disabled' : ''; }

// ── schedules: cron <-> "every day / week / month at HH:MM" ─────────────────
function parseCron(cron) {
    const m = /^(\d{1,2}) (\d{1,2}) (\*|\d{1,2}) \* (\*|[A-Za-z]{3}|[0-7])$/.exec((cron || '').trim());
    if (!m) return { freq: 'custom', cron: cron };
    const time = String(m[2]).padStart(2, '0') + ':' + String(m[1]).padStart(2, '0');
    if (m[3] === '*' && m[4] === '*') return { freq: 'daily', time: time };
    if (m[3] === '*') return { freq: 'weekly', time: time, dow: (DOW_NUM[m[4]] || m[4]).toUpperCase() };
    if (m[4] === '*' && +m[3] >= 1 && +m[3] <= 28) return { freq: 'monthly', time: time, dom: +m[3] };
    return { freq: 'custom', cron: cron };
}

function buildCron(p) {
    if (p.freq === 'custom') return p.cron;
    const [h, mi] = (p.time || '00:00').split(':').map(Number);
    if (p.freq === 'daily') return mi + ' ' + h + ' * * *';
    if (p.freq === 'weekly') return mi + ' ' + h + ' * * ' + p.dow;
    return mi + ' ' + h + ' ' + p.dom + ' * *';
}

function scheduleHtml(f) {
    const id = fid(f.key), p = parseCron(f.value), dis = disabledAttr(f);
    const freq = '<select id="freq-' + id + '"' + dis + '>' +
        [['daily', 'Every day'], ['weekly', 'Every week'], ['monthly', 'Every month'], ['custom', 'Custom (advanced)']]
            .map(o => '<option value="' + o[0] + '"' + (p.freq === o[0] ? ' selected' : '') + '>' + o[1] + '</option>').join('') +
        '</select>';
    const dow = '<select id="dow-' + id + '"' + dis + '>' + DOW.map(d =>
        '<option value="' + d[0] + '"' + (p.dow === d[0] ? ' selected' : '') + '>' + d[1] + '</option>').join('') + '</select>';
    let domOpts = '';
    for (let i = 1; i <= 28; i++) domOpts += '<option' + (p.dom === i ? ' selected' : '') + '>' + i + '</option>';
    const dom = '<select id="dom-' + id + '"' + dis + '>' + domOpts + '</select>';
    const time = '<input type="time" step="60" id="time-' + id + '" value="' + esc(p.time || '21:00') + '"' + dis + '>';
    const custom = '<input type="text" id="cron-' + id + '" value="' + esc(f.value) + '" placeholder="min hour day month weekday"' + dis + '>';
    return '<div class="sched">' + freq + ' <span id="wrap-dow-' + id + '">on ' + dow + '</span>' +
        '<span id="wrap-dom-' + id + '">on day ' + dom + '</span>' +
        '<span id="wrap-time-' + id + '"> at ' + time + ' <span class="tz">' + esc(tzLabel()) + '</span></span>' +
        '<span id="wrap-cron-' + id + '">' + custom + '</span></div>' +
        '<div class="meta" id="human-' + id + '"></div>';
}

function tzLabel() { return scheduleTz === TZ ? 'IST' : scheduleTz; }

function syncSchedule(f) {
    const id = fid(f.key);
    const p = { freq: document.getElementById('freq-' + id).value,
                dow: document.getElementById('dow-' + id).value,
                dom: +document.getElementById('dom-' + id).value,
                time: document.getElementById('time-' + id).value,
                cron: document.getElementById('cron-' + id).value.trim() };
    if (p.freq !== 'custom' && !p.time) { localErrors[f.key] = 'Pick a time.'; }
    document.getElementById('wrap-dow-' + id).style.display = p.freq === 'weekly' ? '' : 'none';
    document.getElementById('wrap-dom-' + id).style.display = p.freq === 'monthly' ? '' : 'none';
    document.getElementById('wrap-time-' + id).style.display = p.freq === 'custom' ? 'none' : '';
    document.getElementById('wrap-cron-' + id).style.display = p.freq === 'custom' ? '' : 'none';
    if (p.freq !== 'custom') document.getElementById('cron-' + id).value = buildCron(p);
    values[f.key] = p.freq === 'custom' ? p.cron : buildCron(p);
    const when = { daily: 'every day', weekly: 'every ' + (DOW.find(d => d[0] === p.dow) || ['', p.dow])[1],
                   monthly: 'on day ' + p.dom + ' of every month' }[p.freq];
    document.getElementById('human-' + id).textContent = p.freq === 'custom'
        ? 'Custom timing (cron): ' + values[f.key]
        : 'Runs ' + when + ' at ' + p.time + ' ' + tzLabel() + '.';
    renderOverview();
}

// ── "how the night looks" - built from the timetable + maximum running times ──
const OVERVIEW_JOBS = [
    ['schedule.lan_cron', 'LAN backup (to the NAS)', 'budget.lan_max_hours'],
    ['schedule.cloud_cron', 'Cloud backup', 'budget.cloud_max_hours'],
    ['schedule.audit_cron', 'Weekly safety check', 'budget.audit_max_hours'],
    ['schedule.weekly_cron', 'Weekly report email', null],
    ['schedule.monthly_cron', 'Monthly report email', null],
];

function renderOverview() {
    const box = document.getElementById('overview');
    if (!box) return;
    const items = OVERVIEW_JOBS.filter(j => fields[j[0]]).map(j => {
        const p = parseCron(values[j[0]]);
        if (p.freq === 'custom') return { mins: 9999, html: '<b>' + esc(j[1]) + '</b>: custom timing' };
        const [h, m] = p.time.split(':').map(Number);
        const when = { daily: 'every day', weekly: 'every ' + (DOW.find(d => d[0] === p.dow) || ['', p.dow])[1],
                       monthly: 'on day ' + p.dom + ' of the month' }[p.freq];
        let until = '';
        const hours = j[2] ? Number(values[j[2]]) : null;
        if (hours) {
            const end = (h * 60 + m + Math.round(hours * 60)) % (24 * 60);
            until = ' &rarr; finished by ' + String(Math.floor(end / 60)).padStart(2, '0') + ':' +
                String(end % 60).padStart(2, '0') + ' at the latest (' + hours + ' h)';
        }
        return { mins: h * 60 + m, html: '<b>' + p.time + '</b> ' + esc(j[1]) + ' <span style="color:#9ca3af">(' +
            when + ')</span>' + until };
    }).sort((a, b) => a.mins - b.mins);
    box.innerHTML = '<div style="font-weight:600;margin-bottom:0.4rem">Your timetable at a glance (' + esc(tzLabel()) +
        ')</div>' + items.map(i => '<div style="margin:0.15rem 0">' + i.html + '</div>').join('') +
        '<div style="color:#9ca3af;font-size:0.75rem;margin-top:0.4rem">Only one backup runs at a time. ' +
        'If two would overlap, the page shows it in red and will not save.</div>';
}

// ── lists: one entry per box, + to add, x to remove ─────────────────────────
function listHtml(f) {
    const id = fid(f.key), items = (f.value || []).length ? f.value : [''];
    const isEmail = f.widget === 'email_list';
    return '<div id="list-' + id + '">' + items.map(v => listRow(f, v, isEmail)).join('') + '</div>' +
        (f.locked || !authEnabled ? '' : '<button type="button" class="btn-small" onclick="addListRow(\'' + f.key + '\')">+ Add ' +
        (isEmail ? 'email' : 'entry') + '</button>');
}

function listRow(f, v, isEmail) {
    return '<div class="list-row"><input type="' + (isEmail ? 'email' : 'text') + '" value="' + esc(v) + '"' +
        (isEmail ? ' placeholder="name@company.com"' : '') + disabledAttr(f) + ' oninput="syncList(\'' + f.key + '\')">' +
        (f.locked || !authEnabled ? '' : '<button type="button" class="btn-small btn-x" title="Remove" onclick="removeListRow(this, \'' + f.key + '\')">&times;</button>') +
        '</div>';
}

function addListRow(key) {
    const f = fields[key];
    const box = document.getElementById('list-' + fid(key));
    box.insertAdjacentHTML('beforeend', listRow(f, '', f.widget === 'email_list'));
    box.lastElementChild.querySelector('input').focus();
    syncList(key);
}

function removeListRow(btn, key) {
    btn.parentElement.remove();
    syncList(key);
}

function syncList(key) {
    const f = fields[key];
    const inputs = [...document.querySelectorAll('#list-' + fid(key) + ' input')];
    const items = inputs.map(i => i.value.trim());
    values[key] = items.filter(x => x.length);
    if (f.widget === 'email_list') {
        inputs.forEach(i => i.classList.toggle('bad', i.value.trim() !== '' && !EMAIL_RE.test(i.value.trim())));
        const bad = values[key].filter(x => !EMAIL_RE.test(x));
        const dup = values[key].filter((x, i, a) => a.indexOf(x) !== i);
        if (bad.length) localErrors[key] = 'Not an email address: ' + bad.join(', ');
        else if (dup.length) localErrors[key] = 'Listed more than once: ' + dup.join(', ');
    }
    onEdit();
}

// ── fixed units (owner: never typed into the value) ─────────────────────────
function withUnit(inputHtml, unit) {
    return unit ? '<div class="with-unit">' + inputHtml + '<span class="unit">' + esc(unit) + '</span></div>' : inputHtml;
}

// rclone size strings ("10M", "500k", "1G") <-> a number of MB
function sizeToMb(v) {
    const m = /^(\d+(?:\.\d+)?)([kMG])$/.exec(String(v || '').trim());
    if (!m) return '';
    const n = Number(m[1]);
    return m[2] === 'k' ? +(n / 1024).toFixed(3) : (m[2] === 'G' ? n * 1024 : n);
}

function mbToSize(n) {
    return Number.isInteger(n) ? n + 'M' : Math.round(n * 1024) + 'k';
}

// ── one editor per field ─────────────────────────────────────────────────────
function editorHtml(f) {
    const id = fid(f.key), dis = disabledAttr(f);
    if (f.widget === 'schedule') return scheduleHtml(f);
    if (f.widget === 'email_list' || f.widget === 'text_list') return listHtml(f);
    if (f.widget === 'toggle') {
        return '<label class="toggle"><input type="checkbox" id="in-' + id + '"' + (f.value ? ' checked' : '') + dis +
            '> <span id="lbl-' + id + '">' + (f.value ? 'On' : 'Off') + '</span></label>';
    }
    if (f.choices) {
        return '<select id="in-' + id + '"' + dis + '>' + f.choices.map(c =>
            '<option' + (c === f.value ? ' selected' : '') + '>' + esc(c) + '</option>').join('') + '</select>';
    }
    if (f.secret) {
        return '<input type="password" id="in-' + id + '" autocomplete="new-password" placeholder="' +
            (f.is_set ? '(set - leave empty to keep it)' : '(not set)') + '"' + dis + '>';
    }
    if (f.widget === 'size_mb') {
        return withUnit('<input type="number" step="any" min="0.01" id="in-' + id + '" value="' +
            esc(sizeToMb(f.value)) + '"' + dis + '>', f.unit);
    }
    if (f.type === 'int' || f.type === 'float') {
        const b = (f.ge != null ? ' min="' + f.ge + '"' : '') + (f.le != null ? ' max="' + f.le + '"' : '');
        return withUnit('<input type="number" step="' + (f.type === 'int' ? '1' : 'any') + '" id="in-' + id + '"' + b +
            ' value="' + esc(f.value == null ? '' : f.value) + '"' + (f.optional ? ' placeholder="empty = automatic"' : '') + dis + '>', f.unit);
    }
    return '<input type="' + (f.widget === 'email' ? 'email' : 'text') + '" id="in-' + id + '" value="' + esc(f.value) + '"' + dis + '>';
}

function readSimple(f) {
    const el = document.getElementById('in-' + fid(f.key));
    if (!el) return;
    if (f.widget === 'toggle') {
        values[f.key] = el.checked;
        document.getElementById('lbl-' + fid(f.key)).textContent = el.checked ? 'On' : 'Off';
        return;
    }
    if (f.secret) { values[f.key] = el.value; return; }
    if (f.widget === 'size_mb') {
        const n = Number(el.value);
        if (el.value.trim() === '' || isNaN(n) || n <= 0) {
            values[f.key] = el.value;
            localErrors[f.key] = 'Enter a number of ' + f.unit + ' greater than 0.';
            return;
        }
        values[f.key] = mbToSize(n);
        if (values[f.key] === mbToSize(Number(sizeToMb(original[f.key])))) values[f.key] = original[f.key];
        return;
    }
    if (f.type === 'int' || f.type === 'float') {
        const raw = el.value.trim();
        if (raw === '') {
            values[f.key] = f.optional ? null : '';
            if (!f.optional) localErrors[f.key] = 'A value is required.';
            return;
        }
        const n = Number(raw);
        values[f.key] = n;
        if (isNaN(n)) localErrors[f.key] = 'Not a number.';
        else if (f.type === 'int' && !Number.isInteger(n)) localErrors[f.key] = 'Whole numbers only.';
        else if (f.ge != null && n < f.ge) localErrors[f.key] = 'Must be at least ' + f.ge + '.';
        else if (f.le != null && n > f.le) localErrors[f.key] = 'Must be at most ' + f.le + '.';
        return;
    }
    values[f.key] = el.value;
    if (f.widget === 'email' && el.value.trim() && !EMAIL_RE.test(el.value.trim())) {
        localErrors[f.key] = 'Not an email address.';
    }
}

function render() {
    document.getElementById('sections').innerHTML = catalog.map((s, i) => {
        const rows = s.fields.map(f => {
            const badges = [];
            if (f.locked) badges.push('<span class="tag badge-locked">changed only on the server</span>');
            if (f.risky) badges.push('<span class="tag badge-risky">important - asks you to confirm</span>');
            if (f.effect === 'agent_restart' || f.effect === 'watchdog_restart') badges.push('<span class="tag badge-restart">needs a restart</span>');
            const meta = [];
            if (f.ge != null || f.le != null) meta.push('allowed ' + (f.ge != null ? f.ge : '...') + ' to ' + (f.le != null ? f.le : '...') + (f.unit ? ' ' + f.unit : ''));
            if (!f.secret && f.default != null && f.default !== '' && f.widget !== 'schedule') {
                meta.push('normal value ' + (Array.isArray(f.default) ? (f.default.join(', ') || 'none') : f.default) + (f.unit && f.widget !== 'size_mb' ? ' ' + f.unit : ''));
            }
            meta.push(f.effect_text);
            const technical = f.level === 'advanced' && f.description && f.description !== f.help
                ? '<div class="tech">' + esc(f.description) + '</div>' : '';
            return '<div class="field" data-level="' + f.level + '" id="row-' + fid(f.key) + '"><div>' +
                '<div class="label">' + esc(f.label) + ' ' + badges.join(' ') + '</div>' +
                (f.help ? '<div class="desc">' + esc(f.help) + '</div>' : '') + technical +
                '<div class="meta">' + esc(meta.join(' · ')) + '</div><div class="name">' + esc(f.key) + '</div>' +
                '</div><div>' + editorHtml(f) + '<div class="err" id="err-' + fid(f.key) + '"></div></div></div>';
        }).join('');
        const overview = s.name === 'schedule' ? '<div id="overview" class="overview"></div>' : '';
        return '<details class="section" open><summary>' + esc(s.title) +
            ' <span class="count" style="color:#6b7280;font-weight:400;font-size:0.8rem"></span></summary>' +
            overview + rows + '</details>';
    }).join('');

    Object.values(fields).forEach(f => {
        const id = fid(f.key);
        if (f.widget === 'schedule') {
            ['freq-', 'dow-', 'dom-', 'time-', 'cron-'].forEach(p => {
                const el = document.getElementById(p + id);
                el.addEventListener('input', onEdit);
                el.addEventListener('change', onEdit);
            });
            syncSchedule(f);
        } else if (f.widget !== 'email_list' && f.widget !== 'text_list') {
            const el = document.getElementById('in-' + id);
            if (el) { el.addEventListener('input', onEdit); el.addEventListener('change', onEdit); }
        }
    });
}

// ── change tracking + validation ─────────────────────────────────────────────
function collect() {
    localErrors = {};
    Object.values(fields).forEach(f => {
        if (f.locked) return;
        if (f.widget === 'schedule') syncSchedule(f);
        else if (f.widget === 'email_list' || f.widget === 'text_list') {
            const items = [...document.querySelectorAll('#list-' + fid(f.key) + ' input')].map(i => i.value.trim());
            values[f.key] = items.filter(x => x.length);
            if (f.widget === 'email_list') {
                const bad = values[f.key].filter(x => !EMAIL_RE.test(x));
                const dup = values[f.key].filter((x, i, a) => a.indexOf(x) !== i);
                if (bad.length) localErrors[f.key] = 'Not an email address: ' + bad.join(', ');
                else if (dup.length) localErrors[f.key] = 'Listed more than once: ' + dup.join(', ');
            }
        } else readSimple(f);
    });
}

function changes() {
    const out = {};
    Object.values(fields).forEach(f => {
        if (f.locked) return;
        const v = values[f.key];
        if (f.secret) { if (v) out[f.key] = v; return; }
        if (JSON.stringify(v) !== JSON.stringify(original[f.key])) out[f.key] = v;
    });
    return out;
}

function showErrors(errs, general) {
    document.querySelectorAll('.field.error').forEach(el => el.classList.remove('error'));
    document.querySelectorAll('.field .err').forEach(el => { el.textContent = ''; });
    Object.entries(errs).forEach(([k, msg]) => {
        const row = document.getElementById('row-' + fid(k));
        if (row) {
            row.classList.add('error');
            document.getElementById('err-' + fid(k)).textContent = msg;
            row.closest('details').open = true;
        }
    });
    const elsewhere = Object.keys(errs).filter(k => fields[k] && fields[k].level !== currentTab);
    const note = elsewhere.length ? 'Also fix on the ' + (currentTab === 'basic' ? 'Advanced' : 'Settings') +
        ' tab: ' + elsewhere.join(', ') : '';
    document.getElementById('general-error').textContent = [general, note].filter(x => x).join('\n');
}

// ── tabs ─────────────────────────────────────────────────────────────────────
function showTab(tab) {
    currentTab = tab;
    ['basic', 'advanced', 'rollover'].forEach(t =>
        document.getElementById('tab-' + t).classList.toggle('active', t === tab));
    const locked = tab === 'advanced' && !isAdmin;       // Advanced needs its own login
    const settingsTab = tab !== 'rollover' && !locked;
    document.getElementById('sections').style.display = settingsTab ? '' : 'none';
    document.getElementById('savebar').style.display = settingsTab ? '' : 'none';
    document.getElementById('rollover-panel').style.display = tab === 'rollover' ? 'block' : 'none';
    document.getElementById('advanced-banner').style.display = tab === 'advanced' && isAdmin ? 'block' : 'none';
    document.getElementById('admin-login').style.display = locked ? 'block' : 'none';
    document.getElementById('admin-bar').style.display = tab === 'advanced' && isAdmin ? 'block' : 'none';
    if (locked) { document.getElementById('admin-password').focus(); return; }
    if (settingsTab) applyTab(); else loadRollover();
}

function applyTab() {
    document.querySelectorAll('#sections details.section').forEach(sec => {
        let n = 0;
        sec.querySelectorAll('.field').forEach(row => {
            const show = row.dataset.level === currentTab;
            row.style.display = show ? '' : 'none';
            if (show) n++;
        });
        sec.style.display = n ? '' : 'none';
        sec.querySelector('.count').textContent = '(' + n + ')';
        sec.open = currentTab === 'basic';
    });
}

// ── FY rollover panel ────────────────────────────────────────────────────────
let rolloverState = null;

async function loadRollover() {
    const resp = await fetch('/api/rollover');
    if (resp.status === 401) { window.location.href = '/login'; return; }
    const d = await resp.json();
    if (!resp.ok) { document.getElementById('ro-message').textContent = d.detail || 'Could not load the FY status.'; return; }
    rolloverState = d;
    document.getElementById('ro-configured').textContent = d.configured_fy || '(no FY folder)';
    document.getElementById('ro-current').textContent = d.current_fy;
    const last = d.last_attempt;
    document.getElementById('ro-last').textContent = last
        ? last.label + ' - ' + last.when + (last.error ? ' - ' + last.error : '') : 'never';
    document.getElementById('ro-message').textContent = d.running
        ? 'A rollover check is running right now.'
        : d.message + (d.running === null && d.can_run
            ? ' (The scheduler (Prefect) is not reachable, so it cannot be started right now.)' : '');
    document.getElementById('ro-approval').style.display = d.approval_pending ? 'block' : 'none';
    document.getElementById('btn-rollover').disabled = !authEnabled || !d.can_run || d.running || d.running === null;
}

async function runRollover() {
    const d = rolloverState;
    if (!d) return;
    if (!window.confirm('Start the FY rollover check now?\n\n' + d.message +
        '\n\nIt follows the same safety rules as the automatic run.')) return;
    const btn = document.getElementById('btn-rollover');
    btn.disabled = true;
    try {
        const resp = await fetch('/api/rollover/run?wait=false', { method: 'POST' });
        const data = await resp.json().catch(() => ({}));
        showToast(data.detail || ('HTTP ' + resp.status), resp.ok ? 'success' : 'error');
    } catch (e) {
        showToast('Could not start the rollover check: ' + e, 'error');
    }
    loadRollover();
}

function onEdit() {
    collect();
    renderOverview();
    const c = changes();
    document.querySelectorAll('.field.changed').forEach(el => el.classList.remove('changed'));
    Object.keys(c).forEach(k => { const r = document.getElementById('row-' + fid(k)); if (r) r.classList.add('changed'); });
    serverOk = false;
    updateSaveButton();
    if (Object.keys(localErrors).length) { showErrors(localErrors, ''); return; }
    showErrors({}, '');
    clearTimeout(validateTimer);
    if (!Object.keys(c).length) { serverOk = true; updateSaveButton(); return; }
    document.getElementById('save-note').textContent = 'Checking...';
    validateTimer = setTimeout(() => serverValidate(c), 400);
}

async function serverValidate(c) {
    const seq = ++validateSeq;
    try {
        const resp = await fetch('/api/settings/validate', { method: 'POST',
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ changes: c }) });
        if (resp.status === 401) { window.location.href = '/login'; return; }
        const data = await resp.json();
        if (seq !== validateSeq) return;            // an older answer - ignore
        if (data.ok) {
            serverOk = true;
            showErrors({}, '');
            document.getElementById('save-note').textContent = data.confirm_phrase
                ? 'Valid. Saving will ask you to type: ' + data.confirm_phrase : 'Valid.';
        } else {
            serverOk = false;
            const byKey = {}, general = [];
            (data.errors || []).forEach(e => {
                if (e.key && fields[e.key]) byKey[e.key] = e.message; else general.push((e.key ? e.key + ': ' : '') + e.message);
            });
            showErrors(byKey, general.join('\n'));
            document.getElementById('save-note').textContent = 'Not valid - see the red messages.';
        }
    } catch (e) {
        serverOk = false;
        document.getElementById('save-note').textContent = 'Could not check the values: ' + e;
    }
    updateSaveButton();
}

function updateSaveButton() {
    const n = Object.keys(changes()).length;
    const btn = document.getElementById('btn-save');
    btn.disabled = n === 0 || !authEnabled || !serverOk || Object.keys(localErrors).length > 0;
    btn.textContent = n === 0 ? 'No changes' : 'Save ' + n + ' change' + (n > 1 ? 's' : '');
    if (n === 0) document.getElementById('save-note').textContent = '';
}

async function saveSettings(phrase) {
    const c = changes();
    if (!Object.keys(c).length) return;
    const btn = document.getElementById('btn-save');
    btn.disabled = true;
    try {
        const resp = await fetch('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ changes: c, phrase: phrase || null }) });
        const data = await resp.json().catch(() => ({}));
        if (resp.status === 409 && data.confirm_phrase) {
            const typed = window.prompt(data.detail + '\n\nSettings: ' + (data.risky || []).join(', ') + '\n\nType the phrase to confirm:');
            if (typed) return saveSettings(typed);
            showToast('Not saved (no confirmation).', 'warning');
        } else if (resp.status === 400 && data.errors) {
            const byKey = {}; data.errors.forEach(e => { if (e.key) byKey[e.key] = e.message; });
            showErrors(byKey, data.detail);
            showToast(data.detail, 'error');
        } else if (resp.ok) {
            const n = (data.changed || []).length;
            showToast(n ? 'Saved ' + n + ' setting(s).\n' + (data.effects || []).join('\n') : 'Nothing changed.', 'success');
            await loadSettings();
            await loadHistory();
            return;
        } else if (resp.status === 401) {
            window.location.href = '/login';
        } else {
            showToast(data.detail || ('Save failed (HTTP ' + resp.status + ')'), 'error');
        }
    } catch (e) {
        showToast('Save failed: ' + e, 'error');
    }
    updateSaveButton();
}

async function loadSettings() {
    const resp = await fetch('/api/settings');
    if (resp.status === 401) { window.location.href = '/login'; return; }
    const data = await resp.json();
    if (!resp.ok) {
        document.getElementById('sections').innerHTML = '<p style="color:#fca5a5">' + esc(data.detail || 'Could not load settings') + '</p>';
        return;
    }
    authEnabled = !!data.auth_enabled;
    isAdmin = !!data.admin;
    document.getElementById('readonly-banner').style.display = authEnabled ? 'none' : 'block';
    catalog = data.sections;
    fields = {}; original = {}; values = {}; localErrors = {}; serverOk = true;
    catalog.forEach(s => s.fields.forEach(f => {
        fields[f.key] = f; original[f.key] = f.value; values[f.key] = f.value;
    }));
    scheduleTz = (fields['schedule.timezone'] || {}).value || TZ;
    render();
    showTab(currentTab);
    // the picker writes crons in one normal form ("00 21" -> "0 21"): that is
    // the baseline, so an untouched schedule never counts as a change
    Object.values(fields).forEach(f => { if (f.widget === 'schedule') original[f.key] = values[f.key]; });
    showErrors({}, '');
    updateSaveButton();
}

async function loadHistory() {
    const resp = await fetch('/api/settings/history');
    if (!resp.ok) return;
    const rows = (await resp.json()).changes || [];
    document.getElementById('history-tbody').innerHTML = rows.length ? rows.map(r =>
        '<tr><td>' + esc(fmtIST(r.changed_at)) + '</td><td>' + esc(r.changed_by) + '</td><td>' + esc(r.key) +
        (r.risky ? ' <span class="tag badge-risky">risky</span>' : '') + '</td><td>' + esc(r.old_value) +
        '</td><td>' + esc(r.new_value) + '</td></tr>').join('')
        : '<tr><td colspan="5" style="color:#6b7280;text-align:center">No changes made on the dashboard yet.</td></tr>';
}

function discardChanges() { loadSettings(); }

// Audit 2026-10-18: prove the SAVED mail settings through the real alert path.
async function sendTestEmail() {
    if (Object.keys(changes()).length) {
        showToast('Save your changes first - the test uses the saved mail settings.', 'warning');
        return;
    }
    const btn = document.getElementById('btn-test-email');
    btn.disabled = true;
    try {
        const resp = await fetch('/api/settings/test-email', { method: 'POST' });
        if (resp.status === 401) { window.location.href = '/login'; return; }
        const data = await resp.json().catch(() => ({}));
        showToast(data.detail || ('HTTP ' + resp.status), resp.ok ? 'success' : 'error');
    } catch (e) {
        showToast('Send test email failed: ' + e, 'error');
    }
    btn.disabled = false;
}

// ── Advanced login ───────────────────────────────────────────────────────────
async function adminLogin(ev) {
    ev.preventDefault();
    const pw = document.getElementById('admin-password');
    const resp = await fetch('/api/admin/login', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: pw.value }) });
    const data = await resp.json().catch(() => ({}));
    pw.value = '';
    if (!resp.ok) { showToast(data.detail || 'Sign-in failed.', 'error'); return; }
    adminMinutes = data.session_minutes || adminMinutes;
    document.getElementById('admin-bar-text').textContent =
        'Advanced is unlocked (locks itself after ' + adminMinutes + ' minutes).';
    await loadSettings();
    showTab('advanced');
}

async function adminLogout() {
    await fetch('/api/admin/logout', { method: 'POST' });
    await loadSettings();
    showTab('advanced');
    showToast('Advanced is locked.', 'info');
}

async function changeAdminPassword(ev) {
    ev.preventDefault();
    const body = { current: document.getElementById('pw-current').value,
                   new: document.getElementById('pw-new').value,
                   confirm: document.getElementById('pw-confirm').value };
    const resp = await fetch('/api/admin/password', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body) });
    const data = await resp.json().catch(() => ({}));
    ['pw-current', 'pw-new', 'pw-confirm'].forEach(id => { document.getElementById(id).value = ''; });
    showToast(data.detail || ('HTTP ' + resp.status), resp.ok ? 'success' : 'error');
    if (resp.ok) { document.getElementById('admin-pw').style.display = 'none'; loadHistory(); }
}

loadSettings();
loadHistory();
