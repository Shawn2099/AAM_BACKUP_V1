// Settings page (owner decision DEC-9). The server decides everything:
// validation, which keys are risky / locked / secret. This file only renders
// the catalog from /api/settings and sends the CHANGED fields back.

let catalog = [];          // sections from the server
let original = {};         // key -> value as loaded
let authEnabled = false;

function esc(text) {
    return (text == null ? '' : String(text)).replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
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

function fieldId(key) { return 'f-' + key.replace(/[^a-zA-Z0-9_]/g, '_'); }

function inputHtml(f) {
    const id = fieldId(f.key);
    const dis = (f.locked || !authEnabled) ? ' disabled' : '';
    if (f.type === 'bool') {
        return '<label><input type="checkbox" id="' + id + '"' + (f.value ? ' checked' : '') + dis + '> ' +
            (f.value ? 'on' : 'off') + '</label>';
    }
    if (f.choices) {
        return '<select id="' + id + '"' + dis + '>' + f.choices.map(c =>
            '<option' + (c === f.value ? ' selected' : '') + '>' + esc(c) + '</option>').join('') + '</select>';
    }
    if (f.type === 'list') {
        return '<textarea id="' + id + '"' + dis + ' placeholder="one per line">' +
            esc((f.value || []).join('\n')) + '</textarea>';
    }
    if (f.secret) {
        return '<input type="password" id="' + id + '" autocomplete="new-password" placeholder="' +
            (f.is_set ? '(set - leave empty to keep)' : '(not set)') + '"' + dis + '>';
    }
    if (f.type === 'int' || f.type === 'float') {
        const bounds = (f.ge != null ? ' min="' + f.ge + '"' : '') + (f.le != null ? ' max="' + f.le + '"' : '');
        const step = f.type === 'int' ? '1' : 'any';
        return '<input type="number" step="' + step + '" id="' + id + '"' + bounds + ' value="' +
            esc(f.value == null ? '' : f.value) + '"' + (f.optional ? ' placeholder="empty = automatic"' : '') + dis + '>';
    }
    return '<input type="text" id="' + id + '" value="' + esc(f.value) + '"' + dis + '>';
}

function render() {
    const parts = catalog.map((s, i) => {
        const rows = s.fields.map(f => {
            const badges = [];
            if (f.locked) badges.push('<span class="tag badge-locked">only in config.yaml</span>');
            if (f.risky) badges.push('<span class="tag badge-risky">RISKY - typed confirmation</span>');
            if (f.effect === 'agent_restart' || f.effect === 'watchdog_restart') {
                badges.push('<span class="tag badge-restart">needs a service restart</span>');
            }
            const meta = [];
            if (f.ge != null || f.le != null) meta.push('range ' + (f.ge != null ? f.ge : '...') + ' - ' + (f.le != null ? f.le : '...'));
            if (!f.secret && f.default != null && f.default !== '') meta.push('default ' + (Array.isArray(f.default) ? JSON.stringify(f.default) : f.default));
            meta.push(f.effect_text);
            return '<div class="field" id="row-' + fieldId(f.key) + '">' +
                '<div><div class="name">' + esc(f.key) + ' ' + badges.join(' ') + '</div>' +
                '<div class="desc">' + esc(f.description) + '</div>' +
                '<div class="meta">' + esc(meta.join(' · ')) + '</div></div>' +
                '<div>' + inputHtml(f) + '<div class="err" id="err-' + fieldId(f.key) + '"></div></div></div>';
        }).join('');
        return '<details class="section"' + (i < 3 ? ' open' : '') + '><summary>' + esc(s.title) +
            ' <span style="color:#6b7280;font-weight:400;font-size:0.8rem">(' + s.fields.length + ')</span></summary>' +
            rows + '</details>';
    });
    document.getElementById('sections').innerHTML = parts.join('');
    document.querySelectorAll('#sections input, #sections select, #sections textarea')
        .forEach(el => el.addEventListener('input', updateSaveButton));
    document.querySelectorAll('#sections input[type=checkbox]')
        .forEach(el => el.addEventListener('change', updateSaveButton));
}

function currentValue(f) {
    const el = document.getElementById(fieldId(f.key));
    if (!el) return undefined;
    if (f.type === 'bool') return el.checked;
    if (f.type === 'list') return el.value.split('\n').map(x => x.trim()).filter(x => x.length);
    if (f.secret) return el.value;
    if (f.type === 'int' || f.type === 'float') {
        if (el.value.trim() === '') return f.optional ? null : el.value;
        return Number(el.value);
    }
    return el.value;
}

function changes() {
    const out = {};
    catalog.forEach(s => s.fields.forEach(f => {
        if (f.locked) return;
        const v = currentValue(f);
        if (f.secret) { if (v) out[f.key] = v; return; }
        if (JSON.stringify(v) !== JSON.stringify(original[f.key])) out[f.key] = v;
    }));
    return out;
}

function updateSaveButton() {
    const c = changes();
    const n = Object.keys(c).length;
    document.querySelectorAll('.field.changed').forEach(el => el.classList.remove('changed'));
    Object.keys(c).forEach(k => {
        const row = document.getElementById('row-' + fieldId(k));
        if (row) row.classList.add('changed');
    });
    const btn = document.getElementById('btn-save');
    btn.disabled = n === 0 || !authEnabled;
    btn.textContent = n === 0 ? 'No changes' : 'Save ' + n + ' change' + (n > 1 ? 's' : '');
}

function clearErrors() {
    document.querySelectorAll('.field.error').forEach(el => el.classList.remove('error'));
    document.querySelectorAll('.field .err').forEach(el => { el.textContent = ''; });
}

async function saveSettings(phrase) {
    const c = changes();
    if (!Object.keys(c).length) return;
    clearErrors();
    const btn = document.getElementById('btn-save');
    btn.disabled = true;
    try {
        const resp = await fetch('/api/settings', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ changes: c, phrase: phrase || null }),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.status === 409 && data.confirm_phrase) {
            const typed = window.prompt(data.detail + '\n\nSettings: ' + (data.risky || []).join(', ') +
                '\n\nType the phrase to confirm:');
            if (typed) return saveSettings(typed);
            showToast('Not saved (no confirmation).', 'warning');
        } else if (resp.status === 400 && data.errors) {
            data.errors.forEach(e => {
                const row = e.key && document.getElementById('row-' + fieldId(e.key));
                if (row) {
                    row.classList.add('error');
                    document.getElementById('err-' + fieldId(e.key)).textContent = e.message;
                }
            });
            showToast(data.detail + '\n' + data.errors.map(e => (e.key ? e.key + ': ' : '') + e.message).join('\n'), 'error');
        } else if (resp.ok) {
            const n = (data.changed || []).length;
            showToast(n ? 'Saved ' + n + ' setting(s).\n' + (data.effects || []).join('\n') : 'Nothing changed.', 'success');
            await loadSettings();
            await loadHistory();
        } else if (resp.status === 401) {
            window.location.href = '/login';
        } else {
            showToast(data.detail || ('Save failed (HTTP ' + resp.status + ')'), 'error');
        }
    } catch (e) {
        showToast('Save failed: ' + e, 'error');
    } finally {
        updateSaveButton();
    }
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
    document.getElementById('readonly-banner').style.display = authEnabled ? 'none' : 'block';
    catalog = data.sections;
    original = {};
    catalog.forEach(s => s.fields.forEach(f => { original[f.key] = f.value; }));
    render();
    updateSaveButton();
}

async function loadHistory() {
    const resp = await fetch('/api/settings/history');
    if (!resp.ok) return;
    const rows = (await resp.json()).changes || [];
    document.getElementById('history-tbody').innerHTML = rows.length ? rows.map(r =>
        '<tr><td>' + esc((r.changed_at || '').slice(0, 19).replace('T', ' ')) + '</td><td>' + esc(r.changed_by) +
        '</td><td>' + esc(r.key) + (r.risky ? ' <span class="tag badge-risky">risky</span>' : '') +
        '</td><td>' + esc(r.old_value) + '</td><td>' + esc(r.new_value) + '</td></tr>').join('')
        : '<tr><td colspan="5" style="color:#6b7280;text-align:center">No changes made on the dashboard yet.</td></tr>';
}

loadSettings();
loadHistory();
