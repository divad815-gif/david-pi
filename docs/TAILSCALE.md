# Tailscale: required private access

![Private household access and separate backup storage](images/private-home.svg)

Tailscale connects your devices privately and supplies the server's HTTPS
address. Its network is called a **tailnet**. A Tailscale account is required for
this release. Check the [current plans](https://tailscale.com/pricing) for your
household size and use.

## 1. Connect the device where you will open the wizard

Create or sign into your household account at [Tailscale](https://tailscale.com/).
On the add-device screen, select the operating system of the computer or phone
you are using for the wizard. Install the [Tailscale app](https://tailscale.com/download)
there, open it, and sign in using the same account. The device should then appear
in the admin console. See the [official quickstart](https://tailscale.com/docs/how-to/quickstart).

This device is your **setup device**. It does not become the home server merely
because you installed Tailscale on it. The installer connects the actual server
in step 2. If onboarding asks for a second device, use that server as the second
device; continue to the admin console when both appear.

**Checkpoint:** the Tailscale app says connected and the admin console shows
your setup device in the intended network.

### Already using Tailscale, or testing in a separate network?

A separate test network is useful for a disposable test, but is not required
for normal installation. Use the client's add-account/switch-account feature
to keep an existing login available. On Linux, `tailscale switch --list` shows
saved accounts and marks the active one. Use `sudo tailscale login` to add an
account, then `sudo tailscale switch <account-or-nickname>` to select a saved
one. Substitute the actual account or nickname; do not type the angle brackets.

Switching accounts does not delete the previous network or its devices. This
computer can reach only its currently active tailnet through that client, so
access to the other network pauses until you switch back. The browser's
Tailscale admin-console login can differ from the app's active account; check
both. A private/incognito window helps separate browser logins but does not
switch the Tailscale app. See [Tailscale account switching](https://tailscale.com/docs/features/client/fast-user-switching).

## 2. Connect the server

Run the [installation command](INSTALL.md#3-run-the-verified-bootstrap-on-the-server)
in the **server terminal**. Enter the intended owner's exact individual
Tailscale login. If the terminal prints a Tailscale login link, open it in the
setup device's browser. Check the selected account and network before approving
the server.

Entering an email in the terminal selects the future administrator; it does
not authenticate as that person. The sign-in link authenticates the server
with Tailscale. An existing browser login may take you straight to device
approval, so check the account shown. Later, the wizard checks your setup
device's actual Tailscale identity and the terminal's one-use claim code.
Signing into the Tailscale website does not switch the Tailscale app's account.

**Checkpoint:** the [Machines page](https://console.tailscale.com/admin/machines)
shows both the server and your setup device under the intended network. Existing
Tailscale settings are inspected; setup does not reset an existing network.

## 3. Enable HTTPS certificates

Do this in the **setup device's browser**, in the Tailscale admin console:

1. Open the [DNS page](https://console.tailscale.com/admin/dns). Check the account
   and network displayed before changing settings.
2. Enable **MagicDNS** if it is not already enabled.
3. Find **HTTPS Certificates** and choose **Enable HTTPS**.
4. Read and accept the confirmation about publishing certificate names. Opening
   that confirmation alone does not enable certificates; finish its confirmation
   action and check that HTTPS is now enabled.

Issued certificate names include the server hostname and tailnet DNS name in
public certificate-transparency records. The website remains private. Choose
a hostname without sensitive information. [Official HTTPS instructions](https://tailscale.com/docs/how-to/set-up-https-certificates).

David-Pi uses Tailscale Serve to obtain and use the certificate; you do not need
to download a certificate or run `tailscale cert` yourself. Serve requires HTTPS
to be enabled. [Official Serve documentation](https://tailscale.com/docs/features/tailscale-serve).

Return to the **server terminal** and press Enter at the HTTPS checkpoint.
If initial setup exits before printing the private wizard link and claim code,
correct the HTTPS problem and rerun the [verified bootstrap command](INSTALL.md#3-run-the-verified-bootstrap-on-the-server).
Verified release details may not have been saved yet, so `sudo david-pi setup`
alone may refuse. If a claim code was already issued, use
[the saved-setup recovery steps](INSTALL.md#if-setup-stops). Follow the private
setup address printed by your server.
This address is different from the Tailscale login and admin-console links.

If the 15-minute claim code expires, run `sudo david-pi setup` in the server
terminal. While a saved setup is waiting to be claimed, it reuses the saved account
and hostname and prints the same private link with a fresh code. You do not
repeat Tailscale sign-in or the account/name questions. The old code and browser
session stop working, so refresh the wizard before pasting the new code.
Tailscale confirms your network identity; the one-use code additionally proves
access to the installation terminal. You only claim ownership during setup,
not on ordinary visits to the finished portal.

**Checkpoint:** the private HTTPS address opens the **Claim your server** page
without a certificate warning. Continue in the [browser wizard](INSTALL.md#5-complete-the-browser-wizard).

## Keep and recover your portal address

The wizard shows the actual assigned HTTPS address with a copy action. Bookmark
it; it will open the finished portal after installation. To find it again, run
`sudo david-pi address` in the server terminal. Use the full address including
the tailnet name and any hostname collision suffix. Changing the website's
display name does not change this address.

The wizard uses private Serve access to reach its temporary setup service and
then the completed portal. It preserves unrelated Serve settings. If HTTPS
port 443 already belongs to another service, resolve that conflict deliberately
before retrying. Do not use `tailscale serve reset` to get past the error.
David-Pi does not enable Funnel.

## Add the rest of the household

After installation, [invite each household member](https://tailscale.com/docs/how-to/invite-users)
to your tailnet using their own login. Then separately admit that exact identity
in the portal's household settings. Joining Tailscale does not grant portal
membership or administrator rights. See [household access](HOUSEHOLD.md).

If the portal becomes unreachable, first check the app's active account on both
devices and retrieve the actual address from the server. A deliberate address
change needs the [reconnect workflow](HOUSEHOLD.md) and Android reconnection;
editing a bookmark alone does not approve a new server origin.
