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
function renderMarkdown(text) {
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
    return marked.parse(text);
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
let currentSessionId = 'default';
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
            Object.assign(sessions, data.sessions || {});
            renderHistory();
        }
    } catch (e) {
        console.error('Failed to load sessions:', e);
    }
}

async function newSession() {
    try {
        const resp = await fetch('/api/sessions', {method: 'POST', headers: authHeaders()});
        if (resp.ok) {
            const data = await resp.json();
            currentSessionId = data.session_id;
            sessions[currentSessionId] = {
                title: 'New Session',
                message_count: 0,
                messages: [],
                updated_at: data.updated_at || new Date().toISOString().replace('T', ' ').slice(0, 19)
            };
        } else {
            throw new Error('Failed to create session');
        }
    } catch (e) {
        console.error('Failed to create session:', e);
        currentSessionId = 'session_' + Date.now();
        sessions[currentSessionId] = {
            title: 'New Session',
            message_count: 0,
            messages: [],
            updated_at: new Date().toISOString().replace('T', ' ').slice(0, 19)
        };
    }
    chatContainer.innerHTML = '';
    renderHistory();
    messageInput.focus();
    if (mobileQuery.matches) closeMobileSidebar();
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
    const options = {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: true};
    const tzSelect = document.getElementById('timezoneSelect');
    let tz = 'en-US';
    if (tzSelect) {
        const selectedText = tzSelect.options[tzSelect.selectedIndex]?.text || '';
        const match = selectedText.match(/GMT([+-])(\d+)/);
        if (match) {
            tz = match[1] + match[2].padStart(2, '0');
        }
    }
    try {
        return d.toLocaleString(navigator.language || 'en-US', {...options, timeZone: tz});
    } catch {
        return d.toLocaleString(navigator.language || 'en-US', options);
    }
}

async function renderHistory() {
    const allSessions = [];
    for (const [id, session] of Object.entries(sessions)) {
        allSessions.push({id, ...session});
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
        const title = session.title || 'New Session';
        const timeDisplay = formatTimestamp(session.updated_at) || '';
        item.innerHTML = `
            <div class="history-title">${title}</div>
            <div class="history-meta">
                ${timeDisplay ? `<span class="history-time">${timeDisplay}</span>` : ``}
                <div class="delete-session-btn" title="Delete session" onclick="deleteSession('${session.id}', event)">🗑</div>
            </div>
        `;
        chatHistoryEl.appendChild(item);
    });
}

async function switchSession(id) {
    if (id === currentSessionId) return;
    currentSessionId = id;
    try {
        const resp = await fetch(`/api/sessions/${id}`, {headers: authHeaders()});
        if (resp.ok) {
            const data = await resp.json();
            const session = sessions[id] || {title: 'Session', messages: []};
            session.messages = data.messages || [];
            sessions[id] = session;
        }
    } catch (e) {
        console.error('Failed to load session:', e);
    }

    const session = sessions[id];
    chatContainer.innerHTML = '';

    if (session && session.messages) {
        session.messages.forEach(msg => {
            const content = msg.content || '';
            const mediaMatches = (content.match(/MEDIA:[^\s]+/g) || []).filter(m => !/\.pcm$/i.test(m));
            const cleanContent = content.replace(/\nMEDIA:[^\s]+/g, '').trim();
            if (mediaMatches.length) {
                const fileData = mediaMatches.map(m => {
                    const mediaPath = m.replace('MEDIA:', '');
                    const relativePath = pathToRelative(mediaPath);
                    const fileName = mediaPath.split(/[\\/]/).pop();
                    const mimeType = getMimeType(fileName);
                    return {path: mediaPath, name: fileName, url: `/api/file/${encodeURIComponent(relativePath)}?api_key=${encodeURIComponent(API_KEY)}`, type: mimeType};
                });
                addMessage(cleanContent, msg.role === 'user', fileData, msg.timestamp);
            } else {
                addMessage(cleanContent, msg.role === 'user', [], msg.timestamp);
            }
        });
    }

    scrollToBottom();
    messageInput.focus();
    renderHistory();
    if (mobileQuery.matches) closeMobileSidebar();
}

