# Remote access — web share (`w`)

Control a workspace's monitor from any browser. Press **`w`** in the monitor to
toggle an ephemeral, per-workspace **web share**: a browser-reachable terminal
into a *dedicated* monitor for that cwd, exposed through an outbound tunnel with
authentication. Live-attach into a running agent works (unlike `textual serve`)
because the terminal runs over a real PTY.

This is the **Option-C MVP** from the remote-dashboard exploration. See
[`PROGRESS.md`](PROGRESS.md) for how it was chosen.

> ⚠️ **The share is effectively a remote-code-execution endpoint.** It gives a
> browser a *writable* terminal into a full-tool-access `claude` plus the box's
> environment (including any API keys). Authentication is mandatory and the
> share should be short-lived. Never run a writable tunnel without an auth gate.

## How it works

```
browser (xterm.js, served by ttyd)
   │  wss, TLS terminated by the tunnel
tunnel connector (cloudflared, outbound-only)        ← pluggable backbone
   │  http://127.0.0.1:<port>   (loopback only)
ttyd  -W  -i 127.0.0.1  -c mv:<token>                ← token gates HTTP + WS
   │  local tmux
tmux new-session -A -s mvweb-<hash> -c <cwd> bin/multiverse
   │  a fresh monitor scoped to this workspace
```

- **Loopback-bound.** `ttyd` listens only on `127.0.0.1`; the tunnel is the sole
  ingress and always terminates TLS, so the browser gets `https`/`wss` (xterm.js
  secure-context features work).
- **Per-share token.** Minted at trigger time, shown in the panel, and revoked
  when you stop the share (the token dies with `ttyd`). Basic-auth covers the
  WebSocket too, so it gates the terminal stream, not just the page.
- **Survives the local TUI.** The share runs as detached processes, so you can
  close the local monitor and keep controlling from your laptop. Any later
  monitor for the same cwd shows `🌐 web: ON` and can stop it. State lives at
  `$MULTIVERSE_ROOT/web_shares/<hash>/record.json`.
- **Co-located by necessity.** All coordination is local tmux + local files, so
  the share must run on the same box as the sessions. It cannot drive sessions
  on a remote host.

## Prerequisites

