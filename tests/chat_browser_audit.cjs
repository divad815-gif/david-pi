const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  });
  const results = [];
  for (const viewport of [
    { name: 'phone', width: 390, height: 844 },
    { name: 'keyboard', width: 390, height: 500 },
    { name: 'android-standalone', width: 390, height: 844, standalone: true, reservedBottom: 56 },
    { name: 'android-standalone-keyboard', width: 390, height: 500, standalone: true, reservedBottom: 56 },
    { name: 'iphone-standalone', width: 393, height: 852, standalone: true, iosStandalone: true, reservedBottom: 56 },
    { name: 'iphone-standalone-keyboard', width: 393, height: 500, standalone: true, iosStandalone: true, reservedBottom: 56 },
    { name: 'desktop', width: 1280, height: 800 },
  ]) {
    const page = await browser.newPage({
      viewport,
      userAgent: viewport.iosStandalone
        ? 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148'
        : viewport.standalone
        ? 'Mozilla/5.0 (Linux; Android 15; Pixel 9 Pro) AppleWebKit/537.36 Chrome/138.0.0.0 Mobile Safari/537.36'
        : undefined,
    });
    if (viewport.standalone) {
      await page.addInitScript(({ iosStandalone }) => {
        if (iosStandalone) Object.defineProperty(window.navigator, 'standalone', { value: true, configurable: true });
        const nativeMatchMedia = window.matchMedia.bind(window);
        window.matchMedia = (query) => {
          if (/display-mode:\s*(standalone|fullscreen|minimal-ui)/.test(query)) {
            return {
              matches: query.includes('standalone'), media: query, onchange: null,
              addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {},
              dispatchEvent() { return false; },
            };
          }
          return nativeMatchMedia(query);
        };
      }, { iosStandalone: Boolean(viewport.iosStandalone) });
    }
    const consoleErrors = [];
    const failedResponses = [];
    const destructiveRequests = [];
    page.on('console', (message) => {
      if (message.type() === 'error') consoleErrors.push(message.text());
    });
    page.on('response', (response) => {
      if (response.status() >= 400) failedResponses.push({ status: response.status(), url: response.url() });
    });
    page.on('request', (request) => {
      if (request.method() === 'DELETE' && request.url().includes('/api/chat/conversations/')) destructiveRequests.push(request.url());
    });
    await page.goto(`${process.env.DAVID_PI_CHAT_TEST_URL || 'http://127.0.0.1:5099'}/chat`, { waitUntil: 'networkidle' });
    await page.locator('#composer').waitFor({ state: 'visible' });
    const singleConversationOpened = page.url().endsWith('/chat/ui-test');
    const threadDeleteAbsent = await page.locator('#deleteConversation').count() === 0;
    const composer = await page.locator('#composer').boundingBox();
    const textarea = await page.locator('#messageBody').boundingBox();
    const send = await page.locator('#composer .send').boundingBox();
    const list = await page.locator('#messageList').boundingBox();
    const emojiHiddenInitially = await page.locator('#emojiTray').isHidden();
    await page.locator('#emojiButton').click();
    const emojiVisibleAfterClick = await page.locator('#emojiTray').isVisible();
    await page.locator('#messageBody').fill('Draft preserved during layout verification');
    const textareaFocused = await page.locator('#messageBody').evaluate((element) => document.activeElement === element);
    await page.locator('#emojiButton').click();
    const draft = await page.locator('#messageBody').inputValue();
    const usableBottom = viewport.height - (viewport.reservedBottom || 0);
    const fullyVisible = (box) => box && box.x >= 0 && box.y >= 0 &&
      box.x + box.width <= viewport.width && box.y + box.height <= usableBottom;
    let conversationRowVisible = true;
    let conversationAvatarIsFixed = true;
    let listDeleteVisible = true;
    if (viewport.width <= 720) {
      await page.locator('#threadBack').click();
      const row = await page.locator('.conversation').boundingBox();
      const avatar = await page.locator('.conversation > .avatar').boundingBox();
      conversationRowVisible = fullyVisible(row) && row.height >= 68 && row.height <= 100;
      conversationAvatarIsFixed = Boolean(avatar && Math.abs(avatar.width - 48) < 1 && Math.abs(avatar.height - 48) < 1);
      listDeleteVisible = await page.locator('.conversation-delete').isVisible();
    }
    await page.locator('.conversation-delete').click();
    const deleteDialogVisible = await page.locator('#deleteChatDialog').isVisible();
    const leaveEnabled = await page.locator('#confirmDeleteChat').isEnabled();
    await page.locator('#cancelDeleteChat').click();
    const deleteDialogClosed = await page.locator('#deleteChatDialog').isHidden();
    await page.locator('.conversation').click();
    await page.locator('#composer').waitFor({ state: 'visible' });
    results.push({
      viewport,
      composer,
      textarea,
      send,
      list,
      composerVisible: fullyVisible(composer),
      textareaVisible: fullyVisible(textarea),
      sendVisible: fullyVisible(send),
      installedWebAppClass: !viewport.standalone || await page.locator('html.installed-web-app').count() === 1,
      textareaFocused,
      emojiHiddenInitially,
      emojiVisibleAfterClick,
      draftPreserved: draft === 'Draft preserved during layout verification',
      singleConversationOpened,
      threadDeleteAbsent,
      listDeleteVisible,
      conversationRowVisible,
      conversationAvatarIsFixed,
      deleteDialogVisible,
      leaveEnabled,
      deleteDialogClosed,
      destructiveRequestCount: destructiveRequests.length,
      messageCount: await page.locator('.bubble').count(),
      consoleErrors,
      failedResponses,
    });
    await page.screenshot({ path: `tests/chat-${viewport.name}.png`, fullPage: false });
    await page.close();
  }
  console.log(JSON.stringify(results, null, 2));
  if (results.some((result) => !result.composerVisible || !result.textareaVisible ||
      !result.sendVisible || !result.emojiHiddenInitially || !result.emojiVisibleAfterClick ||
      !result.installedWebAppClass || !result.textareaFocused || !result.draftPreserved || !result.singleConversationOpened || !result.threadDeleteAbsent || !result.listDeleteVisible ||
      !result.conversationRowVisible || !result.conversationAvatarIsFixed || !result.deleteDialogVisible ||
      !result.leaveEnabled || !result.deleteDialogClosed ||
      result.destructiveRequestCount !== 0 || result.messageCount < 50 ||
      result.failedResponses.some((failure) => !failure.url.endsWith('/favicon.ico')))) {
    process.exitCode = 1;
  }
  await browser.close();
})();
