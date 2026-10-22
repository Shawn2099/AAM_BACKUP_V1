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
let authEnabled = true;     // every page needs a signed-in session
let validateTimer = null;
let validateSeq = 0;
let scheduleTz = TZ;
let currentTab = 'basic';
let isAdmin = false;
let adminMinutes = 30;
let pendingRestart = [];   // saved settings waiting for "Apply changes"
let bootId = '';
let applyCfg = { poll_seconds: 3, timeout_seconds: 300 };

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
    const el = document.createElement('div');
    el.className = 'toast ' + (type || 'info');
    el.textContent = message;
    document.getElementById('toast-container').appendChild(el);
    setTimeout(() => el.classList.add('shown'), 50);
    setTimeout(() => el.remove(), type === 'error' ? 12000 : 7000);
}

function fid(key) { return key.replace(/[^a-zA-Z0-9_]/g, '_'); }
function disabledAttr(f) { return (f.locked || !authEnabled) ? ' disabled' : ''; }

// ── schedules: cron <-> plain choices (every day / week / chosen days / several times a day / month) ──
const DOW_ORDER = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'];

function parseCron(cron) {
    const word = '(?:[A-Za-z]{3}|[0-7])';
    const re = new RegExp('^(\\d{1,2}) (\\d{1,2}(?:,\\d{1,2})*) (\\*|\\d{1,2}) \\* (\\*|' + word + '(?:,' + word + ')*)$');
    const m = re.exec((cron || '').trim());
    if (!m) return { freq: 'custom', cron: cron };
    const pad = n => String(n).padStart(2, '0');
    const hours = m[2].split(',').map(Number);
    const time = pad(hours[0]) + ':' + pad(m[1]);
    const days = m[4] === '*' ? [] : m[4].split(',').map(d => (DOW_NUM[d] || d).toUpperCase());
    if (days.some(d => DOW_ORDER.indexOf(d) < 0)) return { freq: 'custom', cron: cron };
    if (hours.length > 1) {
        if (m[3] !== '*' || days.length) return { freq: 'custom', cron: cron };
        return { freq: 'times', times: hours.map(h => pad(h) + ':' + pad(m[1])) };
    }
    if (m[3] === '*' && !days.length) return { freq: 'daily', time: time };
    if (m[3] === '*' && days.length === 1) return { freq: 'weekly', time: time, dow: days[0] };
    if (m[3] === '*') return { freq: 'days', time: time, days: DOW_ORDER.filter(d => days.indexOf(d) >= 0) };
    if (!days.length && +m[3] >= 1 && +m[3] <= 28) return { freq: 'monthly', time: time, dom: +m[3] };
    return { freq: 'custom', cron: cron };
}

function buildCron(p) {
    if (p.freq === 'custom') return p.cron;
    if (p.freq === 'times') {
        const ts = (p.times || []).filter(t => t).map(t => t.split(':').map(Number));
        const hours = ts.map(t => t[0]).sort((a, b) => a - b).filter((h, i, a) => a.indexOf(h) === i);
        return (ts.length ? ts[0][1] : 0) + ' ' + (hours.length ? hours.join(',') : 0) + ' * * *';
    }
    const [h, mi] = (p.time || '00:00').split(':').map(Number);
    if (p.freq === 'daily') return mi + ' ' + h + ' * * *';
    if (p.freq === 'weekly') return mi + ' ' + h + ' * * ' + p.dow;
    if (p.freq === 'days') return mi + ' ' + h + ' * * ' + (p.days && p.days.length ? p.days.join(',') : 'MON');
    return mi + ' ' + h + ' ' + p.dom + ' * *';
}

function shortDays(days) {
    return days.map(d => (DOW.find(x => x[0] === d) || [d, d])[1].slice(0, 3)).join(', ');
}

