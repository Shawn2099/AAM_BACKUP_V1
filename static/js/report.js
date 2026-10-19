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
            showToast(data.detail || 'Check that it arrived. The report was emailed.', 'success');
        } else if (response.status === 404) {
            showToast(data.detail || 'Try again after the next backup. No backups ran in this period, so there is nothing to send.', 'info');
        } else {
            showToast(data.detail || 'Check the mail settings and try again. The email was not sent.', 'error');
        }
    } catch (err) {
        if (typeof showToast === 'function') {
            showToast('Check your connection and try again. The page could not reach the server (' + err.message + ').', 'error');
        } else {
            alert('Network error: ' + err.message);
        }
    } finally {
        if (btn) { btn.style.pointerEvents = ''; btn.style.opacity = ''; btn.innerText = 'Email ' + label + ' Report'; }
    }
}
