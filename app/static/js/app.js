async function fetchAPI(url) {
    try { const r = await fetch(url); return r.ok ? await r.json() : null; }
    catch (e) { console.error('API error:', url, e); return null; }
}
function formatMoney(v) {
    if (v == null || v === '' || v === '--') return '--';
    const n = parseFloat(v); if (isNaN(n)) return '--';
    return n.toLocaleString('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 0, maximumFractionDigits: 0 });
}
function formatPnl(v) {
    if (v == null) return '--';
    const n = parseFloat(v); if (isNaN(n)) return '--';
    return (n > 0 ? '+' : '') + n.toLocaleString('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function setText(id, text) {
    const el = document.getElementById(id); if (!el) return;
    el.textContent = text;
    if (id.includes('unrealized') || id.includes('daily') || id.includes('pnl')) {
        const n = parseFloat(String(text).replace(/[^0-9.\-]/g, ''));
        if (!isNaN(n)) el.className = 'card-title ' + (n >= 0 ? 'text-success' : 'text-danger');
    }
}
async function triggerRefresh() {
    const btn = document.getElementById('btn-refresh'); btn.disabled = true; btn.textContent = '刷新中...';
    try { await fetch('/api/refresh', { method: 'POST' }); } catch (e) {}
    setTimeout(() => { btn.disabled = false; btn.textContent = '手动刷新'; }, 2000);
}
async function updateConnectionStatus() {
    try {
        const s = await fetchAPI('/api/status'); if (!s) return;
        const badge = document.getElementById('conn-status');
        const mode = document.getElementById('conn-mode');
        const acct = document.getElementById('conn-accounts');
        const upd = document.getElementById('conn-update');
        const fresh = document.getElementById('conn-freshness');
        badge.className = s.connected ? 'badge bg-success' : 'badge bg-danger';
        badge.textContent = s.connected ? '已连接' : '未连接';
        mode.textContent = s.connection_mode || 'Unknown';
        mode.className = 'badge ' + (s.connection_mode === 'Live' ? 'bg-danger' : 'bg-info') + ' ms-1';
        acct.textContent = s.accounts && s.accounts.length > 0 ? '账户: ' + s.accounts[0].substring(0,4) + '****' : '';
        if (s.last_update) upd.textContent = '更新: ' + new Date(s.last_update).toLocaleTimeString();
        if (fresh) { fresh.textContent = s.data_freshness === 'fresh' ? '' : '(数据可能过期)'; fresh.className = 'ms-2 text-warning'; }
    } catch (e) {}
}
updateConnectionStatus(); setInterval(updateConnectionStatus, 5000);
