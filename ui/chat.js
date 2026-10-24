// Lucy Core chat UI logic.
// Loaded by chat.html (/chat.js). The API key and sandbox root are NOT in this file:
// the server injects them into the HTML page as window.__API_KEY__ / window.__SAFE_ROOT__.
// Plain script (not a module) on purpose: the markup uses inline onclick="fn()" handlers,
// which need these functions to be global.

// API key injected server-side (read-only, no secrets in client code)
const API_KEY = window.__API_KEY__ || '';
const SAFE_ROOT_POSIX = window.__SAFE_ROOT__ || ''; // set by the inline config block in chat.html

// Helper: default fetch headers with API key
function authHeaders(extra = {}) {
    const headers = {...extra};
    headers['X-API-Key'] = API_KEY;
    return headers;
}

// Render markdown-lite for assistant messages (bullets, bold, italics, line breaks)
function renderMarkdown(text, keepLineBreaks) {
    // Parse MEDIA: markers into image tags BEFORE marked.js processes the text
    // so they survive innerHTML replacements during streaming.
    text = parseMediaMarkers(text);
    if (typeof marked === 'undefined') {
        // Fallback: basic rendering if marked.js hasn't loaded
        return text
            .replace(/^\s*\* (.+)$/gm, '<li>$1</li>')
            .replace(/(<li>.*<\/li>)/gs, '<ul>$1<\/ul>')
            .replace(/\*\*(.+?)\*\*/g, '<strong>$1<\/strong>')
            .replace(/\*(.+?)\*/g, '<em>$1<\/em>')
            .replace(/\n/g, '<br>');
    }
    // keepLineBreaks: the user's own messages show every Shift+Enter line break as typed
    return keepLineBreaks ? marked.parse(text, { breaks: true }) : marked.parse(text);
}

// -- PCM audio playback for streaming TTS --

// AudioContext singleton — reused across all chunks to avoid clicks/pops.
// The AudioServer outputs 24 kHz s16LE PCM, so we hard-code that here.
let _audioCtx = null;

// Gapless scheduling state (browser fallback; the default path is server-side DirectSound).
// Frames are counted as integers so chunk N+1 starts on exactly the sample chunk N ends on.
const PCM_PREBUFFER_SEC = 0.30;   // lead before the first chunk of an utterance / after a stall
let _pcmBaseTime = 0;             // AudioContext time of scheduled frame 0
let _pcmFrames = 0;               // frames scheduled since _pcmBaseTime
let _pcmChain = Promise.resolve(); // chunks are scheduled strictly in arrival order

function _getAudioContext() {
    if (!_audioCtx) {
        // Explicitly lock to 24000 to match Cielvox 2.6 output exactly
        _audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
    }
    if (_audioCtx.state === 'suspended') {
        _audioCtx.resume();
    }
    return _audioCtx;
}

// Fetches start immediately (in parallel) but are SCHEDULED through a promise chain, so a small
// chunk that downloads faster can never jump ahead of an earlier one.
function playPcmChunk(fileUrl, sampleRate, channels) {
    const fetched = fetch(fileUrl)
        .then(r => (r.ok ? r.arrayBuffer() : (console.warn(`PCM fetch failed: ${r.status}`), null)))
        .catch(e => (console.warn('PCM fetch error:', e), null));
    _pcmChain = _pcmChain.then(async () => {
        const ab = await fetched;
        if (ab) _schedulePcm(ab, sampleRate || 24000);
    }).catch(e => console.warn('PCM playback error:', e));
    return _pcmChain;
}

function _schedulePcm(arrayBuffer, sampleRate) {
    const ctx = _getAudioContext();
    const samples = new Int16Array(arrayBuffer, 0, arrayBuffer.byteLength >> 1);
    if (!samples.length) return;
    const f32 = new Float32Array(samples.length);
    for (let i = 0; i < samples.length; i++) f32[i] = samples[i] / 32768;
    const buf = ctx.createBuffer(1, f32.length, sampleRate);   // mono; Web Audio upmixes
    buf.copyToChannel(f32, 0);

    const now = ctx.currentTime;
    let start = _pcmBaseTime + _pcmFrames / sampleRate;
    if (_pcmFrames === 0 || start < now + 0.005) {
        // First chunk, or the queue ran dry: restart with a prebuffer so the next chunks can arrive.
        if (_pcmFrames > 0) {
            console.warn(`[pcm] underrun: queue ran dry ${Math.round((now - start) * 1000)} ms ago`);
        }
        _pcmBaseTime = now + PCM_PREBUFFER_SEC;
        _pcmFrames = 0;
        start = _pcmBaseTime;
    }
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    src.start(start);
    _pcmFrames += f32.length;
}

function parseMediaMarkers(text) {
    if (!text) return '';
    // Replace MEDIA:path tokens (optionally followed by a caption) with <img> tags.
    // Handles both "MEDIA:path caption" and standalone "MEDIA:path" on its own line.
    // PCM files are excluded here — they are handled by playPcmChunk() in the SSE handler.
    return text.replace(/MEDIA:([^ \n]+)(?: (.+?))?(?=\n|$)/g, function(match, mediaPath, caption) {
        if (/\.pcm$/i.test(mediaPath)) return '';
        const fileName = mediaPath.split(/[\\/]/).pop();
        const fileUrl = `/api/file/${encodeURIComponent(mediaPath)}?api_key=${encodeURIComponent(API_KEY)}`;
        const mimeType = getMimeType(fileName);
        let html = `<img src="${fileUrl}" alt="${fileName}" type="${mimeType}" style="max-width:200px;max-height:200px;border-radius:12px;border:1px solid var(--border);margin-top:8px;display:block;"`;
        if (caption) {
            html += ` title="${escapeHtml(caption)}"`;
        }
        html += '>';
        return html;
    });
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

const chatContainer = document.getElementById('chatContainer');
const messageInput = document.getElementById('messageInput');
const sendButton = document.getElementById('sendButton');
const statusText = document.getElementById('statusText');
const statusIndicator = document.getElementById('statusIndicator');
const chatHistoryEl = document.getElementById('chatHistory');
const settingsOverlay = document.getElementById('settingsOverlay');
const fileInput = document.getElementById('fileInput');
const filePreviewContainer = document.getElementById('filePreviewContainer');
const uploadBtn = document.getElementById('uploadBtn');

let selectedFiles = [];
let isStreaming = false;
let currentAbort = null;   // AbortController for the in-flight /api/chat request
let messageQueue = [];     // messages typed while a reply is still being produced
let queueSeq = 0;
let currentSessionId = null;   // null = the start page (a draft). No session exists until the first message is sent.
let sessions = {};
let currentActivity = null;
let sidebarCollapsed = false;

// Sidebar toggle
const sidebar = document.getElementById('sidebar');
const sidebarToggleBtn = document.getElementById('sidebarToggle');
const mobileMenuBtn = document.getElementById('mobileMenuBtn');
const sidebarBackdrop = document.getElementById('sidebarBackdrop');
const mobileQuery = window.matchMedia('(max-width: 768px)');

function openMobileSidebar() {
    sidebar.classList.add('mobile-open');
    sidebarBackdrop.classList.add('active');
}

function closeMobileSidebar() {
    sidebar.classList.remove('mobile-open');
    sidebarBackdrop.classList.remove('active');
}

if (sidebarToggleBtn && sidebar) {
    sidebarToggleBtn.addEventListener('click', () => {
        if (mobileQuery.matches) {
            // On mobile the same button just closes the drawer
            closeMobileSidebar();
        } else {
            sidebarCollapsed = !sidebarCollapsed;
            sidebar.classList.toggle('collapsed', sidebarCollapsed);
        }
    });
}

if (mobileMenuBtn) {
    mobileMenuBtn.addEventListener('click', () => {
        if (sidebar.classList.contains('mobile-open')) {
            closeMobileSidebar();
        } else {
            openMobileSidebar();
        }
    });
}

if (sidebarBackdrop) {
    sidebarBackdrop.addEventListener('click', closeMobileSidebar);
}

messageInput.focus();

// ---- Path Helpers ----
function pathToRelative(filePath) {
    let relativePath = filePath;
    relativePath = relativePath.replace(/\\/g, '/');
    // Strip the server's sandbox root (injected by the server at page load)
    const root = SAFE_ROOT_POSIX.replace(/\/+$/, '');
    if (root && relativePath.toLowerCase().startsWith(root.toLowerCase() + '/')) {
        relativePath = relativePath.slice(root.length + 1);
    }
    return relativePath;
}

// ---- Session Management via API ----
async function loadSessions() {
    try {
        const resp = await fetch('/api/sessions', {headers: authHeaders()});
        if (resp.ok) {
            const data = await resp.json();
            // Merge instead of replace: keeps messages already loaded for a session. The list carries no messages.
            for (const [id, meta] of Object.entries(data.sessions || {})) {
                const prev = sessions[id] || {};
                sessions[id] = Object.assign({messages: prev.messages || []}, prev, meta);
            }
            renderHistory();
        }
    } catch (e) {
        console.error('Failed to load sessions:', e);
    }
}

// The start page. Nothing is created on the server here: a session only exists once you send a message
// from it (see ensureSession), so refreshing the page or clicking "Create New Session" leaves no empty sessions.
function newSession() {
    closeLibrary();
    switchSeq++;                       // cancel a session load that may still be in flight
    currentSessionId = null;
    chatContainer.innerHTML = '';
    renderHistory();
    if (window.deskOnSwitch) deskOnSwitch();
    messageInput.focus();
    if (mobileQuery.matches) closeMobileSidebar();
}

async function ensureSession() {
    if (currentSessionId) return currentSessionId;
    let id = null, updated = null;
    try {
        const resp = await fetch('/api/sessions', {method: 'POST', headers: authHeaders()});
        if (resp.ok) {
            const data = await resp.json();
            id = data.session_id;
            updated = data.updated_at;
        }
    } catch (e) {
        console.error('Failed to create session:', e);
    }
    if (!id) id = 'session_' + Date.now();
    currentSessionId = id;
    sessions[id] = {
        title: 'New Session', message_count: 0, messages: [],
        updated_at: updated || localNow()
    };
    renderHistory();
    return id;
}

// "YYYY-MM-DD HH:MM:SS" on this PC's clock (not UTC: UTC was the 8 h shift on just-sent messages)
function localNow() {
    const d = new Date(), p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function formatTime(ts) {
    if (!ts) return null;
    const d = new Date(ts);
    if (isNaN(d.getTime())) {
        const fixed = ts.replace(' ', 'T');
        const d2 = new Date(fixed);
        if (isNaN(d2.getTime())) return null;
        return d2.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
    }
    return d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
}

function formatTimestamp(ts) {
    if (!ts) return '';
    const d = new Date(ts.replace(' ', 'T'));
    if (isNaN(d.getTime())) return ts;
    // Stored times are the server PC's local clock ("YYYY-MM-DD HH:MM:SS"): show them exactly as they are.
    const options = {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: true};
    return d.toLocaleString(navigator.language || 'en-US', options);
}

async function renderHistory() {
    const allSessions = [];
    for (const [id, session] of Object.entries(sessions)) {
        if (!window.deskShowSession || deskShowSession(id)) allSessions.push({id, ...session});
    }
    allSessions.sort((a, b) => {
        const ta = a.updated_at || '';
        const tb = b.updated_at || '';
        if (!ta && !tb) return 0;
        if (!ta) return -1;
        if (!tb) return 1;
        return tb.localeCompare(ta);
    });
    chatHistoryEl.innerHTML = '';
    allSessions.forEach(session => {
        const item = document.createElement('div');
        item.className = 'history-item' + (session.id === currentSessionId ? ' active' : '');
        item.onclick = () => switchSession(session.id);
        item.tabIndex = 0;
        item.setAttribute('role', 'button');
        item.addEventListener('keydown', (e) => {
            if (e.target === item && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); switchSession(session.id); }
        });
        const title = escapeHtml(session.title || 'New Session');
        const timeDisplay = formatTimestamp(session.updated_at) || '';
        item.innerHTML = `
            <span class="history-icon"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1.1-4.4A8 8 0 1 1 21 12z"/><path d="M8.5 12h.01M12 12h.01M15.5 12h.01"/></svg></span>
            <div class="history-body">
                <div class="history-title">${title}</div>
                <div class="history-meta">${timeDisplay ? `<span class="history-time">${timeDisplay}</span>` : ``}</div>
            </div>
            <button type="button" class="session-menu-btn" title="Session options" aria-label="Session options" aria-haspopup="menu" onclick="toggleSessionMenu(event)">
                <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="5" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="12" cy="19" r="1.6"/></svg>
            </button>
            <div class="session-menu" role="menu">
                <button type="button" class="session-menu-item danger" role="menuitem" onclick="deleteSession('${session.id}', event)">
                    <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>Delete session
                </button>
            </div>
        `;
        if (window.deskDecorateSession) deskDecorateSession(item, session.id);
        chatHistoryEl.appendChild(item);
    });
}

// A message's saved attachments (files, images, code) -> the file objects the chat renders.
function attachmentToFile(a) {
    if (!a) return null;
    if (a.kind === 'sticker') {                       // Lucy's stickers are not cached: they live in assets/stickers
        const rel = pathToRelative(a.path || '');
        return {sticker: true, path: a.path, name: a.name || 'sticker', type: 'image/png',
                url: `/api/file/${encodeURIComponent(rel)}?api_key=${encodeURIComponent(API_KEY)}`};
    }
    const name = a.name || 'file';
    if (a.missing) return {name: `${name} (removed)`, size: a.size, missing: true, type: 'application/octet-stream', url: null};
    return {name, size: a.size, sha: a.sha, type: a.type || getMimeType(name), url: libraryFileUrl(a.sha)};
}

function renderStoredMessage(msg) {
    const isUser = msg.role === 'user';
    const content = msg.content || '';
    const files = (msg.attachments || []).map(attachmentToFile).filter(Boolean);
    // history written by older versions kept delivered files as "MEDIA:path" lines in the text
    const legacy = (content.match(/MEDIA:[^\s]+/g) || []).filter(m => !/\.pcm$/i.test(m));
    legacy.forEach(m => {
        const mediaPath = m.replace('MEDIA:', '');
        const relativePath = pathToRelative(mediaPath);
        const fileName = mediaPath.split(/[\\/]/).pop();
        files.push({path: mediaPath, name: fileName, type: getMimeType(fileName),
                    url: `/api/file/${encodeURIComponent(relativePath)}?api_key=${encodeURIComponent(API_KEY)}`});
    });
    const _bub = addMessage(content.replace(/\nMEDIA:[^\s]+/g, '').trim(), isUser, files, msg.timestamp);
    if (window.deskTagRow) deskTagRow(_bub, msg);
}

let switchSeq = 0;
async function switchSession(id, force) {
    closeLibrary();
    if (id === currentSessionId && !force) return;
    const mySeq = ++switchSeq;
    currentSessionId = id;
    try {
        // The server answers with an ETag: a session you already opened comes back as a tiny 304 (browser cache).
        const resp = await fetch(`/api/sessions/${encodeURIComponent(id)}`, {headers: authHeaders()});
        if (resp.ok) {
            const data = await resp.json();
            const session = sessions[id] || {title: data.title || 'Session', messages: []};
            if (data.title) session.title = data.title;
            session.messages = data.messages || [];
            sessions[id] = session;
        }
    } catch (e) {
        console.error('Failed to load session:', e);
    }
    if (mySeq !== switchSeq) return;                  // you clicked something else meanwhile

    const session = sessions[id];
    chatContainer.innerHTML = '';
    if (session && session.messages) session.messages.forEach(renderStoredMessage);

    scrollToBottom();
    messageInput.focus();
    renderHistory();
    if (window.deskOnSwitch) deskOnSwitch();
    if (mobileQuery.matches) closeMobileSidebar();
}

async function deleteSession(id, event) {
    event.stopPropagation();
    if (!confirm('Delete this session?')) return;
    try {
        const resp = await fetch(`/api/sessions/${encodeURIComponent(id)}`, {
            method: 'DELETE',
            headers: authHeaders()
        });
        if (resp.ok) {
            delete sessions[id];
            if (currentSessionId === id) newSession();       // back to the start page
            await renderHistory();
        } else {
            console.error('Failed to delete session:', resp.statusText);
        }
    } catch (e) {
        console.error('Failed to delete session:', e);
    }
}

// ---- Message Handling ----

// A sticker is sent as the text "[sticker: 7.png]" (that is what gets saved in the chat
// history) plus the PNG itself as an attachment, so the model can see it. Wherever a user
// message is exactly that text, the UI shows the picture instead of the text.
const STICKER_RE = /^\s*\[sticker:\s*([^\]\n]+?\.png)\s*\]\s*$/i;

function parseStickerMessage(text) {
    const m = typeof text === 'string' ? text.match(STICKER_RE) : null;
    return m ? m[1] : null;
}

function stickerUrl(name) {
    return `/api/stickers/User/${encodeURIComponent(name)}?api_key=${encodeURIComponent(API_KEY)}`;
}

function addMessage(text, isUser = false, files = [], timestamp = null) {
    const row = document.createElement('div');
    row.className = `message-row ${isUser ? 'user-message' : 'assistant-message'}`;

    const timeEl = document.createElement('div');
    timeEl.className = 'message-time';
    timeEl.textContent = formatTime(timestamp) || '';
    row.appendChild(timeEl);

    const contentWrapper = document.createElement('div');
    contentWrapper.className = 'message-content';
    contentWrapper.style.flexDirection = 'row';
    contentWrapper.style.flexWrap = 'wrap';
    contentWrapper.style.gap = '8px';
    // With flex-direction: row the CSS `align-items` only controls the VERTICAL position,
    // so the left/right side has to come from justify-content. (Without this line every
    // message, yours included, sits on the left.)
    contentWrapper.style.justifyContent = isUser ? 'flex-end' : 'flex-start';

    const stickerName = isUser ? parseStickerMessage(text) : null;
    // A message that is only files (no text) gets no empty text bubble.
    const skipBubble = !stickerName && !(text || '').trim() && files && files.length > 0 && !files.some(f => f.pendingText);
    const bubble = document.createElement('div');
    if (stickerName) {
        bubble.className = 'sticker-bubble';
        const img = document.createElement('img');
        img.className = 'sticker-img';
        img.src = stickerUrl(stickerName);
        img.alt = `Sticker ${stickerName}`;
        img.draggable = false;
        img.onerror = () => { bubble.textContent = `[sticker: ${stickerName}]`; };
        bubble.appendChild(img);
        files = [];   // the uploaded copy (for the model) is not shown a second time
    } else {
        bubble.className = `message-bubble ${isUser ? 'user-bubble' : 'assistant-bubble'}`;
        bubble.innerHTML = renderMarkdown(text, isUser);
        enhanceCodeBlocks(bubble);
    }
    if (!skipBubble) contentWrapper.appendChild(bubble);
    row.appendChild(contentWrapper);

    if (files && files.length > 0) {
        const fileGroup = document.createElement('div');
        fileGroup.className = 'file-group';
        fileGroup.style.display = 'flex';
        fileGroup.style.flexWrap = 'wrap';
        fileGroup.style.gap = '8px';
        fileGroup.style.maxWidth = '75%';
        fileGroup.style.marginTop = '4px';
        if (files.some(f => f.url && isTextFile(f.name || f.path || ''))) fileGroup.style.flexBasis = '100%';
        files.forEach(f => {
            if (f.sticker) {                               // Lucy's stickers: shown as stickers, not as files
                const st = document.createElement('img');
                st.className = 'sticker-img';
                st.src = f.url;
                st.alt = f.name || 'sticker';
                st.draggable = false;
                fileGroup.appendChild(st);
                return;
            }
            const fileEl = renderFilePreview(f);
            if (f.missing) fileEl.classList.add('is-removed');
            fileGroup.appendChild(fileEl);
        });
        contentWrapper.appendChild(fileGroup);
    }

    chatContainer.appendChild(row);
    scrollToBottom();
    return bubble;
}

function getMimeType(fileName) {
    const ext = fileName.split('.').pop().toLowerCase();
    const map = {png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp', bmp: 'image/bmp', svg: 'image/svg+xml', wav: 'audio/wav', mp3: 'audio/mpeg'};
    return map[ext] || 'application/octet-stream';
}

function renderFilePreview(fileData, isOutgoing = false) {
    // Code / text files that live on the server get the viewer card instead of a bare link.
    if (!isOutgoing && fileData.url && !(fileData.type || '').startsWith('image/') &&
        isTextFile(fileData.name || fileData.path || '')) {
        return renderFileCard(fileData);
    }
    const wrapper = document.createElement('div');
    wrapper.className = 'file-attachment';
    wrapper.style.margin = '6px 0';
    wrapper.style.clear = 'both';

    if (fileData.type && fileData.type.startsWith('image/')) {
        const img = document.createElement('img');
        img.src = fileData.url || fileData.path || fileData.data_url;
        img.alt = fileData.name || 'image';
        img.style.maxWidth = '200px';
        img.style.maxHeight = '200px';
        img.style.borderRadius = '12px';
        img.style.border = '1px solid var(--border)';
        img.style.cursor = 'pointer';
        wrapper.appendChild(img);

        const nameDiv = document.createElement('div');
        nameDiv.textContent = fileData.name || 'Image';
        nameDiv.style.fontSize = '11px';
        nameDiv.style.color = 'var(--text-faint)';
        nameDiv.style.marginTop = '4px';
        wrapper.appendChild(nameDiv);
    } else {
        const icon = getFileTypeIcon(fileData.name || fileData.path || '');
        const el = document.createElement('div');
        el.style.display = 'flex';
        el.style.alignItems = 'center';
        el.style.gap = '6px';
        el.style.padding = '8px 12px';
        el.style.background = 'var(--bg-sunken)';
        el.style.border = '1px solid var(--border)';
        el.style.borderRadius = '8px';

        const iconSpan = document.createElement('span');
        iconSpan.textContent = icon;
        iconSpan.style.fontSize = '16px';

        const nameSpan = document.createElement('span');
        nameSpan.textContent = (fileData.name || fileData.path || 'file');
        nameSpan.style.fontSize = '12px';
        nameSpan.style.color = 'var(--text-muted)';
        nameSpan.style.maxWidth = '180px';
        nameSpan.style.overflow = 'hidden';
        nameSpan.style.textOverflow = 'ellipsis';
        nameSpan.style.whiteSpace = 'nowrap';

        const downloadLink = document.createElement('a');
        downloadLink.href = fileData.url || fileData.path || '#';
        downloadLink.download = fileData.name || '';
        downloadLink.textContent = '\u2193';
        downloadLink.style.color = 'var(--accent)';
        downloadLink.style.fontSize = '14px';
        downloadLink.style.textDecoration = 'none';
        downloadLink.style.cursor = 'pointer';
        downloadLink.onclick = (e) => {
            e.stopPropagation();   // the arrow downloads; the rest of the chip opens the preview
            if (downloadLink.href === '#') e.preventDefault();
        };

        el.style.cursor = 'pointer';
        el.tabIndex = 0;
        el.setAttribute('role', 'button');
        el.title = 'Tap to preview';
        el.addEventListener('click', () => openGenericFileViewer(fileData));
        el.addEventListener('keydown', (e) => { if (e.target === el && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); openGenericFileViewer(fileData); } });
        el.appendChild(iconSpan);
        el.appendChild(nameSpan);
        el.appendChild(downloadLink);
        wrapper.appendChild(el);

        if (fileData.size) {
            const sizeDiv = document.createElement('div');
            sizeDiv.textContent = formatFileSize(fileData.size);
            sizeDiv.style.fontSize = '11px';
            sizeDiv.style.color = 'var(--text-faint)';
            sizeDiv.style.marginTop = '2px';
            wrapper.appendChild(sizeDiv);
        }
    }

    return wrapper;
}

