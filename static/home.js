const health = document.querySelector('#homeHealth');

fetch('/api/status', { cache: 'no-store' })
  .then((response) => response.json())
  .then((data) => {
    if (!data.ok || !data.health) throw new Error('Health unavailable');
    health.dataset.health = data.health.overall;
    health.lastChild.textContent = ` ${data.health.message}`;
  })
  .catch(() => {
    health.dataset.health = 'unknown';
    health.lastChild.textContent = ' Health check unavailable';
  });
