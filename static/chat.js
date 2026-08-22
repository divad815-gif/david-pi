(() => {
  'use strict';

  const csrf = document.body.dataset.csrf;
  const initial = document.body.dataset.conversation;
  const $ = (id) => document.getElementById(id);
  const state = {
    active: initial || '',
    after: 0,
    before: 0,
    poll: null,
    polling: false,
    attachmentId: null,
    extraFiles: [],
    sending: false,
    pendingClientId: null,
    conversations: [],
    pendingDeleteId: '',
    pendingDeleteName: '',
  };
  const headers = { 'X-CSRF-Token': csrf, 'Content-Type': 'application/json' };

  const escape = (value) => String(value ?? '').replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[character]);

  async function json(url, options = {}) {
    const response = await fetch(url, { credentials: 'same-origin', ...options });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || 'David-Pi could not complete that request.');
    return data;
  }

  function relative(value) {
    if (!value) return '';
    const date = new Date(value);
    const seconds = (Date.now() - date) / 1000;
    if (seconds < 60) return 'now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
    return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
  }

  function selectorValue(value) {
    if (window.CSS && typeof window.CSS.escape === 'function') return window.CSS.escape(String(value));
    return String(value).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
  }

  function clientMessageId() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    return [...bytes].map((item, index) => `${index === 4 || index === 6 || index === 8 || index === 10 ? '-' : ''}${item.toString(16).padStart(2, '0')}`).join('');
  }

  function setComposerStatus(message = '') {
    $('composerStatus').textContent = message;
    $('composerStatus').hidden = !message;
  }

  function isNearBottom() {
    const list = $('messageList');
    return list.scrollHeight - list.scrollTop - list.clientHeight < 90;
  }

  function scrollLatest(behavior = 'auto') {
    const list = $('messageList');
    list.scrollTo({ top: list.scrollHeight, behavior });
    $('jumpLatest').hidden = true;
  }

  function isInstalledWebApp() {
    const standalone = ['standalone', 'fullscreen', 'minimal-ui'].some((mode) =>
      window.matchMedia?.(`(display-mode: ${mode})`).matches
    ) || window.navigator.standalone === true;
    return standalone;
  }

  function syncViewport() {
    const height = Math.round(window.visualViewport?.height || window.innerHeight);
    document.documentElement.classList.toggle('installed-web-app', isInstalledWebApp());
    document.documentElement.style.setProperty('--chat-viewport-height', `${height}px`);
  }

  function observeComposer() {
    if (!window.ResizeObserver) return;
    new ResizeObserver((entries) => {
      const height = Math.ceil(entries[0]?.contentRect?.height || $('composer').offsetHeight || 70);
      document.documentElement.style.setProperty('--composer-height', `${height}px`);
    }).observe($('composer'));
  }

  async function loadConversations() {
    const data = await json('/api/chat/conversations');
    state.conversations = data.conversations;
    $('emptyConversations').hidden = data.conversations.length > 0;
    $('conversationList').innerHTML = data.conversations.map((conversation) => `
      <div class="conversation-card">
        <button class="conversation ${conversation.id === state.active ? 'active' : ''}" data-id="${escape(conversation.id)}">
          <span class="avatar">${escape(conversation.title.slice(0, 1).toUpperCase())}</span>
          <span class="conversation-copy"><strong>${escape(conversation.title)}</strong><span>${escape(conversation.preview)}</span></span>
          <span class="conversation-meta"><time>${relative(conversation.updated_at)}</time>${conversation.unread ? `<span class="badge">${conversation.unread}</span>` : ''}</span>
        </button>
        <button class="conversation-delete" type="button" data-delete-id="${escape(conversation.id)}" data-delete-name="${escape(conversation.title)}" aria-label="Delete chat with ${escape(conversation.title)}">Delete</button>
      </div>`).join('');
    document.querySelectorAll('.conversation').forEach((button) => {
      button.onclick = () => openThread(button.dataset.id);
    });
    document.querySelectorAll('.conversation-delete').forEach((button) => {
      button.onclick = (event) => {
        event.preventDefault();
        event.stopPropagation();
        openDeleteConversation(button.dataset.deleteId, button.dataset.deleteName);
      };
    });
    if (state.active && !document.querySelector(`[data-id="${selectorValue(state.active)}"]`)) state.active = '';
    return data.conversations;
  }

  async function openThread(id) {
    state.active = id;
    state.after = 0;
    state.before = 0;
    setComposerStatus();
    history.replaceState({}, '', `/chat/${id}`);
    $('conversationPane').classList.add('thread-open');
    $('messagePane').hidden = false;
    $('welcomePane').hidden = true;
    const conversation = state.conversations.find((item) => item.id === id);
    $('threadName').textContent = conversation?.title || 'Conversation';
    $('threadMembers').textContent = conversation?.members?.map((member) => member.display_name).join(' · ') || '';
    await loadMessages({ initial: true });
    startPolling();
    loadConversations().catch(() => {});
  }

  function messageHTML(message) {
    const photos = message.attachments.map((attachment) => `
      <button data-photo="${escape(attachment.id)}" data-original="${escape(attachment.download_url)}">
        <img loading="lazy" src="${escape(attachment.preview_url)}" alt="Shared photo">
      </button>`).join('');
    const body = message.deleted ? 'Message deleted' : escape(message.body).replace(/\n/g, '<br>');
    return `<article class="bubble ${message.mine ? 'mine' : ''} ${message.deleted ? 'deleted' : ''}"
        data-message="${message.id}" data-plain-body="${escape(message.body || '')}">
      ${!message.mine ? `<p class="bubble-name">${escape(message.sender_name)}</p>` : ''}
      <div class="bubble-body">${photos ? `<div class="chat-photos">${photos}</div>` : ''}${body}</div>
      <div class="bubble-meta">
        <time>${new Date(message.created_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</time>
        ${message.updated_at && !message.deleted ? '<span>edited</span>' : ''}
        ${message.mine && !message.deleted ? `<span>${escape(message.delivery)}</span><span class="bubble-actions"><button data-edit="${message.id}">Edit</button> <button data-delete="${message.id}">Delete</button></span>` : ''}
      </div>
    </article>`;
  }

  function wireMessages() {
    document.querySelectorAll('[data-photo]').forEach((button) => {
      button.onclick = () => showPhoto(button.dataset.photo, button.dataset.original);
    });
    document.querySelectorAll('[data-delete]').forEach((button) => {
      button.onclick = () => deleteMessage(button.dataset.delete);
    });
    document.querySelectorAll('[data-edit]').forEach((button) => {
      button.onclick = () => editMessage(button.dataset.edit);
    });
  }

  function showThreadEmpty() {
    if (!$('messageList').querySelector('.bubble')) {
      $('messageList').innerHTML = '<p class="thread-empty">No messages yet. Say hello below.</p>';
    } else {
      $('messageList').querySelector('.thread-empty')?.remove();
    }
  }

  async function loadMessages({ after = 0, initial = false, refresh = false } = {}) {
    if (!state.active) return;
    const list = $('messageList');
    const wasNearBottom = isNearBottom();
    const previousScroll = list.scrollTop;
    const query = after ? `?after=${after}&limit=100` : '?limit=50';
    const data = await json(`/api/chat/conversations/${state.active}/messages${query}`);

    if (initial || refresh) list.innerHTML = '';
    list.querySelector('.thread-empty')?.remove();
    let added = 0;
    data.messages.forEach((message) => {
      if (!document.querySelector(`[data-message="${message.id}"]`)) {
        list.insertAdjacentHTML('beforeend', messageHTML(message));
        added += 1;
      }
    });
    wireMessages();
    showThreadEmpty();

    if (data.messages.length) {
      state.after = Math.max(state.after, ...data.messages.map((message) => message.id));
      state.before = state.before || data.messages[0].id;
      markRead(state.after).catch(() => {});
    }
    if (initial) {
      $('olderMessages').hidden = data.messages.length < 50;
      requestAnimationFrame(() => scrollLatest());
    } else if (refresh) {
      list.scrollTop = previousScroll;
    } else if (added) {
      if (wasNearBottom) requestAnimationFrame(() => scrollLatest('smooth'));
      else $('jumpLatest').hidden = false;
    }
  }

  async function loadOlder() {
    if (!state.active || !state.before) return;
    const list = $('messageList');
    const oldHeight = list.scrollHeight;
    try {
      const data = await json(`/api/chat/conversations/${state.active}/messages?before=${state.before}&limit=50`);
      data.messages.slice().reverse().forEach((message) => {
        if (!document.querySelector(`[data-message="${message.id}"]`)) list.insertAdjacentHTML('afterbegin', messageHTML(message));
      });
      wireMessages();
      if (data.messages.length) state.before = Math.min(state.before, ...data.messages.map((message) => message.id));
      $('olderMessages').hidden = data.messages.length < 50;
      list.scrollTop = list.scrollHeight - oldHeight;
    } catch (error) {
      setComposerStatus(error.message);
    }
  }

  async function markRead(messageId) {
    return json(`/api/chat/conversations/${state.active}/read`, {
      method: 'POST', headers, body: JSON.stringify({ message_id: messageId }),
    });
  }

  function startPolling() {
    clearInterval(state.poll);
    state.poll = setInterval(async () => {
      if (document.visibilityState !== 'visible' || !state.active || state.polling) return;
      state.polling = true;
      try {
        await loadMessages({ after: state.after });
        await loadConversations();
      } catch (_error) {
        // Polling failures are transient; explicit user actions still surface errors.
      } finally {
        state.polling = false;
      }
    }, 2500);
  }

  async function send(event) {
    event.preventDefault();
    if (state.sending || !state.active) return;
    const body = $('messageBody').value;
    const files = [...$('photoInput').files, ...state.extraFiles];
    if (!body.trim() && !files.length) {
      setComposerStatus('Write a message or add a photo first.');
      return;
    }
    const form = new FormData();
    form.append('body', body);
    state.pendingClientId ||= clientMessageId();
    form.append('client_message_id', state.pendingClientId);
    files.forEach((file) => form.append('attachments', file));
    state.sending = true;
    $('composer').querySelector('.send').disabled = true;
    setComposerStatus();
    try {
      const data = await json(`/api/chat/conversations/${state.active}/messages`, {
        method: 'POST', headers: { 'X-CSRF-Token': csrf }, body: form,
      });
      $('messageBody').value = '';
      $('photoInput').value = '';
      state.extraFiles = [];
      state.pendingClientId = null;
      showAttachmentCount();
      $('messageList').querySelector('.thread-empty')?.remove();
      if (!document.querySelector(`[data-message="${data.message.id}"]`)) {
        $('messageList').insertAdjacentHTML('beforeend', messageHTML(data.message));
      }
      wireMessages();
      state.after = Math.max(state.after, data.message.id);
      scrollLatest('smooth');
      loadConversations().catch(() => {});
    } catch (error) {
      setComposerStatus(`${error.message} Your draft was kept; tap Send to retry.`);
    } finally {
      state.sending = false;
      $('composer').querySelector('.send').disabled = false;
    }
  }

  async function editMessage(id) {
    const article = document.querySelector(`[data-message="${id}"]`);
    const value = prompt('Edit message', article?.dataset.plainBody || '');
    if (value === null) return;
    try {
      await json(`/api/chat/messages/${id}`, { method: 'PATCH', headers, body: JSON.stringify({ body: value }) });
      await loadMessages({ refresh: true });
    } catch (error) {
      setComposerStatus(error.message);
    }
  }

  async function deleteMessage(id) {
    if (!confirm('Delete this message for everyone in the conversation?')) return;
    try {
      await json(`/api/chat/messages/${id}`, { method: 'DELETE', headers });
      await loadMessages({ refresh: true });
    } catch (error) {
      setComposerStatus(error.message);
    }
  }

  function showConversationList() {
    $('conversationPane').classList.remove('thread-open');
    $('messagePane').hidden = true;
    $('welcomePane').hidden = false;
    state.active = '';
    state.after = 0;
    state.before = 0;
    history.replaceState({}, '', '/chat');
    clearInterval(state.poll);
  }

  let lifecycleRefresh = null;
  async function refreshChatLifecycle() {
    if (lifecycleRefresh) return lifecycleRefresh;
    lifecycleRefresh = (async () => {
      const conversations = await loadConversations();
      const activeStillExists = state.active && conversations.some((item) => item.id === state.active);
      if (activeStillExists) {
        await loadMessages({ refresh: true });
        startPolling();
      } else if (conversations.length === 1) {
        await openThread(conversations[0].id);
      } else if (state.active) {
        showConversationList();
      }
      return conversations;
    })().finally(() => { lifecycleRefresh = null; });
    return lifecycleRefresh;
  }

  function resetDeleteConversation() {
    state.pendingDeleteId = '';
    state.pendingDeleteName = '';
    $('deleteChatPhrase').value = '';
    $('confirmDeleteChat').disabled = true;
    $('deleteChatError').textContent = '';
    $('deleteChatError').hidden = true;
  }

  function closeDeleteConversation() {
    if ($('deleteChatDialog').open) $('deleteChatDialog').close();
    resetDeleteConversation();
  }

  function openDeleteConversation(id, name) {
    if (!id) return;
    state.pendingDeleteId = id;
    state.pendingDeleteName = name || 'this conversation';
    $('deleteChatCopy').textContent = `This permanently removes ${state.pendingDeleteName} and every message for everyone. Leaving Chat never performs this action.`;
    $('deleteChatDialog').showModal();
    $('deleteChatPhrase').focus();
  }

  function setDeleteConversationState() {
    $('confirmDeleteChat').disabled = $('deleteChatPhrase').value.trim() !== 'DELETE CHAT';
  }

  async function deleteConversation(event) {
    event.preventDefault();
    const id = state.pendingDeleteId;
    if (!id || $('deleteChatPhrase').value.trim() !== 'DELETE CHAT') return;
    $('confirmDeleteChat').disabled = true;
    try {
      await json(`/api/chat/conversations/${id}`, {
        method: 'DELETE', headers, body: JSON.stringify({ confirmation: 'DELETE CHAT', conversation_id: id }),
      });
      closeDeleteConversation();
      showConversationList();
      $('messageList').innerHTML = '';
      await loadConversations();
    } catch (error) {
      $('deleteChatError').textContent = error.message;
      $('deleteChatError').hidden = false;
      setDeleteConversationState();
    }
  }

  function setNewChatState() {
    const count = document.querySelectorAll('#userChoices input:checked').length;
    $('createChat').disabled = count < 1;
    if (count) {
      $('newChatError').hidden = true;
      $('newChatError').textContent = '';
    }
  }

  function closeNewChat() {
    if ($('newChatDialog').open) $('newChatDialog').close();
    $('newChatForm').reset();
    $('newChatError').hidden = true;
    $('newChatError').textContent = '';
    $('createChat').disabled = true;
  }

  async function newDialog() {
    try {
      const data = await json('/api/chat/users');
      $('newChatForm').reset();
      $('newChatError').hidden = true;
      $('newChatError').textContent = '';
      $('userChoices').innerHTML = data.users.length ? data.users.map((user) => `
        <label class="user-choice"><input type="checkbox" value="${escape(user.owner_id)}"><strong>${escape(user.display_name)}</strong></label>`).join('') : '<p>No other verified David-Pi users are known yet. They must open the site once first.</p>';
      document.querySelectorAll('#userChoices input').forEach((input) => input.addEventListener('change', setNewChatState));
      setNewChatState();
      $('newChatDialog').showModal();
    } catch (_error) {
      $('emptyConversations').textContent = 'David-Pi could not load household members. Please try again.';
    }
  }

  async function createChat(event) {
    event.preventDefault();
    const memberIds = [...document.querySelectorAll('#userChoices input:checked')].map((input) => input.value);
    if (!memberIds.length) {
      $('newChatError').textContent = 'Choose at least one person, or tap Cancel to leave.';
      $('newChatError').hidden = false;
      $('createChat').disabled = true;
      return;
    }
    $('createChat').disabled = true;
    try {
      const data = await json('/api/chat/conversations', {
        method: 'POST', headers, body: JSON.stringify({ member_ids: memberIds, title: $('groupTitle').value }),
      });
      closeNewChat();
      await loadConversations();
      await openThread(data.id);
    } catch (_error) {
      $('newChatError').textContent = 'David-Pi could not create that conversation. Nothing was changed; you can retry or cancel.';
      $('newChatError').hidden = false;
      setNewChatState();
    }
  }

  function showAttachmentCount() {
    const count = $('photoInput').files.length + state.extraFiles.length;
    $('attachmentPreview').hidden = !count;
    $('attachmentPreview').textContent = count ? `${count} photo${count === 1 ? '' : 's'} ready to send` : '';
  }

  async function searchGifs(event) {
    event.preventDefault();
    $('gifResults').textContent = 'Searching…';
    try {
      const data = await json(`/api/chat/gifs/search?q=${encodeURIComponent($('gifQuery').value)}`);
      if (!data.configured) {
        $('gifResults').textContent = 'GIF search is waiting for its GIPHY key.';
        return;
      }
      $('gifResults').innerHTML = data.results.map((gif) => `
        <button type="button" data-gif="${escape(gif.selected)}"><img src="${escape(gif.preview)}" alt="GIF result"></button>`).join('') || 'No GIFs found.';
      document.querySelectorAll('[data-gif]').forEach((button) => {
        button.onclick = () => chooseGif(button.dataset.gif);
      });
    } catch (error) {
      $('gifResults').textContent = error.message;
    }
  }

  async function chooseGif(url) {
    try {
      const response = await fetch(`/api/chat/gifs/fetch?url=${encodeURIComponent(url)}`, { credentials: 'same-origin' });
      if (!response.ok) throw new Error('That GIF could not be imported.');
      state.extraFiles = [new File([await response.blob()], 'giphy.gif', { type: 'image/gif' })];
      showAttachmentCount();
      $('gifDialog').close();
    } catch (error) {
      setComposerStatus(error.message);
    }
  }

  function showPhoto(id, original) {
    state.attachmentId = id;
    $('fullPhoto').src = original;
    $('fullPhoto').style.transform = '';
    $('photoViewer').showModal();
  }

  async function savePhoto() {
    try {
      await json(`/api/chat/attachments/${state.attachmentId}/save-to-media`, { method: 'POST', headers });
      $('savePhoto').textContent = 'Saved to Media';
      setTimeout(() => { $('savePhoto').textContent = 'Save to Media'; }, 1500);
    } catch (error) {
      setComposerStatus(error.message);
    }
  }

  function vapidBytes(value) {
    const normalized = value.replace(/-/g, '+').replace(/_/g, '/');
    const padded = normalized + '='.repeat((4 - normalized.length % 4) % 4);
    return Uint8Array.from(atob(padded), (character) => character.charCodeAt(0));
  }

  function setNotificationButton(label, stateName = '') {
    $('notifyButton').textContent = label;
    $('notifyButton').classList.toggle('ready', stateName === 'ready');
    $('notifyButton').classList.toggle('blocked', stateName === 'blocked');
  }

  function showNotificationHelp(title, message, settings = false) {
    $('notificationTitle').textContent = title;
    $('notificationHelp').textContent = message;
    $('openNotificationSettings').hidden = !settings;
    $('notificationDialog').showModal();
  }

  function closeNotificationHelp() {
    if ($('notificationDialog').open) $('notificationDialog').close();
  }

  function androidNotificationsEnabled() {
    try { return typeof window.DavidPiPush.notificationsEnabled === 'function' && window.DavidPiPush.notificationsEnabled(); } catch (_error) { return false; }
  }

  function androidPushConfigured() {
    try { return typeof window.DavidPiPush.isConfigured === 'function' && window.DavidPiPush.isConfigured(); } catch (_error) { return false; }
  }

  async function notificationState() {
    if (window.DavidPiPush) {
      if (!androidNotificationsEnabled()) { setNotificationButton('Alerts off', 'blocked'); return; }
      if (!androidPushConfigured()) { setNotificationButton('Alerts setup', 'blocked'); return; }
      setNotificationButton('Alerts on', 'ready');
      window.DavidPiPush.requestToken();
      return;
    }
    if (!('Notification' in window)) { setNotificationButton('Alert help', 'blocked'); return; }
    if (Notification.permission === 'denied') { setNotificationButton('Alerts off', 'blocked'); return; }
    if (Notification.permission !== 'granted') { setNotificationButton('Allow alerts'); return; }
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      setNotificationButton('Alerts setup', 'blocked');
      return;
    }
    try {
      const registration = await navigator.serviceWorker.ready;
      const subscription = await registration.pushManager.getSubscription();
      if (!subscription) { setNotificationButton('Finish alerts', 'blocked'); return; }
      await json('/api/chat/push/web', { method: 'POST', headers, body: JSON.stringify(subscription.toJSON()) });
      setNotificationButton('Alerts on', 'ready');
    } catch (_error) {
      setNotificationButton('Retry alerts', 'blocked');
    }
  }

  async function notifications() {
    if (window.DavidPiPush) {
      if (!androidNotificationsEnabled()) {
        showNotificationHelp('Turn alerts back on', 'Android notifications are off for David-Pi. Open settings, choose Notifications, and allow them. Then return here and tap Alerts again.', true);
        return;
      }
      if (!androidPushConfigured()) {
        showNotificationHelp('One setup step remains', 'Chat works normally. Instant Android alerts still need the private notification provider configured on the David-Pi app; your permission choice did not damage Chat.');
        return;
      }
      window.DavidPiPush.requestToken();
      setNotificationButton('Alerts on', 'ready');
      return;
    }
    if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) {
      showNotificationHelp('Install David-Pi first', 'Add David-Pi to the Home Screen, open that installed app, then return to Chat to allow private notifications.');
      return;
    }
    if (Notification.permission === 'denied') {
      showNotificationHelp('Notifications are blocked', 'Open this device\'s notification settings for David-Pi and turn notifications on. On iPhone: Settings, Notifications, David-Pi, then Allow Notifications.');
      return;
    }
    try {
      const key = await json('/api/chat/push/public-key');
      if (!key.public_key) { showNotificationHelp('Alerts are not configured', 'Chat works normally, but the private notification provider is still waiting for server setup.'); return; }
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') {
        setNotificationButton('Alerts off', 'blocked');
        showNotificationHelp('Notifications were not enabled', 'Nothing is broken. You can keep using Chat and enable alerts later from this button.');
        return;
      }
      const registration = await navigator.serviceWorker.ready;
      let subscription = await registration.pushManager.getSubscription();
      if (!subscription) subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: vapidBytes(key.public_key) });
      await json('/api/chat/push/web', { method: 'POST', headers, body: JSON.stringify(subscription.toJSON()) });
      setNotificationButton('Alerts on', 'ready');
    } catch (_error) {
      showNotificationHelp('Alerts could not be enabled', 'Chat still works. David-Pi could not finish notification setup, so no settings were changed.');
    }
  }

  window.davidPiRegisterAndroidPush = async (token) => {
    try {
      await json('/api/chat/push/android', { method: 'POST', headers, body: JSON.stringify({ token }) });
      setNotificationButton('Alerts on', 'ready');
    } catch (_error) {
      setNotificationButton('Alerts setup', 'blocked');
    }
  };

  const emojis = ['😀', '😂', '🥰', '😍', '😊', '😭', '❤️', '👍', '🎉', '🔥', '🤔', '😴', '🍻', '🍕', '👀', '🙌', '💀', '😅', '😘', '🤗'];
  $('emojiTray').innerHTML = emojis.map((emoji) => `<button type="button">${emoji}</button>`).join('');
  $('emojiTray').querySelectorAll('button').forEach((button) => {
    button.onclick = () => {
      $('messageBody').value += button.textContent;
      $('emojiTray').hidden = true;
      $('emojiButton').setAttribute('aria-expanded', 'false');
      $('messageBody').focus();
    };
  });

  $('newChat').onclick = newDialog;
  $('closeNewChat').onclick = closeNewChat;
  $('cancelNewChat').onclick = closeNewChat;
  $('newChatDialog').addEventListener('cancel', (event) => { event.preventDefault(); closeNewChat(); });
  $('newChatForm').onsubmit = createChat;
  $('composer').onsubmit = send;
  $('photoInput').onchange = showAttachmentCount;
  $('olderMessages').onclick = loadOlder;
  $('jumpLatest').onclick = () => scrollLatest('smooth');
  $('messageList').addEventListener('scroll', () => { if (isNearBottom()) $('jumpLatest').hidden = true; }, { passive: true });
  $('emojiButton').setAttribute('aria-expanded', 'false');
  $('emojiButton').onclick = () => {
    const opening = $('emojiTray').hidden;
    $('emojiTray').hidden = !opening;
    $('emojiButton').setAttribute('aria-expanded', String(opening));
  };
  $('messageBody').addEventListener('focus', () => {
    window.setTimeout(() => {
      syncViewport();
      scrollLatest();
      $('messageBody').scrollIntoView({ block: 'nearest' });
    }, 120);
  });
  $('threadBack').onclick = showConversationList;
  $('deleteChatForm').onsubmit = deleteConversation;
  $('deleteChatPhrase').oninput = setDeleteConversationState;
  $('closeDeleteChat').onclick = closeDeleteConversation;
  $('cancelDeleteChat').onclick = closeDeleteConversation;
  $('deleteChatDialog').addEventListener('cancel', (event) => { event.preventDefault(); closeDeleteConversation(); });
  $('closePhoto').onclick = () => $('photoViewer').close();
  $('savePhoto').onclick = savePhoto;
  $('notifyButton').onclick = notifications;
  $('closeNotificationHelp').onclick = closeNotificationHelp;
  $('dismissNotificationHelp').onclick = closeNotificationHelp;
  $('openNotificationSettings').onclick = () => { try { window.DavidPiPush.openNotificationSettings(); } catch (_error) {} closeNotificationHelp(); };
  $('gifButton').onclick = () => $('gifDialog').showModal();
  $('closeGif').onclick = () => $('gifDialog').close();
  $('gifForm').onsubmit = searchGifs;

  syncViewport();
  observeComposer();
  window.addEventListener('resize', syncViewport, { passive: true });
  window.visualViewport?.addEventListener('resize', syncViewport, { passive: true });
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');
  notificationState();
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      notificationState();
      refreshChatLifecycle().catch((error) => { $('emptyConversations').textContent = error.message; });
    }
  });
  window.addEventListener('pageshow', (event) => {
    if (event.persisted) refreshChatLifecycle().catch((error) => { $('emptyConversations').textContent = error.message; });
  });
  refreshChatLifecycle().catch((error) => { $('emptyConversations').textContent = error.message; });
})();
