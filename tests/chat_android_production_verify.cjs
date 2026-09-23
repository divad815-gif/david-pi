(async () => {
  const targets = await (await fetch('http://127.0.0.1:9226/json')).json();
  const target = targets.find((item) => item.type === 'page' && item.url.endsWith('/')) || targets.find((item) => item.type === 'page');
  if (!target) throw new Error('David-Pi Android WebView was not found.');
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', reject, { once: true });
  });
  let nextId = 0;
  const pending = new Map();
  socket.addEventListener('message', (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) reject(new Error(message.error.message));
      else resolve(message.result);
    }
  });
  const call = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++nextId;
    pending.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });
  const server = process.env.DAVID_PI_VERIFY_URL;
  if (!server || !/^https:\/\/[a-z0-9-]+\.[a-z0-9-]+\.ts\.net\/?$/.test(server)) throw new Error('Set DAVID_PI_VERIFY_URL to the approved household HTTPS origin');
  await call('Page.navigate', { url: `${server.replace(/\/$/, '')}/chat` });
  await new Promise((resolve) => setTimeout(resolve, 3500));
  const evaluated = await call('Runtime.evaluate', {
    expression: `(async () => {
      const api = await (await fetch('/api/chat/conversations')).json();
      const composer = document.querySelector('#composer')?.getBoundingClientRect();
      const list = document.querySelector('#messageList')?.getBoundingClientRect();
      return {
        conversationCount: api.conversations.length,
        conversationTitle: api.conversations[0]?.title || '',
        activePath: location.pathname,
        threadDeleteAbsent: !document.querySelector('#deleteConversation'),
        composerVisible: Boolean(composer && composer.left >= -1 && composer.top >= -1 && composer.right <= innerWidth + 1 && composer.bottom <= innerHeight + 1),
        messageListFullWidth: Boolean(list && list.width >= innerWidth * .95),
        viewport: { width: innerWidth, height: innerHeight },
        composerBox: composer ? { left: composer.left, top: composer.top, right: composer.right, bottom: composer.bottom } : null,
      };
    })()`,
    awaitPromise: true,
    returnByValue: true,
  });
  const result = evaluated.result.value;
  const listEvaluated = await call('Runtime.evaluate', {
    expression: `(() => {
      document.querySelector('#threadBack')?.click();
      const row = document.querySelector('.conversation')?.getBoundingClientRect();
      const avatar = document.querySelector('.conversation > .avatar')?.getBoundingClientRect();
      const deleteButton = document.querySelector('.conversation-delete')?.getBoundingClientRect();
      return {
        title: document.querySelector('.conversation-copy strong')?.textContent || '',
        rowVisible: Boolean(row && row.left >= -1 && row.right <= innerWidth + 1 && row.height >= 68 && row.height <= 100),
        avatarFixed: Boolean(avatar && Math.abs(avatar.width - 48) < 1 && Math.abs(avatar.height - 48) < 1),
        listDeleteVisible: Boolean(deleteButton && deleteButton.left >= -1 && deleteButton.right <= innerWidth + 1),
      };
    })()`,
    returnByValue: true,
  });
  result.listTitle = listEvaluated.result.value.title;
  result.listRowVisible = listEvaluated.result.value.rowVisible;
  result.listAvatarFixed = listEvaluated.result.value.avatarFixed;
  result.listDeleteVisible = listEvaluated.result.value.listDeleteVisible;
  console.log(JSON.stringify(result));
  if (result.conversationCount !== 1 || !result.conversationTitle ||
      !result.activePath.startsWith('/chat/') || !result.threadDeleteAbsent ||
      !result.composerVisible || !result.messageListFullWidth || !result.listTitle ||
      !result.listRowVisible || !result.listAvatarFixed || !result.listDeleteVisible) process.exitCode = 1;
  socket.close();
})();
