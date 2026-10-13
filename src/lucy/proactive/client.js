/* Lucy proactive client. Injected into the chat page by Lucy Core (GET /), self-contained.
 *
 * Listens to /api/proactive/stream and shows what Lucy says first as a toast (plus a system notification
 * when the tab is hidden). To handle it inside chat.js instead, listen for the cancelable events:
 *     window.addEventListener('lucy:proactive',      e => { e.preventDefault(); ... e.detail ... });
 *     window.addEventListener('lucy:proactive-open', e => { e.preventDefault(); switchToSession(e.detail.session_id); });
 */
(function () {
  'use strict';
  if (window.__lucyProactive) return;
  window.__lucyProactive = true;

  var KEY = window.LUCY_PROACTIVE_KEY || '';
  var BASE = '/api/proactive';
  var lastId = parseInt(sessionStorage.getItem('lucy_pro_last') || '0', 10) || 0;
  var es = null, retry = 0, toasts = [], hiddenCount = 0, baseTitle = document.title;

  function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ 'Content-Type': 'application/json', 'X-API-Key': KEY }, opts.headers || {});
    return fetch(BASE + path, opts).catch(function () {});
  }
  function post(path, body) { return api(path, { method: 'POST', body: JSON.stringify(body) }); }

  // ---- styles (namespaced; neon cyan = Lucy)
  var css = document.createElement('style');
  css.textContent =
    '#lucy-pro-stack{position:fixed;right:16px;bottom:16px;z-index:2147483000;display:flex;flex-direction:column;gap:10px;max-width:min(380px,92vw);font:14px/1.45 system-ui,sans-serif}' +
    '.lucy-pro{background:#0b1418;color:#e6f7fb;border:1px solid #00E5FF;border-radius:12px;padding:12px 14px;box-shadow:0 6px 24px rgba(0,229,255,.18);animation:lucyIn .25s ease-out}' +
    '.lucy-pro.reminder{border-color:#ffd54a;box-shadow:0 6px 24px rgba(255,213,74,.2)}.lucy-pro.alert{border-color:#ff6b6b}' +
    '.lucy-pro h4{margin:0 0 4px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#00E5FF}' +
    '.lucy-pro.reminder h4{color:#ffd54a}.lucy-pro.alert h4{color:#ff6b6b}' +
    '.lucy-pro p{margin:0;white-space:pre-wrap;word-break:break-word}.lucy-pro img{max-width:96px;display:block;margin-top:8px}' +
    '.lucy-pro .row{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}' +
    '.lucy-pro button{background:transparent;color:#9fdbe8;border:1px solid #2a4a55;border-radius:6px;padding:3px 9px;font-size:12px;cursor:pointer}' +
    '.lucy-pro button:hover{border-color:#00E5FF;color:#fff}' +
    '@keyframes lucyIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}';
  document.head.appendChild(css);
  var stack = document.createElement('div');
  stack.id = 'lucy-pro-stack';
  function mount() { if (!stack.parentNode) document.body.appendChild(stack); }

  function mediaUrl(path) {
    if (!path) return '';
    return '/api/file/' + String(path).split('/').map(encodeURIComponent).join('/') + '?api_key=' + encodeURIComponent(KEY);
  }

  function dismiss(el, ev, action) {
    if (ev && ev.id) post('/feedback', { id: ev.id, action: action || 'dismiss' });
    if (el && el.parentNode) el.parentNode.removeChild(el);
  }

  function button(label, fn) {
    var b = document.createElement('button'); b.type = 'button'; b.textContent = label; b.onclick = fn; return b;
  }

  function show(ev) {
    var det = { detail: ev, cancelable: true };
    if (!window.dispatchEvent(new CustomEvent('lucy:proactive', det))) return;   // chat.js took over
    mount();
    var el = document.createElement('div');
    el.className = 'lucy-pro ' + (ev.kind || '');
    var h = document.createElement('h4'); h.textContent = ev.title || 'Lucy'; el.appendChild(h);
    var p = document.createElement('p'); p.textContent = String(ev.text || '').replace(/\*\*/g, '').replace(/^_|_$/gm, ''); el.appendChild(p);
    if (ev.media) { var img = document.createElement('img'); img.src = mediaUrl(ev.media); img.alt = ''; el.appendChild(img); }
    var row = document.createElement('div'); row.className = 'row';
    row.appendChild(button('Open', function () {
      post('/feedback', { id: ev.id, action: 'open' });
      var open = new CustomEvent('lucy:proactive-open', { detail: ev, cancelable: true });
      if (window.dispatchEvent(open)) location.reload();      // default: reload so the chat shows the new message
    }));
    if (ev.kind !== 'reminder') row.appendChild(button('Not now (1h)', function () { dismiss(el, ev, 'snooze'); }));
    if (ev.mutable) row.appendChild(button('Stop these', function () { dismiss(el, ev, 'mute'); }));
    row.appendChild(button('Dismiss', function () { dismiss(el, ev, 'dismiss'); }));
    el.appendChild(row);
    stack.appendChild(el);
    toasts.push(el);
    if (ev.kind !== 'reminder' && ev.kind !== 'alert') setTimeout(function () { dismiss(el, ev, 'dismiss'); }, 30000);
    while (stack.children.length > 4) stack.removeChild(stack.firstChild);
    if (document.hidden) notifyOS(ev);
  }

  function notifyOS(ev) {
    hiddenCount++; document.title = '(' + hiddenCount + ') ' + baseTitle;
    try {
      if ('Notification' in window && Notification.permission === 'granted') {
        var n = new Notification(ev.title || 'Lucy', { body: String(ev.text || '').replace(/\*\*/g, '').slice(0, 200), tag: 'lucy-' + ev.id });
        n.onclick = function () { window.focus(); n.close(); };
      }
    } catch (e) {}
  }
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) { hiddenCount = 0; document.title = baseTitle; }
    post('/presence', { visible: !document.hidden });
  });
  // permission can only be requested from a user gesture: piggyback on the first click anywhere
  document.addEventListener('click', function once() {
    document.removeEventListener('click', once);
    try { if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission(); } catch (e) {}
  });

  function connect() {
    if (es) es.close();
    es = new EventSource(BASE + '/stream?since=' + lastId + '&api_key=' + encodeURIComponent(KEY));
    es.onopen = function () { retry = 0; };
    es.onmessage = function (m) {
      var ev; try { ev = JSON.parse(m.data); } catch (e) { return; }
      if (ev.id && ev.id <= lastId) return;
      if (ev.id) { lastId = ev.id; sessionStorage.setItem('lucy_pro_last', String(lastId)); }
      show(ev);
    };
    es.onerror = function () {
      es.close(); retry = Math.min(retry + 1, 6);
      setTimeout(connect, 1000 * Math.pow(2, retry));
    };
  }

  window.addEventListener('beforeunload', function () { try { es && es.close(); } catch (e) {} });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', connect); else connect();
})();