function getFileTypeIcon(filename) {
    const ext = filename.split('.').pop().toLowerCase();
    const icons = {
        'pdf': '\uD83D\uDCC4', 'doc': '\uD83D\uDCC4', 'docx': '\uD83D\uDCC4', 'txt': '\uD83D\uDCC4',
        'jpg': '\uD83D\uDFE0', 'jpeg': '\uD83D\uDFE0', 'png': '\uD83D\uDFE0', 'gif': '\uD83D\uDFE0',
        'mp3': '\uD83C\uDFAF', 'wav': '\uD83C\uDFAF', 'ogg': '\uD83C\uDFAF',
        'mp4': '\uD83D\uDCF9', 'mov': '\uD83D\uDCF9', 'webm': '\uD83D\uDCF9',
        'zip': '\uD83D\u9C8B', 'rar': '\uD83D\u9C8B', '7z': '\uD83D\u9C8B',
        'json': '\uD83D\uDDC4', 'yaml': '\uD83D\uDDC4', 'yml': '\uD83D\uDDC4',
        'py': '\uD83D\uA782', 'js': '\uD83D\uDD90', 'html': '\uD83D\uDCBB', 'css': '\uD83D\uDDEE',
    };
    return icons[ext] || '\uD83D\u9C96';
}

function formatFileSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1073741824) return `${(bytes / 1048576).toFixed(1)} MB`;
    return `${(bytes / 1073741824).toFixed(1)} GB`;
}

function handleFileSelect(event) {
    const files = Array.from(event.target.files);
    files.forEach(file => {
        selectedFiles.push(file);
        const reader = new FileReader();
        reader.onload = (e) => {
            addFilePreview(file, e.target.result);
        };
        if (file.type.startsWith('image/')) {
            reader.readAsDataURL(file);
        }
    });
    event.target.value = '';
    updateSendButtonState();
}

function addFilePreview(file, dataUrl) {
    const preview = document.createElement('div');
    preview.className = 'file-preview';
    preview.dataset.filename = file.name;

    const nameSpan = document.createElement('span');
    nameSpan.className = 'file-name';
    nameSpan.textContent = file.name;
    nameSpan.title = file.name;

    const sizeSpan = document.createElement('span');
    sizeSpan.className = 'file-size';
    sizeSpan.textContent = formatFileSize(file.size);

    const removeBtn = document.createElement('span');
    removeBtn.className = 'remove-file';
    removeBtn.textContent = '\u00d7';
    removeBtn.onclick = () => {
        selectedFiles = selectedFiles.filter(f => f.name !== file.name);
        preview.remove();
        updateSendButtonState();
    };

    preview.appendChild(nameSpan);
    preview.appendChild(sizeSpan);
    preview.appendChild(removeBtn);
    filePreviewContainer.appendChild(preview);
}

function updateSendButtonState() {
    uploadBtn.disabled = false;   // attachments can be prepared while a reply is running
    updateSendButton();
}

// The main button changes with context, like most chat apps:
//   idle                      -> "Send"
//   replying, box is empty    -> "Stop"  (abort the current reply)
//   replying, text typed      -> "Queue" (sent after the current reply finishes)
const SEND_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4 20-7z"/></svg>';
const STOP_ICON = '<svg class="fill-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';

function setSendButton(icon, label, extraClass) {
    // Only touch the DOM when something changed (this runs on every keystroke)
    if (sendButton.dataset.state !== label) {
        sendButton.dataset.state = label;
        sendButton.innerHTML = icon;
        sendButton.setAttribute('aria-label', label);
    }
    sendButton.classList.toggle('queue-mode', extraClass === 'queue-mode');
}

function updateSendButton() {
    const hasInput = messageInput.value.trim().length > 0 || selectedFiles.length > 0;
    sendButton.disabled = false;
    sendButton.classList.toggle('stop-mode', isStreaming && !hasInput);
    if (isStreaming && !hasInput) {
        setSendButton(STOP_ICON, 'Stop');
        sendButton.title = 'Stop the current reply (Esc)';
    } else if (isStreaming) {
        setSendButton(SEND_ICON, 'Queue', 'queue-mode');
        sendButton.title = 'Queue: send this after the current reply finishes';
    } else {
        setSendButton(SEND_ICON, 'Send');
        sendButton.title = 'Send (Enter)';
    }
}

function onSendClick() {
    const hasInput = messageInput.value.trim().length > 0 || selectedFiles.length > 0;
    if (isStreaming && !hasInput) {
        stopGeneration();
    } else {
        sendMessage();
    }
}

function stopGeneration() {
    if (currentAbort) currentAbort.abort();
}

// ---- Queued messages ----
const queueTray = document.createElement('div');
queueTray.id = 'queueTray';
queueTray.className = 'queue-tray';
document.getElementById('chatForm').parentNode.insertBefore(queueTray, document.getElementById('chatForm'));

function renderQueue() {
    queueTray.innerHTML = '';
    queueTray.style.display = messageQueue.length ? 'flex' : 'none';
    messageQueue.forEach((item, idx) => {
        const row = document.createElement('div');
        row.className = 'queue-item';
        const label = document.createElement('span');
        label.className = 'queue-text';
        const queuedSticker = parseStickerMessage(item.message);
        const fileNote = (!queuedSticker && item.files.length) ? ` [+${item.files.length} file${item.files.length > 1 ? 's' : ''}]` : '';
        label.textContent = `${idx + 1}. ${queuedSticker ? '\u{1F5BC} Sticker ' + queuedSticker : (item.message || '(file)')}${fileNote}`;
        label.title = item.message;
        const now = document.createElement('button');
        now.type = 'button';
        now.className = 'queue-btn';
        now.textContent = 'Send now';
        now.title = 'Stop the current reply and send this next';
        now.onclick = () => sendQueuedNow(item.id);
        const del = document.createElement('button');
        del.type = 'button';
        del.className = 'queue-btn';
        del.textContent = '\u2715';
        del.title = 'Remove from queue';
        del.onclick = () => removeQueued(item.id);
        row.appendChild(label);
        row.appendChild(now);
        row.appendChild(del);
        queueTray.appendChild(row);
    });
}

function enqueueMessage(message, files) {
    messageQueue.push({id: ++queueSeq, message, files, sessionId: currentSessionId});
    renderQueue();
}

function removeQueued(id) {
    messageQueue = messageQueue.filter(q => q.id !== id);
    renderQueue();
}

function sendQueuedNow(id) {
    // Move it to the front and interrupt: the stream's finally-block then sends it.
    const idx = messageQueue.findIndex(q => q.id === id);
    if (idx > 0) messageQueue.unshift(messageQueue.splice(idx, 1)[0]);
    renderQueue();
    if (isStreaming) stopGeneration(); else processQueue();
}

function processQueue() {
    if (isStreaming || messageQueue.length === 0) return;
    const next = messageQueue.shift();
    renderQueue();
    if (next.sessionId !== currentSessionId) {
        // The user switched conversations meanwhile; don't post into the wrong one.
        processQueue();
        return;
    }
    sendMessage(next);
}

function scrollToBottom() {
    chatContainer.scrollTop = chatContainer.scrollHeight;
}

function setSending(sending) {
    isStreaming = sending;
    // The input stays usable while a reply is running so the next message
    // can be prepared, queued, or used to interrupt.
    messageInput.disabled = false;
    updateSendButton();
    if (!sending) {
        const ti = document.getElementById('thinkingIndicator');
        if (ti) {
            ti.remove();
        }
    }
}

