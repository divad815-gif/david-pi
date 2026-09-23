function csrf() {
  return document.cookie.split("; ").find(value => value.startsWith("david_pi_csrf="))?.split("=")[1] || "";
}

const canonicalDavidPiOrigin = document.querySelector('meta[name="paired-server-origin"]')?.content || "";

document.querySelectorAll("time.local-time[datetime]").forEach(element => {
  const value = element.getAttribute("datetime");
  if (!value) return;
  const date = new Date(value);
  if (!Number.isNaN(date.valueOf())) {
    element.textContent = date.toLocaleString([], {dateStyle: "medium", timeStyle: "short"});
  }
});

let lastPairing = null;
let pairingCountdown = null;
let pairingBusy = false;
function backupMessage(message) {
  const output = document.querySelector('#backupActionMessage');
  output.textContent = message; output.hidden = !message;
  if (message) output.scrollIntoView({block:'nearest'});
}
async function backupRequest(url, options) {
  const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(url, {...options, signal:controller.signal});
    const data = await response.json().catch(error => { if (controller.signal.aborted) throw error; return {}; });
    return {ok:response.ok, status:response.status, data};
  }
  finally { clearTimeout(timeout); }
}
document.querySelectorAll("[data-copy-endpoint]").forEach(button => {
  const endpoint = `${canonicalDavidPiOrigin}${button.dataset.copyEndpoint}`;
  button.dataset.copyValue = endpoint;
  const label = button.closest(".shortcut-copy")?.querySelector("code");
  if (label) label.textContent = endpoint;
});

async function createPairing(platform, button) {
  if (pairingBusy) return;
  pairingBusy = true; backupMessage(''); lastPairing = null;
  document.querySelector('#pairing-result').hidden = true;
  clearInterval(pairingCountdown);
  button.disabled = true;
  try {
    const response = await backupRequest("/api/device-backup/pairing-token", {
      method: "POST", headers: {"X-CSRF-Token": decodeURIComponent(csrf())}
    });
    const data = response.data;
    if (!response.ok) throw new Error(data.error?.message || data.error || "Pairing could not start.");
    if (data.server_url !== canonicalDavidPiOrigin) throw new Error("Pairing returned an invalid server address.");
    const link = document.querySelector("#pairing-link");
    const target = new URL(data.deep_link);
    if (target.protocol !== "davidpibackup:" || target.host !== "pair" ||
        target.searchParams.get("server") !== canonicalDavidPiOrigin) throw new Error("Invalid pairing link.");
    link.href = data.deep_link;
    link.textContent = `Open ${window.davidPiServerName || "Home server"}`;
    link.hidden = false;
    const qr = document.querySelector("#pairing-qr");
    qr.src = data.qr_data_uri;
    qr.hidden = false;
    document.querySelector("#pairing-title").textContent = "Open this on the Android phone";
    document.querySelector("#pairing-help").textContent = "Scan the code, then confirm your household’s server address in the Android app.";
    document.querySelector("#manual-code").textContent = data.manual_code;
    document.querySelector("#pairing-result").hidden = false;
    lastPairing = {server: data.server_url, code: data.manual_code, platform};
    if (pairingCountdown) clearInterval(pairingCountdown);
    const expiresAt = new Date(data.expires_at).getTime();
    const expiry = document.querySelector("#pairing-expiry");
    if (expiry) {
      const updateExpiry = () => {
        const seconds = Math.max(0, Math.ceil((expiresAt - Date.now()) / 1000));
        expiry.textContent = seconds
          ? `Expires in ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`
          : "Code expired · generate another";
        if (!seconds && pairingCountdown) clearInterval(pairingCountdown);
      };
      updateExpiry();
      pairingCountdown = setInterval(updateExpiry, 1000);
    }
    document.querySelector("#pairing-result").scrollIntoView({behavior: "smooth"});
  } catch (error) {
    backupMessage(error.name === 'AbortError' ? 'Pairing could not be confirmed. Check the connection, then try again.' : error.message);
  } finally {
    pairingBusy = false;
    button.disabled = false;
  }
}

document.querySelectorAll(".create-pairing").forEach(control => control.addEventListener("click", event => {
  const button = event.currentTarget;
  createPairing("android", button);
}));

document.querySelectorAll("[data-copy-value]").forEach(button => {
  button.addEventListener("click", async () => {
    const original = button.textContent;
    try {
      await navigator.clipboard.writeText(button.dataset.copyValue || "");
      button.textContent = "Copied";
    } catch (_) {
      button.textContent = "Select the text";
    }
    setTimeout(() => { button.textContent = original; }, 1600);
  });
});

document.querySelector("#copy-pairing")?.addEventListener("click", async event => {
  if (!lastPairing) return;
  const button = event.currentTarget;
  try {
    await navigator.clipboard.writeText(`${window.davidPiServerName || "Home server"}: ${lastPairing.server}\nPairing code: ${lastPairing.code}\nType: ${lastPairing.platform}`);
    button.textContent = 'Copied';
  } catch (_error) { backupMessage('Copy is unavailable. Use the setup code shown above.'); }
});

let phoneToRemove = null, removingPhone = false;
const removeDialog = document.querySelector('#backupRemoveDialog'), removeError = document.querySelector('#backupRemoveError');
document.querySelector('#backupKeepPhone').addEventListener('click', () => { if (!removingPhone) { phoneToRemove = null; removeDialog.close(); } });
removeDialog.addEventListener('cancel', event => { if (removingPhone) event.preventDefault(); else phoneToRemove = null; });
document.querySelector('#backupRemovePhone').addEventListener('click', async () => {
  if (!phoneToRemove || removingPhone) return;
  removingPhone = true; removeError.hidden = true;
  const accept = document.querySelector('#backupRemovePhone'), cancel = document.querySelector('#backupKeepPhone');
  accept.disabled = cancel.disabled = true;
  try {
    const response = await backupRequest(`/api/device-backup/devices/${encodeURIComponent(phoneToRemove.dataset.deviceId)}/revoke`, {
      method:'POST', headers:{'X-CSRF-Token':decodeURIComponent(csrf())}
    });
    if (!response.ok) throw new Error('The phone could not be removed. Its saved media is unchanged.');
    location.reload();
  } catch (error) {
    removeError.textContent = error.name === 'AbortError' ? 'The response was interrupted. Check the phone list before retrying.' : error.message;
    removeError.hidden = false;
  } finally { removingPhone = false; accept.disabled = cancel.disabled = false; }
});
document.querySelectorAll(".revoke-device").forEach(button => {
  button.addEventListener("click", () => {
    phoneToRemove = button; removeError.hidden = true; removeDialog.showModal();
  });
});