Rootless, outbound-only static binaries on the box (`multiverse setup` reports
whether they're present):

- [`ttyd`](https://github.com/tsl0922/ttyd) — the browser terminal bridge.
- [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) — the tunnel connector.

No root, no `TUN` device, no VPN enrollment — which is why this works in
locked-down / outbound-only containers (e.g. Beaker).

## Backbones

Selected by `MULTIVERSE_WEB_TUNNEL`. The token is always a second factor.
`cloudflare` and the back-compat alias `cloudflare-mtls` are the **same
connector** — the auth strength is set by the Access *policy* you attach in
Cloudflare, not by the plugin.

| Backbone | Edge auth | Plan | Use |
|:--|:--|:--|:--|
| `quick` (default) | none — per-share **token** only; public `*.trycloudflare.com` URL | none | dev / quick demos |
| `cloudflare` + Access **SSO / one-time-PIN** | **per-user identity** (Google/GitHub/email) **+ token** | **free** Zero Trust | recommended real use |
| `cloudflare` + Access **mTLS** | **device client-cert** (from your SSH key) **+ token** | **paid** (Business+) | true device-binding |

`quick` is zero-setup but the URL is public, so the token is the only gate and
the tunnel is rate-limited/ephemeral.

**Free vs paid for `cloudflare`:** the named tunnel + a free Access **SSO /
one-time-PIN** policy gives real per-user login from any browser at no cost —
identity-bound (only *you* can log in), though not device-bound. **mTLS** (a
client cert only your device holds, optionally generated from your SSH key) is
the *device*-bound upgrade, but **bring-your-own-CA mTLS for Access requires a
paid plan** — the free plan cannot use a custom CA in Access policies. For free
*device*-binding, self-host mTLS on a small VPS (see the end of this doc).

## One-time setup: `cloudflare` (named tunnel + Access)

Edge auth must be enforced where the browser's TLS terminates: Cloudflare's
edge. That requires a **named tunnel on your own domain** — the quick tunnel
can't run an Access policy. There is no laptop↔box SSH anywhere: your laptop's
TLS handshake goes to Cloudflare; the box only dials outbound.

You need a (free) **Cloudflare account** — there is no "cloudflared account";
`cloudflared` is just the connector binary. Check your account/plan at
**dash.cloudflare.com → Zero Trust → Settings → Plans**.

1. **Add a domain to Cloudflare** (any domain on a Cloudflare zone; free DNS).
2. **Create a named tunnel** and a credentials file on the box:
   ```bash
   cloudflared tunnel login
   cloudflared tunnel create multiverse        # prints a tunnel UUID/name
   ```
3. **Ingress config** — map a hostname to the loopback port `ttyd` will bind.
   In `~/.cloudflared/config.yml`:
   ```yaml
   tunnel: multiverse
   credentials-file: /root/.cloudflared/<TUNNEL_UUID>.json
   ingress:
     - hostname: mv.yourdomain.com
       service: http://127.0.0.1:7681
     - service: http_status:404
   ```
   ```bash
   cloudflared tunnel route dns multiverse mv.yourdomain.com
   ```
4. **Access application + policy** (Cloudflare Zero Trust dashboard) — create a
   **self-hosted Access application** for `mv.yourdomain.com`, then attach a
   policy. Either rejects unauthorized requests at the edge (including the
   WebSocket) *before* traffic reaches the box:

   - **Free — SSO / one-time-PIN (recommended):** policy = *Allow* where the
     email is yours (Google/GitHub login, or email one-time-PIN). Real per-user
     identity, any browser, no client install, **$0**.
   - **Paid — mTLS (device-binding):** create an **mTLS root CA** (Access →
     Service auth → Mutual TLS), upload your CA, and add a policy that
     **requires a valid client certificate**. Rejects any device without your
     cert. Requires a **paid plan (Business+)** — confirmed: the free plan can't
     use a custom CA in Access. For free device-binding, use the self-hosted
     VPS path instead (below).
5. **(mTLS only) Generate your client cert** (optionally from your existing SSH
   key) and **import it into your laptop's browser/keychain**. You enroll each
   device once — that's the nature of device-only auth; the token is the
   complement that works anywhere instantly. (SSO policies need no cert import.)
6. **Point the plugin at it** — create `$MULTIVERSE_ROOT/web-tunnel.env`
   (sourced automatically by `bin/multiverse`):
   ```sh
   export MULTIVERSE_WEB_TUNNEL=cloudflare        # SSO or mTLS — same connector
   export MULTIVERSE_WEB_TUNNEL_NAME=multiverse
   export MULTIVERSE_WEB_HOSTNAME=mv.yourdomain.com
   export MULTIVERSE_WEB_LOCAL_PORT=7681          # must match the ingress rule
   ```

Relaunch `multiverse` and press `w` → Start. The panel shows the hostname URL +
the per-share token. Open it on an authorized device: Cloudflare runs your
Access policy (SSO login or client-cert check), then `ttyd` prompts for `mv` /
`<token>`.

## Security model (defense in depth)

1. **Edge auth** (`cloudflare`): only your authorized identity (SSO) or your
   enrolled device (mTLS) reaches the box at all — with mTLS, not even someone
   on your same IP.
2. **Per-share token** (`ttyd -c`): second factor, covers the WebSocket, rotated
   every share.
3. **Loopback bind**: `ttyd` is never publicly reachable; only the tunnel hits
   `127.0.0.1`.
4. **Ephemeral + visible + revocable**: the share exists only while you want it,
   shows `🌐 web: ON` in every monitor for the cwd, and stops with `s`.

## Free device-binding without a paid plan: self-hosted mTLS on a VPS

If you want true *device*-binding (mTLS) but not a paid Cloudflare plan, run the
TLS edge yourself on a cheap VPS — no laptop↔box SSH is involved (the box dials
*out* to the VPS):

1. On a small VPS you control, run Caddy/nginx terminating TLS with **client
   certificate required** (your own CA), reverse-proxying (WebSocket-enabled) to
   a local port.
2. From the box (outbound): `ssh -R 127.0.0.1:7681:127.0.0.1:7681 vps` so the
   VPS's loopback reaches the box's `ttyd`.
3. Your laptop hits `https://vps-host/` and must present your client cert; the
   ttyd token is the second factor.

This is the same guarantee as Cloudflare mTLS, self-owned. It can be added as a
dedicated `web_share` provider (the tunnel layer is pluggable) if you go this
route.

## Limits / notes

- Multiple workspaces each need their own hostname→port mapping (one ingress
  rule + Access app per concurrently-shared workspace), or a path-based route.
- `quick` tunnels don't support client certs and are for non-sensitive use only.
- Stopping the local monitor does **not** stop the share (by design) — stop it
  explicitly with `w` → `s` when you're done.
- nested-tmux detach: attaching into a session from the in-browser monitor is
  tmux-in-tmux; detach with your tmux prefix + `d` to return to the dashboard.