async function sendMessage(queued) {
    if (!queued && window.deskPreSend && deskPreSend()) return;     // P9: /commands are handled here
    const fromQueue = !!queued;
    const message = fromQueue ? queued.message : messageInput.value.trim();
    const sendFiles = fromQueue ? queued.files : selectedFiles;
    if (!message && sendFiles.length === 0) return;

    if (isStreaming && !fromQueue) {
        // A reply is still running: park this message instead of dropping it.
        enqueueMessage(message, [...selectedFiles]);
        messageInput.value = '';
        autosizeInput();
        selectedFiles = [];
        filePreviewContainer.innerHTML = '';
        updateSendButton();
        return;
    }

    // The start page is a draft: the session is only created now, by the first message you send.
    const sendSid = await ensureSession();
    if (window.deskOnSession) await deskOnSession(sendSid);
    const deskOpts = window.deskTakeOpts ? deskTakeOpts() : {};

    currentActivity = null;
    window._toolShownAt = 0;
    if (window._pendingThink) { clearTimeout(window._pendingThink); window._pendingThink = null; }

    const imageFiles = sendFiles.filter(f => f.type.startsWith('image/'));
    await Promise.all(imageFiles.map(f => readFileAsDataURL(f)));

    const outgoingFiles = sendFiles.map(f => ({
        name: f.name,
        size: f.size,
        type: f.type,
        data_url: f.type.startsWith('image/') ? f._dataUrl : undefined,
        url: f.type.startsWith('image/') ? undefined : URL.createObjectURL(f),   // lets you preview / download what you just sent
        path: f._savedPath || null
    })).filter(f => f);

    const now = localNow();
    addMessage(message || '(file)', true, outgoingFiles, now);
    const filesToSend = [...sendFiles];
    sendFiles.forEach(f => delete f._dataUrl);
    if (!fromQueue) {
        // (a queued send must not wipe what the user is typing right now)
        selectedFiles = [];
        filePreviewContainer.innerHTML = '';
        messageInput.value = '';
        autosizeInput();
    }
    currentAbort = new AbortController();
    setSending(true);

    const thinkingIndicator = document.createElement('div');
    thinkingIndicator.className = 'thinking-indicator active';
    thinkingIndicator.id = 'thinkingIndicator';
    thinkingIndicator.innerHTML = '<span class="dot"></span><span>Thinking...</span>';

    let streamingBubble = addMessage('', false, [], now);
    const streamingRow = streamingBubble.closest('.message-row');
    if (streamingRow) {
        streamingRow.parentNode.insertBefore(thinkingIndicator, streamingRow);
    }
    if (thinkingIndicator) {
        thinkingIndicator.scrollIntoView({behavior: 'auto', block: 'start'});
    }

    let fullResponse = '';
    let pendingMedia = [];
    let firstContentReceived = false;
    // Watchdog: the indicator is hidden while text streams. If the stream then goes quiet (the model is
    // generating a tool call, a retry round is running, a long prompt is being processed...) bring it back,
    // otherwise the UI looks frozen while the GPU is busy. Tool labels ("Searching: x") are left alone.
    let stallTimer = null;
    const STALL_MS = 1500;
    const armStall = () => {
        clearTimeout(stallTimer);
        stallTimer = setTimeout(() => {
            if (currentActivity !== 'think' && currentActivity !== null && currentActivity !== undefined) return;
            if (window._activityHideTimer) { clearTimeout(window._activityHideTimer); window._activityHideTimer = null; }
            currentActivity = 'think';
            const ti = document.getElementById('thinkingIndicator');
            if (ti) {
                ti.classList.add('active');
                const label = ti.querySelector('span:last-child');
                if (label) label.textContent = 'Thinking...';
            }
        }, STALL_MS);
    };
    const voiceMode = voiceToggle && voiceToggle.checked;

    try {
        const formData = new FormData();
formData.append('message', message);
formData.append('session_id', sendSid);
formData.append('stream', 'true');
formData.append('voice_mode', voiceMode ? 'true' : 'false');
if (deskOpts.mode) formData.append('mode', deskOpts.mode);
if (deskOpts.reuse && deskOpts.reuse.length) formData.append('reuse_shas', JSON.stringify(deskOpts.reuse));
filesToSend.forEach(f => formData.append('files', f));

        const response = await fetch(`/api/chat?api_key=${API_KEY}`, {
            method: 'POST',
            body: formData,
            signal: currentAbort.signal
        });

        if (!response.ok) {
            const errText = await response.text();
            throw new Error(`Server error: ${response.status} - ${errText}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        // SSE events can straddle network chunk boundaries, so keep a
        // persistent buffer and only process COMPLETE events (ended by
        // a blank line). Without this, a split event fails JSON.parse
        // and is silently dropped — e.g. the "Reading: file" activity.
        let sseBuffer = '';

        while (true) {
            const {done, value} = await reader.read();
            if (done) { clearTimeout(stallTimer); break; }
            armStall();

            sseBuffer += decoder.decode(value, {stream: true});
            const lines = sseBuffer.split('\n\n');
            sseBuffer = lines.pop(); // trailing partial event stays buffered

            for (const line of lines) {
                if (line.startsWith('data: ')) {
                    try {
                        const data = JSON.parse(line.substring(6));
                        if (data.content) {
                            firstContentReceived = true;
                            if (window._pendingThink) {
                                clearTimeout(window._pendingThink);
                                window._pendingThink = null;
                            }
                            if (currentActivity === 'think') {
                                thinkingIndicator.classList.remove('active');
                            }
                            fullResponse += data.content;
                            streamingBubble.innerHTML = renderMarkdown(fullResponse);
                            if (thinkingIndicator.classList.contains('active')) {
                                thinkingIndicator.scrollIntoView({behavior: 'smooth', block: 'start'});
                            } else {
                                scrollToBottom();
                            }
                        } else if (data.turn) {
                            deskTurn(streamingBubble, data.turn);
                        } else if (data.turn_done) {
                            deskTurnDone(streamingBubble, data.turn_done);
                        } else if (data.attachment) {
                            const _af = attachmentToFile(data.attachment);
                            if (_af) getFileGroup(streamingBubble).appendChild(renderFilePreview(_af));
                            scrollToBottom();
                        } else if (data.approval_request) {
                            // Safety layer: the agent is waiting for your OK (Approve / Deny / Always allow).
                            getFileGroup(streamingBubble).appendChild(renderApprovalCard(data.approval_request, sendSid));
                            scrollToBottom();
                        } else if (data.approval_resolved) {
                            markApprovalResolved(data.approval_resolved.id, data.approval_resolved.decision);
                        } else if (data.error) {
                            streamingBubble.textContent = `Error: ${data.error}`;
                        } else if (data.media) {
                            const mediaPath = data.media;
                            const relativePath = pathToRelative(mediaPath);
                            let fileUrl = `/api/file/${encodeURIComponent(relativePath)}?api_key=${encodeURIComponent(API_KEY)}`;
                            const fileName = mediaPath.split(/[\\/]/).pop();
                            const mimeType = getMimeType(fileName);

                            // Streaming TTS PCM chunk: play it only. Never render it in the chat,
                            // and never record it in pendingMedia (so it isn't saved to history).
                            if (fileName.toLowerCase().endsWith('.pcm')) {
                                playPcmChunk(fileUrl, 24000, 1);
                                continue;
                            }

                            const fileData = {path: mediaPath, name: fileName, url: fileUrl, type: mimeType};
                            pendingMedia.push(`MEDIA:${mediaPath}`);

                            if (isTextFile(fileName)) {
                                // Code / text file: show the viewer card (kept outside the bubble,
                                // so it is not wiped when later text re-renders the bubble)
                                getFileGroup(streamingBubble).appendChild(renderFileCard(fileData));
                            } else {
                                // Media image: render into the immortal file-group sibling so it
                                // survives the next streamingBubble.innerHTML = renderMarkdown(...) .
                                // (Appending directly to streamingBubble gets wiped on the next chunk.)
                                const img = document.createElement('img');
                                img.className = 'streamed-media';
                                img.src = fileUrl;
                                img.alt = fileName;
                                getFileGroup(streamingBubble).appendChild(img);
                            }

                            // Auto-play WAV audio — do NOT render a download link in the bubble
                            if (fileName.toLowerCase().endsWith('.wav')) {
                                const audio = new Audio(fileUrl);
                                audio.play().catch(e => console.warn('Voice autoplay failed:', e));
                            }
                            if (thinkingIndicator.classList.contains('active')) {
                                thinkingIndicator.scrollIntoView({behavior: 'smooth', block: 'start'});
                            } else {
                                scrollToBottom();
                            }
                        } else if (data.activity !== undefined) {
                            const activity = data.activity;
                            currentActivity = activity;
                            if (activity === null) {
                                if (window._activityHideTimer) {
                                    clearTimeout(window._activityHideTimer);
                                }
                                window._activityHideTimer = setTimeout(() => {
                                    thinkingIndicator.classList.remove('active');
                                    window._activityHideTimer = null;
                                }, 1500);
                            } else {
                                if (window._activityHideTimer) {
                                    clearTimeout(window._activityHideTimer);
                                    window._activityHideTimer = null;
                                }
                                thinkingIndicator.classList.add('active');
                                const text = thinkingIndicator.querySelector('span:last-child');
                                // Tools like read_local_file finish in milliseconds, so the server
                                // flips back to "think" almost instantly. Hold a tool label on
                                // screen for a minimum time so it is actually readable.
                                const MIN_TOOL_LABEL_MS = 1200;
                                if (window._pendingThink) {
                                    clearTimeout(window._pendingThink);
                                    window._pendingThink = null;
                                }
                                if (activity === 'think' && window._toolShownAt) {
                                    const remaining = MIN_TOOL_LABEL_MS - (Date.now() - window._toolShownAt);
                                    if (remaining > 0) {
                                        window._pendingThink = setTimeout(() => {
                                            window._pendingThink = null;
                                            window._toolShownAt = 0;
                                            if (text && currentActivity === 'think') text.textContent = 'Thinking...';
                                        }, remaining);
                                        continue;
                                    }
                                }
                                window._toolShownAt = (activity !== 'think') ? Date.now() : 0;
                                if (text) {
                                    const labels = {
                                        'think': 'Thinking...',
                                        'search': 'Searching...',
                                        'read': 'Reading...',
                                        'write': 'Writing...',
                                        'edit': 'Writing...', // kept for backward compat with old 'edit:' prefix
                                        'list': 'Listing...',
                                        'run': 'Running...',
                                        'send': 'Sending...',
                                        'analyze': 'Analyzing...',
                                        'speak': 'Speaking...',
                                        'load': 'Loading model...',
                                        'remember': 'Remembering...',
                                        'recall': 'Recalling...',
                                        'forget': 'Forgetting...',
                                        'history': 'Searching past chats...',
                                        'approval': 'Waiting for your OK...',
                                    };
                                    if (activity.includes(':')) {
                                        const splitIdx = activity.indexOf(':');
                                        const prefix = activity.substring(0, splitIdx);
                                        const path = activity.substring(splitIdx + 1);
                                        text.textContent = `${labels[prefix] || prefix}: ${path}`;
                                    } else {
                                        text.textContent = labels[activity] || activity;
                                    }
                                }
                            }
                        }
                    } catch (e) {
                        // Ignore parse errors
                    }
                }
            }
        }

        // Highlight code blocks once, now that the reply is complete (doing it on every
        // streamed chunk would re-colour the growing block hundreds of times).
        enhanceCodeBlocks(streamingBubble);

        if (window.deskAfterTurn) deskAfterTurn(streamingBubble, deskOpts, fullResponse);
        const sess = sessions[sendSid] || (sessions[sendSid] = {title: 'New Session', message_count: 0, messages: [], updated_at: now});
        sess.messages = sess.messages || [];
        sess.messages.push({role: 'user', content: message, timestamp: now});
        const assistantResponse = fullResponse + (pendingMedia.length > 0 ? '\n' + pendingMedia.join('\n') : '');
        sess.messages.push({role: 'assistant', content: assistantResponse, timestamp: now});
        sess.updated_at = now;
        renderHistory();

    } catch (error) {
        if (error && error.name === 'AbortError') {
            // Stopped by the user: keep whatever was already streamed and mark it.
            const note = '\n\n*(stopped)*';
            streamingBubble.innerHTML = renderMarkdown((fullResponse || '') + note);
            enhanceCodeBlocks(streamingBubble);
        } else {
            console.error('Error:', error);
            streamingBubble.textContent = `Error: ${error.message || 'Could not connect to Lucy Core.'}`;
        }
    } finally {
        clearTimeout(stallTimer);
        currentAbort = null;
        setSending(false);
        messageInput.focus();
        // The session is listed by the server once it has a message; titles are generated a moment later.
        loadSessions();
        [3000, 9000].forEach(ms => setTimeout(() => {
            if (sessions[sendSid] && sessions[sendSid].title === 'New Session') loadSessions();
        }, ms));
        // Send the next queued message, if any (also how "Send now" interrupts).
        setTimeout(processQueue, 0);
    }
}

async function readFileAsDataURL(file) {
    return new Promise((resolve) => {
        const reader = new FileReader();
        reader.onload = (e) => {
            file._dataUrl = e.target.result;
            resolve();
        };
        reader.readAsDataURL(file);
    });
}

// ---- Autosave (fires on every field change, no Save button needed) ----
const AUTOSAVE_FIELDS = [
    {id: 'themeSelect', key: 'theme'},
    {id: 'fontSelect', key: 'font'},
    {id: 'fontSizeSelect', key: 'font_size'},
    {id: 'languageSelect', key: 'language'},
    {id: 'providerSelect', key: 'provider'},
    {id: 'modelSelect', key: 'model'},
    {id: 'temperatureInput', key: 'temperature'},
    {id: 'reasoningSelect', key: 'reasoning'},
    {id: 'kvCacheSelect', key: 'kv_cache'},
    {id: 'ttsSelect', key: 'voice'},
];

function bindAutosave() {
    AUTOSAVE_FIELDS.forEach(({id, key}) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.addEventListener('change', () => saveSetting(key, el.value));
    });
}

// ---- Theme (Light / Dark) ----
// Dark = cyan neon on deep navy. Light = azure on near-white. The choice is
// remembered in this browser (localStorage); an inline script in chat.html applies it
// before the page paints so there is no flash of the wrong theme.
const THEME_KEY = 'lucy.theme';
const THEME_BAR_COLOR = {dark: '#020a18', light: '#f4f8ff'};   // browser/phone address bar

function getSavedTheme() {
    try {
        const t = localStorage.getItem(THEME_KEY);
        return (t === 'light' || t === 'dark') ? t : 'dark';
    } catch (e) {
        return 'dark';
    }
}

function applyTheme(theme, persist = false) {
    if (theme !== 'light' && theme !== 'dark') theme = 'dark';
    document.documentElement.setAttribute('data-theme', theme);
    const bar = document.querySelector('meta[name="theme-color"]');
    if (bar) bar.setAttribute('content', THEME_BAR_COLOR[theme]);
    const select = document.getElementById('themeSelect');
    if (select && select.value !== theme) select.value = theme;
    if (persist) {
        try { localStorage.setItem(THEME_KEY, theme); } catch (e) { /* private mode: still applies this session */ }
    }
}

function saveSetting(key, value) {
    if (key === 'theme') {
        applyTheme(value, true);
    } else if (key === 'model' || key === 'temperature' || key === 'reasoning' || key === 'kv_cache') {
        saveLlmSetting(key, value);
        return;                       // flashSaved() is called once the server confirms
    } else {
        // Other fields (provider, voice, ...) are still placeholders
        console.log('Autosave (placeholder):', key, '=', value);
    }
    flashSaved();
}

// ---- Settings > Model (Lucy 12B / 4B, temperature, reasoning, KV cache) ----
let _llmPollTimer = null;

function _applyLlmStatus(st) {
    const sel = document.getElementById('modelSelect');
    if (sel) {
        sel.innerHTML = st.models.map(m =>
            `<option value="${m.id}">${m.label} (port ${m.port})</option>`).join('');
        sel.value = st.active;
    }
    const temp = document.getElementById('temperatureInput');
    if (temp && document.activeElement !== temp) temp.value = Number(st.temperature).toFixed(2);
    const presets = document.getElementById('temperaturePreset');
    if (presets) {
        presets.textContent = 'Preset: ' + st.models.map(m =>
            `${m.label.replace('Lucy ', '')} ${m.temperature_preset}`).join(' / ') +
            '. Choosing a model resets it to its preset.';
    }
    const r = document.getElementById('reasoningSelect');
    if (r) r.value = st.reasoning ? 'on' : 'off';
    const kv = document.getElementById('kvCacheSelect');
    if (kv) kv.value = st.kv_cache.startsWith('q4') ? 'q4' : 'q8';

    const label = document.getElementById('llmStatus');
    if (label) {
        const active = st.models.find(m => m.id === st.active);
        label.className = 'llm-status';
        if (st.phase === 'loading' || st.phase === 'stopping') {
            label.textContent = st.message || 'Loading...';
            label.classList.add('loading');
        } else if (st.phase === 'error') {
            label.textContent = 'Error: ' + st.message;
            label.classList.add('error');
        } else if (st.phase === 'sleeping') {
            label.textContent = st.message || 'Unloaded (idle). It loads again when you send a message.';
            label.classList.add('loading');
        } else if (active && active.ready) {
            label.textContent = `${active.label} is loaded on port ${active.port}` +
                (st.fallback_note ? ` - ${st.fallback_note}` : '');
            label.classList.add('ready');
        } else {
            label.textContent = `${active ? active.label : 'Model'} is not running`;
            label.classList.add('error');
        }
    }
    // keep polling while a load is in progress
    clearTimeout(_llmPollTimer);
    if (st.phase === 'loading' || st.phase === 'stopping') {
        _llmPollTimer = setTimeout(loadLlmStatus, 1500);
    }
}

async function loadLlmStatus() {
    try {
        const res = await fetch(`/api/llm/status?api_key=${API_KEY}`);
        if (res.ok) _applyLlmStatus(await res.json());
    } catch (e) {
        console.error('Failed to load LLM status:', e);
    }
}

async function saveLlmSetting(key, value) {
    let v = value;
    if (key === 'temperature') v = parseFloat(value);
    if (key === 'reasoning') v = (value === 'on');
    try {
        const res = await fetch(`/api/llm/settings?api_key=${API_KEY}`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({[key]: v})
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            alert(err.detail || 'Could not save setting');
            loadLlmStatus();
            return;
        }
        _applyLlmStatus(await res.json());
        flashSaved();
    } catch (e) {
        console.error('Failed to save LLM setting:', e);
    }
}

function flashSaved() {
    const indicator = document.getElementById('settingsSavedIndicator');
    if (!indicator) return;
    indicator.classList.add('visible');
    clearTimeout(window._savedIndicatorTimer);
    window._savedIndicatorTimer = setTimeout(() => {
        indicator.classList.remove('visible');
    }, 1200);
}

function openSettings() {
    settingsOverlay.style.display = 'flex';
    loadLlmStatus();
    loadSkills();
    loadConnectors();
    loadMcp();
    loadSafety();
}

function closeSettings() {
    settingsOverlay.style.display = 'none';
}

// ---- Settings Tabs ----
function switchSettingsTab(tab) {
    if (tab === 'persona') loadPersona();
    if (tab === 'evolve') loadEvolve();
    if (tab === 'overview') deskLoadOverview();
    if (tab === 'tasks') startTasksPolling(); else stopTasksPolling();
    document.querySelectorAll('.settings-tab-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === tab);
    });
    document.querySelectorAll('.settings-panel').forEach(panel => {
        panel.classList.toggle('active', panel.id === `panel-${tab}`);
    });
}

// ---- Skills (placeholder — hook up to skills.sqlite via backend later) ----
const skillsTableBody = document.getElementById('skillsTableBody');

async function loadSkills() {
    try {
        const resp = await fetch(`/api/skills?api_key=${API_KEY}`);
        const data = await resp.json();
        renderSkills(data.skills);
    } catch (e) {
        console.error('Failed to load skills:', e);
    }
}

function renderSkills(skills) {
    if (!skillsTableBody) return;
    if (!skills || skills.length === 0) {
        skillsTableBody.innerHTML = '<tr><td colspan="2" class="settings-table-empty">No skills loaded yet</td></tr>';
        return;
    }
    skillsTableBody.innerHTML = skills.map(s => {
        const status = s.source || 'unknown';
        const statusClass = status === 'learned' ? 'status-learned' : 'status-imported';
        return `
            <tr data-name="${(s.name || '').toLowerCase()}">
                <td>${s.name || ''}</td>
                <td><span class="skill-status ${statusClass}">${status}</span></td>
            </tr>
        `;
    }).join('');
}

function filterSkills() {
    const q = document.getElementById('skillSearchInput').value.trim().toLowerCase();
    skillsTableBody.querySelectorAll('tr[data-name]').forEach(row => {
        row.style.display = row.dataset.name.includes(q) ? '' : 'none';
    });
}

function addSkill() {
    const query = document.getElementById('skillSearchInput').value.trim();
    // TODO: POST to backend to add/install a skill (e.g. /api/skills)
    console.log('Add skill requested:', query);
}

function toggleSkill(id) {
    // TODO: POST to backend to enable/disable a skill
    console.log('Toggle skill:', id);
}

function removeSkill(id) {
    // TODO: DELETE to backend to remove a skill
    console.log('Remove skill:', id);
}

// ---- Connectors (placeholder — tokens/config come from backend later) ----
const connectorsTableBody = document.getElementById('connectorsTableBody');

async function loadConnectors() {
    try {
        const resp = await fetch(`/api/connectors?api_key=${API_KEY}`);
        const data = await resp.json();
        renderConnectors(data.connectors);
    } catch (e) {
        console.error('Failed to load connectors:', e);
        connectorsTableBody.innerHTML = '<tr><td colspan="2" class="settings-table-empty">Failed to load connectors</td></tr>';
    }
}

function renderConnectors(connectors) {
    if (!connectorsTableBody) return;
    if (!connectors || connectors.length === 0) {
        connectorsTableBody.innerHTML = '<tr><td colspan="2" class="settings-table-empty">No connectors configured yet</td></tr>';
        return;
    }
    connectorsTableBody.innerHTML = connectors.map(c => {
        const masked = c.masked_token || 'Not set';
        return `
            <tr data-name="${(c.name || '').toLowerCase()}">
                <td>${c.name || ''}</td>
                <td class="settings-token">${masked}</td>
                <td class="settings-row-actions">
                    <button class="settings-row-btn" onclick="editConnector('${c.id}')">Edit</button>
                    <button class="settings-row-btn" onclick="removeConnector('${c.id}')">Remove</button>
                </td>
            </tr>
        `;
    }).join('');
}

function filterConnectors() {
    const q = document.getElementById('connectorSearchInput').value.trim().toLowerCase();
    connectorsTableBody.querySelectorAll('tr[data-name]').forEach(row => {
        row.style.display = row.dataset.name.includes(q) ? '' : 'none';
    });
}

function addConnector() {
    const name = document.getElementById('connectorSearchInput').value.trim();
    if (!name) {
        alert('Please enter a connector name');
        return;
    }
    openTokenModal(name, null);
}

function openTokenModal(name, id) {
    const modal = document.getElementById('tokenModal');
    const modalTitle = document.getElementById('tokenModalName');
    const modalInput = document.getElementById('tokenModalInput');
    const modalId = document.getElementById('tokenModalId');
    const modalName = document.getElementById('tokenModalConnectorName');
    modalTitle.textContent = id ? `Edit "${name}"` : `Add "${name}"`;
    modalId.value = id || '';
    modalName.value = name;
    modalInput.value = '';
    modal.style.display = 'flex';
    modalInput.focus();
    modalInput.onkeyup = (e) => {
        if (e.key === 'Enter') saveToken();
    };
}

function closeTokenModal() {
    document.getElementById('tokenModal').style.display = 'none';
}

async function saveToken() {
    const id = document.getElementById('tokenModalId').value;
    const name = document.getElementById('tokenModalConnectorName').value;
    const token = document.getElementById('tokenModalInput').value.trim();
    const endpoint = id ? `/api/connectors/${id}?api_key=${API_KEY}` : `/api/connectors?api_key=${API_KEY}`;
    const method = id ? 'PATCH' : 'POST';
    try {
        await fetch(endpoint, {
            method: method,
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(id ? {token} : {name, token})
        });
        closeTokenModal();
        loadConnectors();
    } catch (e) {
        console.error('Failed to save connector:', e);
    }
}

async function editConnector(id) {
    // Fetch the connector to get its name
    const resp = await fetch(`/api/connectors?api_key=${API_KEY}`);
    const data = await resp.json();
    const connector = data.connectors.find(c => c.id == id);
    if (connector) {
        openTokenModal(connector.name, id);
    }
}

async function removeConnector(id) {
    if (!confirm('Remove this connector?')) return;
    await fetch(`/api/connectors/${id}?api_key=${API_KEY}`, {method: 'DELETE'});
    loadConnectors();
}

async function restartServer() {
    if (!confirm('Restart Lucy Core server?')) return;
    try {
        const res = await fetch(`/api/server/restart?api_key=${API_KEY}`, {method: 'POST'});
        const data = await res.json();
        setStatus(data.status || 'restarting', 'var(--warn)');
        setTimeout(() => {
            window.location.reload();
        }, 1500);
    } catch (e) {
        console.error('Restart failed:', e);
    }
}

async function stopServer() {
    if (!confirm('Stop Lucy Core server?')) return;
    try {
        // The server kills its own process, so the response may never arrive —
        // a dropped connection here is expected, not an error.
        try { await fetch(`/api/server/stop?api_key=${API_KEY}`, {method: 'POST'}); } catch (_) {}
        setStatus('Server stopped', 'var(--danger)');
        const stopBtn = document.getElementById('stopBtn');
        if (stopBtn) stopBtn.classList.add('stop');
        setTimeout(() => {
            window.location.reload();
        }, 2000);
    } catch (e) {
        console.error('Stop failed:', e);
    }
}

// ---- Logs ----
let _logsTimer = null;

function openLogsModal() {
    document.getElementById('logsModal').style.display = 'flex';
    fetchLogs();
    if (_logsTimer) clearInterval(_logsTimer);
    _logsTimer = setInterval(() => {
        const auto = document.getElementById('logsAutoRefresh');
        if (auto && auto.checked) fetchLogs();
    }, 3000);
}

function closeLogsModal() {
    document.getElementById('logsModal').style.display = 'none';
    if (_logsTimer) { clearInterval(_logsTimer); _logsTimer = null; }
}

async function fetchLogs() {
    const box = document.getElementById('logsContent');
    if (!box) return;
    try {
        const res = await fetch(`/api/logs?lines=300&api_key=${API_KEY}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const lines = data.logs || [];
        // Only auto-scroll if the user was already at the bottom
        const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
        box.textContent = lines.length ? lines.join('\n') : '(log is empty)';
        if (atBottom || box.dataset.first !== '1') {
            box.scrollTop = box.scrollHeight;
            box.dataset.first = '1';
        }
    } catch (e) {
        box.textContent = `Could not load logs: ${e.message}`;
    }
}

// ---- Voice (Stelnet Audio Server) ----
const voiceToggle = document.getElementById('voiceToggle');
const voiceToggleRow = document.getElementById('voiceToggleRow');
let voiceBusy = false;

async function toggleVoice(wantOn) {
    if (voiceBusy) return;
    voiceBusy = true;
    voiceToggle.disabled = true;
    voiceToggleRow.classList.add('voice-pending');
    try {
        const endpoint = wantOn ? '/api/voice/start' : '/api/voice/stop';
        const res = await fetch(`${endpoint}?api_key=${API_KEY}`, {method: 'POST'});
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json().catch(() => ({}));
        if (data.error) throw new Error(data.error);
        const running = typeof data.running === 'boolean' ? data.running : wantOn;
        applyVoiceState(running);
    } catch (e) {
        console.error('Voice toggle failed:', e);
        applyVoiceState(!wantOn); // revert the switch on failure
        alert(`Failed to ${wantOn ? 'start' : 'stop'} the voice server.\n${e.message || ''}`);
    } finally {
        voiceBusy = false;
        voiceToggle.disabled = false;
        voiceToggleRow.classList.remove('voice-pending');
    }
}

function applyVoiceState(running) {
    voiceToggle.checked = running;
    voiceToggleRow.classList.toggle('voice-active', running);
}

async function loadVoiceStatus() {
    try {
        const res = await fetch(`/api/voice/status?api_key=${API_KEY}`);
        const data = await res.json();
        applyVoiceState(!!data.running);
    } catch (e) {
        console.error('Failed to load voice status:', e);
    }
}

function setStatus(text, color) {
    const dot = document.getElementById('statusDot');
    const label = document.getElementById('statusText');
    if (color) {dot.style.background = color; dot.style.boxShadow = `0 0 8px ${color}`;};
    if (text) label.textContent = text;
}

// ---- File viewer: cards for code/text files sent through the chat ----
// When Lucy sends a code or text file, it shows as a card (name, language, size, a short
// highlighted preview) with Copy / Download / Open. "Open" shows the whole file in a viewer.
// Code blocks in messages get the same highlighting and a Copy button.
const FILE_TYPES = {
    py: {lang: 'python', label: 'Python'}, pyw: {lang: 'python', label: 'Python'},
    js: {lang: 'javascript', label: 'JavaScript'}, mjs: {lang: 'javascript', label: 'JavaScript'},
    cjs: {lang: 'javascript', label: 'JavaScript'}, jsx: {lang: 'javascript', label: 'JSX'},
    ts: {lang: 'typescript', label: 'TypeScript'}, tsx: {lang: 'typescript', label: 'TSX'},
    json: {lang: 'json', label: 'JSON'}, jsonl: {lang: 'json', label: 'JSON Lines'},
    md: {lang: 'markdown', label: 'Markdown'}, markdown: {lang: 'markdown', label: 'Markdown'},
    txt: {lang: 'plaintext', label: 'Text'}, log: {lang: 'plaintext', label: 'Log'}, csv: {lang: 'plaintext', label: 'CSV'},
    html: {lang: 'xml', label: 'HTML'}, htm: {lang: 'xml', label: 'HTML'}, xml: {lang: 'xml', label: 'XML'},
    css: {lang: 'css', label: 'CSS'}, scss: {lang: 'scss', label: 'SCSS'}, less: {lang: 'less', label: 'Less'},
    yaml: {lang: 'yaml', label: 'YAML'}, yml: {lang: 'yaml', label: 'YAML'},
    toml: {lang: 'ini', label: 'TOML'}, ini: {lang: 'ini', label: 'INI'}, cfg: {lang: 'ini', label: 'Config'},
    conf: {lang: 'ini', label: 'Config'}, env: {lang: 'ini', label: 'Env'},
    sh: {lang: 'bash', label: 'Shell'}, bash: {lang: 'bash', label: 'Shell'}, zsh: {lang: 'bash', label: 'Shell'},
    bat: {lang: 'dos', label: 'Batch'}, cmd: {lang: 'dos', label: 'Batch'},
    ps1: {lang: 'powershell', label: 'PowerShell'}, psm1: {lang: 'powershell', label: 'PowerShell'},
    sql: {lang: 'sql', label: 'SQL'},
    c: {lang: 'c', label: 'C'}, h: {lang: 'c', label: 'C header'},
    cpp: {lang: 'cpp', label: 'C++'}, cc: {lang: 'cpp', label: 'C++'}, cxx: {lang: 'cpp', label: 'C++'}, hpp: {lang: 'cpp', label: 'C++'},
    cs: {lang: 'csharp', label: 'C#'}, java: {lang: 'java', label: 'Java'}, kt: {lang: 'kotlin', label: 'Kotlin'},
    go: {lang: 'go', label: 'Go'}, rs: {lang: 'rust', label: 'Rust'}, php: {lang: 'php', label: 'PHP'},
    rb: {lang: 'ruby', label: 'Ruby'}, lua: {lang: 'lua', label: 'Lua'}, swift: {lang: 'swift', label: 'Swift'},
    r: {lang: 'r', label: 'R'}, pl: {lang: 'perl', label: 'Perl'},
    diff: {lang: 'diff', label: 'Diff'}, patch: {lang: 'diff', label: 'Patch'},
};
const FILE_NAMES = {
    'dockerfile': {lang: 'plaintext', label: 'Dockerfile'},
    'makefile': {lang: 'makefile', label: 'Makefile'},
    '.gitignore': {lang: 'plaintext', label: 'Git ignore'},
    '.env': {lang: 'ini', label: 'Env'},
};
// language names people put after ``` in markdown -> highlight.js language
const CODE_FENCE_ALIASES = {
    py: 'python', python3: 'python', js: 'javascript', node: 'javascript', ts: 'typescript', sh: 'bash', zsh: 'bash',
    shell: 'bash', console: 'bash', bat: 'dos', batch: 'dos', cmd: 'dos', ps1: 'powershell', pwsh: 'powershell',
    html: 'xml', svg: 'xml', yml: 'yaml', toml: 'ini', md: 'markdown', cs: 'csharp', 'c++': 'cpp', rs: 'rust', rb: 'ruby',
    jsonc: 'json', text: 'plaintext', txt: 'plaintext',
};
const TEXT_PREVIEW_LIMIT = 512 * 1024;   // most bytes read from the server for one file
const HIGHLIGHT_LIMIT = 200000;          // characters; bigger files are shown without colours
const CARD_PREVIEW_LINES = 12;

function fileTypeInfo(name) {
    const base = String(name || '').split(/[\\/]/).pop().toLowerCase();
    if (FILE_NAMES[base]) return FILE_NAMES[base];
    const dot = base.lastIndexOf('.');
    return dot >= 0 ? (FILE_TYPES[base.slice(dot + 1)] || null) : null;
}

function isTextFile(name) { return !!fileTypeInfo(name); }

function fileBadge(name) {
    const base = String(name || '').split(/[\\/]/).pop();
    const dot = base.lastIndexOf('.');
    const ext = dot > 0 ? base.slice(dot + 1) : '';
    return (ext || (fileTypeInfo(name) || {label: 'TXT'}).label).slice(0, 4).toUpperCase();
}

// Coloured HTML for some code; plain escaped text if the highlighter is missing or the code is huge.
function highlightCode(code, lang) {
    try {
        if (typeof hljs !== 'undefined' && lang && code.length <= HIGHLIGHT_LIMIT && hljs.getLanguage(lang)) {
            return hljs.highlight(code, {language: lang, ignoreIllegals: true}).value;
        }
    } catch (e) { /* fall through to plain text */ }
    return escapeHtml(code);
}

async function copyText(text) {
    try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(text);
            return true;
        }
    } catch (e) { /* e.g. not a secure context: use the fallback below */ }
    try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.cssText = 'position:fixed;top:-1000px;opacity:0';
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand('copy');
        ta.remove();
        return ok;
    } catch (e) {
        return false;
    }
}

function flashButton(btn, text, ms = 1400) {
    const original = btn.dataset.label || btn.textContent;
    btn.dataset.label = original;
    btn.textContent = text;
    clearTimeout(btn._flash);
    btn._flash = setTimeout(() => { btn.textContent = original; }, ms);
}

// Read at most `limit` bytes of a text file from the server.
async function fetchTextFile(url, limit = TEXT_PREVIEW_LIMIT) {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const total = parseInt((resp.headers && resp.headers.get && resp.headers.get('Content-Length')) || '', 10) || null;
    const chunks = [];
    let received = 0;
    if (resp.body && resp.body.getReader) {
        const reader = resp.body.getReader();
        while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            chunks.push(value);
            received += value.length;
            if (received > limit) { try { await reader.cancel(); } catch (e) {} break; }
        }
    } else {
        const buf = new Uint8Array(await resp.arrayBuffer());
        chunks.push(buf);
        received = buf.length;
    }
    const merged = new Uint8Array(Math.min(received, limit));
    let offset = 0;
    for (const c of chunks) {
        if (offset >= merged.length) break;
        const part = c.length > merged.length - offset ? c.subarray(0, merged.length - offset) : c;
        merged.set(part, offset);
        offset += part.length;
    }
    const truncated = received > limit || (total !== null && total > limit);
    const binary = merged.subarray(0, 8000).includes(0);
    let text = binary ? '' : new TextDecoder('utf-8').decode(merged);
    if (text.charCodeAt(0) === 0xFEFF) text = text.slice(1);
    return {text, bytes: total !== null ? total : received, truncated, binary};
}

function splitLines(text) {
    const lines = text.replace(/\r\n?/g, '\n').split('\n');
    if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop();   // trailing newline is not a line
    return lines;
}

// Line-number gutter + highlighted code, side by side (no wrapping, so rows stay aligned).
function buildCodeView(lines, lang) {
    const view = document.createElement('div');
    view.className = 'code-view';
    const gutter = document.createElement('pre');
    gutter.className = 'code-gutter';
    gutter.setAttribute('aria-hidden', 'true');
    gutter.textContent = lines.map((_, i) => i + 1).join('\n');
    const pre = document.createElement('pre');
    pre.className = 'code-text';
    const code = document.createElement('code');
    code.className = 'hljs';
    code.innerHTML = highlightCode(lines.join('\n'), lang);
    pre.appendChild(code);
    view.appendChild(gutter);
    view.appendChild(pre);
    return view;
}

function fileMetaText(file) {
    const parts = [file.info.label];
    if (file.truncated) {
        parts.push(`first ${formatFileSize(TEXT_PREVIEW_LIMIT)} of ${formatFileSize(file.bytes)}`);
    } else {
        parts.push(`${file.lines.length} line${file.lines.length === 1 ? '' : 's'}`);
        parts.push(formatFileSize(file.bytes));
    }
    return parts.join(' \u00B7 ');
}

function makeFcButton(label, title, act) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'fc-btn';
    b.textContent = label;
    b.title = title;
    b.dataset.act = act;
    return b;
}

