// Alerts history: read-only list of the mail audit log. Text is inserted with textContent only.
(function () {
    var page = 1;
    var pages = 1;
    var summary = document.getElementById('ah-summary');
    var body = document.getElementById('ah-rows');
    var prev = document.getElementById('ah-prev');
    var next = document.getElementById('ah-next');

    function cell(tr, text) {
        var td = document.createElement('td');
        td.textContent = text === null || text === undefined ? '' : String(text);
        tr.appendChild(td);
    }

    function render(data) {
        page = data.page;
        pages = data.pages;
        summary.textContent = data.total + ' entries - page ' + data.page + ' of ' + data.pages;
        body.textContent = '';
        data.rows.forEach(function (r) {
            var tr = document.createElement('tr');
            cell(tr, r.time);
            cell(tr, r.kind + ' (' + r.class_label + ')');
            cell(tr, r.fault_code);
            cell(tr, r.note ? r.title + ' - ' + r.note : r.title);
            cell(tr, r.fy);
            cell(tr, r.leg);
            cell(tr, r.status);
            cell(tr, r.recipient_count === null ? '-' : r.recipient_count);
            cell(tr, r.recovered_after || '');
            body.appendChild(tr);
        });
        prev.disabled = page <= 1;
        next.disabled = page >= pages;
    }

    function load(n) {
        fetch('/api/alerts-history?page=' + n, { headers: { 'Accept': 'application/json' } })
            .then(function (resp) {
                if (resp.status === 401) { summary.textContent = 'Session expired. Refresh the page and sign in again.'; return null; }
                if (!resp.ok) { summary.textContent = 'Could not read the history. Try again.'; return null; }
                return resp.json();
            })
            .then(function (data) { if (data) render(data); })
            .catch(function () { summary.textContent = 'Could not read the history. Try again.'; });
    }

    prev.addEventListener('click', function () { if (page > 1) load(page - 1); });
    next.addEventListener('click', function () { if (page < pages) load(page + 1); });
    load(1);
})();
