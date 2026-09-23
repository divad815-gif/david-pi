# Tailscale: required private access

![Private household access and separate backup storage](images/private-home.svg)

Tailscale connects your devices privately and supplies the server’s HTTPS
address. A Tailscale account is always required for this release. Check the
[current plans](https://tailscale.com/pricing) for your household size and use.

1. Create your own account at [Tailscale](https://tailscale.com/).
2. Install the [Tailscale app](https://tailscale.com/download) on the phone or
   computer opening setup and sign in.
3. Run the server installer. Follow its Tailscale login link and approve the
   server under the intended owner’s network.
4. Enable MagicDNS and HTTPS certificates in your Tailscale DNS settings if the
   setup check requests them. Certificate issuance records the certificate
   hostname in public certificate-transparency logs; the website itself remains
   private. Choose a hostname that does not reveal sensitive information.
5. Return to the terminal and open the private HTTPS wizard link from step 2’s device.

The wizard uses Tailscale Serve to forward private HTTPS to the local setup
service, then switches its own mapping to the completed portal. It inspects and
preserves unrelated existing Serve settings. If HTTPS port 443 already belongs
to another service, resolve that conflict deliberately before retrying. Never
use `tailscale serve reset` merely to get past a setup error.

After installation, invite each household member through your Tailscale admin
console using their own login, then separately admit that exact identity in the
portal’s household settings. Joining the network does not make someone a portal
member or administrator. Limit network access through your Tailscale access
policy where needed. Keep at least one verified portal administrator.

If the server is unreachable, check Tailscale is signed in on both devices,
confirm the actual hostname in the Tailscale machine list, and inspect Serve
status from a local server terminal. A website display-name change does not
change the address. A deliberate address change requires re-opening the new
address and re-pairing/reconnecting Android as directed.

Official references: [Serve](https://tailscale.com/docs/features/tailscale-serve),
[HTTPS certificates](https://tailscale.com/docs/how-to/set-up-https-certificates),
[inviting users](https://tailscale.com/docs/how-to/invite-users).