function renderFileCard(fileData) {
    const name = fileData.name || String(fileData.path || 'file').split(/[\\/]/).pop();
    const info = fileTypeInfo(name) || {lang: 'plaintext', label: 'Text'};

    const card = document.createElement('div');
    card.className = 'file-card';
    card.dataset.state = 'loading';

    const head = document.createElement('div');
    head.className = 'file-card-head';
    const badge = document.createElement('span');
    badge.className = 'file-card-icon';
    badge.textContent = fileBadge(name);
    const title = document.createElement('div');
    title.className = 'file-card-title';
    const nameEl = document.createElement('span');
    nameEl.className = 'file-card-name';
    nameEl.textContent = name;
    const metaEl = document.createElement('span');
    metaEl.className = 'file-card-meta';
    metaEl.textContent = `${info.label} \u00B7 loading\u2026`;
    title.appendChild(nameEl);
    title.appendChild(metaEl);

    const actions = document.createElement('div');
    actions.className = 'file-card-actions';
    const copyBtn = makeFcButton('Copy', 'Copy the whole file', 'copy');
    copyBtn.disabled = true;
    const dl = document.createElement('a');
    dl.className = 'fc-btn';
    dl.dataset.act = 'download';
    dl.textContent = 'Download';
    dl.title = 'Download the file';
    dl.href = fileData.url;
    dl.download = name;
    const openBtn = makeFcButton('Open', 'Open in the viewer', 'open');
    openBtn.disabled = true;
    actions.appendChild(copyBtn);
    actions.appendChild(dl);
    actions.appendChild(openBtn);

    head.appendChild(badge);
    head.appendChild(title);
    head.appendChild(actions);

    const body = document.createElement('div');
    body.className = 'file-card-body';
    body.innerHTML = '<div class="file-card-note">Loading preview\u2026</div>';
    card.appendChild(head);
    card.appendChild(body);

    let loaded = null;
    const openIt = () => { if (loaded) openFileViewer(loaded); };
    head.addEventListener('click', (e) => { if (!e.target.closest('.fc-btn')) openIt(); });
    body.addEventListener('click', openIt);
    openBtn.addEventListener('click', openIt);
    copyBtn.addEventListener('click', async () => {
        if (!loaded) return;
        flashButton(copyBtn, (await copyText(loaded.text)) ? 'Copied \u2713' : 'Copy failed');
    });

    fetchTextFile(fileData.url).then(res => {
        if (res.binary) {
            card.dataset.state = 'binary';
            metaEl.textContent = `${info.label} \u00B7 binary file`;
            body.innerHTML = '<div class="file-card-note">This looks like a binary file, so there is no preview. Use Download.</div>';
            return;
        }
        const lines = splitLines(res.text);
        loaded = {name, info, text: res.text, lines, truncated: res.truncated, bytes: res.bytes, url: fileData.url};
        metaEl.textContent = fileMetaText(loaded);
        body.innerHTML = '';
        if (res.text.length === 0) {
            body.innerHTML = '<div class="file-card-note">(empty file)</div>';
        } else {
            body.appendChild(buildCodeView(lines.slice(0, CARD_PREVIEW_LINES), info.lang));
            if (lines.length > CARD_PREVIEW_LINES || res.truncated) {
                const more = document.createElement('div');
                more.className = 'file-card-more';
                more.textContent = lines.length > CARD_PREVIEW_LINES
                    ? `Show all ${res.truncated ? 'of the loaded ' : ''}${lines.length} lines`
                    : 'Open to see more';
                body.appendChild(more);
            }
        }
        card.dataset.state = 'ready';
        copyBtn.disabled = res.truncated;   // a partial copy would be misleading
        if (res.truncated) copyBtn.title = 'File too large to copy here. Use Download.';
        openBtn.disabled = false;
    }).catch(err => {
        card.dataset.state = 'error';
        metaEl.textContent = `${info.label} \u00B7 preview unavailable`;
        body.innerHTML = '';
        const note = document.createElement('div');
        note.className = 'file-card-note';
        note.textContent = `Could not load the file (${err.message || 'error'}). You can still try Download.`;
        body.appendChild(note);
    });

    return card;
}

// ---- The preview modal (opened by tapping an image or a file in the chat) ----
// One modal in the same style as the Logs modal, with three modes:
//   image : zoom (buttons, wheel, pinch, double-tap), drag to pan, download
//   text  : line numbers + syntax colours for text and code files, copy, download
//   other : file details and a Download button (no preview exists for that kind of file)
let fileViewerEl = null;
let fvLastFocus = null;
const fvImg = {scale: 1, fit: 1, tx: 0, ty: 0, nw: 0, nh: 0, isFit: true,
               pointers: new Map(), pinch: null, downAt: null, moved: false, lastTap: 0, lastTapX: 0, lastTapY: 0};
const FV_MIN_FACTOR = 0.5;   // may zoom out to half of "fit"
const FV_MAX_SCALE = 16;     // 1600%

function ensureFileViewer() {
    if (fileViewerEl) return fileViewerEl;
    const overlay = document.createElement('div');
    overlay.className = 'fv-overlay';
    overlay.id = 'fileViewer';
    overlay.innerHTML =
        '<div class="token-modal fv-modal" role="dialog" aria-modal="true" aria-label="Preview">' +
            '<div class="token-modal-header fv-header">' +
                '<span class="file-card-icon fv-badge"></span>' +
                '<div class="fv-titlebox"><span class="token-modal-title fv-name"></span><span class="fv-meta"></span></div>' +
                '<button type="button" class="token-modal-close" data-act="close" aria-label="Close" title="Close (Esc)">\u00d7</button>' +
            '</div>' +
            '<div class="fv-body"></div>' +
            '<div class="token-modal-footer fv-footer">' +
                '<div class="fv-zoom" role="group" aria-label="Zoom">' +
                    '<button type="button" class="token-modal-btn fv-zoom-btn" data-act="zoom-out" title="Zoom out (-)" aria-label="Zoom out">\u2212</button>' +
                    '<button type="button" class="token-modal-btn fv-zoom-readout" data-act="zoom-toggle" title="Switch between fit and 100%">100%</button>' +
                    '<button type="button" class="token-modal-btn fv-zoom-btn" data-act="zoom-in" title="Zoom in (+)" aria-label="Zoom in">+</button>' +
                    '<button type="button" class="token-modal-btn" data-act="zoom-fit" title="Fit to window (0)">Fit</button>' +
                '</div>' +
                '<button type="button" class="token-modal-btn" data-act="copy" title="Copy the whole file">Copy</button>' +
                '<a class="token-modal-btn token-modal-save fv-download" data-act="download" title="Download">Download</a>' +
                '<button type="button" class="token-modal-btn" data-act="close">Close</button>' +
            '</div>' +
        '</div>';
    overlay.addEventListener('click', (e) => { if (e.target === overlay) closeFileViewer(); });
    overlay.querySelectorAll('[data-act="close"]').forEach(b => b.addEventListener('click', closeFileViewer));
    const stageCenter = () => { const s = fvStageSize(); return {x: s.w / 2, y: s.h / 2}; };
    overlay.querySelector('[data-act="zoom-in"]').addEventListener('click', () => { const c = stageCenter(); fvZoomAt(1.25, c.x, c.y); });
    overlay.querySelector('[data-act="zoom-out"]').addEventListener('click', () => { const c = stageCenter(); fvZoomAt(0.8, c.x, c.y); });
    overlay.querySelector('[data-act="zoom-fit"]').addEventListener('click', fvFit);
    overlay.querySelector('[data-act="zoom-toggle"]').addEventListener('click', () => { const c = stageCenter(); fvToggleZoom(c.x, c.y); });
    document.body.appendChild(overlay);
    fileViewerEl = overlay;
    return overlay;
}

function isFileViewerOpen() { return !!fileViewerEl && fileViewerEl.style.display === 'flex'; }
function fvMode() { return fileViewerEl ? fileViewerEl.dataset.mode : ''; }

// Common set-up for every mode: title, badge, meta line, Download link, empty body.
function prepFileViewer(mode, info) {
    const el = ensureFileViewer();
    el.dataset.mode = mode;
    el.querySelector('.fv-badge').textContent = info.badge || 'FILE';
    const nameEl = el.querySelector('.fv-name');
    nameEl.textContent = info.name;
    nameEl.title = info.name;
    el.querySelector('.fv-meta').textContent = info.meta || '';
    const dl = el.querySelector('[data-act="download"]');
    if (info.url) {
        dl.href = info.url;
        dl.download = info.name;
        dl.style.display = '';
    } else {
        dl.removeAttribute('href');
        dl.style.display = 'none';
    }
    el.querySelector('.fv-body').innerHTML = '';
    return el;
}

function showFileViewer() {
    const el = fileViewerEl;
    if (el.style.display !== 'flex') fvLastFocus = document.activeElement;
    el.style.display = 'flex';
    el.querySelector('.token-modal-close').focus();
}

function closeFileViewer() {
    if (!fileViewerEl) return;
    fileViewerEl.style.display = 'none';
    fileViewerEl.querySelector('.fv-body').innerHTML = '';   // free the DOM of big files and images
    fvImg.pointers.clear();
    fvImg.pinch = null;
    fvImg.nw = fvImg.nh = 0;
    if (fvLastFocus && fvLastFocus.focus && document.contains(fvLastFocus)) { try { fvLastFocus.focus(); } catch (e) {} }
    fvLastFocus = null;
}

// ---- text / code ----
function openFileViewer(file) {
    const el = prepFileViewer('text', {name: file.name, badge: fileBadge(file.name), meta: fileMetaText(file), url: file.url});
    // fresh Copy button each time, so a previous file's handler can't linger
    const oldCopy = el.querySelector('[data-act="copy"]');
    const copyBtn = oldCopy.cloneNode(true);
    oldCopy.replaceWith(copyBtn);
    copyBtn.textContent = 'Copy';
    copyBtn.disabled = file.truncated;
    copyBtn.title = file.truncated ? 'File too large to copy here. Use Download.' : 'Copy the whole file';
    copyBtn.addEventListener('click', async () => {
        flashButton(copyBtn, (await copyText(file.text)) ? 'Copied \u2713' : 'Copy failed');
    });
    const body = el.querySelector('.fv-body');
    body.appendChild(buildCodeView(file.lines, file.info.lang));
    if (file.truncated) {
        const note = document.createElement('div');
        note.className = 'file-card-note';
        note.textContent = `Only the first ${formatFileSize(TEXT_PREVIEW_LIMIT)} is shown. Use Download for the whole file.`;
        body.appendChild(note);
    }
    showFileViewer();
    body.scrollTop = 0;
}

// ---- files with no preview ----
function openGenericFileViewer(file) {
    const name = file.name || String(file.path || 'file').split(/[\\/]/).pop();
    const dot = name.lastIndexOf('.');
    const badge = (dot > 0 ? name.slice(dot + 1) : 'FILE').slice(0, 4).toUpperCase();
    const meta = [file.type && file.type !== 'application/octet-stream' ? file.type : '', file.size ? formatFileSize(file.size) : '']
        .filter(Boolean).join(' \u00B7 ');
    const el = prepFileViewer('other', {name, badge, meta, url: file.url});
    const box = document.createElement('div');
    box.className = 'fv-other';
    const big = document.createElement('span');
    big.className = 'file-card-icon';
    big.textContent = badge;
    const nm = document.createElement('div');
    nm.className = 'fv-other-name';
    nm.textContent = name;
    const note = document.createElement('div');
    note.className = 'fv-other-note';
    note.textContent = file.url
        ? 'There is no preview for this kind of file. Use Download to open it on your device.'
        : 'There is no preview for this kind of file, and its copy is not available to download from here.';
    box.append(big, nm, note);
    el.querySelector('.fv-body').appendChild(box);
    showFileViewer();
}

// ---- images ----
function fvImageName(info) {
    let name = (info.name || '').split(/[\\/]/).pop();
    if (/\.[A-Za-z0-9]{2,5}$/.test(name)) return name;
    let ext = '';
    const m = /^data:image\/([a-z0-9.+-]+)/i.exec(info.url || '');
    if (m) ext = m[1].replace('jpeg', 'jpg').replace('svg+xml', 'svg');
    else {
        const p = /\.([A-Za-z0-9]{2,5})(?:[?#]|$)/.exec(decodeURIComponent((info.url || '').split('?')[0]));
        if (p) ext = p[1].toLowerCase();
    }
    return (name || 'image') + (ext ? '.' + ext : '.png');
}

function fvStageSize() {
    const st = fileViewerEl && fileViewerEl.querySelector('.fv-stage');
    return st ? {w: st.clientWidth, h: st.clientHeight} : {w: 0, h: 0};
}

function fvApply() {
    const img = fileViewerEl && fileViewerEl.querySelector('.fv-img');
    if (!img || !fvImg.nw) return;
    img.style.transform = `translate(${fvImg.tx}px, ${fvImg.ty}px) scale(${fvImg.scale})`;
    fileViewerEl.querySelector('.fv-zoom-readout').textContent = Math.round(fvImg.scale * 100) + '%';
}

// keep the picture on screen: centred when smaller than the window, edge-bound when larger
function fvClamp() {
    const {w, h} = fvStageSize();
    const iw = fvImg.nw * fvImg.scale, ih = fvImg.nh * fvImg.scale;
    fvImg.tx = iw <= w ? (w - iw) / 2 : Math.min(0, Math.max(w - iw, fvImg.tx));
    fvImg.ty = ih <= h ? (h - ih) / 2 : Math.min(0, Math.max(h - ih, fvImg.ty));
}

function fvFit() {
    const {w, h} = fvStageSize();
    if (!fvImg.nw || !w || !h) return;
    fvImg.fit = Math.min(w / fvImg.nw, h / fvImg.nh, 1);
    fvImg.scale = fvImg.fit;
    fvImg.isFit = true;
    fvClamp();
    fvApply();
}

// Zoom by a factor, keeping the point (px, py) of the stage fixed under the cursor / fingers.
function fvZoomAt(factor, px, py) {
    if (!fvImg.nw) return;
    const s = fvImg.scale;
    const ns = Math.min(FV_MAX_SCALE, Math.max(fvImg.fit * FV_MIN_FACTOR, s * factor));
    if (ns === s) return;
    fvImg.tx = px - (px - fvImg.tx) * (ns / s);
    fvImg.ty = py - (py - fvImg.ty) * (ns / s);
    fvImg.scale = ns;
    fvImg.isFit = Math.abs(ns - fvImg.fit) < 0.001;
    fvClamp();
    fvApply();
}

// Fit <-> zoomed in (100%, or 2x fit when the picture is already shown at 100%)
function fvToggleZoom(px, py) {
    if (!fvImg.isFit) { fvFit(); return; }
    const target = fvImg.fit < 0.999 ? 1 : fvImg.fit * 2;
    fvZoomAt(target / fvImg.scale, px, py);
}

function fvBindStage(stage) {
    const rel = (e) => { const r = stage.getBoundingClientRect(); return {x: e.clientX - r.left, y: e.clientY - r.top}; };
    const pinchState = () => {
        const [a, b] = [...fvImg.pointers.values()];
        const r = stage.getBoundingClientRect();
        return {dist: Math.hypot(a.x - b.x, a.y - b.y) || 1, scale: fvImg.scale, tx: fvImg.tx, ty: fvImg.ty,
                cx: (a.x + b.x) / 2 - r.left, cy: (a.y + b.y) / 2 - r.top};
    };

    stage.addEventListener('wheel', (e) => {
        e.preventDefault();
        const p = rel(e);
        fvZoomAt(Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015)), p.x, p.y);   // ctrl = trackpad pinch
    }, {passive: false});

    stage.addEventListener('pointerdown', (e) => {
        if (e.pointerType === 'mouse' && e.button !== 0) return;
        stage.setPointerCapture(e.pointerId);
        fvImg.pointers.set(e.pointerId, {x: e.clientX, y: e.clientY});
        if (fvImg.pointers.size === 1) { fvImg.downAt = {x: e.clientX, y: e.clientY}; fvImg.moved = false; }
        if (fvImg.pointers.size === 2) { fvImg.pinch = pinchState(); fvImg.moved = true; }
        stage.classList.add('dragging');
    });

    stage.addEventListener('pointermove', (e) => {
        const p = fvImg.pointers.get(e.pointerId);
        if (!p) return;
        if (fvImg.pointers.size === 2 && fvImg.pinch) {
            p.x = e.clientX; p.y = e.clientY;
            const [a, b] = [...fvImg.pointers.values()];
            const r = stage.getBoundingClientRect();
            const st = fvImg.pinch;
            const ns = Math.min(FV_MAX_SCALE, Math.max(fvImg.fit * FV_MIN_FACTOR, st.scale * (Math.hypot(a.x - b.x, a.y - b.y) / st.dist)));
            const cx = (a.x + b.x) / 2 - r.left, cy = (a.y + b.y) / 2 - r.top;
            fvImg.tx = cx - (st.cx - st.tx) * (ns / st.scale);
            fvImg.ty = cy - (st.cy - st.ty) * (ns / st.scale);
            fvImg.scale = ns;
            fvImg.isFit = Math.abs(ns - fvImg.fit) < 0.001;
            fvClamp(); fvApply();
            return;
        }
        fvImg.tx += e.clientX - p.x;
        fvImg.ty += e.clientY - p.y;
        p.x = e.clientX; p.y = e.clientY;
        if (fvImg.downAt && Math.hypot(e.clientX - fvImg.downAt.x, e.clientY - fvImg.downAt.y) > 6) fvImg.moved = true;
        fvClamp(); fvApply();
    });

    const release = (e) => {
        if (!fvImg.pointers.has(e.pointerId)) return;
        fvImg.pointers.delete(e.pointerId);
        if (fvImg.pointers.size < 2) fvImg.pinch = null;
        if (fvImg.pointers.size > 0) return;
        stage.classList.remove('dragging');
        if (e.type === 'pointerup' && !fvImg.moved) {            // a tap: two quick taps toggle the zoom
            const now = Date.now(), p = rel(e);
            if (now - fvImg.lastTap < 320 && Math.hypot(p.x - fvImg.lastTapX, p.y - fvImg.lastTapY) < 30) {
                fvImg.lastTap = 0;
                fvToggleZoom(p.x, p.y);
            } else {
                fvImg.lastTap = now; fvImg.lastTapX = p.x; fvImg.lastTapY = p.y;
            }
        }
    };
    stage.addEventListener('pointerup', release);
    stage.addEventListener('pointercancel', release);
}

function openImageViewer(info) {
    const url = info.url;
    if (!url) return;
    const name = fvImageName(info);
    const ext = (name.split('.').pop() || 'IMG').slice(0, 4).toUpperCase();
    const baseMeta = info.size ? formatFileSize(info.size) : '';
    const el = prepFileViewer('image', {name, badge: ext, meta: baseMeta || 'Loading\u2026', url});
    const body = el.querySelector('.fv-body');
    const stage = document.createElement('div');
    stage.className = 'fv-stage';
    const img = document.createElement('img');
    img.className = 'fv-img';
    img.alt = name;
    img.draggable = false;
    stage.appendChild(img);
    body.appendChild(stage);
    fvImg.pointers.clear(); fvImg.pinch = null; fvImg.nw = fvImg.nh = 0; fvImg.lastTap = 0;
    fvBindStage(stage);
    el.querySelector('.fv-zoom-readout').textContent = '\u2014';
    img.onload = () => {
        fvImg.nw = img.naturalWidth || 1;
        fvImg.nh = img.naturalHeight || 1;
        img.style.width = fvImg.nw + 'px';
        img.style.height = fvImg.nh + 'px';
        el.querySelector('.fv-meta').textContent = [`${fvImg.nw} \u00D7 ${fvImg.nh}`, baseMeta].filter(Boolean).join(' \u00B7 ');
        fvFit();
    };
    img.onerror = () => {
        body.innerHTML = '';
        const note = document.createElement('div');
        note.className = 'fv-other';
        note.innerHTML = '<div class="fv-other-note"></div>';
        note.firstChild.textContent = 'Could not load this image. You can still try Download.';
        body.appendChild(note);
        el.querySelector('.fv-meta').textContent = 'preview unavailable';
    };
    showFileViewer();       // show first, so the stage has a size when the image arrives
    img.src = url;
}

// Tapping any picture in the chat opens it in the viewer (stickers stay as they are).
chatContainer.addEventListener('click', (e) => {
    const img = e.target.closest ? e.target.closest('img') : null;
    if (!img || img.classList.contains('sticker-img') || !chatContainer.contains(img)) return;
    e.preventDefault();
    openImageViewer({url: img.currentSrc || img.src, name: img.getAttribute('alt') || ''});
});

document.addEventListener('keydown', (e) => {
    if (!isFileViewerOpen() || fvMode() !== 'image' || e.ctrlKey || e.metaKey || e.altKey) return;
    const {w, h} = fvStageSize();
    if (e.key === '+' || e.key === '=') { e.preventDefault(); fvZoomAt(1.25, w / 2, h / 2); }
    else if (e.key === '-' || e.key === '_') { e.preventDefault(); fvZoomAt(0.8, w / 2, h / 2); }
    else if (e.key === '0') { e.preventDefault(); fvFit(); }
    else if (e.key === '1') { e.preventDefault(); fvZoomAt(1 / fvImg.scale, w / 2, h / 2); }
});

window.addEventListener('resize', () => {
    if (!isFileViewerOpen() || fvMode() !== 'image' || !fvImg.nw) return;
    if (fvImg.isFit) fvFit(); else { fvClamp(); fvApply(); }
});