function scheduleHtml(f) {
    const id = fid(f.key), p = parseCron(f.value), dis = disabledAttr(f);
    const options = [['daily', 'Every day'], ['weekly', 'Every week'], ['days', 'On chosen days'],
                     ['times', 'Several times a day'], ['monthly', 'Every month']];
    if (p.freq === 'custom') options.push(['custom', 'Other (set by support)']);
    const freq = '<select id="freq-' + id + '"' + dis + '>' + options.map(o =>
        '<option value="' + o[0] + '"' + (p.freq === o[0] ? ' selected' : '') + '>' + o[1] + '</option>').join('') + '</select>';
    const dow = '<select id="dow-' + id + '"' + dis + '>' + DOW.map(d =>
        '<option value="' + d[0] + '"' + (p.dow === d[0] ? ' selected' : '') + '>' + d[1] + '</option>').join('') + '</select>';
    const chips = '<span class="chips" id="days-' + id + '">' + DOW.map(d =>
        '<label class="chip"><input type="checkbox" value="' + d[0] + '"' + dis +
        ((p.days || []).indexOf(d[0]) >= 0 ? ' checked' : '') + '><span>' + d[1].slice(0, 3) + '</span></label>').join('') + '</span>';
    let domOpts = '';
    for (let i = 1; i <= 28; i++) domOpts += '<option' + (p.dom === i ? ' selected' : '') + '>' + i + '</option>';
    const dom = '<select id="dom-' + id + '"' + dis + '>' + domOpts + '</select>';
    const time = '<input type="time" step="60" id="time-' + id + '" value="' + esc(p.time || '21:00') + '"' + dis + '>';
    const times = '<span id="times-' + id + '">' + (p.times || ['09:00', '21:00']).map(t => timeRow(t, dis)).join('') + '</span>' +
        (dis ? '' : ' <button type="button" class="btn-small" onclick="addTimeRow(\'' + f.key + '\')">+ Add time</button>');
    const custom = '<input type="text" id="cron-' + id + '" value="' + esc(f.value) + '" placeholder="min hour day month weekday"' + dis + '>';
    return '<div class="sched">' + freq + ' <span id="wrap-dow-' + id + '">on ' + dow + '</span>' +
        '<span id="wrap-days-' + id + '">' + chips + '</span>' +
        '<span id="wrap-dom-' + id + '">on day ' + dom + '</span>' +
        '<span id="wrap-time-' + id + '"> at ' + time + ' <span class="tz">' + esc(tzLabel()) + '</span></span>' +
        '<span id="wrap-times-' + id + '"> at ' + times + ' <span class="tz">' + esc(tzLabel()) + '</span></span>' +
        '<span id="wrap-cron-' + id + '">' + custom + '</span></div>' +
        '<div class="meta" id="human-' + id + '"></div>';
}

function timeRow(t, dis) {
    return '<span class="trow"><input type="time" step="60" value="' + esc(t) + '"' + dis + '>' +
        (dis ? '' : '<button type="button" class="btn-small btn-x" title="Remove this time" onclick="removeTimeRow(this)">&times;</button>') +
        '</span>';
}

function addTimeRow(key) {
    const box = document.getElementById('times-' + fid(key));
    box.insertAdjacentHTML('beforeend', timeRow('12:00', ''));
    onEdit();
}

function removeTimeRow(btn) {
    const box = btn.parentElement.parentElement;
    if (box.children.length > 1) btn.parentElement.remove();
    onEdit();
}

function tzLabel() { return scheduleTz === TZ ? 'IST' : scheduleTz; }

// One sentence a person can read: "every day at 21:00 IST". Used under the picker and in the review.
function scheduleSentence(p) {
    const tz = ' ' + tzLabel();
    if (p.freq === 'daily') return 'every day at ' + p.time + tz;
    if (p.freq === 'weekly') return 'every ' + (DOW.find(d => d[0] === p.dow) || ['', p.dow])[1] + ' at ' + p.time + tz;
    if (p.freq === 'days') return 'every ' + shortDays(p.days) + ' at ' + p.time + tz;
    if (p.freq === 'times') return 'every day at ' + p.times.join(' and ') + tz;
    if (p.freq === 'monthly') return 'on day ' + p.dom + ' of every month at ' + p.time + tz;
    return null;
}

