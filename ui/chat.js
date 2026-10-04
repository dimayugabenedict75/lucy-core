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

function parseMediaMarkers(text) {
    if (!text) return '';
    // Replace MEDIA:path tokens (optionally followed by a caption) with <img> tags.
    // Handles both "MEDIA:path caption" and standalone "MEDIA:path" on its own line.
    return text.replace(/MEDIA:([^\s\n]+)(?:\s+(.+?))?(?=\n|$)/g, function(match, mediaPath, caption) {
        const fileName = mediaPath.split(/[\\/]/).pop();
        const fileUrl = `/api/file/${encodeURIComponent(mediaPath)}?api_key=${encodeURIComponent(API_KEY)}`;
        const mimeType = getMimeType(fileName);
        let html = `<img src="${fileUrl}" alt="${fileName}" type="${mimeType}" style="max-width:200px;max-height:200px;border-radius:12px;border:1px solid #222;margin-top:8px;display:block;"`;
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
            const mediaMatches = content.match(/MEDIA:[^\s]+/g);
            const cleanContent = content.replace(/\nMEDIA:[^\s]+/g, '').trim();
            if (mediaMatches) {
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

    const bubble = document.createElement('div');
    bubble.className = `message-bubble ${isUser ? 'user-bubble' : 'assistant-bubble'}`;
    bubble.innerHTML = renderMarkdown(text);
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
        img.style.border = '1px solid #222';
        img.onclick = () => window.open(img.src, '_blank');
        img.style.cursor = 'pointer';
        wrapper.appendChild(img);

        const nameDiv = document.createElement('div');
        nameDiv.textContent = fileData.name || 'Image';
        nameDiv.style.fontSize = '11px';
        nameDiv.style.color = '#666';
        nameDiv.style.marginTop = '4px';
        wrapper.appendChild(nameDiv);
    } else {
        const icon = getFileTypeIcon(fileData.name || fileData.path || '');
        const el = document.createElement('div');
        el.style.display = 'flex';
        el.style.alignItems = 'center';
        el.style.gap = '6px';
        el.style.padding = '8px 12px';
        el.style.background = '#0c0c0c';
        el.style.border = '1px solid #222';
        el.style.borderRadius = '8px';

        const iconSpan = document.createElement('span');
        iconSpan.textContent = icon;
        iconSpan.style.fontSize = '16px';

        const nameSpan = document.createElement('span');
        nameSpan.textContent = (fileData.name || fileData.path || 'file');
        nameSpan.style.fontSize = '12px';
        nameSpan.style.color = '#aaa';
        nameSpan.style.maxWidth = '180px';
        nameSpan.style.overflow = 'hidden';
        nameSpan.style.textOverflow = 'ellipsis';
        nameSpan.style.whiteSpace = 'nowrap';

        const downloadLink = document.createElement('a');
        downloadLink.href = fileData.url || fileData.path || '#';
        downloadLink.download = fileData.name || '';
        downloadLink.textContent = '\u2193';
        downloadLink.style.color = '#00E5FF';
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
            sizeDiv.style.color = '#666';
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
        const fileNote = item.files.length ? ` [+${item.files.length} file${item.files.length > 1 ? 's' : ''}]` : '';
        label.textContent = `${idx + 1}. ${item.message || '(file)'}${fileNote}`;
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
                            const fileData = {path: mediaPath, name: fileName, url: fileUrl, type: mimeType};
                            pendingMedia.push(`MEDIA:${mediaPath}`);

                            // Render the media attachment immediately in the streaming bubble
                            const img = document.createElement('img');
                            img.src = fileUrl;
                            img.alt = fileName;
                            img.style.maxWidth = '200px';
                            img.style.maxHeight = '200px';
                            img.style.borderRadius = '12px';
                            img.style.border = '1px solid #222';
                            img.style.marginTop = '8px';
                            img.style.display = 'block';
                            streamingBubble.appendChild(img);

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
    {id: 'ttsSelect', key: 'voice'},
];

function bindAutosave() {
    AUTOSAVE_FIELDS.forEach(({id, key}) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.addEventListener('change', () => saveSetting(key, el.value));
    });
}

function saveSetting(key, value) {
    if (key === 'timezone') {
        // Already wired to a real endpoint
        saveSettings();
    } else {
        // TODO: replace with a real config endpoint once it exists, e.g.
        // fetch(`/api/settings/${key}?api_key=${API_KEY}`, {
        //     method: 'POST',
        //     headers: { 'Content-Type': 'application/json' },
        //     body: JSON.stringify({ value })
        // });
        console.log('Autosave (placeholder):', key, '=', value);
    }
    flashSaved();
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
        setStatus(data.status || 'restarting', '#ffa500');
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
        setStatus('Server stopped', '#ff4444');
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
        if (settingsOverlay.style.display === 'flex') {
            closeSettings();
        } else if (isStreaming) {
            stopGeneration();
        }
    }
});