// ---- Fenced code blocks inside messages: highlighting + a Copy button ----
function enhanceCodeBlocks(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('pre > code').forEach(codeEl => {
        const pre = codeEl.parentElement;
        if (pre.closest('.code-block')) return;
        const m = (codeEl.className || '').match(/language-([\w+#.-]+)/);
        const fence = m ? m[1].toLowerCase() : '';
        const lang = CODE_FENCE_ALIASES[fence] || fence;
        const text = codeEl.textContent;

        const block = document.createElement('div');
        block.className = 'code-block';
        const head = document.createElement('div');
        head.className = 'code-block-head';
        const label = document.createElement('span');
        label.className = 'code-block-lang';
        label.textContent = fence || 'code';
        const copy = document.createElement('button');
        copy.type = 'button';
        copy.className = 'fc-btn';
        copy.textContent = 'Copy';
        copy.title = 'Copy this code';
        copy.addEventListener('click', async () => {
            flashButton(copy, (await copyText(text)) ? 'Copied \u2713' : 'Copy failed');
        });
        head.appendChild(label);
        head.appendChild(copy);

        pre.parentNode.insertBefore(block, pre);
        block.appendChild(head);
        block.appendChild(pre);
        codeEl.innerHTML = highlightCode(text, lang);
        codeEl.classList.add('hljs');
    });
}

// Cards live in the message's file group (outside the markdown bubble), so they are not
// wiped when the bubble is re-rendered as more text streams in.
function getFileGroup(bubble) {
    const wrapper = bubble.parentElement;
    let group = wrapper.querySelector(':scope > .file-group');
    if (!group) {
        group = document.createElement('div');
        group.className = 'file-group';
        group.style.display = 'flex';
        group.style.flexWrap = 'wrap';
        group.style.gap = '8px';
        group.style.maxWidth = '75%';
        group.style.marginTop = '4px';
        wrapper.appendChild(group);
    }
    group.style.flexBasis = '100%';   // a card gets its own line under the text
    return group;
}

// ---- Emoji & sticker picker ----
const EMOJI_GROUPS = [
    {label: 'Smileys', items: '😀 😃 😄 😁 😆 😅 😂 🤣 🙂 😉 😊 😇 🥰 😍 🤩 😘 😋 😛 😜 🤪 🤔 🤨 😐 😑 😶 🙄 😏 😴 😌 😎 🤓 🥳 😭 😢 😤 😠 😡 🥺 😱 😳 🤯 🥵 🥶 😬 🤗 🫡 😮'.split(' ')},
    {label: 'Gestures', items: '👍 👎 👏 🙌 🙏 🤝 👋 ✌️ 🤞 👌 🤙 💪 👀 🫶 🤌 ☝️ 👆 👇 👈 👉'.split(' ')},
    {label: 'Hearts & symbols', items: '❤️ 🧡 💛 💚 💙 💜 🖤 🤍 💔 💖 💯 ✨ ⭐ 🔥 💥 💤 💢 ✅ ❌ ❓ ❗ ⚡'.split(' ')},
    {label: 'Fun', items: '🎉 🎊 🎁 🎮 🎧 🎵 ☕ 🍕 🍔 🍜 🍰 🍺 🌸 🌙 ☀️ 🌈 🐶 🐱 🚀 💡 📌 🔧 💻 📱'.split(' ')},
];

const pickerBtn = document.getElementById('pickerBtn');
const pickerPop = document.getElementById('pickerPop');
const pickerEmoji = document.getElementById('pickerEmoji');
const pickerStickers = document.getElementById('pickerStickers');
let pickerTab = 'emoji';
let stickerNames = null;     // null until the list has been loaded once
let stickerBusy = false;

function pickerIsOpen() { return !pickerPop.hidden; }

function openPicker() {
    pickerPop.hidden = false;
    pickerBtn.classList.add('active');
    pickerBtn.setAttribute('aria-expanded', 'true');
    if (pickerTab === 'stickers') loadStickers();
}

function closePicker() {
    pickerPop.hidden = true;
    pickerBtn.classList.remove('active');
    pickerBtn.setAttribute('aria-expanded', 'false');
}

function setPickerTab(tab) {
    pickerTab = tab;
    pickerPop.querySelectorAll('.picker-tab').forEach(t => {
        const on = t.dataset.tab === tab;
        t.classList.toggle('active', on);
        t.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    pickerEmoji.hidden = tab !== 'emoji';
    pickerStickers.hidden = tab !== 'stickers';
    if (tab === 'stickers') loadStickers();   // re-read the folder so new files show up
}

// ---- Emoji: click inserts at the cursor (the picker stays open for several) ----
function renderEmojiGrid() {
    pickerEmoji.innerHTML = '';
    EMOJI_GROUPS.forEach(group => {
        const heading = document.createElement('div');
        heading.className = 'picker-group';
        heading.textContent = group.label;
        pickerEmoji.appendChild(heading);
        const grid = document.createElement('div');
        grid.className = 'emoji-grid';
        group.items.forEach(ch => {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'emoji-cell';
            b.textContent = ch;
            b.addEventListener('click', () => insertEmoji(ch));
            grid.appendChild(b);
        });
        pickerEmoji.appendChild(grid);
    });
}

function insertEmoji(ch) {
    const el = messageInput;
    const start = el.selectionStart != null ? el.selectionStart : el.value.length;
    const end = el.selectionEnd != null ? el.selectionEnd : start;
    el.value = el.value.slice(0, start) + ch + el.value.slice(end);
    const pos = start + ch.length;
    el.setSelectionRange(pos, pos);
    el.focus();
    el.dispatchEvent(new Event('input'));   // updates the Send/Queue/Stop button
}

// ---- Stickers: only the PNGs in assets/stickers/User, a click sends one immediately ----
async function loadStickers() {
    if (stickerNames === null) pickerStickers.innerHTML = '<div class="picker-empty">Loading…</div>';
    try {
        const resp = await fetch('/api/stickers', {headers: authHeaders()});
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        stickerNames = (data.stickers || []).map(s => s.name);
    } catch (e) {
        if (stickerNames === null) {
            pickerStickers.innerHTML = '<div class="picker-empty">Could not load stickers.<small>Is the /api/stickers route added to api.py?</small></div>';
        }
        return;
    }
    renderStickerGrid();
}

function renderStickerGrid() {
    pickerStickers.innerHTML = '';
    if (!stickerNames || stickerNames.length === 0) {
        pickerStickers.innerHTML = '<div class="picker-empty">No stickers found.<small>Put PNG files in assets/stickers/User</small></div>';
        return;
    }
    const grid = document.createElement('div');
    grid.className = 'sticker-grid';
    stickerNames.forEach(name => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'sticker-cell';
        b.title = name;
        const img = document.createElement('img');
        img.src = stickerUrl(name);
        img.alt = name;
        img.loading = 'lazy';
        img.draggable = false;
        b.appendChild(img);
        b.addEventListener('click', () => sendSticker(name));
        grid.appendChild(b);
    });
    pickerStickers.appendChild(grid);
}

async function sendSticker(name) {
    if (stickerBusy) return;
    stickerBusy = true;
    try {
        const resp = await fetch(stickerUrl(name));
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const blob = await resp.blob();
        const file = new File([blob], `sticker_${name}`, {type: 'image/png'});
        closePicker();
        const token = `[sticker: ${name}]`;
        if (isStreaming) {
            enqueueMessage(token, [file]);          // Lucy is still replying: send it right after
        } else {
            // not awaited to the end: sendMessage resolves only when the whole reply is done
            const sending = sendMessage({message: token, files: [file], sessionId: currentSessionId});
            await Promise.race([sending, new Promise(r => setTimeout(r, 300))]);
        }
    } catch (e) {
        console.error('Failed to send sticker:', e);
        alert('Could not send the sticker.');
    } finally {
        stickerBusy = false;
    }
}

pickerBtn.addEventListener('click', () => { pickerIsOpen() ? closePicker() : openPicker(); });
pickerPop.querySelectorAll('.picker-tab').forEach(t => t.addEventListener('click', () => setPickerTab(t.dataset.tab)));
// keep the text cursor in the message box while clicking inside the picker
pickerPop.addEventListener('mousedown', (e) => e.preventDefault());
document.addEventListener('mousedown', (e) => {
    if (pickerIsOpen() && !pickerPop.contains(e.target) && !pickerBtn.contains(e.target)) closePicker();
});
renderEmojiGrid();

applyTheme(getSavedTheme());

// ---- Init ----
const greetingTime = document.getElementById('greetingTime');
if (greetingTime) {
    greetingTime.textContent = formatTime(localNow()) || '';
}
loadSessions();
loadVoiceStatus();
bindAutosave();

// Enter sends (or queues while a reply is running); Shift+Enter inserts a new line.
// Ignored while an IME (Japanese/Chinese/Korean input) is composing, where Enter confirms the text.
messageInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
        e.preventDefault();
        sendMessage();
    }
});

// The message box is a textarea that grows with its content (up to its CSS max-height, then scrolls).
function autosizeInput() {
    messageInput.style.height = 'auto';
    const borders = messageInput.offsetHeight - messageInput.clientHeight;
    messageInput.style.height = (messageInput.scrollHeight + borders) + 'px';
}
messageInput.addEventListener('input', () => {
    autosizeInput();
    updateSendButton();
});

// On narrow phones there is only room for a short placeholder (a textarea would clip the long one mid-word).
const narrowScreen = window.matchMedia('(max-width: 480px)');
function updatePlaceholder() {
    messageInput.placeholder = narrowScreen.matches ? 'Message...' : 'Type a message...';
}
updatePlaceholder();
narrowScreen.addEventListener('change', updatePlaceholder);

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        if (isFileViewerOpen()) {
            closeFileViewer();
        } else if (pickerIsOpen()) {
            closePicker();
        } else if (settingsOverlay.style.display === 'flex') {
            closeSettings();
        } else if (isStreaming) {
            stopGeneration();
        }
    }
});


// ---- Neon UI helpers: empty-state hero, quick actions, session menu, Recent Sessions fold ----
function closeSessionMenus() {
    document.querySelectorAll('.history-item.menu-open').forEach(i => i.classList.remove('menu-open'));
}

function toggleSessionMenu(ev) {
    ev.stopPropagation();
    const item = ev.currentTarget.closest('.history-item');
    const wasOpen = item.classList.contains('menu-open');
    closeSessionMenus();
    if (!wasOpen) item.classList.add('menu-open');
}

document.addEventListener('click', closeSessionMenus);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeSessionMenus(); });

(function neonUi() {
    // The hero (logo + quick actions) shows whenever the open session has no messages.
    const stage = document.getElementById('chatStage');
    if (stage) {
        const syncEmpty = () => stage.classList.toggle(
            'is-empty', !chatContainer.querySelector('.message-row, .thinking-indicator'));
        new MutationObserver(syncEmpty).observe(chatContainer, {childList: true});
        syncEmpty();
    }

    // Quick actions only start a message: they fill the box and focus it, nothing is sent.
    document.querySelectorAll('.quick-chip').forEach(btn => {
        btn.addEventListener('click', () => {
            messageInput.value = btn.dataset.prompt || '';
            messageInput.dispatchEvent(new Event('input'));
            messageInput.focus();
            messageInput.setSelectionRange(messageInput.value.length, messageInput.value.length);
        });
    });

    // Recent Sessions folds the list away.
    const recentToggle = document.getElementById('recentToggle');
    if (recentToggle) {
        recentToggle.addEventListener('click', () => {
            const open = recentToggle.getAttribute('aria-expanded') !== 'true';
            recentToggle.setAttribute('aria-expanded', String(open));
            chatHistoryEl.hidden = !open;
        });
    }
})();

// ---- Voice input (mic button) ----
// Tap the mic and talk. Tap again (or just stop talking) and the recording is turned into text by Lucy's
// language model (POST /api/stt, see stt.py). The text is sent as your message.
//   tap while listening = stop and send      Esc while listening = cancel
const MIC_AUTO_SEND = true;      // true: the transcript is sent right away. false: it only fills the message box so you can check it first.
const MIC_SILENCE_MS = 1800;     // stop by itself after this much quiet once you have spoken (0 = only stop when you tap)
const MIC_MAX_MS = 30000;        // hard limit for one voice message (audio encoders in LLMs usually handle about 30 s per clip)
const MIC_RATE = 16000;          // what the speech model wants: 16 kHz mono

const micButton = document.getElementById('micButton');
const mic = {state: 'idle', stream: null, pcm: [], rate: 48000, ctx: null, analyser: null, tap: null, mute: null, raf: 0,
             startedAt: 0, heardSpeech: false, lastVoiceAt: 0, cancelled: false, placeholder: ''};

// ---- little pop-up message above the input ----
let micToastTimer = 0;
function showMicToast(text, kind) {
    let el = document.getElementById('micToast');
    if (!el) {
        el = document.createElement('div');
        el.id = 'micToast';
        el.className = 'mic-toast';
        el.setAttribute('role', 'status');
        (document.getElementById('chatStage') || document.body).appendChild(el);
    }
    el.textContent = text;
    el.dataset.kind = kind || 'info';
    el.classList.add('show');
    clearTimeout(micToastTimer);
    micToastTimer = setTimeout(() => el.classList.remove('show'), kind === 'error' ? 9000 : 4000);
}

function micUnavailableReason() {
    if (!window.isSecureContext) {
        return 'Your browser only allows the microphone on secure pages (https:// or localhost), and this page is on ' +
               location.origin + '. Open Lucy over HTTPS (for example with Tailscale Serve), or use http://localhost:' +
               (location.port || '80') + ' on the PC.';
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return 'This browser cannot use the microphone.';
    if (!(window.AudioContext || window.webkitAudioContext) || typeof OfflineAudioContext === 'undefined') return 'This browser cannot record audio.';
    return '';
}

function micPermissionMessage(err) {
    const name = err && err.name;
    if (name === 'NotAllowedError' || name === 'SecurityError') return 'Microphone permission was blocked. Allow it in your browser\u2019s site settings, then tap the mic again.';
    if (name === 'NotFoundError' || name === 'OverconstrainedError') return 'No microphone was found.';
    if (name === 'NotReadableError') return 'The microphone is busy (another app may be using it).';
    return 'Could not start the microphone.';
}

function setMicUi(state) {
    mic.state = state;
    micButton.classList.toggle('recording', state === 'recording');
    micButton.classList.toggle('busy', state === 'transcribing');
    micButton.setAttribute('aria-pressed', String(state === 'recording'));
    micButton.disabled = state === 'transcribing';
    document.getElementById('chatForm').classList.toggle('is-listening', state === 'recording');
    if (state === 'recording') {
        micButton.title = 'Tap to stop and send';
        micButton.setAttribute('aria-label', 'Stop recording and send');
    } else if (state === 'transcribing') {
        micButton.title = 'Transcribing\u2026';
        messageInput.placeholder = 'Transcribing\u2026';
    } else {
        micButton.title = 'Voice message (tap to talk)';
        micButton.setAttribute('aria-label', 'Voice message');
        messageInput.placeholder = mic.placeholder || messageInput.placeholder;
        micButton.style.removeProperty('--lvl');
    }
}

function releaseMicHardware() {
    cancelAnimationFrame(mic.raf);
    try { if (mic.tap) { mic.tap.onaudioprocess = null; if (mic.tap.port) mic.tap.port.onmessage = null; mic.tap.disconnect(); } } catch (_) {}
    try { if (mic.mute) mic.mute.disconnect(); } catch (_) {}
    mic.tap = mic.mute = null;
    if (mic.stream) mic.stream.getTracks().forEach(t => t.stop());   // turns off the browser's "recording" indicator
    mic.stream = null;
    if (mic.ctx) { mic.ctx.close().catch(() => {}); mic.ctx = null; }
    mic.analyser = null;
}

// Raw microphone samples, collected straight from the audio graph (no compressed recording, so nothing has to be decoded later).
const MIC_TAP_WORKLET = `
class LucyPcmTap extends AudioWorkletProcessor {
    constructor() { super(); this.buf = new Float32Array(2048); this.n = 0; }
    process(inputs) {
        const ch = inputs[0] && inputs[0][0];
        if (ch) for (let i = 0; i < ch.length; i++) {
            this.buf[this.n++] = ch[i];
            if (this.n === this.buf.length) { this.port.postMessage(this.buf.slice(0)); this.n = 0; }
        }
        return true;
    }
}
registerProcessor('lucy-pcm-tap', LucyPcmTap);
`;

async function attachPcmTap(ctx, source) {
    const mute = ctx.createGain();            // the tap must be connected somewhere to run; silent, so no feedback
    mute.gain.value = 0;
    mute.connect(ctx.destination);
    if (ctx.audioWorklet && typeof AudioWorkletNode !== 'undefined') {
        try {
            const url = URL.createObjectURL(new Blob([MIC_TAP_WORKLET], {type: 'application/javascript'}));
            try { await ctx.audioWorklet.addModule(url); } finally { URL.revokeObjectURL(url); }
            const node = new AudioWorkletNode(ctx, 'lucy-pcm-tap', {numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1, channelCountMode: 'explicit'});
            node.port.onmessage = (e) => { if (mic.state === 'recording') mic.pcm.push(e.data); };
            source.connect(node);
            node.connect(mute);
            return {tap: node, mute};
        } catch (e) { console.warn('AudioWorklet unavailable, falling back:', e); }
    }
    const proc = ctx.createScriptProcessor(4096, 1, 1);       // older browsers
    proc.onaudioprocess = (e) => { if (mic.state === 'recording') mic.pcm.push(new Float32Array(e.inputBuffer.getChannelData(0))); };
    source.connect(proc);
    proc.connect(mute);
    return {tap: proc, mute};
}

async function startMic() {
    if (mic.state !== 'idle') return;
    const why = micUnavailableReason();
    if (why) { showMicToast(why, 'error'); return; }

    let stream;
    try {
        stream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true}});
    } catch (e) {
        showMicToast(micPermissionMessage(e), 'error');
        return;
    }
    mic.stream = stream;
    mic.pcm = [];
    mic.cancelled = false;
    mic.heardSpeech = false;
    mic.placeholder = messageInput.placeholder;

    try {
        const AC = window.AudioContext || window.webkitAudioContext;
        mic.ctx = new AC();
        if (mic.ctx.state === 'suspended') await mic.ctx.resume();
        mic.rate = mic.ctx.sampleRate;
        const source = mic.ctx.createMediaStreamSource(stream);
        mic.analyser = mic.ctx.createAnalyser();            // level meter: the glow on the button, and "you stopped talking"
        mic.analyser.fftSize = 1024;
        source.connect(mic.analyser);
        const t = await attachPcmTap(mic.ctx, source);
        mic.tap = t.tap;
        mic.mute = t.mute;
    } catch (e) {
        console.error('Could not start recording:', e);
        releaseMicHardware();
        showMicToast('This browser could not start recording.', 'error');
        return;
    }

    mic.startedAt = performance.now();
    mic.lastVoiceAt = mic.startedAt;
    setMicUi('recording');
    mic.raf = requestAnimationFrame(micTick);
}

function micTick() {
    if (mic.state !== 'recording') return;
    const now = performance.now();
    if (mic.analyser) {
        const buf = new Uint8Array(mic.analyser.fftSize);
        mic.analyser.getByteTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) { const v = (buf[i] - 128) / 128; sum += v * v; }
        const rms = Math.sqrt(sum / buf.length);
        micButton.style.setProperty('--lvl', Math.min(1, rms * 6).toFixed(3));
        if (rms > 0.02) { mic.heardSpeech = true; mic.lastVoiceAt = now; }
        else if (MIC_SILENCE_MS && mic.heardSpeech && now - mic.lastVoiceAt > MIC_SILENCE_MS) { stopMic(false); return; }
    }
    const secs = Math.floor((now - mic.startedAt) / 1000);
    messageInput.placeholder = `Listening\u2026 ${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, '0')}  \u00B7  tap the mic to send  \u00B7  Esc to cancel`;
    if (now - mic.startedAt > MIC_MAX_MS) { stopMic(false); return; }
    mic.raf = requestAnimationFrame(micTick);
}

function stopMic(cancel) {
    if (mic.state !== 'recording') return;
    mic.cancelled = !!cancel;
    const heldMs = performance.now() - mic.startedAt;
    const chunks = mic.pcm, rate = mic.rate;
    mic.pcm = [];
    releaseMicHardware();           // stops collecting and frees the microphone
    onMicStopped(chunks, rate, heldMs);
}

async function onMicStopped(chunks, rate, heldMs) {
    if (mic.cancelled || !chunks.length || heldMs < 400) {
        setMicUi('idle');
        if (!mic.cancelled && heldMs < 400) showMicToast('Hold on a little longer: tap the mic, speak, then tap it again.');
        return;
    }
    setMicUi('transcribing');
    try {
        const wav = await pcmToWav16k(chunks, rate);
        if (!wav) { showMicToast('That was too short to understand. Try again.'); return; }
        const form = new FormData();
        form.append('file', wav, 'voice.wav');
        const res = await fetch('/api/stt', {method: 'POST', headers: authHeaders(), body: form});
        if (!res.ok) {
            let detail = `Transcription failed (HTTP ${res.status}).`;
            try { const j = await res.json(); if (j && j.detail) detail = String(j.detail); } catch (_) {}
            showMicToast(detail, 'error');
            return;
        }
        const text = ((await res.json()).text || '').trim();
        if (!text) { showMicToast('I did not catch anything. Try again a little closer to the mic.'); return; }
        const existing = messageInput.value.trim();
        messageInput.value = existing ? existing + ' ' + text : text;
        messageInput.dispatchEvent(new Event('input'));
        if (MIC_AUTO_SEND) onSendClick(); else messageInput.focus();
    } catch (e) {
        console.error('Voice input failed:', e);
        showMicToast('Voice input failed: ' + (e && e.message ? e.message : 'unknown error'), 'error');
    } finally {
        setMicUi('idle');
    }
}

// Collected samples (at the microphone's own rate, usually 44.1 or 48 kHz) -> 16 kHz mono 16-bit WAV.
async function pcmToWav16k(chunks, rate) {
    let total = 0;
    for (const c of chunks) total += c.length;
    const all = new Float32Array(total);
    for (let i = 0, o = 0; i < chunks.length; o += chunks[i].length, i++) all.set(chunks[i], o);
    const outLen = Math.round(total * MIC_RATE / rate);
    if (outLen < MIC_RATE * 0.3) return null;
    if (rate === MIC_RATE) return encodeWav(all, MIC_RATE);
    try {
        const off = new OfflineAudioContext(1, outLen, MIC_RATE);       // the browser's own resampler
        const buf = off.createBuffer(1, total, rate);
        buf.copyToChannel(all, 0);
        const src = off.createBufferSource();
        src.buffer = buf;
        src.connect(off.destination);
        src.start();
        return encodeWav((await off.startRendering()).getChannelData(0), MIC_RATE);
    } catch (e) {
        console.warn('Browser resampler failed, using the simple one:', e);
        const ratio = rate / MIC_RATE, out = new Float32Array(outLen);
        for (let i = 0; i < outLen; i++) {                              // average the samples that fall into each output sample
            const a = Math.floor(i * ratio), b = Math.min(total, Math.max(a + 1, Math.floor((i + 1) * ratio)));
            let s = 0;
            for (let j = a; j < b; j++) s += all[j];
            out[i] = s / (b - a);
        }
        return encodeWav(out, MIC_RATE);
    }
}

function encodeWav(samples, rate) {
    const buf = new ArrayBuffer(44 + samples.length * 2);
    const v = new DataView(buf);
    const tag = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    tag(0, 'RIFF'); v.setUint32(4, 36 + samples.length * 2, true); tag(8, 'WAVE');
    tag(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    tag(36, 'data'); v.setUint32(40, samples.length * 2, true);
    for (let i = 0, o = 44; i < samples.length; i++, o += 2) {
        const s = Math.max(-1, Math.min(1, samples[i]));
        v.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
    }
    return new Blob([buf], {type: 'audio/wav'});
}

micButton.addEventListener('click', () => {
    if (mic.state === 'idle') startMic();
    else if (mic.state === 'recording') stopMic(false);
});
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && mic.state === 'recording') { e.preventDefault(); stopMic(true); }
});


// =====================================================================================================
// Library: everything that was shared in your chats (your uploads, files and images Lucy delivered).
// The server keeps one cached copy of each in Lucy_Core/cache (see src/lucy/server/library.py), so this
// works after a refresh or restart. Stickers are deliberately not in here: they live in assets/stickers.
// =====================================================================================================
const LIB_TABS = [
    {id: 'all', label: 'All', cats: []},
    {id: 'images', label: 'Images', cats: ['Images']},
    {id: 'code', label: 'Code', cats: ['Code']},
    {id: 'documents', label: 'Documents', cats: ['Documents']},
    {id: 'media', label: 'Audio & video', cats: ['Audio', 'Video']},
    {id: 'other', label: 'Other', cats: ['Archives', 'Other']},
];
const LIB_PAGE = 60;
const lib = {open: false, tab: 'all', items: [], total: 0, counts: {}, loading: false, seq: 0, error: '', io: null, searchTimer: 0};

