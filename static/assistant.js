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

function csrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)david_pi_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : '';
}

function message(text, role, loading = false) {
  const item = document.createElement('article');
  item.className = `chat-message ${role}${loading ? ' loading' : ''}`;
  const label = document.createElement('small');
  label.textContent = role === 'user' ? 'You' : 'David-Pi';
  const copy = document.createElement('p');
  copy.textContent = text;
  item.append(label, copy);
  log.append(item);
  item.scrollIntoView({ behavior: 'smooth', block: 'end' });
  return item;
}

async function ask(question) {
  const clean = question.trim();
  if (!clean || send.disabled) return;
  intro.hidden = true;
  error.textContent = '';
  message(clean, 'user');
  input.value = '';
  input.style.height = '';
  send.disabled = true;
  input.disabled = true;
  const waiting = message('Thinking…', 'assistant', true);
  try {
    const response = await fetch('/api/assistant', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
      body: JSON.stringify({ question: clean, conversation_id: conversationId || null }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || 'I could not answer that right now.');
    waiting.querySelector('p').textContent = result.answer;
    waiting.classList.remove('loading');
    conversationId = result.conversation_id || conversationId;
    sessionStorage.setItem('davidPiAssistantConversation', conversationId);
    providerStatus.textContent = `Answered by ${result.provider || 'David-Pi'}`;
  } catch (problem) {
    waiting.remove();
    error.textContent = problem.message;
  } finally {
    send.disabled = false;
    input.disabled = false;
    input.focus();
  }
}

async function loadProviders() {
  try {
    const result = await fetch('/api/assistant/providers', { cache: 'no-store' }).then((response) => response.json());
    const local = result.providers.find((provider) => provider.name === 'David-Pi Rules');
    const windows = result.providers.find((provider) => provider.name === 'Windows Codex');
    if (local?.available && windows?.available) {
      providerStatus.textContent = 'David-Pi answers ready · Windows helper online';
    } else if (local?.available) {
      providerStatus.textContent = 'David-Pi answers ready · Windows helper offline';
    } else {
      providerStatus.textContent = 'Health questions are available';
    }
  } catch {
    providerStatus.textContent = 'David-Pi answers are available';
  }
}

async function openConversation(id) {
  const response = await fetch(`/api/assistant/conversations/${encodeURIComponent(id)}`, { cache: 'no-store' });
  if (!response.ok) return;
  const result = await response.json();
  conversationId = id;
  sessionStorage.setItem('davidPiAssistantConversation', id);
  log.replaceChildren();
  intro.hidden = true;
  result.messages.filter((item) => item.role !== 'system').forEach((item) => message(item.content, item.role));
  historyPanel.close();
}

async function loadHistory() {
  const response = await fetch('/api/assistant/conversations', { cache: 'no-store' });
  if (!response.ok) return;
  const result = await response.json();
  conversationList.replaceChildren();
  result.conversations.forEach((conversation) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = conversation.title;
    button.addEventListener('click', () => openConversation(conversation.id));
    conversationList.append(button);
  });
}

form.addEventListener('submit', (event) => { event.preventDefault(); ask(input.value); });
document.querySelectorAll('.quick-questions button').forEach((button) => button.addEventListener('click', () => ask(button.textContent)));
document.querySelector('#historyButton').addEventListener('click', async () => {
  historyPanel.showModal();
  await loadHistory();
});
document.querySelector('#closeHistory').addEventListener('click', () => historyPanel.close());
document.querySelector('#newConversation').addEventListener('click', () => {
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
