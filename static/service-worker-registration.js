if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {
    // Online navigation remains available when offline support cannot start.
  });
}