function libraryFileUrl(sha) {
    return `/api/library/file/${sha}?api_key=${encodeURIComponent(API_KEY)}`;
}

function libraryEl(id) { return document.getElementById(id); }

function openLibrary() {
    lib.open = true;
    document.querySelector('.main-chat').classList.add('library-open');
    document.body.classList.add('library-open');
    const btn = libraryEl('libraryBtn');
    btn.classList.add('active');
    btn.setAttribute('aria-pressed', 'true');
    // "This session" only makes sense when a session exists (the start page has none yet)
    const scope = libraryEl('libScope');
    scope.disabled = !currentSessionId;
    if (!currentSessionId) scope.checked = false;
    libraryEl('libScopeRow').classList.toggle('disabled', !currentSessionId);
    if (mobileQuery.matches) closeMobileSidebar();
    loadLibrary(true);
}

function closeLibrary() {
    if (!lib.open) return;
    lib.open = false;
    document.querySelector('.main-chat').classList.remove('library-open');
    document.body.classList.remove('library-open');
    const btn = libraryEl('libraryBtn');
    btn.classList.remove('active');
    btn.setAttribute('aria-pressed', 'false');
    if (lib.io) { lib.io.disconnect(); lib.io = null; }
}

async function loadLibrary(reset = true) {
    if (reset) lib.items = [];
    const seq = ++lib.seq;
    lib.loading = true;
    lib.error = '';
    const tab = LIB_TABS.find(t => t.id === lib.tab) || LIB_TABS[0];
    const p = new URLSearchParams({limit: LIB_PAGE, offset: lib.items.length});
    if (tab.cats.length) p.set('category', tab.cats.join(','));
    const origin = libraryEl('libOrigin').value;
    if (origin) p.set('origin', origin);
    const q = libraryEl('libSearch').value.trim();
    if (q) p.set('q', q);
    if (window.deskLibPinned) p.set('pinned', '1');
    if (libraryEl('libScope').checked && currentSessionId) p.set('session_id', currentSessionId);
    try {
        const resp = await fetch('/api/library?' + p.toString(), {headers: authHeaders()});
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        if (seq !== lib.seq) return;                   // a newer request replaced this one
        lib.items = lib.items.concat(data.items || []);
        lib.total = data.total || 0;
        lib.counts = data.counts || {};
    } catch (e) {
        if (seq !== lib.seq) return;
        lib.error = e.message || 'Could not load the library.';
    } finally {
        if (seq === lib.seq) { lib.loading = false; renderLibrary(); }
    }
}

function libTabCount(tab) {
    if (!tab.cats.length) return lib.counts._all || 0;
    return tab.cats.reduce((n, c) => n + (lib.counts[c] || 0), 0);
}

function renderLibrary() {
    // tabs (with counts)
    const tabsEl = libraryEl('libTabs');
    tabsEl.innerHTML = '';
    LIB_TABS.forEach(t => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'lib-tab' + (t.id === lib.tab ? ' active' : '');
        b.setAttribute('role', 'tab');
        b.setAttribute('aria-selected', t.id === lib.tab ? 'true' : 'false');
        const n = libTabCount(t);
        b.innerHTML = `<span>${escapeHtml(t.label)}</span><span class="n">${n}</span>`;
        b.addEventListener('click', () => { lib.tab = t.id; loadLibrary(true); });
        tabsEl.appendChild(b);
    });
    libraryEl('libSub').textContent = lib.counts._all ? `${lib.counts._all} item${lib.counts._all === 1 ? '' : 's'}` : '';

    // grid
    if (lib.io) { lib.io.disconnect(); lib.io = null; }
    const grid = libraryEl('libGrid');
    grid.innerHTML = '';
    lib.items.forEach(item => grid.appendChild(libCard(item)));

    const empty = libraryEl('libEmpty');
    const filtered = !!(libraryEl('libSearch').value.trim() || libraryEl('libOrigin').value || libraryEl('libScope').checked || lib.tab !== 'all');
    empty.classList.toggle('show', !lib.items.length && !lib.loading);
    if (lib.error) {
        empty.innerHTML = '<strong>Could not load the library</strong>';
        empty.appendChild(document.createTextNode(lib.error));
    } else if (!lib.items.length) {
        empty.innerHTML = filtered
            ? '<strong>Nothing matches</strong>Try another filter or search.'
            : '<strong>Your library is empty</strong>Images and files you send, and code or files Lucy creates, show up here.<br>Stickers are not saved here (they already live in assets/stickers).';
    }
    libraryEl('libMore').classList.toggle('show', lib.items.length < lib.total);
}

function libSvg(path) {
    return `<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${path}</svg>`;
}

function libCard(item) {
    const card = document.createElement('div');
    card.className = 'lib-card';
    card.tabIndex = 0;
    card.setAttribute('role', 'button');
    card.title = item.name;

    const fileUrl = `${item.url}?api_key=${encodeURIComponent(API_KEY)}`;
    const thumb = document.createElement('div');
    thumb.className = 'lib-thumb';
    if (item.kind === 'images' && item.thumb_url) {
        const img = document.createElement('img');
        img.loading = 'lazy';
        img.decoding = 'async';
        img.alt = item.name;
        img.src = `${item.thumb_url}?api_key=${encodeURIComponent(API_KEY)}`;
        img.onerror = () => { img.onerror = null; img.src = fileUrl; };
        thumb.appendChild(img);
    } else {
        const badge = document.createElement('span');
        badge.className = 'lib-badge';
        badge.textContent = fileBadge(item.name);
        thumb.appendChild(badge);
        if (isTextFile(item.name) && item.size < 50 * 1024 * 1024) {
            thumb.classList.add('has-snippet');
            thumb._loadSnippet = () => {               // first lines of the file, loaded only when the card scrolls into view
                fetchTextFile(fileUrl, 3000).then(res => {
                    if (res.binary || !res.text) return;
                    const pre = document.createElement('pre');
                    pre.className = 'lib-snippet';
                    pre.textContent = splitLines(res.text).slice(0, 9).join('\n');
                    thumb.insertBefore(pre, thumb.firstChild);
                }).catch(() => {});
            };
            if ('IntersectionObserver' in window) {
                if (!lib.io) {
                    lib.io = new IntersectionObserver(entries => entries.forEach(en => {
                        if (en.isIntersecting) { lib.io.unobserve(en.target); en.target._loadSnippet && en.target._loadSnippet(); }
                    }), {root: libraryEl('libBody'), rootMargin: '200px'});
                }
                lib.io.observe(thumb);
            } else {
                thumb._loadSnippet();
            }
        }
    }
    card.appendChild(thumb);

    const info = document.createElement('div');
    info.className = 'lib-info';
    const nameEl = document.createElement('div');
    nameEl.className = 'lib-name';
    nameEl.textContent = item.name;
    const meta = document.createElement('div');
    meta.className = 'lib-meta';
    const typeLabel = (fileTypeInfo(item.name) || {}).label || item.category;
    meta.textContent = [typeLabel, item.size ? formatFileSize(item.size) : '', formatTimestamp(item.created_at)].filter(Boolean).join(' \u00B7 ');
    info.append(nameEl, meta);
    card.appendChild(info);

    const foot = document.createElement('div');
    foot.className = 'lib-foot';
    const origin = document.createElement('span');
    origin.className = 'lib-origin' + (item.origin === 'assistant' ? ' lucy' : '');
    origin.textContent = item.origin === 'assistant' ? 'Lucy' : 'You';
    const sess = document.createElement('button');
    sess.type = 'button';
    sess.className = 'lib-session';
    sess.textContent = item.session_title;
    sess.title = 'Open the session this came from';
    sess.addEventListener('click', (e) => { e.stopPropagation(); switchSession(item.session_id); });
    const dl = document.createElement('a');
    dl.className = 'lib-act';
    dl.title = 'Download';
    dl.href = `${item.url}?download=1&api_key=${encodeURIComponent(API_KEY)}`;
    dl.download = item.name;
    dl.innerHTML = libSvg('<path d="M12 4v11M7 11l5 5 5-5M5 20h14"/>');
    dl.addEventListener('click', (e) => e.stopPropagation());
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'lib-act danger';
    del.title = 'Remove from the library';
    del.innerHTML = libSvg('<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>');
    del.addEventListener('click', (e) => { e.stopPropagation(); deleteLibraryItem(item); });
    foot.append(origin, sess, dl, del);
    card.appendChild(foot);

    const open = () => openLibraryItem(item);
    card.addEventListener('click', open);
    card.addEventListener('keydown', (e) => { if (e.target === card && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); open(); } });
    return card;
}

// Same viewers as in the chat: zoomable images, line-numbered code, plain details for everything else.
async function openLibraryItem(item) {
    const url = libraryFileUrl(item.sha);
    const file = {name: item.name, type: item.type, size: item.size, url};
    if (item.kind === 'images') { openImageViewer(file); return; }
    if (isTextFile(item.name)) {
        try {
            const res = await fetchTextFile(url);
            if (!res.binary) {
                openFileViewer({name: item.name, info: fileTypeInfo(item.name) || {lang: 'plaintext', label: 'Text'},
                                text: res.text, lines: splitLines(res.text), truncated: res.truncated, bytes: res.bytes, url});
                return;
            }
        } catch (e) { /* fall through to the details view, which still offers Download */ }
    }
    openGenericFileViewer(file);
}

async function deleteLibraryItem(item) {
    if (!confirm(`Remove "${item.name}" from the library?\nIt disappears from the chats that used it too.`)) return;
    try {
        const resp = await fetch(`/api/library/${item.sha}`, {method: 'DELETE', headers: authHeaders()});
        if (!resp.ok && resp.status !== 404) throw new Error(`HTTP ${resp.status}`);
        loadLibrary(true);
    } catch (e) {
        alert('Could not remove it: ' + (e.message || e));
    }
}

libraryEl('libSearch').addEventListener('input', () => {
    clearTimeout(lib.searchTimer);
    lib.searchTimer = setTimeout(() => loadLibrary(true), 250);
});
libraryEl('libOrigin').addEventListener('change', () => loadLibrary(true));
libraryEl('libScope').addEventListener('change', () => loadLibrary(true));
libraryEl('libMore').addEventListener('click', () => loadLibrary(false));
// capture phase: runs BEFORE the viewer's own Escape handler, so Escape closes only the viewer when one is open
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && lib.open && !isFileViewerOpen()) closeLibrary();
}, true);

// A proactive message ("Open" on Lucy's toast): the page used to reload to show it, but a reload now lands on the
// start page, so open the session the message was written into instead.
window.addEventListener('lucy:proactive-open', async (e) => {
    const sid = e.detail && e.detail.session_id;
    if (!sid) return;
    e.preventDefault();
    await loadSessions();
    switchSession(sid, true);
});

// ---- Safety layer (P1): approval cards + Settings > Safety ----
const APPROVAL_DONE = {approve: 'Approved', always: 'Approved (always this chat)', deny: 'Denied', timeout: 'Timed out (denied)'};

function renderApprovalCard(req, sid) {
    const card = document.createElement('div');
    card.className = 'approval-card';
    card.dataset.approvalId = req.id;
    const title = document.createElement('div');
    title.className = 'approval-title';
    title.textContent = 'Lucy wants to: ' + (req.summary || req.tool);
    card.appendChild(title);
    if (req.reason) {
        const why = document.createElement('div');
        why.className = 'approval-reason';
        why.textContent = 'Why it asks: ' + req.reason;
        card.appendChild(why);
    }
    if (req.details) {
        const pre = document.createElement('pre');
        pre.className = 'approval-details' + (req.details_kind === 'diff' ? ' diff' : '');
        if (req.details_kind === 'diff') {
            req.details.split('\n').forEach(line => {
                const span = document.createElement('span');
                span.textContent = line + '\n';
                if (line.startsWith('+') && !line.startsWith('+++')) span.className = 'add';
                else if (line.startsWith('-') && !line.startsWith('---')) span.className = 'del';
                pre.appendChild(span);
            });
        } else {
            pre.textContent = req.details;
        }
        card.appendChild(pre);
    }
    const row = document.createElement('div');
    row.className = 'approval-actions';
    const btn = (label, decision, cls) => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'approval-btn ' + cls;
        b.textContent = label;
        b.onclick = () => answerApproval(req.id, decision, sid, card);
        row.appendChild(b);
    };
    btn('Approve', 'approve', 'ok');
    btn('Deny', 'deny', 'no');
    if (req.can_always) btn('Always allow in this chat', 'always', 'always');
    card.appendChild(row);
    return card;
}

async function answerApproval(id, decision, sid, card) {
    card.querySelectorAll('button').forEach(b => b.disabled = true);
    try {
        const resp = await fetch(`/api/safety/approvals/${encodeURIComponent(id)}?api_key=${API_KEY}`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({decision, session_id: sid})
        });
        if (!resp.ok) throw new Error(await resp.text());
        markApprovalResolved(id, decision);
    } catch (e) {
        markApprovalResolved(id, 'gone');
    }
}

function markApprovalResolved(id, decision) {
    document.querySelectorAll(`.approval-card[data-approval-id="${CSS.escape(id)}"]`).forEach(card => {
        card.querySelectorAll('button').forEach(b => b.disabled = true);
        const row = card.querySelector('.approval-actions');
        if (row) row.textContent = APPROVAL_DONE[decision] || 'No longer pending';
        card.classList.add('resolved', decision === 'approve' || decision === 'always' ? 'yes' : 'no');
    });
}

async function loadSafety() {
    const sel = document.getElementById('safetyModeSelect');
    if (!sel) return;
    try {
        const resp = await fetch(`/api/safety/status?api_key=${API_KEY}`);
        const st = await resp.json();
        sel.innerHTML = st.modes.map(m => `<option value="${m.id}">${escapeHtml(m.label)}</option>`).join('');
        sel.value = st.mode;
        document.getElementById('safetyPauseToggle').checked = !!st.paused;
        const info = document.getElementById('safetyStatus');
        const bits = [];
        if (st.halted) bits.push('⚠ ' + st.halted);
        if (st.paused) bits.push('Agent is PAUSED: no tool runs anywhere.');
        (st.config_problems || []).forEach(p => bits.push('Config: ' + p));
        if (st.drives) {
            const order = {full: 0, storage: 1, ask: 2, off: 3};
            const ds = Object.keys(st.drives).filter(k => k !== 'other')
                .sort((a, b) => a.localeCompare(b))
                .map(k => k + ' ' + st.drives[k]);
            if (st.drives.other) ds.push('new drives ' + st.drives.other);
            bits.push('Drives: ' + ds.join(', ') + '  (full = free, storage = deleting asks, ask = every change asks)');
        }
        if ((st.write_folders || []).length) bits.push('Also writes without asking in: ' + st.write_folders.join(', '));
        info.textContent = bits.join('\n');
        loadSealState();
        loadSafetyBackups();
        loadBrokerStatus();
    } catch (e) {
        console.error('Failed to load safety status:', e);
    }
}

// Settings > Safety: accept changes YOU made to the safety files (same as seal.bat). Shown only when something differs from the lock.
async function loadSealState() {
    const group = document.getElementById('sealGroup');
    if (!group) return;
    try {
        const r = await fetch(`/api/safety/seal?api_key=${API_KEY}`);
        const s = await r.json();
        const files = s.pending || [];
        group.style.display = files.length ? '' : 'none';
        document.getElementById('sealFiles').textContent = files.join('\n');
    } catch (e) { group.style.display = 'none'; }
}

async function sealSafetyChanges() {
    const files = document.getElementById('sealFiles').textContent;
    const typed = prompt('These safety files differ from the lock:\n\n' + files +
        '\n\nDid YOU make these changes and want to keep them? Type SEAL to accept.');
    if (typed === null) return;
    if (typed.trim() !== 'SEAL') { alert('Not sealed.'); return; }
    const resp = await fetch(`/api/safety/seal?api_key=${API_KEY}`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({confirm: 'SEAL'})
    });
    if (!resp.ok) { alert('Could not seal: ' + await resp.text()); return; }
    const s = await resp.json();
    flashSaved();
    if (s.needs_restart) alert('Sealed. Restart Lucy Core so the changed safety code is the code that runs.');
    loadSafety();
}

async function loadBrokerStatus() {
    const el = document.getElementById('brokerStatus');
    if (!el) return;
    try {
        const r = await fetch(`/api/safety/broker?api_key=${API_KEY}`);
        const b = await r.json();
        el.textContent = b.running ? 'Admin helper: running (starts on demand for admin actions, exits when idle).'
                                   : 'Admin helper: not running. It starts with a Windows UAC prompt the first time Lucy needs admin.';
    } catch (e) { el.textContent = ''; }
}

async function setSafetyMode(mode) {
    const resp = await fetch(`/api/safety/mode?api_key=${API_KEY}`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({mode})
    });
    if (!resp.ok) alert('Could not change the mode: ' + await resp.text());
    else flashSaved();
    loadSafety();
}

async function setSafetyPaused(paused) {
    await fetch(`/api/safety/pause?api_key=${API_KEY}`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({paused, kill_jobs: true})
    });
    flashSaved();
    loadSafety();
}

// ---- MCP servers (P4): the list under Settings > Connectors ----
async function loadMcp() {
    const body = document.getElementById('mcpServersBody');
    if (!body) return;
    try {
        const resp = await fetch(`/api/mcp/status?api_key=${API_KEY}`);
        const st = await resp.json();
        document.getElementById('mcpStatus').textContent = (st.problems || []).map(p => 'Config: ' + p).join('\n');
        const servers = st.servers || [];
        if (!servers.length) {
            body.innerHTML = '<tr><td colspan="3" class="settings-table-empty">No MCP servers in .core/mcp.json</td></tr>';
            return;
        }
        body.innerHTML = '';
        servers.forEach(s => {
            const tr = document.createElement('tr');
            const name = document.createElement('td');
            name.textContent = s.tools ? `${s.name} (${s.tools} tools)` : s.name;
            name.title = `${s.transport}, tools ask as: ${s.tier}`;
            const state = document.createElement('td');
            if (s.error) { state.textContent = '\u26A0 ' + s.error.slice(0, 80); state.title = s.error; }
            else if (!s.enabled) state.textContent = 'off';
            else if (s.running) state.textContent = 'running';
            else state.textContent = s.tools ? 'ready (starts when used)' : 'not listed yet';
            const act = document.createElement('td');
            act.className = 'settings-row-actions';
            const toggle = document.createElement('button');
            toggle.className = 'settings-row-btn';
            toggle.textContent = s.enabled ? 'Turn off' : 'Turn on';
            toggle.onclick = async () => { await mcpPost(s.key, 'enabled', {enabled: !s.enabled}); flashSaved(); setTimeout(loadMcp, 1500); };
            act.appendChild(toggle);
            if (s.enabled) {
                const refresh = document.createElement('button');
                refresh.className = 'settings-row-btn';
                refresh.textContent = 'Refresh';
                refresh.onclick = async () => { refresh.textContent = '...'; await mcpPost(s.key, 'refresh', {}); loadMcp(); };
                act.appendChild(refresh);
            }
            tr.append(name, state, act);
            body.appendChild(tr);
        });
    } catch (e) {
        console.error('Failed to load MCP servers:', e);
    }
}

async function mcpPost(key, action, payload) {
    const resp = await fetch(`/api/mcp/${encodeURIComponent(key)}/${action}?api_key=${API_KEY}`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
    });
    if (!resp.ok) {
        let msg = await resp.text();
        try { msg = JSON.parse(msg).detail || msg; } catch (e) {}
        alert(`MCP ${key}: ${msg}`);
    }
}

async function loadSafetyBackups() {
    const body = document.getElementById('safetyBackupsBody');
    if (!body) return;
    const resp = await fetch(`/api/safety/backups?limit=30&api_key=${API_KEY}`);
    const data = await resp.json();
    const items = data.backups || [];
    if (!items.length) {
        body.innerHTML = '<tr><td colspan="3" class="settings-table-empty">No file changes yet</td></tr>';
        return;
    }
    body.innerHTML = '';
    items.forEach(b => {
        const tr = document.createElement('tr');
        const what = document.createElement('td');
        what.textContent = `${b.ts}  ${b.kind === 'created' ? 'created' : 'changed'}  ${b.path}`;
        what.title = `${b.tool} (${b.id})`;
        const tool = document.createElement('td');
        tool.textContent = b.tool;
        const act = document.createElement('td');
        const btn = document.createElement('button');
        btn.className = 'settings-action-btn';
        btn.textContent = 'Restore';
        btn.onclick = async () => {
            if (!confirm(`Put ${b.path} back the way it was before this change?`)) return;
            const r = await fetch(`/api/safety/backups/${encodeURIComponent(b.id)}/restore?api_key=${API_KEY}`, {method: 'POST'});
            const res = await r.json();
            alert(res.message || (res.ok ? 'Restored' : 'Failed'));
            loadSafetyBackups();
        };
        act.appendChild(btn);
        tr.append(what, tool, act);
        body.appendChild(tr);
    });
}

// ---- Persona (P6): Lucy's SELF notes, change log with Revert, daily note ----
async function loadPersona() {
    const status = document.getElementById('personaStatus');
    if (!status) return;
    try {
        const st = await (await fetch(`/api/persona?api_key=${API_KEY}`)).json();
        const r = st.reflection || {};
        const bits = [`SELF: ${st.chars}/${st.chars_max} characters.`];
        const e = st.edits_today || {};
        bits.push(`Edits today: chat ${e.agent || 0}/${(st.daily_limits || {}).agent}, reflection ${e.reflection || 0}/${(st.daily_limits || {}).reflection}.`);
        if (r.last_run) bits.push(`Last reflection: ${new Date(r.last_run * 1000).toLocaleString()} (${r.last_reason}, ${r.last_status}).`);
        else bits.push(r.enabled ? 'No reflection yet: it runs when you have been away a while, and nightly.' : 'Automatic reflection is off (LUCY_REFLECT=0).');
        if (st.flagged_memories) bits.push(`${st.flagged_memories} memories may contradict another one (see the daily note).`);
        (st.problems || []).forEach(p => bits.push('SELF file: ' + p));
        status.textContent = bits.join('\n');
        const self = (st.sections || []).filter(s => s.items.length)
            .map(s => s.title + ':\n' + s.items.map(i => '  - ' + i).join('\n')).join('\n');
        document.getElementById('personaSelf').textContent = self || 'SELF is empty so far.';
        loadPersonaChanges();
        loadPersonaNote();
    } catch (e) {
        console.error('Failed to load persona:', e);
    }
}

