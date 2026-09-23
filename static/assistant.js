const form = document.querySelector('#assistantForm');
const input = document.querySelector('#assistantInput');
const send = document.querySelector('#assistantSend');
const log = document.querySelector('#chatLog');
const intro = document.querySelector('#assistantIntro');
const error = document.querySelector('#assistantError');
const providerStatus = document.querySelector('#providerStatus');
const historyPanel = document.querySelector('#historyPanel');
const conversationList = document.querySelector('#conversationList');
let conversationId = sessionStorage.getItem('davidPiAssistantConversation') || '';
let activeAnswer = null;
let answerGeneration = 0;
let historyGeneration = 0;
let windowsAvailable = null;

function requestError(result, fallback) {
  if (typeof result?.error === 'string' && result.error.trim()) return result.error;
  if (typeof result?.error?.message === 'string' && result.error.message.trim()) return result.error.message;
  return fallback;
}

function cancelActiveAnswer() {
  answerGeneration += 1;
  activeAnswer?.abort();
  activeAnswer = null;
  log.setAttribute('aria-busy', 'false');
  send.disabled = false;
  input.disabled = false;
}

function csrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)david_pi_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : '';
}

function message(text, role, loading = false, senderName = '') {
  const item = document.createElement('article');
  item.className = `chat-message ${role}${loading ? ' loading' : ''}`;
  const label = document.createElement('small');
  label.textContent = role === 'user' ? (senderName || 'You') : (role === 'system' ? 'System' : (window.davidPiServerName || 'Home server'));
  const copy = document.createElement('p');
  copy.textContent = text;
  item.append(label, copy);
  log.append(item);
  const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  item.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'end' });
  return item;
}

async function ask(question) {
  const clean = question.trim();
  if (!clean || send.disabled) return;
  if (windowsAvailable === false && /^ask windows\s*:/i.test(clean)) {
    error.textContent = 'The Windows helper is offline. Your question is kept; try again when it is online, or ask a household or server question.';
    input.value = clean;
    return;
  }
  const generation = ++answerGeneration;
  const controller = new AbortController();
  activeAnswer?.abort();
  activeAnswer = controller;
  const requestedConversation = conversationId;
  intro.hidden = true;
  error.textContent = '';
  message(clean, 'user');
  input.value = '';
  input.style.height = '';
  send.disabled = true;
  input.disabled = true;
  log.setAttribute('aria-busy', 'true');
  const waiting = message('Thinking…', 'assistant', true);
  try {
    const response = await fetch('/api/assistant', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
      body: JSON.stringify({ question: clean, conversation_id: conversationId || null }),
      signal: controller.signal,
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(requestError(result, 'I could not answer that right now.'));
    if (generation !== answerGeneration || requestedConversation !== conversationId) return;
    waiting.querySelector('p').textContent = result.answer;
    waiting.classList.remove('loading');
    conversationId = result.conversation_id || conversationId;
    sessionStorage.setItem('davidPiAssistantConversation', conversationId);
    providerStatus.textContent = 'Answered by local help';
  } catch (problem) {
    if (problem.name === 'AbortError' || generation !== answerGeneration) return;
    waiting.remove();
    error.textContent = problem.message;
    input.value = clean;
  } finally {
    if (generation === answerGeneration) {
      activeAnswer = null;
      log.setAttribute('aria-busy', 'false');
      send.disabled = false;
      input.disabled = false;
      input.focus();
    }
  }
}

async function loadProviders() {
  try {
    const response = await fetch('/api/assistant/providers', { cache: 'no-store' });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || !Array.isArray(result.providers)) throw new Error('Provider status unavailable.');
    const local = result.providers.find((provider) => provider.name === 'David-Pi Rules');
    const windows = result.providers.find((provider) => provider.name === 'Windows Codex');
    windowsAvailable = Boolean(windows?.available);
    const remoteSuggestion = document.querySelector('[data-requires-windows]');
    if (remoteSuggestion) {
      remoteSuggestion.disabled = !windowsAvailable;
      remoteSuggestion.title = windowsAvailable ? '' : 'The Windows helper is offline. Household and server questions still work.';
    }
    if (local?.available && windows?.available) {
      providerStatus.textContent = 'Local server help is ready';
    } else if (local?.available) {
      providerStatus.textContent = 'Local server help is ready';
    } else {
      providerStatus.textContent = 'Assistant providers are unavailable right now.';
    }
  } catch {
    windowsAvailable = null;
    providerStatus.textContent = 'Provider status could not be checked. Try again when the connection is ready.';
  }
}