async function deleteSession(id, event) {
    event.stopPropagation();
    if (!confirm('Delete this session?')) return;
    try {
        const resp = await fetch(`/api/sessions/${id}`, {
            method: 'DELETE',
            headers: authHeaders()
        });
        if (resp.ok) {
            delete sessions[id];
            if (currentSessionId === id) {
                const newResp = await fetch('/api/sessions', {headers: authHeaders()});
                const data = newResp.json ? await newResp.json() : {sessions: {}};
                const sessionList = data.sessions || {};
                const firstId = Object.keys(sessionList)[0];
                currentSessionId = firstId || 'default';
                chatContainer.innerHTML = '';
            }
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
        bubble.innerHTML = renderMarkdown(text);
        enhanceCodeBlocks(bubble);
    }
    contentWrapper.appendChild(bubble);
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
            const fileEl = renderFilePreview(f);
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
        img.onclick = () => window.open(img.src, '_blank');
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
            if (downloadLink.href === '#') e.preventDefault();
        };

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
function updateSendButton() {
    const hasInput = messageInput.value.trim().length > 0 || selectedFiles.length > 0;
    sendButton.disabled = false;
    sendButton.classList.toggle('stop-mode', isStreaming && !hasInput);
    if (isStreaming && !hasInput) {
        sendButton.textContent = 'Stop';
        sendButton.title = 'Stop the current reply (Esc)';
    } else if (isStreaming) {
        sendButton.textContent = 'Queue';
        sendButton.title = 'Send this after the current reply finishes';
    } else {
        sendButton.textContent = 'Send';
        sendButton.title = '';
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
    const fromQueue = !!queued;
    const message = fromQueue ? queued.message : messageInput.value.trim();
    const sendFiles = fromQueue ? queued.files : selectedFiles;
    if (!message && sendFiles.length === 0) return;

    if (isStreaming && !fromQueue) {
        // A reply is still running: park this message instead of dropping it.
        enqueueMessage(message, [...selectedFiles]);
        messageInput.value = '';
        selectedFiles = [];
        filePreviewContainer.innerHTML = '';
        updateSendButton();
        return;
    }

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
        path: f._savedPath || null
    })).filter(f => f);

    const now = new Date().toISOString().replace('T', ' ').slice(0, 19);
    addMessage(message || '(file)', true, outgoingFiles, now);
    const filesToSend = [...sendFiles];
    sendFiles.forEach(f => delete f._dataUrl);
    if (!fromQueue) {
        // (a queued send must not wipe what the user is typing right now)
        selectedFiles = [];
        filePreviewContainer.innerHTML = '';
        messageInput.value = '';
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
    const voiceMode = voiceToggle && voiceToggle.checked;

    try {
        const formData = new FormData();
formData.append('message', message);
formData.append('session_id', currentSessionId);
formData.append('stream', 'true');
formData.append('voice_mode', voiceMode ? 'true' : 'false');
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
            if (done) break;

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

        if (!sessions[currentSessionId]) {
            sessions[currentSessionId] = {title: 'New Session', message_count: 0, messages: [], updated_at: now};
        }
        sessions[currentSessionId].messages.push({role: 'user', content: message, timestamp: now});
        const assistantResponse = fullResponse + (pendingMedia.length > 0 ? '\n' + pendingMedia.join('\n') : '');
        sessions[currentSessionId].messages.push({role: 'assistant', content: assistantResponse, timestamp: now});
        sessions[currentSessionId].updated_at = now;
        renderHistory();
        // Re-fetch session title after async backend title generation completes
        setTimeout(() => {
            fetch(`/api/sessions/${currentSessionId}`, {headers: authHeaders()})
                .then(r => r.ok ? r.json() : null)
                .then(data => {
                    if (data && data.session_id && sessions[currentSessionId]) {
                        sessions[currentSessionId].title = data.title || sessions[currentSessionId].title;
                        sessions[currentSessionId].messages = data.messages || sessions[currentSessionId].messages;
                        renderHistory();
                    }
                })
                .catch(() => {});
        }, 2000);

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
        currentAbort = null;
        setSending(false);
        messageInput.focus();
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

async function loadSettings() {
    try {
        const res = await fetch(`/api/settings/timezone?api_key=${API_KEY}`);
        const data = await res.json();
        const tzSelect = document.getElementById('timezoneSelect');
        if (tzSelect) {
            tzSelect.innerHTML = data.available_timezones.map(opt =>
                `<option value="${opt.value}" ${opt.value === data.timezone ? 'selected' : ''}>${opt.label}</option>`
            ).join('');
        }
    } catch (e) {
        console.error('Failed to load settings:', e);
    }
}

function saveSettings() {
    const tz = document.getElementById('timezoneSelect').value;
    fetch(`/api/settings/timezone?api_key=${API_KEY}`, {
        method: 'POST',
        body: JSON.stringify({timezone: tz}),
        headers: {'Content-Type': 'application/json'}
    });
}

// ---- Autosave (fires on every field change, no Save button needed) ----
const AUTOSAVE_FIELDS = [
    {id: 'themeSelect', key: 'theme'},
    {id: 'fontSelect', key: 'font'},
    {id: 'fontSizeSelect', key: 'font_size'},
    {id: 'languageSelect', key: 'language'},
    {id: 'timezoneSelect', key: 'timezone'},
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
// Dark = scarlet + silver on black. Light = cerulean + gold on white. The choice is
// remembered in this browser (localStorage); an inline script in chat.html applies it
// before the page paints so there is no flash of the wrong theme.
const THEME_KEY = 'lucy.theme';
const THEME_BAR_COLOR = {dark: '#000000', light: '#ffffff'};   // browser/phone address bar

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
    } else if (key === 'timezone') {
        // Already wired to a real endpoint
        saveSettings();
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
        } else if (active && active.ready) {
            label.textContent = `${active.label} is loaded on port ${active.port}`;
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
    loadSettings();
    loadLlmStatus();
    loadSkills();
    loadConnectors();
}

function closeSettings() {
    settingsOverlay.style.display = 'none';
}

// ---- Settings Tabs ----
function switchSettingsTab(tab) {
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

// ---- The full viewer (opened from a card) ----
let fileViewerEl = null;

function ensureFileViewer() {
    if (fileViewerEl) return fileViewerEl;
    const overlay = document.createElement('div');
    overlay.className = 'fv-overlay';
    overlay.id = 'fileViewer';
    overlay.innerHTML =
        '<div class="fv-modal" role="dialog" aria-modal="true" aria-label="File viewer">' +
            '<div class="fv-head">' +
                '<span class="file-card-icon fv-badge"></span>' +
                '<div class="file-card-title"><span class="file-card-name fv-name"></span><span class="file-card-meta fv-meta"></span></div>' +
                '<div class="file-card-actions">' +
                    '<button type="button" class="fc-btn" data-act="copy" title="Copy the whole file">Copy</button>' +
                    '<a class="fc-btn" data-act="download" title="Download the file">Download</a>' +
                    '<button type="button" class="fc-btn" data-act="close" title="Close (Esc)">\u2715</button>' +
                '</div>' +
            '</div>' +
            '<div class="fv-body"></div>' +
        '</div>';
    overlay.addEventListener('click', (e) => { if (e.target === overlay) closeFileViewer(); });
    overlay.querySelector('[data-act="close"]').addEventListener('click', closeFileViewer);
    document.body.appendChild(overlay);
    fileViewerEl = overlay;
    return overlay;
}

function isFileViewerOpen() { return !!fileViewerEl && fileViewerEl.style.display === 'flex'; }

function openFileViewer(file) {
    const el = ensureFileViewer();
    el.querySelector('.fv-badge').textContent = fileBadge(file.name);
    el.querySelector('.fv-name').textContent = file.name;
    el.querySelector('.fv-meta').textContent = fileMetaText(file);
    const dl = el.querySelector('[data-act="download"]');
    dl.href = file.url;
    dl.download = file.name;
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
    body.innerHTML = '';
    body.appendChild(buildCodeView(file.lines, file.info.lang));
    if (file.truncated) {
        const note = document.createElement('div');
        note.className = 'file-card-note';
        note.textContent = `Only the first ${formatFileSize(TEXT_PREVIEW_LIMIT)} is shown. Use Download for the whole file.`;
        body.appendChild(note);
    }
    el.style.display = 'flex';
    body.scrollTop = 0;
    el.querySelector('[data-act="close"]').focus();
}

function closeFileViewer() {
    if (!fileViewerEl) return;
    fileViewerEl.style.display = 'none';
    fileViewerEl.querySelector('.fv-body').innerHTML = '';   // free the DOM of big files
}

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
    greetingTime.textContent = formatTime(new Date().toISOString()) || '';
}
loadSettings();
loadSessions();
loadVoiceStatus();
bindAutosave();

messageInput.addEventListener('keypress', (e) => {
    // Enter while a reply is running queues the message (sendMessage handles it)
    if (e.key === 'Enter') {
        sendMessage();
    }
});
messageInput.addEventListener('input', updateSendButton);

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