async function loadPersonaChanges() {
    const body = document.getElementById('personaChangesBody');
    if (!body) return;
    const data = await (await fetch(`/api/persona/changelog?limit=30&api_key=${API_KEY}`)).json();
    const items = (data.entries || []).filter(e => e.op !== 'revert');
    if (!items.length) {
        body.innerHTML = '<tr><td colspan="3" class="settings-table-empty">No changes yet</td></tr>';
        return;
    }
    body.innerHTML = '';
    items.forEach(en => {
        const tr = document.createElement('tr');
        const what = document.createElement('td');
        what.textContent = `#${en.id} ${en.ts}  ${en.op} [${en.section}]  ${(en.diff || '').replace(/\n/g, '  ')}`;
        what.title = en.reason || '';
        if (en.reverted) what.style.opacity = '0.5';
        const by = document.createElement('td');
        by.textContent = en.actor;
        const act = document.createElement('td');
        if (en.revertible) {
            const btn = document.createElement('button');
            btn.className = 'settings-action-btn';
            btn.textContent = 'Revert';
            btn.onclick = async () => {
                const r = await fetch(`/api/persona/revert?api_key=${API_KEY}`, {
                    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id: en.id})});
                if (!r.ok) { const d = await r.json().catch(() => ({})); alert(d.detail || 'Could not revert'); }
                loadPersona();
            };
            act.appendChild(btn);
        } else if (en.reverted) {
            act.textContent = 'reverted';
        }
        tr.append(what, by, act);
        body.appendChild(tr);
    });
}

async function loadPersonaNote() {
    const el = document.getElementById('personaNote');
    if (!el) return;
    const data = await (await fetch(`/api/persona/notes?days=1&api_key=${API_KEY}`)).json();
    const n = (data.notes || [])[0];
    el.textContent = n ? n.text : 'No daily note yet.';
}

async function runReflectionNow() {
    const btn = document.getElementById('personaReflectBtn');
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = 'Reflecting...';
    try {
        const r = await fetch(`/api/persona/reflect?api_key=${API_KEY}`, {method: 'POST'});
        const d = await r.json();
        if (!r.ok) alert(d.detail || 'Could not reflect');
        else if (d.status === 'model_unavailable') alert('The model is not running. Start it in Settings > Model first.');
    } catch (e) {
        alert('Reflection failed: ' + e);
    }
    btn.disabled = false;
    btn.textContent = old;
    loadPersona();
}

// ---- Evolve (P7): Lane B proposals (Approve / Reject / diff), applied releases (Rollback), Lucy's own tools ----
async function evolvePost(url, body) {
    const r = await fetch(`${url}?api_key=${API_KEY}`, {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body || {})});
    const d = await r.json().catch(() => ({}));
    if (!r.ok) alert(d.detail || 'That did not work');
    return r.ok ? d : null;
}

function evolveBtn(label, onclick) {
    const b = document.createElement('button');
    b.className = 'settings-action-btn';
    b.textContent = label;
    b.onclick = onclick;
    return b;
}

function evolveRows(bodyId, items, empty, render) {
    const body = document.getElementById(bodyId);
    if (!body) return;
    if (!items.length) { body.innerHTML = `<tr><td colspan="3" class="settings-table-empty">${empty}</td></tr>`; return; }
    body.innerHTML = '';
    items.forEach(it => {
        const tr = document.createElement('tr');
        const [what, status, act] = [document.createElement('td'), document.createElement('td'), document.createElement('td')];
        render(it, what, status, act);
        tr.append(what, status, act);
        body.appendChild(tr);
    });
}

async function loadEvolve() {
    let st;
    try { st = await (await fetch(`/api/selfdev/status?api_key=${API_KEY}`)).json(); }
    catch (e) { console.error('Failed to load Evolve:', e); return; }
    const props = (st.proposals || []).filter(p => !['discarded', 'rejected'].includes(p.status)).slice(0, 15);
    evolveRows('evolveProposalsBody', props, 'None', (p, what, status, act) => {
        what.textContent = `${p.id}  ${p.title}`;
        what.title = p.reason || '';
        status.textContent = p.status + (p.failure ? ` (${p.failure})` : '');
        act.appendChild(evolveBtn('Diff', () => showEvolveDiff(p.id)));
        if (p.status === 'ready') {
            act.appendChild(evolveBtn('Approve', async () => {
                if (!confirm(`Apply "${p.title}" to Lucy Core? Lucy must be restarted afterwards. You can roll it back.`)) return;
                if (await evolvePost(`/api/selfdev/proposals/${p.id}/approve`)) alert('Applied. Restart Lucy Core to use it.');
                loadEvolve();
            }));
            act.appendChild(evolveBtn('Reject', async () => { await evolvePost(`/api/selfdev/proposals/${p.id}/reject`); loadEvolve(); }));
        }
    });
    evolveRows('evolveReleasesBody', (st.releases || []).slice(0, 10), 'None', (m, what, status, act) => {
        what.textContent = `${m.id}  ${m.title || ''}`;
        status.textContent = m.status;
        if (m.status === 'applied') act.appendChild(evolveBtn('Roll back', async () => {
            if (!confirm(`Put back the files from before "${m.title}"? Restart Lucy afterwards.`)) return;
            if (await evolvePost('/api/selfdev/rollback', {id: m.id})) alert('Rolled back. Restart Lucy Core.');
            loadEvolve();
        }));
    });
    const tiers = ['read', 'write', 'exec', 'system', 'network'];
    evolveRows('evolveToolsBody', st.tools || [], 'None yet', (t, what, status, act) => {
        what.textContent = `${t.name} v${t.version} [${t.enabled ? t.status : 'off'}] ${t.description}  (used ${t.uses_total}x, ${t.fails_total} failures)`;
        what.title = (t.reason ? 'Why: ' + t.reason : '') + (t.disabled_reason ? '\nOff: ' + t.disabled_reason : '') + `\nNeeds: ${(t.needs || []).join(', ') || 'nothing'}`;
        const sel = document.createElement('select');
        tiers.slice(tiers.indexOf(t.tier_floor)).forEach(x => { const o = document.createElement('option'); o.value = o.textContent = x; sel.appendChild(o); });
        sel.value = t.tier;
        sel.title = 'read / write = runs without asking (in the default mode); exec and up = asks first';
        sel.onchange = async () => { await evolvePost(`/api/selfdev/tools/${t.name}/tier`, {tier: sel.value}); loadEvolve(); };
        status.appendChild(sel);
        act.appendChild(evolveBtn(t.enabled ? 'Switch off' : 'Switch on', async () => {
            await evolvePost(`/api/selfdev/tools/${t.name}/${t.enabled ? 'disable' : 'enable'}`); loadEvolve();
        }));
        if (t.version > 1) act.appendChild(evolveBtn('Previous version', async () => { await evolvePost(`/api/selfdev/tools/${t.name}/revert`); loadEvolve(); }));
    });
    const ideas = st.ideas || {};
    const el = document.getElementById('evolveIdeas');
    if (el) el.textContent = !ideas.ran ? 'No trace review yet (it runs weekly).'
        : `Trace review ${ideas.window} (${ideas.events} events):\n` + ((ideas.ideas || []).map(i => `${i.n}. [${i.kind}] ${i.title}\n   ${i.suggestion}`).join('\n') || 'Nothing stood out.')
          + '\nTo work on one, ask Lucy in the chat, e.g. "work on improvement idea 1".';
}

async function showEvolveDiff(id) {
    const el = document.getElementById('evolveDiff');
    const r = await fetch(`/api/selfdev/proposals/${id}?api_key=${API_KEY}`);
    const p = await r.json();
    const c = p.checks || {};
    el.textContent = `${p.id} ${p.title}\nWhy: ${p.reason}\nTests: ${c.tests_ok === undefined ? '-' : (c.tests_ok ? 'passed' : 'FAILED')}  `
        + `Trial run: ${c.canary_ok === undefined ? '-' : (c.canary_ok ? 'passed' : 'FAILED')}\n${(p.events || []).join('\n')}\n\n${p.diff || '(no diff yet)'}`;
}

async function runTraceReview() {
    const btn = document.getElementById('evolveMineBtn');
    btn.disabled = true;
    await evolvePost('/api/selfdev/mine');
    btn.disabled = false;
    loadEvolve();
}

// ---- Tasks (P8): the job queue. Approve / Deny / Always for a parked step, OK-to-start, Run now, Continue, Cancel ----
let tasksTimer = null;
function startTasksPolling() { loadTasks(); stopTasksPolling(); tasksTimer = setInterval(loadTasks, 5000); }
function stopTasksPolling() { if (tasksTimer) { clearInterval(tasksTimer); tasksTimer = null; } }

async function tasksCall(method, url, body) {
    const r = await fetch(`${url}?api_key=${API_KEY}`, {method, headers: {'Content-Type': 'application/json'},
        body: body === undefined ? undefined : JSON.stringify(body)});
    const d = await r.json().catch(() => ({}));
    if (!r.ok) alert(d.detail || 'That did not work');
    return r.ok ? d : null;
}

async function loadTasks() {
    let d;
    try { d = await (await fetch(`/api/jobs?api_key=${API_KEY}`)).json(); }
    catch (e) { console.error('Failed to load tasks:', e); return; }
    const st = d.status || {};
    const today = st.today || {}, caps = st.caps || {};
    const stEl = document.getElementById('tasksStatus');
    if (stEl) stEl.textContent = `${st.enabled ? 'On' : 'OFF (jobs.enabled in proactive.yaml)'} - ${st.active || 0} active, ${st.parked || 0} waiting for you. `
        + `Today: ${today.runs || 0}/${caps.runs || '-'} runs, ${today.tool_calls || 0}/${caps.tool_calls || '-'} tool calls.`;
    const body = document.getElementById('tasksBody');
    if (!body) return;
    const jobs = (d.jobs || []).filter(j => !j.system).concat((d.jobs || []).filter(j => j.system));
    if (!jobs.length) { body.innerHTML = '<tr><td colspan="3" class="settings-table-empty">No tasks</td></tr>'; return; }
    body.innerHTML = '';
    jobs.forEach(j => {
        const tr = document.createElement('tr');
        const [what, status, act] = [document.createElement('td'), document.createElement('td'), document.createElement('td')];
        const t = document.createElement('strong');
        t.textContent = `#${j.id} ${j.title}`;
        what.appendChild(t);
        const meta = [j.system ? 'upkeep' : (j.access === 'full' ? 'full tools' : 'read-only'), j.repeat, j.due_in ? `next in ${j.due_in}` : ''].filter(Boolean).join(' - ');
        const sub = document.createElement('div');
        sub.className = 'llm-status';
        sub.textContent = (j.goal && !j.system ? j.goal.slice(0, 140) + ' | ' : '') + meta;
        what.appendChild(sub);
        if (j.parked) {
            const p = document.createElement('pre');
            p.className = 'llm-status safety-status';
            p.textContent = `Waiting for your OK: ${j.parked.summary}\n${j.parked.reason || ''}\n${j.parked.details || ''}`.slice(0, 1500);
            what.appendChild(p);
        } else if (j.progress && j.progress.length && !j.system) {
            const p = document.createElement('div');
            p.className = 'llm-status';
            p.textContent = j.progress.map(x => x.text).slice(-2).join(' / ');
            what.appendChild(p);
        }
        if (j.last_result && !j.system && j.status !== 'running') {
            const r = document.createElement('div');
            r.className = 'llm-status';
            r.textContent = 'Last result: ' + j.last_result.slice(0, 200);
            what.appendChild(r);
        }
        const u = j.usage || {}, b = j.budget || {};
        status.textContent = j.status_label + (j.running || j.status === 'parked' || j.status === 'stopped'
            ? ` (${u.tool_calls || 0}/${b.tool_calls || '-'} calls, ${Math.floor((u.seconds || 0) / 60)}/${Math.floor((b.seconds || 0) / 60)} min)` : '')
            + (j.last_error && j.status !== 'done' ? ` - ${j.last_error.slice(0, 80)}` : '');
        const go = (label, fn) => act.appendChild(evolveBtn(label, async () => { await fn(); loadTasks(); }));
        if (j.status === 'parked' && j.parked) {
            go('Approve', () => tasksCall('POST', `/api/jobs/${j.id}/decision`, {decision: 'approve', approval_id: j.parked.id}));
            go('Deny', () => tasksCall('POST', `/api/jobs/${j.id}/decision`, {decision: 'deny', approval_id: j.parked.id}));
            if (j.parked.can_always) go('Always for this task', () => tasksCall('POST', `/api/jobs/${j.id}/decision`, {decision: 'always', approval_id: j.parked.id}));
        }
        if (j.status === 'waiting_ok') go('OK to start', () => tasksCall('POST', `/api/jobs/${j.id}/ok`));
        if (j.status === 'scheduled') go('Run now', () => tasksCall('POST', `/api/jobs/${j.id}/run`));
        if (j.status === 'stopped') go('Continue', () => tasksCall('POST', `/api/jobs/${j.id}/extend`, {}));
        if (['done', 'failed', 'cancelled'].includes(j.status)) go('Remove', () => tasksCall('DELETE', `/api/jobs/${j.id}`));
        else go('Cancel', () => tasksCall('POST', `/api/jobs/${j.id}/cancel`));
        tr.append(what, status, act);
        body.appendChild(tr);
    });
}

async function addTask() {
    const goal = document.getElementById('taskGoal').value.trim();
    if (!goal) return;
    const d = await tasksCall('POST', '/api/jobs', {goal, when: document.getElementById('taskWhen').value.trim() || null,
        repeat: document.getElementById('taskRepeat').value || null, access: document.getElementById('taskFull').checked ? 'full' : 'read'});
    if (d) { document.getElementById('taskGoal').value = ''; document.getElementById('taskWhen').value = ''; loadTasks(); }
}

// =====================================================================================================================
// P9 - desktop-style features (projects, edit/regenerate/branch, / commands and @files, usage meter, plan mode, rewind,
// Library pins/cap/side pane, settings overview, export, quick entry). Backend: /api/projects, /api/desk/*, /api/library.
// =====================================================================================================================
const PLAN_APPROVE_TEXT = 'Plan approved. Carry it out now, step by step, and tell me when it is done.';
const desk = {projects: [], sessionProject: {}, active: null, commands: null, plan: false, opts: {}, skipPre: false,
              lastText: '', llm: null, popKind: '', popItems: [], popIdx: 0, fileTimer: 0};

async function deskApi(method, url, body) {
    const o = {method, headers: authHeaders(body && !(body instanceof FormData) ? {'Content-Type': 'application/json'} : {})};
    if (body) o.body = body instanceof FormData ? body : JSON.stringify(body);
    const r = await fetch(url, o);
    if (!r.ok) {
        let d = '';
        try { d = (await r.json()).detail; } catch (e) { /* no body */ }
        throw new Error(d || ('HTTP ' + r.status));
    }
    return r.json();
}
function deskEl(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
}
function deskOverlay(title, onClose) {
    const ov = deskEl('div', 'settings-overlay');
    ov.style.display = 'flex';
    const modal = deskEl('div', 'settings-modal desk-modal');
    const head = deskEl('div', 'settings-header');
    const h = deskEl('h3', '', title);
    const x = deskEl('button', 'close-x-btn', '\u00D7');
    const close = () => { ov.remove(); if (onClose) onClose(); };
    x.onclick = close;
    ov.addEventListener('click', (e) => { if (e.target === ov) close(); });
    head.append(h, x);
    const body = deskEl('div', 'desk-modal-body');
    modal.append(head, body);
    ov.appendChild(modal);
    document.body.appendChild(ov);
    return {body, close};
}
function deskField(label, el) {
    const g = deskEl('div', 'setting-group');
    g.append(deskEl('label', '', label), el);
    return g;
}

// ---------------------------------------------------------------- 1. projects
async function deskLoadProjects() {
    try {
        const d = await deskApi('GET', '/api/projects');
        desk.projects = d.projects || [];
        desk.sessionProject = d.sessions || {};
        if (desk.active && !desk.projects.some(p => p.id === desk.active)) desk.active = null;
    } catch (e) { console.error('projects:', e); }
    deskRenderProjects();
    renderHistory();
    deskUpdateChip();
}
function deskShowSession(id) {
    return !desk.active || desk.sessionProject[id] === desk.active;
}
function deskRenderProjects() {
    const list = document.getElementById('deskProjectList');
    if (!list) return;
    list.innerHTML = '';
    const add = (id, name, count) => {
        const row = deskEl('div', 'desk-proj' + (desk.active === id ? ' active' : ''));
        row.tabIndex = 0;
        const n = deskEl('span', 'desk-proj-name', name);
        const c = deskEl('span', 'desk-proj-count', count === null ? '' : String(count));
        row.append(n, c);
        if (id) {
            const edit = deskEl('button', 'desk-mini-btn', '\u270E');
            edit.title = 'Edit project';
            edit.onclick = (e) => { e.stopPropagation(); deskProjectModal(id); };
            row.appendChild(edit);
        }
        row.onclick = () => { desk.active = id; deskRenderProjects(); renderHistory(); };
        list.appendChild(row);
    };
    add(null, 'All chats', null);
    desk.projects.forEach(p => add(p.id, p.name, p.chats));
}
function deskUpdateChip() {
    const chip = document.getElementById('deskProjectChip');
    if (!chip) return;
    const pid = currentSessionId ? desk.sessionProject[currentSessionId] : null;
    const p = desk.projects.find(x => x.id === pid);
    chip.hidden = !p;
    chip.textContent = p ? '\uD83D\uDCC1 ' + p.name : '';
}
async function deskOnSession(sid) {
    const s = sessions[sid] || {};
    const fresh = !(s.message_count || 0) && !(s.messages || []).length;
    if (desk.active && fresh && !desk.sessionProject[sid]) {
        try {
            await deskApi('POST', '/api/projects/assign', {session_id: sid, project_id: desk.active});
            desk.sessionProject[sid] = desk.active;
            renderHistory();
            deskUpdateChip();
        } catch (e) { console.error('assign:', e); }
    }
}
async function deskProjectModal(pid) {
    let p = {name: '', folder: '', instructions: '', files: [], notes: []};
    if (pid) { try { p = await deskApi('GET', '/api/projects/' + pid); } catch (e) { alert(e.message); return; } }
    const {body, close} = deskOverlay(pid ? 'Project' : 'New project');
    const name = deskEl('input'); name.type = 'text'; name.value = p.name; name.placeholder = 'Project name';
    const folder = deskEl('input'); folder.type = 'text'; folder.value = p.folder || ''; folder.placeholder = 'C:/path/to/folder (optional)';
    const instr = deskEl('textarea', 'desk-textarea'); instr.rows = 5; instr.value = p.instructions || '';
    instr.placeholder = 'Standing instructions Lucy follows in every chat of this project';
    body.append(deskField('Name', name), deskField('Folder (Lucy works here by default)', folder), deskField('Instructions', instr));
    const save = deskEl('button', 'settings-row-btn', pid ? 'Save' : 'Create project');
    save.onclick = async () => {
        try {
            const payload = {name: name.value, folder: folder.value, instructions: instr.value};
            const res = pid ? await deskApi('PATCH', '/api/projects/' + pid, payload) : await deskApi('POST', '/api/projects', payload);
            close(); await deskLoadProjects();
            if (!pid) { desk.active = res.id; deskRenderProjects(); renderHistory(); }
        } catch (e) { alert(e.message); }
    };
    body.appendChild(save);
    if (pid) {
        const files = deskEl('div', 'desk-list');
        const fresh = async () => { close(); deskProjectModal(pid); };
        (p.files || []).forEach(f => {
            const r = deskEl('div', 'desk-list-row');
            r.append(deskEl('span', '', f.name + (f.missing ? ' (missing)' : '')));
            const rm = deskEl('button', 'settings-row-btn', 'Remove');
            rm.onclick = async () => { await deskApi('DELETE', `/api/projects/${pid}/files/${f.sha}`); fresh(); };
            r.appendChild(rm); files.appendChild(r);
        });
        const up = deskEl('input'); up.type = 'file';
        up.onchange = async () => {
            if (!up.files[0]) return;
            const fd = new FormData(); fd.append('file', up.files[0]);
            try { await deskApi('POST', `/api/projects/${pid}/files`, fd); fresh(); } catch (e) { alert(e.message); }
        };
        body.append(deskEl('h4', 'desk-sub', 'Files'), files, up);
        const notes = deskEl('div', 'desk-list');
        if (!(p.notes || []).length) notes.appendChild(deskEl('div', 'desk-hint', 'Nothing yet. When Lucy remembers something in this project it shows up here.'));
        (p.notes || []).forEach(n => {
            const r = deskEl('div', 'desk-list-row');
            r.append(deskEl('span', '', n.text));
            const rm = deskEl('button', 'settings-row-btn', 'Forget');
            rm.onclick = async () => { await deskApi('DELETE', `/api/projects/${pid}/notes/${n.id}`); fresh(); };
            r.appendChild(rm); notes.appendChild(r);
        });
        body.append(deskEl('h4', 'desk-sub', 'Project notes (its own memory)'), notes);
        const del = deskEl('button', 'settings-row-btn danger', 'Delete project (chats are kept)');
        del.onclick = async () => {
            if (!confirm('Delete this project? Its chats stay, without a project.')) return;
            await deskApi('DELETE', '/api/projects/' + pid); close(); deskLoadProjects();
        };
        body.appendChild(del);
    }
}
function deskPickProject(sid) {
    const {body, close} = deskOverlay('Move chat to a project');
    const mk = (id, label) => {
        const b = deskEl('button', 'settings-row-btn desk-pick', label);
        b.onclick = async () => {
            try { await deskApi('POST', '/api/projects/assign', {session_id: sid, project_id: id}); } catch (e) { alert(e.message); }
            close(); deskLoadProjects();
        };
        body.appendChild(b);
    };
    mk(null, 'No project');
    desk.projects.forEach(p => mk(p.id, p.name));
}
function deskDecorateSession(item, sid) {
    const menu = item.querySelector('.session-menu');
    if (!menu) return;
    const mk = (label, fn) => {
        const b = deskEl('button', 'session-menu-item', label);
        b.type = 'button'; b.setAttribute('role', 'menuitem');
        b.onclick = (e) => { e.stopPropagation(); closeSessionMenus(); fn(); };
        menu.insertBefore(b, menu.firstChild);
    };
    mk('Move to project\u2026', () => deskPickProject(sid));
    mk('Export as JSON', () => deskExport('json', sid));
    mk('Export as Markdown', () => deskExport('md', sid));
}