function syncSchedule(f) {
    const id = fid(f.key);
    const p = { freq: document.getElementById('freq-' + id).value,
                dow: document.getElementById('dow-' + id).value,
                dom: +document.getElementById('dom-' + id).value,
                time: document.getElementById('time-' + id).value,
                days: [...document.querySelectorAll('#days-' + id + ' input:checked')].map(i => i.value),
                times: [...document.querySelectorAll('#times-' + id + ' input')].map(i => i.value).filter(t => t),
                cron: document.getElementById('cron-' + id).value.trim() };
    const show = (name, on) => { document.getElementById('wrap-' + name + '-' + id).style.display = on ? '' : 'none'; };
    show('dow', p.freq === 'weekly');
    show('days', p.freq === 'days');
    show('dom', p.freq === 'monthly');
    show('time', ['daily', 'weekly', 'days', 'monthly'].indexOf(p.freq) >= 0);
    show('times', p.freq === 'times');
    show('cron', p.freq === 'custom');
    if (['daily', 'weekly', 'days', 'monthly'].indexOf(p.freq) >= 0 && !p.time) localErrors[f.key] = 'Pick a time.';
    if (p.freq === 'days' && !p.days.length) localErrors[f.key] = 'Tick at least one day.';
    if (p.freq === 'times') {
        const minutes = p.times.map(t => t.split(':')[1]);
        if (!p.times.length) localErrors[f.key] = 'Add at least one time.';
        else if (minutes.some(x => x !== minutes[0])) {
            localErrors[f.key] = 'Use the same minutes for every time (for example 09:30 and 21:30).';
        } else if (p.times.filter((t, i, a) => a.indexOf(t) !== i).length) localErrors[f.key] = 'Remove the repeated time.';
    }
    const cron = p.freq === 'custom' ? p.cron : buildCron(p);
    if (p.freq !== 'custom') document.getElementById('cron-' + id).value = cron;
    values[f.key] = cron;
    const sentence = scheduleSentence(parseCron(cron));
    document.getElementById('human-' + id).textContent = sentence
        ? 'Runs ' + sentence + '.' : 'Runs on a custom timing written by support: ' + cron;
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
    const items = OVERVIEW_JOBS.filter(j => fields[j[0]]).flatMap(j => {
        const p = parseCron(values[j[0]]);
        if (p.freq === 'custom') return [{ mins: 9999, html: '<b>' + esc(j[1]) + '</b>: custom timing' }];
        const hours = j[2] ? Number(values[j[2]]) : null;
        const when = { daily: 'every day', weekly: 'every ' + (DOW.find(d => d[0] === p.dow) || ['', p.dow])[1],
                       days: 'every ' + (p.days ? shortDays(p.days) : ''), times: 'every day',
                       monthly: 'on day ' + p.dom + ' of the month' }[p.freq];
        return (p.times || [p.time]).map(t => {
            const [h, m] = t.split(':').map(Number);
            let until = '';
            if (hours) {
                const end = (h * 60 + m + Math.round(hours * 60)) % (24 * 60);
                until = ' &rarr; finished by ' + String(Math.floor(end / 60)).padStart(2, '0') + ':' +
                    String(end % 60).padStart(2, '0') + ' at the latest (' + hours + ' h)';
            }
            return { mins: h * 60 + m, html: '<b>' + t + '</b> ' + esc(j[1]) + ' <span class="hint">(' +
                when + ')</span>' + until };
        });
    }).sort((a, b) => a.mins - b.mins);
    box.innerHTML = '<div class="label">Your timetable at a glance (' + esc(tzLabel()) +
        ')</div>' + items.map(i => '<div style="margin:0.15rem 0">' + i.html + '</div>').join('') +
        '<div class="hint">Only one backup runs at a time. ' +
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
        const dup = values[key].filter((x, i, a) => a.findIndex(y => y.toLowerCase() === x.toLowerCase()) !== i);
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

// ── folder picker (Advanced source folder; the server lists and checks, never creates) ────
function fyLeaf(path) {
    const m = /([^\\/]+)[\\/]*$/.exec(path || '');
    return m && /^FY\d\d-\d\d$/i.test(m[1]) ? m[1] : null;
}

function folderHtml(f) {
    const id = fid(f.key), dis = disabledAttr(f);
    const canPick = !!fyLeaf(f.value);
    return '<input type="text" id="in-' + id + '" value="' + esc(f.value) + '"' + dis + '>' +
        (canPick ? ' <button type="button" class="btn" id="browse-' + id + '"' + dis + '>Choose folder...</button>' : '') +
        '<div class="folder-box" id="box-' + id + '" hidden></div><div class="folder-check" id="chk-' + id + '"></div>';
}

async function browseFolders(f, path) {
    const id = fid(f.key), box = document.getElementById('box-' + id);
    box.hidden = false;
    box.textContent = 'Loading...';
    let resp, data;
    try {
        resp = await fetch('/api/settings/browse?path=' + encodeURIComponent(path || ''));
        data = await resp.json();
    } catch (e) { box.textContent = 'The server did not answer properly. Try again in a moment.'; return; }
    if (!resp.ok) { box.textContent = data.detail || 'The server could not list this folder.'; return; }
    const sep = data.path.indexOf('/') >= 0 && data.path.indexOf('\\') < 0 ? '/' : '\\';
    const join = n => /[\\/]$/.test(data.path) ? data.path + n : data.path + sep + n;
    let html = '<div class="folder-path">' + esc(data.path || 'This server') + '</div>';
    if (data.path) {
        html += '<div><button type="button" class="btn" data-use="' + esc(data.path) + '">Use this folder (the ' +
            esc(fyLeaf(original[f.key]) || 'FY') + ' folder must be inside it)</button></div>';
    }
    if (data.parent !== null && data.path) html += '<div><a href="#" data-go="' + esc(data.parent) + '">.. (up)</a></div>';
    else if (data.path) html += '<div><a href="#" data-go="">.. (all drives)</a></div>';
    html += data.folders.map(n => '<div><a href="#" data-go="' + esc(data.path ? join(n) : n) + '">' + esc(n) + '</a></div>').join('');
    box.innerHTML = html;
    box.querySelectorAll('[data-go]').forEach(a => a.addEventListener('click', e => {
        e.preventDefault(); browseFolders(f, a.getAttribute('data-go'));
    }));
    box.querySelectorAll('[data-use]').forEach(b => b.addEventListener('click', () => useFolder(f, b.getAttribute('data-use'))));
}

async function useFolder(f, parent) {
    const id = fid(f.key), chk = document.getElementById('chk-' + id);
    chk.textContent = 'Checking...';
    let resp, data;
    try {
        resp = await fetch('/api/settings/check-source', { method: 'POST',
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ parent: parent }) });
        data = await resp.json();
    } catch (e) { chk.textContent = 'The server did not answer properly. Try again in a moment.'; return; }
    if (!resp.ok) { chk.textContent = data.detail || 'The check failed.'; return; }
    if (!data.ok) { chk.innerHTML = '<div class="err">' + data.problems.map(esc).join('<br>') + '</div>'; return; }
    chk.innerHTML = '<div>' + esc(data.source) + (data.cloud_target ? ' - the cloud copy stays at ' + esc(data.cloud_target) : '') +
        '</div>' + data.warnings.map(w => '<div class="meta">' + esc(w) + '</div>').join('');
    const input = document.getElementById('in-' + id);
    input.value = data.source;
    document.getElementById('box-' + id).hidden = true;
    input.dispatchEvent(new Event('input', { bubbles: true }));
}

