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
ttyd  -W  -i 127.0.0.1  -P 30  [-c mv:<token>]       ← token optional (see below)
   │  PTY — NO wrapping tmux, so attach isn't a nested tmux attach
bin/multiverse                                       ← a fresh monitor for this cwd
```

- **Loopback-bound.** `ttyd` listens only on `127.0.0.1`; the tunnel is the sole
  ingress and always terminates TLS, so the browser gets `https`/`wss` (xterm.js
  secure-context features work).
- **Per-share token (optional on `cloudflare`).** For `quick` it's the only gate
  and always on. For an edge-authenticated backbone (`cloudflare`/Access) it's an
  opt-in second factor — **default off** (`MULTIVERSE_WEB_TOKEN`, or toggle per
  share with `t` in the panel). When used, ttyd basic-auth covers the page *and*
  the WebSocket; it's minted per share and dies with `ttyd`.
- **Survives the local TUI.** The share runs as detached processes, so you can
  close the local monitor and keep controlling from your laptop. Any later
  monitor for the same cwd shows `🌐 web: ON` and can stop it. State lives at
  `$MULTIVERSE_ROOT/web_shares/<hash>/record.json`.
- **Live-attach is a single tmux layer.** The monitor runs *directly* under
  ttyd's PTY (not inside a wrapping tmux), so its attach (`App.suspend()` + `tmux
  attach-session`) works exactly like local attach — detach with the normal
  `Ctrl-b d`. Wrapping it in tmux would make attach a *nested* tmux attach, which
  tmux refuses. ttyd respawns the (stateless) monitor on reconnect; the claude
  sessions it manages persist regardless.
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

Selected by `MULTIVERSE_WEB_TUNNEL` (default `quick`); the `w` panel also starts
`quick` on demand (`q`) even when `cloudflare` is the default. The ttyd token is
mandatory for `quick` (its only gate) and **optional, default off** for
`cloudflare` (`MULTIVERSE_WEB_TOKEN`, or `t` in the panel).
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
2. **Create the tunnel.** Keep all cloudflared files in an **absolute, persistent**
   dir (on Beaker, somewhere under `/weka`; **not** `~/.cloudflared`, which is
   ephemeral and HOME-dependent):
   ```bash
   export CFDIR=/abs/persistent/cloudflared        # an absolute path you control
   mkdir -p "$CFDIR"; export TUNNEL_ORIGIN_CERT="$CFDIR/cert.pem"
   cloudflared tunnel login          # headless: prints a URL — open it on your laptop, pick your zone
   # `login` writes cert.pem to ~/.cloudflared REGARDLESS of TUNNEL_ORIGIN_CERT, so move it:
   cp ~/.cloudflared/cert.pem "$CFDIR/cert.pem"
   cloudflared tunnel create --credentials-file "$CFDIR/multiverse.json" multiverse   # note the UUID
   ```
3. **Ingress config** at `$CFDIR/config.yml` — use **literal absolute paths**. (A
   *quoted* heredoc leaves a literal `$CFDIR` in the file, which breaks the tunnel
   — write the real path or use an *unquoted* heredoc so it expands.)
   ```yaml
   tunnel: <TUNNEL_UUID>
   credentials-file: /abs/persistent/cloudflared/multiverse.json    # absolute!
   ingress:
     - hostname: mv.yourdomain.com
       service: http://127.0.0.1:7681
     - service: http_status:404
   ```
   ```bash
   cloudflared tunnel route dns multiverse mv.yourdomain.com
   cloudflared tunnel --config "$CFDIR/config.yml" ingress validate   # expect: OK
   ```
   *(To share several workspaces at once, replace this single rule with **path
   slots** — see [Multiple workspaces at once](#multiple-workspaces-at-once-path-slots) below.)*
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
6. **Point the plugin at it** — create `web-tunnel.env` at the **monitor's**
   `MULTIVERSE_ROOT` (sourced automatically by `bin/multiverse`):
   ```sh
   export MULTIVERSE_WEB_TUNNEL=cloudflare          # SSO or mTLS — same connector
   export MULTIVERSE_WEB_TUNNEL_NAME=multiverse
   export MULTIVERSE_WEB_HOSTNAME=mv.yourdomain.com
   export MULTIVERSE_WEB_LOCAL_PORT=7681            # must match the ingress rule
   export MULTIVERSE_WEB_TUNNEL_CONFIG=/abs/persistent/cloudflared/config.yml  # absolute --config
   export MULTIVERSE_WEB_TOKEN=off                  # off = Access-only; on = +ttyd token (or toggle with `t`)
   # optional — concurrent multi-workspace shares (see "Multiple workspaces" below):
   export MULTIVERSE_WEB_SLOTS="proj-a:7681 proj-b:7682 proj-c:7683"
   ```

   > ⚠️ **HOME-divergence trap — the #1 setup mistake.** `MULTIVERSE_ROOT` is
   > resolved by the *monitor* (`$MULTIVERSE_ROOT`, else `$HOME/.multiverse`). In
   > some deployments (e.g. Beaker, where a wrapper sets `HOME=/weka/...` for the
   > monitor) that is **not** your interactive shell's `$HOME/.multiverse` — so a
   > `web-tunnel.env` written from your shell can land where the monitor never
   > looks, and the backbone silently falls back to `quick`. Find the real path
   > from a running monitor and write the file there:
   > ```bash
   > tr '\0' '\n' < /proc/$(pgrep -f src.monitor | head -1)/environ | grep '^MULTIVERSE_ROOT='
   > ```
   > `MULTIVERSE_WEB_TUNNEL_CONFIG` solves the same trap for the tunnel: without
   > it, `cloudflared tunnel run` only auto-discovers `~/.cloudflared/config.yml`
   > under the *monitor's* `$HOME`. With it, the plugin runs
   > `cloudflared tunnel --config <abs> run` (the flag must precede `run`).

Relaunch `multiverse` and press `w` → **Enter** to start `cloudflare` (or `q`
for a one-off `quick` tunnel; `t` toggles the token first). Open the hostname on
an authorized device: Cloudflare runs your Access policy (SSO login or
client-cert check). With `MULTIVERSE_WEB_TOKEN=off` that's the whole login; with
the token on, `ttyd` then prompts once for `mv` / the per-share token shown in
the panel (skip the dialog by opening `https://mv:<token>@mv.yourdomain.com`).

