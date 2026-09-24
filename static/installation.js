(function () {
  'use strict';
  let config = {};
  try { config = JSON.parse(document.getElementById('installation-config')?.textContent || '{}'); } catch (_) {}
  try {
    // Only the non-sensitive website label is retained for the offline shelf.
    // Membership, credentials and server configuration are never cached here.
    if (typeof config.display_name === 'string') localStorage.setItem('davidPiSiteLabel',config.display_name.slice(0,80));
    else if (!document.getElementById('installation-config')) config.display_name=localStorage.getItem('davidPiSiteLabel') || 'Home server';
  } catch (_) {}
  window.DavidPiInstallation = Object.freeze(config);
  window.davidPiServerName = config.display_name || 'David-Pi';
  document.addEventListener('DOMContentLoaded',()=>{
    document.querySelectorAll('[data-server-name]').forEach(element=>element.textContent=window.davidPiServerName);
    if(document.body.dataset.offlineShelf==='true')document.title=`Offline audiobooks · ${window.davidPiServerName}`;
  });
})();