// ── one editor per field ─────────────────────────────────────────────────────
function editorHtml(f) {
    const id = fid(f.key), dis = disabledAttr(f);
    if (f.widget === 'schedule') return scheduleHtml(f);
    if (f.widget === 'folder') return folderHtml(f);
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
            if (f.effect === 'agent_restart') badges.push('<span class="tag badge-restart">applies with Apply changes</span>');
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
            ' <span class="count summary-count"></span></summary>' +
            overview + rows + '</details>';
    }).join('');

    Object.values(fields).forEach(f => {
        const id = fid(f.key);
        if (f.widget === 'schedule') {
            const box = document.getElementById('freq-' + id).closest('.sched');
            box.addEventListener('input', onEdit);
            box.addEventListener('change', onEdit);
            syncSchedule(f);
        } else if (f.widget !== 'email_list' && f.widget !== 'text_list') {
            const el = document.getElementById('in-' + id);
            if (el) { el.addEventListener('input', onEdit); el.addEventListener('change', onEdit); }
            const pick = document.getElementById('browse-' + id);
            if (pick) pick.addEventListener('click', () => browseFolders(f, ''));
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
                const dup = values[f.key].filter((x, i, a) => a.findIndex(y => y.toLowerCase() === x.toLowerCase()) !== i);
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

const STEP_MARK = { done: '✓', skipped: '–', failed: '✗', pending: '•' };
const STEP_CLASS = { done: 'step-done', skipped: 'step-skipped', failed: 'step-failed', pending: 'step-pending' };

function renderRolloverSteps(journal) {
    const box = document.getElementById('ro-steps');
    if (!journal) { box.style.display = 'none'; return; }
    box.style.display = 'block';
    document.getElementById('ro-steps-title').textContent = journal.from_fy + ' → ' + journal.to_fy;
    const list = document.getElementById('ro-steps-list');
    list.textContent = '';
    journal.steps.forEach(function (s) {
        const li = document.createElement('li');
        li.className = STEP_CLASS[s.state] || '';
        li.textContent = (STEP_MARK[s.state] || '•') + ' ' + s.label +
            (s.state === 'failed' && s.error ? ' - will retry: ' + s.error : '') +
            (s.state === 'skipped' ? ' - not needed' : '');
        list.appendChild(li);
    });
}

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
    renderRolloverSteps(d.journal);
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
        showToast('Try again, and ask support if it keeps failing. The rollover check could not be started: ' + e, 'error');
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
            reviewWord = data.confirm_phrase || reviewWord;
            document.getElementById('save-note').textContent = data.confirm_phrase
                ? 'Valid. You will review the changes and type ' + data.confirm_phrase + ' to save.' : 'Valid. You will review the changes before they are saved.';
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
    btn.textContent = n === 0 ? 'No changes' : 'Review ' + n + ' change' + (n > 1 ? 's' : '');
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
        if (resp.status === 409 && data.confirm_phrase && reviewOpen()) {
            document.getElementById('review-error').textContent = data.detail;
            document.getElementById('review-ok').disabled = false;
            btn.disabled = false;
            return;
        } else if (resp.status === 409 && data.confirm_phrase) {
            const typed = window.prompt(data.detail + '\n\nSettings: ' + (data.risky || []).join(', ') + '\n\nType the phrase to confirm:');
            if (typed) return saveSettings(typed);
            showToast('Press Save again and type the confirmation to apply these settings. Nothing was saved.', 'warning');
        } else if (resp.status === 400 && data.errors) {
            closeReview();
            const byKey = {}; data.errors.forEach(e => { if (e.key) byKey[e.key] = e.message; });
            showErrors(byKey, data.detail);
            showToast(data.detail, 'error');
        } else if (resp.ok) {
            const n = (data.changed || []).length;
            closeReview();
            showToast(n ? 'Saved ' + n + ' setting(s).\n' + (data.effects || []).join('\n') : 'Nothing changed.', 'success');
            await loadSettings();
            await loadHistory();
            return;
        } else if (resp.status === 401) {
            window.location.href = '/login';
        } else {
            closeReview();
            showToast(data.detail || ('Try again, and ask support if it keeps failing. The save failed (HTTP ' + resp.status + ').'), 'error');
        }
    } catch (e) {
        showToast('Try saving again. The page could not reach the server (' + e + ').', 'error');
    }
    updateSaveButton();
}

async function loadSettings() {
    const resp = await fetch('/api/settings');
    if (resp.status === 401) { window.location.href = '/login'; return; }
    const data = await resp.json();
    if (!resp.ok) {
        document.getElementById('sections').innerHTML = '<p class="load-error">' + esc(data.detail || 'Could not load settings') + '</p>';
        return;
    }
    isAdmin = !!data.admin;
    pendingRestart = data.pending_restart || [];
    bootId = data.boot_id || '';
    applyCfg = data.apply || applyCfg;
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
    renderApplyBar();
}

async function loadHistory() {
    const resp = await fetch('/api/settings/history');
    if (!resp.ok) return;
    const rows = (await resp.json()).changes || [];
    document.getElementById('history-tbody').innerHTML = rows.length ? rows.map(r =>
        '<tr><td>' + esc(fmtIST(r.changed_at)) + '</td><td>' + esc(r.changed_by) + '</td><td>' + esc(r.key) +
        (r.risky ? ' <span class="tag badge-risky">risky</span>' : '') + '</td><td>' + esc(r.old_value) +
        '</td><td>' + esc(r.new_value) + '</td></tr>').join('')
        : '<tr><td colspan="5" class="empty">No changes made on the dashboard yet.</td></tr>';
}

function discardChanges() { loadSettings(); }

// Audit 2026-10-18: prove the SAVED mail settings through the real alert path.
async function sendTestEmail() {
    if (Object.keys(changes()).length) {
        showToast('Save your changes first, then send the test. The test uses the saved mail settings.', 'warning');
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
        showToast('Try again. The page could not reach the server (' + e + ').', 'error');
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
    if (!resp.ok) { showToast(data.detail || 'Check the password and try again. Sign-in failed.', 'error'); return; }
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

let passwordWhich = 'admin';
function showPasswordForm(which) {
    passwordWhich = which;
    document.getElementById('pw-title').textContent =
        which === 'login' ? 'Change the dashboard password (everyone signs in with it)'
                          : 'Change the Advanced password';
    document.getElementById('admin-pw').style.display = 'block';
}

async function changePassword(ev) {
    ev.preventDefault();
    const body = { current: document.getElementById('pw-current').value,
                   new: document.getElementById('pw-new').value,
                   confirm: document.getElementById('pw-confirm').value };
    const resp = await fetch(passwordWhich === 'login' ? '/api/admin/dashboard-password' : '/api/admin/password', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body) });
    const data = await resp.json().catch(() => ({}));
    ['pw-current', 'pw-new', 'pw-confirm'].forEach(id => { document.getElementById(id).value = ''; });
    showToast(data.detail || ('HTTP ' + resp.status), resp.ok ? 'success' : 'error');
    if (resp.ok) { document.getElementById('admin-pw').style.display = 'none'; loadHistory(); }
}

loadSettings();
loadHistory();


// ── Apply changes: restart the backup service so saved schedule / on-off settings take effect ──
function renderApplyBar() {
    const bar = document.getElementById('apply-bar');
    if (!bar) return;
    // always visible: "Apply changes" when something waits, otherwise a plain manual restart
    const waiting = pendingRestart.length > 0;
    bar.style.display = 'flex';
    bar.className = waiting ? '' : 'idle';
    const names = pendingRestart.map(k => (fields[k] || {}).label || k);
    document.getElementById('apply-text').textContent = waiting
        ? (pendingRestart.length === 1 ? '1 saved change is waiting' : pendingRestart.length + ' saved changes are waiting')
          + ' for a restart: ' + names.join(', ') + '.'
        : 'Nothing is waiting: every saved setting is already in use. You can still restart the backup service.';
    const btn = document.getElementById('btn-apply');
    btn.textContent = waiting ? 'Apply changes' : 'Restart service';
    btn.className = waiting ? 'btn-trigger' : 'btn-secondary';
}

function showRestartOverlay(text) {
    const o = document.getElementById('restart-overlay');
    o.style.display = 'flex';
    document.getElementById('restart-text').textContent = text;
}

async function applyChanges() {
    const manual = pendingRestart.length === 0;
    if (!window.confirm((manual ? 'Restart the backup service now?' : 'Apply the saved changes by restarting the backup service now?') +
        '\n\nIt is only restarted when no backup, check or rollover is running. ' +
        'The dashboard is unavailable for about a minute.')) return;
    const btn = document.getElementById('btn-apply');
    btn.disabled = true;
    try {
        const resp = await fetch('/api/settings/apply', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ force: manual }) });
        const data = await resp.json().catch(() => ({}));
        if (resp.status === 401) { window.location.href = '/login'; return; }
        if (!resp.ok) { showToast(data.detail || ('Could not apply (HTTP ' + resp.status + ')'), 'error'); btn.disabled = false; return; }
        if (data.status === 'nothing_to_apply') { showToast('Nothing is waiting to be applied.', 'info'); await loadSettings(); btn.disabled = false; return; }
        await waitForRestart(bootId);
    } catch (e) {
        showToast('Try again. The page could not reach the server (' + e + ').', 'error');
        btn.disabled = false;
    }
}