## Multiple workspaces at once (path slots)

By default the `cloudflare` backbone is **one hostname → one port → one share**:
the named tunnel routes `mv.yourdomain.com` to a single ttyd, so a second
concurrent share fails to bind the port. To share several workspaces at once,
declare **path slots** — each workspace gets its own path under the *same*
hostname:

```sh
export MULTIVERSE_WEB_SLOTS="open-vocab:7681 objaverse:7682 misc:7683"
```

- Each `label:port` is a slot. A cloudflare share takes a free slot and is served
  at `https://mv.yourdomain.com/<label>/` — the plugin runs ttyd with
  `-b /<label>` so it serves correctly under the sub-path.
- **Path = workspace name, or customizable.** On start, the plugin picks the slot
  whose label matches the workspace's folder name (slugged); if none matches, the
  first free slot. Name slots after your workspaces and the paths auto-match — or
  name them anything.
- **N concurrent** = number of slots. Starting one when all slots are taken gives
  a clear *"all slots in use"* error.

The cloudflared ingress needs **one rule per slot** — a regex matching the
label's path, routed to that slot's loopback port:

```yaml
ingress:
  - hostname: mv.yourdomain.com
    path: "^/open-vocab(/.*)?$"
    service: http://127.0.0.1:7681
  - hostname: mv.yourdomain.com
    path: "^/objaverse(/.*)?$"
    service: http://127.0.0.1:7682
  - hostname: mv.yourdomain.com
    path: "^/misc(/.*)?$"
    service: http://127.0.0.1:7683
  - service: http_status:404
```

> **No Cloudflare dashboard change.** Your one self-hosted Access app on
> `mv.yourdomain.com` already gates *every path* under it — the SSO policy
> applies to `/open-vocab`, `/objaverse`, … alike. You add **no** Access apps,
> **no** DNS records, **no** team settings; the only change is this ingress in
> your own `config.yml`.

Keep the ingress and `MULTIVERSE_WEB_SLOTS` **in sync** (same labels + ports).
Generating both from one slot list in your init script is the cleanest way (see
[Persisting across container restarts](#limits--notes)). Verify routing without a
live request:

```bash
cloudflared tunnel --config "$CFDIR/config.yml" ingress validate
cloudflared tunnel --config "$CFDIR/config.yml" ingress rule https://mv.yourdomain.com/open-vocab/
```

**Boundary:** paths are the **pre-declared slot labels**, so adding a new
workspace path means editing the slot list + regenerating the ingress. Truly
arbitrary per-share paths would need an always-on local path-router in front of
the ttyds instead of static ingress — more moving parts, and unnecessary for a
known set of workspaces.

## Security model (defense in depth)

1. **Edge auth** (`cloudflare`): only your authorized identity (SSO) or your
   enrolled device (mTLS) reaches the box at all — with mTLS, not even someone
   on your same IP.
2. **Per-share token** (`ttyd -c`): mandatory for `quick` (its only gate); an
   opt-in second factor for `cloudflare` (`MULTIVERSE_WEB_TOKEN` / `t`, default
   off), covering the WebSocket, rotated every share.
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

- Multiple workspaces can be shared at once via **path slots** under one hostname
  (one Access app covers all paths) — see [Multiple workspaces at once](#multiple-workspaces-at-once-path-slots). Without slots, `cloudflare` is one share at a time.
- `quick` tunnels don't support client certs and are for non-sensitive use only.
- Stopping the local monitor does **not** stop the share (by design) — stop it
  explicitly with `w` → `s` when you're done.
- **Detach = normal `Ctrl-b d`.** The in-browser monitor runs directly under ttyd
  (no wrapping tmux), so attaching into a session is a single tmux layer.
- **Idle keepalive.** ttyd pings every 30s (`-P 30`) because Cloudflare drops
  idle WebSockets at ~100s; long-lived connections also hit Cloudflare's ~8h cap
  (reconnect + re-login — the monitor respawns, sessions persist).
- **Persisting across container restarts.** Only `/weka`-style persistent storage
  survives; binaries in `/usr/local/bin` don't. Reinstall `ttyd`/`cloudflared`
  and (re)write `config.yml` + `web-tunnel.env` from your container-init script on
  each boot (an idempotent helper is the cleanest). The Cloudflare tunnel, DNS
  route, Access app, and creds persist server-side / in `/weka`, so they need no
  recreation — only the config + env do.