// ---------------------------------------------------------------- 2. edit / regenerate / branch (+ 11 export)
function deskExport(fmt, sid) {
    sid = sid || currentSessionId;
    if (!sid) { alert('Open a chat first.'); return; }
    const a = document.createElement('a');
    a.href = `/api/desk/sessions/${encodeURIComponent(sid)}/export?format=${fmt}&api_key=${encodeURIComponent(API_KEY)}`;
    a.download = '';
    document.body.appendChild(a); a.click(); a.remove();
}
function deskSend(text, opts) {
    if (isStreaming) { alert('Wait for the reply to finish (or press Stop) first.'); return; }
    messageInput.value = text;
    autosizeInput();
    desk.opts = opts || {};
    desk.skipPre = true;
    sendMessage();
}
function deskTagRow(bubble, msg) {
    const row = bubble && bubble.closest ? bubble.closest('.message-row') : null;
    if (!row) return;
    if (msg.id) row.dataset.mid = msg.id;
    if (msg.role === 'user') row.dataset.raw = msg.content || '';
    deskRefreshActions();
}
function deskRefreshActions() {
    const rows = [...chatContainer.querySelectorAll('.message-row')];
    const lastAssistant = [...rows].reverse().find(r => r.classList.contains('assistant-message') && r.dataset.mid);
    rows.forEach(row => {
        let bar = row.querySelector(':scope > .msg-actions');
        if (bar) bar.remove();
        if (!row.dataset.mid || !currentSessionId) return;
        bar = deskEl('div', 'msg-actions');
        const btn = (label, title, fn) => { const b = deskEl('button', 'msg-act', label); b.type = 'button'; b.title = title; b.onclick = fn; bar.appendChild(b); };
        const isUser = row.classList.contains('user-message');
        btn('Copy', 'Copy the text', () => copyText((row.querySelector('.message-bubble') || row).innerText));
        if (isUser) btn('Edit', 'Edit this message and answer it again', () => deskEdit(row));
        else {
            if (row === lastAssistant) btn('Regenerate', 'Answer the last message again', () => deskRegenerate());
            btn('Branch', 'Continue from here in a new chat', () => deskBranch(row.dataset.mid));
        }
        row.appendChild(bar);
    });
}
function deskEdit(row) {
    const bubble = row.querySelector('.message-bubble');
    if (!bubble || row.dataset.editing) return;
    row.dataset.editing = '1';
    const old = bubble.innerHTML;
    const ta = deskEl('textarea', 'desk-textarea'); ta.value = row.dataset.raw || bubble.innerText; ta.rows = 3;
    const save = deskEl('button', 'settings-row-btn', 'Save & resend');
    const cancel = deskEl('button', 'settings-row-btn', 'Cancel');
    const bar = deskEl('div', 'desk-edit-bar'); bar.append(save, cancel);
    bubble.innerHTML = ''; bubble.append(ta, bar);
    cancel.onclick = () => { bubble.innerHTML = old; delete row.dataset.editing; };
    save.onclick = async () => {
        const text = ta.value.trim();
        if (!text) return;
        try {
            const res = await deskApi('POST', `/api/desk/sessions/${encodeURIComponent(currentSessionId)}/edit`, {message_id: Number(row.dataset.mid)});
            await switchSession(currentSessionId, true);
            deskSend(text, {reuse: res.attachments});
        } catch (e) { alert(e.message); cancel.onclick(); }
    };
    ta.focus();
}
async function deskRegenerate() {
    if (!currentSessionId) return;
    if (isStreaming) { alert('Wait for the reply to finish (or press Stop) first.'); return; }
    try {
        const res = await deskApi('POST', `/api/desk/sessions/${encodeURIComponent(currentSessionId)}/regenerate`);
        await switchSession(currentSessionId, true);
        deskSend(res.text, {reuse: res.attachments});
    } catch (e) { alert(e.message); }
}
async function deskBranch(mid) {
    if (!currentSessionId) return;
    try {
        if (!mid) {
            const rows = [...chatContainer.querySelectorAll('.message-row[data-mid]')];
            if (!rows.length) { alert('Nothing to branch from yet.'); return; }
            mid = rows[rows.length - 1].dataset.mid;
        }
        const res = await deskApi('POST', `/api/desk/sessions/${encodeURIComponent(currentSessionId)}/branch`, {message_id: Number(mid)});
        await loadSessions(); await deskLoadProjects();
        switchSession(res.session_id, true);
    } catch (e) { alert(e.message); }
}
function deskTurn(bubble, t) {
    const row = bubble.closest('.message-row');
    let prev = row ? row.previousElementSibling : null;
    while (prev && !prev.classList.contains('message-row')) prev = prev.previousElementSibling;
    if (prev) { prev.dataset.mid = t.user_id; prev.dataset.raw = desk.lastText; if (t.checkpoint) prev.dataset.cp = t.checkpoint; }
}
function deskTurnDone(bubble, t) {
    const row = bubble.closest('.message-row');
    if (row) row.dataset.mid = t.assistant_id;
    deskRefreshActions();
}

// ---------------------------------------------------------------- 3. slash commands + @files
function deskTakeOpts() {
    const o = Object.assign({}, desk.opts);
    if (desk.plan && !o.mode) o.mode = 'plan';
    if (o.mode === 'plan') deskSetPlan(false);
    desk.opts = {};
    desk.lastText = messageInput.value.trim();
    return o;
}
function deskClearInput() { messageInput.value = ''; autosizeInput(); updateSendButton(); }
function deskPreSend() {
    if (desk.skipPre) { desk.skipPre = false; return false; }
    deskHidePop();
    if (isStreaming || !desk.commands) return false;
    const m = messageInput.value.trim().match(/^\/([A-Za-z]+)(?:[ \t]+([\s\S]*))?$/);
    if (!m) return false;
    const cmd = desk.commands.find(c => c.name === m[1].toLowerCase());
    if (!cmd) return false;
    const args = (m[2] || '').trim();
    const need = () => { messageInput.placeholder = `/${cmd.name} ${cmd.args}`; messageInput.value = `/${cmd.name} `; messageInput.focus(); return true; };
    switch (cmd.action) {
        case 'plan':
            if (!args) { deskClearInput(); deskSetPlan(true); return true; }
            messageInput.value = args; desk.opts = {mode: 'plan'}; return false;
        case 'research':
            if (!args) return need();
            messageInput.value = 'Research: ' + args; desk.opts = {mode: 'research'}; return false;
        case 'text':
            if (!args) return need();
            messageInput.value = cmd.template.replace('{args}', args); return false;
        case 'regenerate': deskClearInput(); deskRegenerate(); return true;
        case 'branch': deskClearInput(); deskBranch(); return true;
        case 'export': deskClearInput(); deskExport(args.toLowerCase() === 'json' ? 'json' : 'md'); return true;
        case 'new': deskClearInput(); newSession(); return true;
        case 'rewind': deskClearInput(); deskRewindOpen(); return true;
        case 'help': {
            deskClearInput();
            addMessage('**Commands**\n\n' + desk.commands.map(c => `- \`/${c.name}${c.args ? ' ' + c.args : ''}\` \u2014 ${c.help}`).join('\n') +
                       '\n\nType `@` to point at a file in your project or Lucy folder.', false, [], null);
            return true;
        }
    }
    return false;
}
function deskHidePop() { const p = document.getElementById('deskPop'); if (p) p.hidden = true; desk.popKind = ''; desk.popItems = []; }
function deskShowPop(kind, items) {
    const pop = document.getElementById('deskPop');
    if (!pop || !items.length) { deskHidePop(); return; }
    desk.popKind = kind; desk.popItems = items; desk.popIdx = 0;
    pop.innerHTML = '';
    items.forEach((it, i) => {
        const b = deskEl('button', 'desk-pop-item' + (i === 0 ? ' sel' : ''));
        b.type = 'button';
        b.append(deskEl('span', 'desk-pop-main', kind === 'cmd' ? '/' + it.name + (it.args ? ' ' + it.args : '') : (it.dir ? '\uD83D\uDCC1 ' : '') + it.rel),
                 deskEl('span', 'desk-pop-help', kind === 'cmd' ? it.help : (it.size != null ? formatFileSize(it.size) : '')));
        b.onmousedown = (e) => { e.preventDefault(); deskPick(i); };
        pop.appendChild(b);
    });
    pop.hidden = false;
}
function deskMovePop(d) {
    const kids = [...document.querySelectorAll('#deskPop .desk-pop-item')];
    if (!kids.length) return;
    kids[desk.popIdx].classList.remove('sel');
    desk.popIdx = (desk.popIdx + d + kids.length) % kids.length;
    kids[desk.popIdx].classList.add('sel');
    kids[desk.popIdx].scrollIntoView({block: 'nearest'});
}
function deskPick(i) {
    const it = desk.popItems[i];
    if (!it) return;
    if (desk.popKind === 'cmd') {
        deskHidePop();
        if (it.args) { messageInput.value = '/' + it.name + ' '; messageInput.focus(); autosizeInput(); updateSendButton(); }
        else { messageInput.value = '/' + it.name; sendMessage(); }
    } else {
        const pos = messageInput.selectionStart, v = messageInput.value;
        const before = v.slice(0, pos).replace(/@"?[^\s"]*$/, '');
        const ref = /\s/.test(it.rel) ? `@"${it.rel}" ` : `@${it.rel}${it.dir ? '/' : ''} `;
        messageInput.value = before + ref + v.slice(pos);
        const np = (before + ref).length;
        messageInput.setSelectionRange(np, np);
        deskHidePop(); autosizeInput(); updateSendButton(); messageInput.focus();
    }
}
function deskPopInput() {
    const v = messageInput.value, pos = messageInput.selectionStart || 0, before = v.slice(0, pos);
    const sm = v.match(/^\/([A-Za-z]*)$/);
    if (sm && desk.commands) { deskShowPop('cmd', desk.commands.filter(c => c.name.startsWith(sm[1].toLowerCase()))); return; }
    const fm = before.match(/(?:^|\s)@"?([^\s"]*)$/);
    if (fm) {
        clearTimeout(desk.fileTimer);
        desk.fileTimer = setTimeout(async () => {
            try {
                const q = new URLSearchParams({q: fm[1], limit: 10});
                if (currentSessionId) q.set('session_id', currentSessionId);
                const d = await deskApi('GET', '/api/desk/files?' + q.toString());
                deskShowPop('file', d.files || []);
            } catch (e) { deskHidePop(); }
        }, 150);
        return;
    }
    deskHidePop();
}

// ---------------------------------------------------------------- 4. usage meter + model / thinking, 5. plan mode
function deskApplyLlm(st) {
    desk.llm = st;
    const sel = document.getElementById('deskModel');
    if (sel) { sel.innerHTML = st.models.map(m => `<option value="${m.id}">${escapeHtml(m.label)}</option>`).join(''); sel.value = st.active; }
    const t = document.getElementById('deskThink');
    if (t) t.classList.toggle('on', !!st.reasoning);
}
async function deskRefreshMeter() {
    const box = document.getElementById('deskMeter');
    if (!box) return;
    if (!currentSessionId) { box.hidden = true; return; }
    try {
        const u = await deskApi('GET', '/api/desk/usage?session_id=' + encodeURIComponent(currentSessionId));
        const h = n => n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n);
        box.hidden = false;
        box.className = 'desk-meter ' + u.level;
        document.getElementById('deskMeterFill').style.width = u.pct + '%';
        document.getElementById('deskMeterText').textContent = `${h(u.tokens)} / ${h(u.limit)}`;
        box.title = `About ${u.pct}% of the model's memory for this chat is used (estimate). Start a new chat or branch when it is nearly full.`;
    } catch (e) { box.hidden = true; }
}
function deskSetPlan(on) {
    desk.plan = !!on;
    const b = document.getElementById('deskPlan');
    if (b) b.classList.toggle('on', desk.plan);
    messageInput.placeholder = desk.plan ? 'Describe the task: Lucy will show a plan first...' : 'Type your message...';
}
function deskAfterTurn(bubble, opts, fullResponse) {
    if (opts && opts.mode === 'plan' && (fullResponse || '').trim()) {
        const card = deskEl('div', 'plan-card');
        card.append(deskEl('div', 'plan-card-title', 'Plan ready \u2014 nothing has been changed yet.'));
        const ok = deskEl('button', 'approval-btn ok', 'Approve & run');
        const edit = deskEl('button', 'approval-btn', 'Change it\u2026');
        ok.onclick = () => { card.remove(); deskSend(PLAN_APPROVE_TEXT); };
        edit.onclick = () => { card.remove(); messageInput.placeholder = 'What should change in the plan?'; messageInput.focus(); desk.plan = false; };
        card.append(ok, edit);
        getFileGroup(bubble).appendChild(card);
        scrollToBottom();
    }
    deskRefreshActions();
    deskRefreshMeter();
}

// ---------------------------------------------------------------- 6. rewind
async function deskRewindOpen() {
    if (!currentSessionId) { alert('Open a chat first.'); return; }
    let cps = [];
    try { cps = (await deskApi('GET', `/api/desk/sessions/${encodeURIComponent(currentSessionId)}/checkpoints`)).checkpoints; }
    catch (e) { alert(e.message); return; }
    const {body, close} = deskOverlay('Rewind');
    body.appendChild(deskEl('div', 'desk-hint', 'Go back to just before one of your messages. Files Lucy changed with her file tools are put back too. Changes made by shell commands cannot be undone.'));
    if (!cps.length) body.appendChild(deskEl('div', 'desk-hint', 'No rewind points in this chat yet.'));
    cps.forEach(cp => {
        const r = deskEl('div', 'desk-list-row desk-rewind-row');
        const info = deskEl('div', 'desk-rewind-info');
        info.append(deskEl('div', 'desk-rewind-text', cp.preview || '(message)'),
                    deskEl('div', 'desk-hint', `${formatTimestamp(cp.created_at) || ''} \u00B7 ${cp.files.length} file(s) changed`));
        const go = async (files, conv) => {
            const what = (files && conv) ? 'the chat and the files' : (files ? 'the files only' : 'the chat only');
            if (!confirm(`Rewind ${what} to before this message?`)) return;
            try {
                const res = await deskApi('POST', `/api/desk/sessions/${encodeURIComponent(currentSessionId)}/rewind`, {checkpoint_id: cp.id, files, conversation: conv});
                close();
                if (conv) { await switchSession(currentSessionId, true); messageInput.value = res.text || ''; autosizeInput(); updateSendButton(); }
                if (res.restored.length || res.failed.length) alert(`Restored ${res.restored.length} file(s).` + (res.failed.length ? '\nCould not restore:\n' + res.failed.join('\n') : ''));
                deskRefreshMeter();
            } catch (e) { alert(e.message); }
        };
        const both = deskEl('button', 'settings-row-btn', 'Rewind both'); both.onclick = () => go(true, true);
        const fo = deskEl('button', 'settings-row-btn', 'Files only'); fo.onclick = () => go(true, false);
        fo.disabled = !cp.files.length;
        const co = deskEl('button', 'settings-row-btn', 'Chat only'); co.onclick = () => go(false, true);
        r.append(info, both, fo, co);
        body.appendChild(r);
    });
}

// ---------------------------------------------------------------- 9. Library pins / cap / cleanup / side pane
function deskDecorateCard(card, item) {
    const foot = card.querySelector('.lib-foot');
    if (!foot) return;
    if (item.pinned) card.classList.add('pinned');
    const pin = deskEl('button', 'lib-act' + (item.pinned ? ' on' : ''), item.pinned ? '\u2605' : '\u2606');
    pin.type = 'button'; pin.title = item.pinned ? 'Unpin (it can be cleaned up again)' : 'Pin (never cleaned up)';
    pin.onclick = async (e) => { e.stopPropagation(); try { await deskApi('POST', `/api/library/${item.sha}/pin`, {pinned: !item.pinned}); } catch (er) { alert(er.message); } loadLibrary(true); deskLibStats(); };
    const side = deskEl('button', 'lib-act', '\u25E7');
    side.type = 'button'; side.title = 'Open beside the chat';
    side.onclick = (e) => { e.stopPropagation(); deskOpenSide(item); };
    foot.insertBefore(side, foot.querySelector('a.lib-act'));
    foot.insertBefore(pin, side);
}
async function deskLibStats() {
    const el = document.getElementById('deskLibInfo');
    if (!el) return;
    try {
        const s = await deskApi('GET', '/api/library/stats');
        const cap = s.cap_bytes ? formatFileSize(s.cap_bytes) : 'no cap';
        el.textContent = `Using ${formatFileSize(s.bytes)} of ${cap} \u00B7 ${s.pinned} pinned`;
    } catch (e) { el.textContent = ''; }
}
function deskLibBar() {
    if (document.getElementById('deskLibBar')) return;
    const bar = deskEl('div', 'desk-libbar'); bar.id = 'deskLibBar';
    const info = deskEl('span', 'desk-hint'); info.id = 'deskLibInfo';
    const lab = deskEl('label', 'lib-scope'); const cb = deskEl('input'); cb.type = 'checkbox';
    cb.onchange = () => { window.deskLibPinned = cb.checked; loadLibrary(true); };
    lab.append(cb, deskEl('span', '', 'Pinned only'));
    const cap = deskEl('button', 'settings-row-btn', 'Size cap\u2026');
    cap.onclick = async () => {
        const v = prompt('Maximum cache size in MB (0 = no limit). Oldest unpinned files are removed when it is full:', '2048');
        if (v === null) return;
        try { const r = await deskApi('POST', '/api/library/cap', {cap_mb: Number(v)}); if (r.removed) alert(`Removed ${r.removed} old file(s).`); } catch (e) { alert(e.message); }
        loadLibrary(true); deskLibStats();
    };
    const clean = deskEl('button', 'settings-row-btn', 'Clean up\u2026');
    clean.onclick = async () => {
        const v = prompt('Remove unpinned files not used for how many days? (0 = all unpinned files)', '30');
        if (v === null) return;
        try {
            const days = Math.max(0, Number(v) || 0);
            const pv = await deskApi('GET', '/api/library/cleanup/preview?older_than_days=' + days);
            if (!pv.files) { alert('Nothing to clean up.'); return; }
            if (!confirm(`Remove ${pv.files} file(s), ${formatFileSize(pv.bytes)}? Chats will show them as removed. Pinned files stay.`)) return;
            await deskApi('POST', '/api/library/cleanup', {older_than_days: days});
        } catch (e) { alert(e.message); }
        loadLibrary(true); deskLibStats();
    };
    bar.append(info, lab, cap, clean);
    const tabs = document.getElementById('libTabs');
    tabs.parentNode.insertBefore(bar, tabs.nextSibling);
}
async function deskOpenSide(item) {
    closeLibrary();
    const pane = document.getElementById('sidePane'), body = document.getElementById('sidePaneBody');
    document.getElementById('sidePaneName').textContent = item.name;
    const url = libraryFileUrl(item.sha);
    document.getElementById('sidePaneDl').href = url.replace('/file/', '/file/') + '&download=1';
    body.innerHTML = '';
    if (item.kind === 'images') { const i = deskEl('img', 'side-img'); i.src = url; body.appendChild(i); }
    else if (/\.pdf$/i.test(item.name)) { const f = deskEl('iframe', 'side-frame'); f.src = url; body.appendChild(f); }
    else if (isTextFile(item.name)) {
        try { const r = await fetchTextFile(url); const pre = deskEl('pre', 'side-text'); pre.textContent = r.binary ? '(binary file)' : r.text; body.appendChild(pre); }
        catch (e) { body.appendChild(deskEl('div', 'desk-hint', 'Could not load the file.')); }
    } else body.appendChild(deskEl('div', 'desk-hint', 'No preview for this file type. Use Download.'));
    pane.hidden = false;
    document.querySelector('.main-chat').classList.add('has-side');
}
function deskCloseSide() {
    document.getElementById('sidePane').hidden = true;
    document.querySelector('.main-chat').classList.remove('has-side');
}

// ---------------------------------------------------------------- 10. settings overview
async function deskLoadOverview() {
    const box = document.getElementById('overviewBody');
    if (!box) return;
    box.textContent = 'Loading...';
    let o;
    try { o = await deskApi('GET', '/api/desk/overview'); } catch (e) { box.textContent = 'Could not load: ' + e.message; return; }
    box.innerHTML = '';
    const card = (title, lines, tab) => {
        const c = deskEl('div', 'ov-card');
        const h = deskEl('div', 'ov-head'); h.append(deskEl('strong', '', title));
        if (tab) { const b = deskEl('button', 'settings-row-btn', 'Open'); b.onclick = () => switchSettingsTab(tab); h.appendChild(b); }
        c.appendChild(h);
        (lines.length ? lines : ['Nothing here yet.']).forEach(l => c.appendChild(deskEl('div', 'desk-hint', l)));
        box.appendChild(c);
    };
    const p = o.permission || {};
    card('Permissions', [`Mode: ${p.label || p.mode || '?'}`, p.paused ? 'Agent is PAUSED' : 'Agent is running'], 'safety');
    card('Connectors', (o.connectors || []).map(s => `${s.name}: ${s.error ? 'error' : !s.enabled ? 'off' : s.running ? 'running' : 'ready'}${s.tools ? ` (${s.tools} tools)` : ''}`), 'connectors');
    const t = o.tasks || {};
    card('Tasks', [t.enabled === false ? 'Background tasks are off' : `${t.active || 0} active, ${t.parked || 0} waiting for your approval, ${t.waiting_ok || 0} waiting for OK`], 'tasks');
    const l = o.library || {};
    card('Library', [`${l.files || 0} files, ${formatFileSize(l.bytes || 0)}${l.cap_bytes ? ' of ' + formatFileSize(l.cap_bytes) : ''}, ${l.pinned || 0} pinned`]);
    card('Projects', [`${o.projects || 0} project(s)`]);
}

// ---------------------------------------------------------------- wiring
(function deskInit() {
    const _apply = _applyLlmStatus;
    _applyLlmStatus = function (st) { _apply(st); try { deskApplyLlm(st); } catch (e) { /* bar not present */ } };
    const _card = libCard;
    libCard = function (item) { const c = _card(item); try { deskDecorateCard(c, item); } catch (e) { /* keep the plain card */ } return c; };
    const _openLib = openLibrary;
    openLibrary = function () { _openLib(); deskLibBar(); deskLibStats(); };
    window.deskOnSwitch = function () { deskUpdateChip(); deskRefreshActions(); deskRefreshMeter(); };

    const on = (id, fn) => { const e = document.getElementById(id); if (e) e.addEventListener('click', fn); };
    on('deskNewProject', () => deskProjectModal(null));
    on('deskPlan', () => deskSetPlan(!desk.plan));
    on('deskRewind', () => deskRewindOpen());
    on('deskThink', () => {
        if (!desk.llm) return;
        if (confirm('Changing Thinking reloads the model (takes a moment). Continue?')) saveLlmSetting('reasoning', desk.llm.reasoning ? 'off' : 'on');
    });
    const sel = document.getElementById('deskModel');
    if (sel) sel.addEventListener('change', () => saveLlmSetting('model', sel.value));
    on('sidePaneClose', deskCloseSide);

    messageInput.addEventListener('input', deskPopInput);
    messageInput.addEventListener('blur', () => setTimeout(deskHidePop, 150));
    messageInput.addEventListener('keydown', (e) => {                    // capture: runs before the Enter-to-send handler
        const pop = document.getElementById('deskPop');
        if (!pop || pop.hidden) return;
        if (e.key === 'ArrowDown') { e.preventDefault(); deskMovePop(1); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); deskMovePop(-1); }
        else if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); e.stopImmediatePropagation(); deskPick(desk.popIdx); }
        else if (e.key === 'Escape') { e.preventDefault(); deskHidePop(); }
    }, true);

    deskApi('GET', '/api/desk/commands').then(d => { desk.commands = d.commands; }).catch(() => {});
    deskLoadProjects();
    loadLlmStatus();
    deskRefreshMeter();

    const q = new URLSearchParams(location.search).get('q');           // quick entry (Ctrl+Alt+Space) opens /?q=text
    if (q) {
        history.replaceState(null, '', location.pathname);
        setTimeout(() => { newSession(); messageInput.value = q; autosizeInput(); updateSendButton(); sendMessage(); }, 500);
    }
})();