async function waitForRestart(oldBootId) {
    showRestartOverlay('Restarting the backup service...');
    const deadline = Date.now() + applyCfg.timeout_seconds * 1000;
    while (Date.now() < deadline) {
        await new Promise(r => setTimeout(r, applyCfg.poll_seconds * 1000));
        try {
            const h = await fetch('/health', { cache: 'no-store' });
            const body = await h.json().catch(() => ({}));
            if (body.boot_id && body.boot_id !== oldBootId) {
                showRestartOverlay('Back online. Loading the page...');
                window.location.reload();
                return;
            }
        } catch (e) { /* the service is down while it restarts */ }
    }
    document.getElementById('restart-overlay').style.display = 'none';
    showToast('Ask support to check the server. The backup service did not come back within ' + Math.round(applyCfg.timeout_seconds / 60) +
        ' minutes; this dashboard warns you if it stays down.', 'error');
    document.getElementById('btn-apply').disabled = false;
}


// ── Review dialog: every save is read through by a person first ──────────────
let reviewWord = 'APPROVE';
const DOW_NAME = Object.fromEntries(DOW);

function scheduleText(cron) {
    return scheduleSentence(parseCron(cron)) || cron;
}

function valueText(f, v, isOld) {
    if (f.secret) return isOld ? (f.is_set ? '(set)' : '(not set)') : '(new value, hidden)';
    if (v === null || v === undefined || v === '') return '(empty)';
    if (f.widget === 'schedule') return scheduleText(v);
    if (typeof v === 'boolean') return v ? 'On' : 'Off';
    if (Array.isArray(v)) return v.length ? v.join(', ') : '(none)';
    return String(v) + (f.unit && typeof v === 'number' ? ' ' + f.unit : '');
}

