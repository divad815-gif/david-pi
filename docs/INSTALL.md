# Install your private home server

This guide describes the portable 10.0.0 release. It remains an unpublished
candidate until the [release gates](RELEASE.md) pass. Do not use an older public
download as a test of this guide.

## 1. Prepare the machine

Use 64-bit Raspberry Pi OS based on Debian 13 on a Pi 4/5, Debian 13 AMD64, or
Ubuntu Server 24.04 LTS AMD64. Start with a supported native installation;
Windows, WSL, macOS, network filesystems and 32-bit machines are not targets.
Use your own Linux administrator account. There is no shared default password.
If you need SSH, enable it using the OS instructions and retain a working local
or SSH session until the portal is verified.

Have 4 GB memory and 8 GiB free OS space. Store large libraries on a prepared
ext4 drive when possible. Plug in the data drive before setup. The installer
will not format or repartition it. [Prepare storage first](STORAGE.md).

## 2. Connect the setup device

Install Tailscale on the computer or phone you will use for the wizard. Sign in
to your own account. The server must join that same network. Review
[Tailscale setup](TAILSCALE.md) before inviting household members.

![Private setup sequence](images/setup-flow.svg)

## 3. Run the verified bootstrap

Once this stable release is published:

```sh
curl -fsSL https://github.com/divad815-gif/david-pi/releases/latest/download/install.sh | sudo bash
```

For inspection first, download `install.sh` and `install.sh.sha256` from the
same stable GitHub release, run `sha256sum -c install.sh.sha256`, read the script,
then run `sudo bash install.sh`. The checksum confirms download integrity; the
release publisher and HTTPS channel remain part of the trust model.

The bootstrap checks the OS, CPU, memory, storage and occupied ports. It installs
missing prerequisites from official package sources and asks for the intended administrator login and a suggested hostname.
It shows the Tailscale login link when sign-in is required. It inspects existing Serve configuration
and stops on conflicts instead of replacing another private website.

## 4. Complete the browser wizard

1. Open the printed private HTTPS setup link from your Tailscale-connected device.
2. Claim the server using the short-lived, single-use claim token. The identity
   must match the intended administrator chosen during terminal setup. Do not
   forward this link or token.
3. Enter the website display name, for example **John’s home**. The terminal
   already suggested a hostname such as **john-pi** before creating HTTPS.
   A hostname collision may add a suffix; keep the actual address shown.
4. Choose a prepared local ext4 folder/drive. The wizard creates its own
   application directory. Existing unrelated files remain untouched.
5. Select modules. Phone Backup also enables Media. Skip modules you do not need;
   you can enable them later without losing previously saved content.
6. Configure optional providers or choose **Skip**. Local recipes, watchlists,
   text chat and uploaded chat images need no provider account.
7. Choose your timezone, movie country, and optional independent backup location.
8. Run the final verification. Save the displayed private address. Add a
   household member only after your own account can open the portal.

The portal runs on loopback and Tailscale supplies HTTPS. Nothing is exposed
through Funnel. Keep the local terminal available until installation succeeds.

## If setup stops

Read the specific failure shown before retrying. Correct the disk, port,
Tailscale or credential issue and run `sudo david-pi setup` again. Setup is
resumable. An expired claim requires a new terminal-generated token; refreshing
an old link does not grant ownership. Never delete installation configuration to
work around an account problem. See [troubleshooting](TROUBLESHOOTING.md).
