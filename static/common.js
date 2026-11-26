const 投递台 = {
  async getJSON(url, opt) {
    const r = await fetch(url, opt);
    if (r.status === 401) { location.href = '/login'; throw new Error('未登录'); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.错误 || data.error || r.statusText);
    return data;
  },
  async postJSON(url, body) {
    return this.getJSON(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
  },
  安全文本(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  },
  徽章(状态) {
    const span = document.createElement('span');
    span.className = '徽章 ' + (状态 || '');
    span.textContent = 状态 || '—';
    return span;
  },
  成败徽章(状态) {
    const s = document.createElement('span');
    const ok = 状态 === '成功';
    s.className = '徽章 ' + (ok ? '成功' : '失败');
    s.textContent = ok ? '成功' : (状态 || '失败');
    return s;
  },
  版本徽章(版本, 现行版本) {
    const s = document.createElement('span');
    const 旧 = 现行版本 && 版本 && 版本 !== 现行版本;
    s.className = '徽章 密钥版本 ' + (旧 ? '旧钥' : '现行');
    s.textContent = 版本 ? ('🔑 ' + 版本 + (旧 ? ' · 旧钥' : ' · 现行')) : '版本未封';
    return s;
  },
  同票徽章(同票) {
    const s = document.createElement('span');
    s.className = '徽章 ' + (同票 ? '同票' : '异票');
    s.textContent = 同票 ? '正式/影子同票' : '票不一致';
    return s;
  },
  查询参数() {
    return new URLSearchParams(location.search || '');
  },
  行(html) {
    const tr = document.createElement('tr');
    tr.innerHTML = html;
    return tr;
  },
  空态(msg) { return msg || '暂无数据'; },
};
