function csrf() {
  return document.cookie.split("; ").find(value => value.startsWith("david_pi_csrf="))?.split("=")[1] || "";
}

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
document.querySelectorAll("[data-copy-endpoint]").forEach(button => {
  const endpoint = `${location.origin}${button.dataset.copyEndpoint}`;
  button.dataset.copyValue = endpoint;
  const label = button.closest(".shortcut-copy")?.querySelector("code");
  if (label) label.textContent = endpoint;
});

async function createPairing(platform, button) {
  button.disabled = true;
  try {
    const response = await fetch("/api/device-backup/pairing-token", {
      method: "POST", headers: {"X-CSRF-Token": decodeURIComponent(csrf())}
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || data.error || "Pairing could not start.");
    const androidPairing = platform === "android";
    const link = document.querySelector("#pairing-link");
    link.href = androidPairing
      ? data.deep_link
      : `shortcuts://run-shortcut?name=Back%20Up%20to%20David-Pi&input=text&text=${encodeURIComponent(data.manual_code)}`;
    link.textContent = androidPairing ? "Open David-Pi" : "Open Back Up to David-Pi";
    link.hidden = false;
    const qr = document.querySelector("#pairing-qr");
    qr.src = data.qr_data_uri;
    qr.hidden = !androidPairing;
    document.querySelector("#pairing-title").textContent = androidPairing
      ? "Open this on the Android phone"
      : "Pair the iPhone Shortcut";
    document.querySelector("#pairing-help").textContent = androidPairing
      ? "Scan the code with the Android phone, or open the app link on that phone."
      : "Install the Apple-shared Shortcut first, then tap the button below. The six-digit code is passed directly to the Shortcut and expires after ten minutes.";
    document.querySelector("#manual-code").textContent = data.manual_code;
    document.querySelector("#pairing-result").hidden = false;
    lastPairing = {server: location.origin, code: data.manual_code, platform};
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
    alert(error.message);
  } finally {
    button.disabled = false;
  }
}

document.querySelectorAll(".create-pairing").forEach(control => control.addEventListener("click", event => {
  const button = event.currentTarget;
  const platform = button.dataset.platform;
  if (platform === "ios") {
    document.querySelector("#pairing-result").hidden = true;
    document.querySelector("#iphone-setup").hidden = false;
    document.querySelector("#iphone-setup").scrollIntoView({behavior: "smooth"});
    return;
  }
  document.querySelector("#iphone-setup").hidden = true;
  createPairing(platform, button);
}));

document.querySelector(".generate-ios-pairing")?.addEventListener("click", event => {
  createPairing("ios", event.currentTarget);
});

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
  await navigator.clipboard.writeText(
    `David-Pi: ${lastPairing.server}\nPairing code: ${lastPairing.code}\nType: ${lastPairing.platform}`
  );
  event.currentTarget.textContent = "Copied";
});

document.querySelectorAll(".revoke-device").forEach(button => {
  button.addEventListener("click", async () => {
    if (!confirm("Remove this phone from David-Pi Backup? Its access will stop immediately. Existing media stays safe.")) return;
    button.disabled = true;
    const response = await fetch(`/api/device-backup/devices/${button.dataset.deviceId}/revoke`, {
      method: "POST", headers: {"X-CSRF-Token": decodeURIComponent(csrf())}
    });
    if (response.ok) location.reload();
    else {
      button.disabled = false;
      alert("The phone could not be removed.");
    }
  });
});