async function openConversation(id) {
  const generation = ++historyGeneration;
  const historyState = document.querySelector('#historyState');
  historyState.dataset.state = 'loading';
  historyState.textContent = 'Opening conversation…';
  try {
    const response = await fetch(`/api/assistant/conversations/${encodeURIComponent(id)}`, { cache: 'no-store' });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(requestError(result, 'Conversation could not be opened.'));
    if (generation !== historyGeneration) return;
    cancelActiveAnswer();
    conversationId = id;
    sessionStorage.setItem('davidPiAssistantConversation', id);
    log.replaceChildren();
    intro.hidden = true;
    (result.messages || []).filter((item) => item.role !== 'system').forEach((item) => {
      const senderName = item.role === 'user'
        ? (item.mine ? 'You' : (item.sender_name || 'Household member'))
        : '';
      message(item.content, item.role, false, senderName);
    });
    historyPanel.close();
  } catch (problem) {
    if (generation !== historyGeneration) return;
    historyState.dataset.state = 'error';
    historyState.textContent = problem.message;
  }
}

async function loadHistory() {
  const generation = ++historyGeneration;
  const historyState = document.querySelector('#historyState');
  conversationList.setAttribute('aria-busy', 'true');
  historyState.dataset.state = 'loading';
  historyState.textContent = 'Loading conversations…';
  conversationList.replaceChildren();
  try {
    const response = await fetch('/api/assistant/conversations', { cache: 'no-store' });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || !Array.isArray(result.conversations)) throw new Error(requestError(result, 'Conversation history could not be loaded.'));
    if (generation !== historyGeneration) return;
    result.conversations.forEach((conversation) => {
      const button = document.createElement('button');
      button.type = 'button';
      const title = document.createElement('span');
      title.textContent = conversation.title;
      const creator = document.createElement('small');
      creator.textContent = `Started by ${conversation.creator_name || 'Household member'}`;
      button.append(title, creator);
      if (conversation.id === conversationId) button.setAttribute('aria-current', 'true');
      button.addEventListener('click', () => openConversation(conversation.id));
      conversationList.append(button);
    });
    historyState.dataset.state = result.conversations.length ? 'ready' : 'empty';
    historyState.textContent = result.conversations.length
      ? `${result.conversations.length} conversation${result.conversations.length === 1 ? '' : 's'}`
      : 'No saved conversations yet.';
  } catch (problem) {
    if (generation !== historyGeneration) return;
    historyState.dataset.state = 'error';
    historyState.textContent = problem.message;
  } finally {
    if (generation === historyGeneration) conversationList.setAttribute('aria-busy', 'false');
  }
}

form.addEventListener('submit', (event) => { event.preventDefault(); ask(input.value); });
document.querySelectorAll('.quick-questions button').forEach((button) => button.addEventListener('click', () => ask(button.textContent)));
document.querySelector('#historyButton').addEventListener('click', async () => {
  historyPanel.showModal();
  await loadHistory();
});
document.querySelector('#closeHistory').addEventListener('click', () => historyPanel.close());
document.querySelector('#newConversation').addEventListener('click', () => {
  historyGeneration += 1;
  cancelActiveAnswer();
  conversationId = '';
  sessionStorage.removeItem('davidPiAssistantConversation');
  log.replaceChildren();
  intro.hidden = false;
  historyPanel.close();
  input.focus();
});
input.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); form.requestSubmit(); }
});
input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 128)}px`;
});
loadProviders();
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') loadProviders(); });