function reviewOpen() {
    const d = document.getElementById('review-dialog');
    return !!(d && d.open);
}

function reviewAndSave() {
    const c = changes();
    if (!Object.keys(c).length) return;
    const d = document.getElementById('review-dialog');
    if (!d || typeof d.showModal !== 'function') return saveSettings();     // very old browser: the older prompt flow
    let anyRisky = false;
    document.getElementById('review-list').innerHTML = Object.keys(c).map(k => {
        const f = fields[k];
        const tier = f.risky ? 'risky' : (f.note ? 'notable' : '');
        if (f.risky) anyRisky = true;
        return '<li class="' + tier + '"><div class="rv-label">' + esc(f.label) + '</div>' +
            '<div class="rv-change"><span class="rv-old">' + esc(valueText(f, original[k], true)) + '</span> &rarr; ' +
            '<span class="rv-new">' + esc(valueText(f, c[k], false)) + '</span></div>' +
            (f.note ? '<div class="rv-note">' + esc(f.note) + '</div>' : '') +
            (f.effect === 'agent_restart' ? '<div class="rv-note">Takes effect when you press Apply changes.</div>' : '') + '</li>';
    }).join('');
    document.getElementById('review-confirm').hidden = !anyRisky;
    document.getElementById('review-word').textContent = reviewWord;
    document.getElementById('review-phrase').placeholder = reviewWord;
    document.getElementById('review-phrase').value = '';
    document.getElementById('review-error').textContent = '';
    document.getElementById('review-ok').disabled = false;
    d.showModal();
}

function closeReview() {
    const d = document.getElementById('review-dialog');
    if (d && d.open) d.close();
}

async function confirmReview() {
    const needWord = !document.getElementById('review-confirm').hidden;
    const typed = document.getElementById('review-phrase').value;
    if (needWord && typed.trim().toUpperCase() !== reviewWord) {
        document.getElementById('review-error').textContent = 'Type ' + reviewWord + ' exactly, then press Save changes.';
        return;
    }
    document.getElementById('review-error').textContent = '';
    document.getElementById('review-ok').disabled = true;
    await saveSettings(needWord ? typed : null);
    if (reviewOpen()) document.getElementById('review-ok').disabled = false;   // a refusal keeps the dialog open
}
