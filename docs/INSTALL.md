# Install your private home server

This guide describes the portable home server. Stable publication still requires
all [release acceptance checks](RELEASE.md). Friends testing an explicitly
published beta should use that beta's pinned installer and [testing instructions](TESTING.md);
`latest` always selects stable releases. Older public 9.x downloads do not follow
this guide.

**Replacing a failed server from a backup?** Follow [clean-machine recovery](RECOVERY.md#prepare-the-clean-target)
instead of creating a new home. Its `--prepare-recovery` installer installs the
required tools without creating a new household or claim code.

## Know which screen to use

| Place | What you do there |
| --- | --- |
| **Server terminal** | Run the installation command on the Pi or Linux PC that will store your content. An SSH connection to that machine also counts. |
| **Setup device** | Use your everyday computer or phone to open the browser wizard. Install and connect Tailscale on this device too. |
| **Tailscale admin console** | A website for connecting devices and enabling HTTPS certificates. This is separate from your home portal. |
| **Your private home portal** | The full `https://…ts.net/` address printed in the server terminal. This starts as the setup wizard and becomes your home page. |

The setup device and server can be the same supported Linux computer. If they
are different, both must be connected to the same Tailscale network. Signing
into Tailscale's website alone does not connect a device: its Tailscale app must
also be connected.

![Private setup sequence](images/setup-flow.svg)

## 1. Prepare the server

Use 64-bit Raspberry Pi OS based on Debian 13 on a Pi 4/5, Debian 13 AMD64, or
Ubuntu Server 24.04 LTS AMD64. Start with a supported native installation;
Windows, WSL, macOS, network filesystems and 32-bit machines are not targets.
Use your own Linux administrator account. There is no shared default password.
If you need SSH, enable it using the OS instructions and retain a working local
or SSH session until the portal is verified.

Have two CPU cores, 4 GB memory and 8 GiB free OS space, plus room for content.
Plug in a prepared ext4 data drive before setup if you plan to use one. An
existing folder on the server's local ext4 filesystem also works. The installer
will not format or repartition a drive. [Prepare storage first](STORAGE.md).

**Checkpoint:** you can open a terminal on the server, and the intended data
drive is attached and mounted, if you are using one.

## 2. Connect the setup device

Follow [Tailscale setup](TAILSCALE.md) to sign into your household network and
connect the computer or phone where you will open the wizard. On Tailscale's
**add device** screen, choose the operating system of that device. The server
installer handles connecting the server in the next step.

Use the intended owner's individual login for this first setup. A separate
test network is optional for people testing disposable installations; an
ordinary household installation does not require an extra test account.

**Checkpoint:** the setup device appears connected in Tailscale's machine list.
Keep the Tailscale admin console open in your browser.

## 3. Run the verified bootstrap on the server

For a published stable portable release, run this in an interactive **server
terminal** where you can answer prompts. A local keyboard or SSH session works;
an unattended script does not provide the required conversation.

```sh
curl -fsSL https://github.com/divad815-gif/david-pi/releases/latest/download/install.sh | sudo bash
```

If `curl` is missing, install `curl` and `ca-certificates` with the operating
system's package manager first. For inspection before execution, download
`install.sh` and `install.sh.sha256` from the same stable GitHub release, run
`sha256sum -c install.sh.sha256`, read the script, then run `sudo bash install.sh`.
The checksum confirms download integrity; the publisher and HTTPS channel
remain part of the trust model.

An explicitly published beta uses the installer attached to that exact beta,
for example the `v10.0.0-beta.1` release asset, rather than the `latest` URL.
A beta-specific installer pins its version. A normal latest installer refuses
to select a beta silently. Check the release page for the supported test scope.

The terminal first shows download, machine-check and package-installation
messages. It installs missing prerequisites from official package sources.
Answer its prompts with your exact Tailscale account login, your first name,
and a suggested hostname such as `john-pi`. The hostname forms part of the web
address; you choose the website's visible name later.

The email **selects who may become administrator**. Typing it does not sign you
in, create an account, or grant access. Tailscale authenticates the server in
the next step. When you open the wizard, it verifies the Tailscale account of
your browser's device and also requires the one-use claim code from the server
terminal. Double-check the email: renewing a code keeps the saved account.

If a Tailscale sign-in link appears, open it on your setup device, check the
account/network shown, and approve **the server**. This link signs in the server
even though you open it on a different device. It is not the portal link.
If the browser is already signed in, Tailscale may show device approval without
asking for your password again. Check the account before approving.

Before the private wizard can open, enable HTTPS certificates in the Tailscale
admin console. Follow the exact [HTTPS steps](TAILSCALE.md#3-enable-https-certificates),
including the certificate-name confirmation. Return to the server terminal
and press Enter at **Press Enter after HTTPS Certificates is enabled**. The
claim token is created after this checkpoint, so its 15-minute timer does not
run while you are enabling certificates. If initial setup exits before printing
the private wizard link and claim code, correct the reported problem and rerun
the verified bootstrap command above. The saved release details needed by
`sudo david-pi setup` may not exist yet. If a claim code was already issued,
follow [the recovery steps below](#if-setup-stops).
Existing Serve conflicts are reported without replacing another private site.

**Checkpoint:** Tailscale shows the server and setup device in the intended
network, and the terminal prints a private HTTPS setup address and claim token.

## 4. Find and save your private link

The terminal separates the address from the one-use claim token. This is an
abbreviated, fictional example; the account, network and token are placeholders:

```text
1. Connect this server to Tailscale
   Your setup administrator account: john@example.test

2. Enable private HTTPS in your Tailscale network
   Open https://console.tailscale.com/admin/dns using account john@example.test.
   Press Enter after HTTPS Certificates is enabled:

3. Open your private setup wizard
   https://john-pi.example.ts.net/
   Connect this browser's computer or phone to Tailscale using john@example.test.

4. Claim your home server
   One-use claim token (valid 15 minutes):
   <your private one-use token appears here>

Bookmark https://john-pi.example.ts.net/ — this same link opens your home page after setup.
Find this link again: sudo david-pi address
If the claim token expires: sudo david-pi setup
```

Use the full address printed by **your** server. Tailscale may assign a suffix
when a hostname is already taken, so do not construct the address yourself.
Copy it into the setup device's browser; a terminal may also make it clickable.
Bookmark the address. The wizard shows it with a copy action too.

If the link scrolls out of view, retrieve it from the server terminal:

```sh
sudo david-pi address
```

This only shows address information; it does not restart setup or issue a new
claim token. After installation the same address opens the portal. If you deliberately change
the server's Tailscale address, follow the [reconnect workflow](HOUSEHOLD.md)
before updating bookmarks or Android pairing; this command shows the address
saved for the installation and does not approve a new one.

## 5. Complete the browser wizard

1. **Claim your server.** Paste the terminal's token into the token field. It
   expires after 15 minutes and can be used once. Keep it private; it does not
   belong in the address bar. Your setup device's active Tailscale identity must
   match the intended administrator. A browser signed into another account does
   not change that device identity.
2. **Name your home.** Enter a display name such as **John-Pi** or **John's home**,
   then your own name. Confirm the private address shown. Use **Find your
   timezone** to search for your city or region, then select it
   from the **Timezone** dropdown and check the browser's suggestion. Set the
   two-letter country used for movie availability, for example `US`. The display
   name can change later without changing your address or moving your files.
3. **Choose existing storage.** Use **Store your household’s content on** to
   select a detected location
   using its drive label, total size and free space. The wizard distinguishes the system disk
   from other drives. It creates a dedicated `david-pi-data` folder there. Choose
   an **Independent backup location**, or **Skip backups for now — configure
   later**. You
   do not need to know Linux mount paths for detected choices. See
   [storage choices](STORAGE.md) if a drive is missing or you need a custom folder.
4. **Select your modules.** Leave checked the features you want. Phone Backup
   also requires Media. Disabled modules retain any existing content and keys.
5. **Optional services.** Test your own provider credentials or use
   **Skip / configure later**. Local recipes, manual watchlists, text chat and
   supported uploaded images work without external provider keys. Pi-hole has a
   separate setup after the portal is ready.
6. **Verify and start.** Review your choices and select **Install my home server**
   once. Keep the page open while **Setting up your home** shows progress.

## 6. Wait for verification, then open your home

The progress bar tracks completion of five stages: preparing storage, saving
settings and local keys, starting features, checking features, and opening the
website. It also shows elapsed time. It is **not a countdown or
an estimate of minutes remaining**. Starting services can take longer than
other stages, especially on a slow machine or connection.

If the page briefly says it is reconnecting, keep Tailscale connected and allow
it to reconnect as setup hands over to the portal. Do not click Install again.
After setup saves its installation configuration, inspect the saved job from
the server terminal with `sudo david-pi status`. During earlier storage
preparation, this command can report **Installation has not been configured**;
use the browser's progress and the original terminal error at that stage.
If you close the browser, reopen your saved private
address. If the page says setup needs attention, follow its specific error and
the [recovery guidance](TROUBLESHOOTING.md#setup-is-slow-disconnected-or-failed).

When **Open your home server** appears, follow it and confirm your chosen name
appears on the home page. Check that your selected local modules open. A skipped
backup should say **not configured**; a newly configured backup remains
**restore unverified** until tested. Add household members only after your own
administrator account can open the portal.

The portal uses private Tailscale HTTPS and does not enable Funnel. Keep the
server powered on and Tailscale connected on devices accessing it.

## If setup stops

Correct the specific disk, port, account or certificate issue before retrying.
Use the recovery step that matches how far setup reached:

- **No private wizard link or claim code was issued:** rerun the
  [verified bootstrap command](#3-run-the-verified-bootstrap-on-the-server).
  Early package, sign-in or certificate failures can occur before verified
  release details are saved. If `sudo david-pi setup` says **Use a verified
  stable release installer**, return to that bootstrap command; do not invent
  an image address or change saved state.
- **A claim was issued, but installation configuration has not been saved:**
  run `sudo david-pi setup`. It keeps the saved account, address and installation
  identity and prints a fresh 15-minute token. It replaces the old token and
  browser session without reinstalling packages. Refresh the wizard and enter
  the new token; browser form entries may need to be entered again. If an
  installation operation is still running, let it finish before retrying.
- **Configuration was saved, but setup failed during service startup or
  verification:** inspect `sudo david-pi status`, then run `sudo david-pi setup`
  after correcting the reported problem. This requests repair of the saved
  installation. Follow the new job with `sudo david-pi status` until it succeeds,
  then run `sudo david-pi verify` and reopen the portal. It does not create
  another household or extend health-check time limits.

The 15 minutes is the deadline to claim, not to finish installation. Renewing a
token keeps the selected administrator login. A mistyped initial login currently
has no supported correction command; see [account troubleshooting](TROUBLESHOOTING.md#find-the-link-or-check-the-right-account).

Renewal checks that the server still uses the saved Tailscale account and
address. If either changed, restore the original connection before retrying;
renewing a code does not approve moving the server. An installation already in
progress is left running. Once installation configuration has been saved, the
same command resumes an incomplete installation instead of creating a new claim. Refreshing an
expired token in the browser alone does not renew it. Do not delete
configuration or storage to start over. See [troubleshooting](TROUBLESHOOTING.md).
