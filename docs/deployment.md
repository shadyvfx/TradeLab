# Running TradeLab in production

`serve.py` runs all of TradeLab in one process: the website, sign-up and sign-in, guest limits, the account-only lessons, each account's saved progress, the email checker and the AI tutor with its streamed answers. It needs Python 3.9 or newer and the packages in `requirements.txt`:

```bash
python3 -m pip install -r requirements.txt
python3 serve.py
```

What it adds over `flask run`:

- **A production server.** [Hypercorn](https://hypercorn.readthedocs.io/) with 32 worker threads, on macOS, Linux and Windows. It handles HTTPS itself (TLS 1.2 or newer, HTTP/2), and slow or idle connections never hold up other visitors.
- **HTTPS three ways:** a certificate it makes for your computer, your own certificate (such as Let's Encrypt's), or behind a reverse proxy like Caddy that handles HTTPS for it.
- **Security headers** on every response, including a Content-Security-Policy that only runs TradeLab's own scripts, and Strict-Transport-Security on real domain names.
- **Rate limits** on sign-in, sign-up, the email checker and the tutor (see [Security](#security)).
- A **health check** at `/api/health`, one log line per request (never the query string or body), and a clean stop on Control-C or `SIGTERM`.

The Mac launcher, `Start TradeLab Demo.command`, starts this same server on http://127.0.0.1:5001.

## Choose your setup

| You want | Run | Section |
| --- | --- | --- |
| TradeLab on this computer | `python3 serve.py` | [1](#1-on-this-computer) |
| Phones and laptops on the same Wi-Fi, over HTTPS | `python3 serve.py --https --host 0.0.0.0` | [2](#2-https-on-your-network) |
| A public website with a domain name | Caddy + `python3 serve.py --behind-proxy` | [3](#3-a-server-with-a-domain-name) |
| A hosting platform (Render, Railway, Fly.io) | `python serve.py --behind-proxy --host 0.0.0.0` | [4](#4-hosting-platforms) |

## 1. On this computer

```bash
python3 serve.py
```

Open http://localhost:5001. Only this computer can reach it, and it doesn't need HTTPS: browsers treat `localhost` as secure. If the model runs on another computer, add its address: `--model-url 192.168.1.23:8080`.

## 2. HTTPS on your network

For a demo where people open TradeLab on their own phones or laptops:

```bash
python3 serve.py --https --host 0.0.0.0
```

It prints the addresses to share, such as `https://192.168.1.20:8443`. All devices must be on the same Wi-Fi, and if macOS asks whether Python may accept incoming connections, choose **Allow**.

The first run creates two things in `~/.tradelab/certs`:

- `tradelab-local-ca.pem`: TradeLab's own **certificate authority**, made once for this computer.
- `tradelab-server.pem`: a certificate for this computer's names and network addresses, signed by that authority. When you join a different network, TradeLab makes a new one automatically.

Browsers show a warning the first time ("Your connection is not private"), because they don't know TradeLab's authority yet. You can click through it (**Advanced**, then **Proceed**), or trust the authority once on each device and the warning is gone for good, even on new networks. Copy it with a `.crt` name first, which phones recognize:

```bash
cp ~/.tradelab/certs/tradelab-local-ca.pem ~/Desktop/tradelab-local-ca.crt
```

- **Mac (Safari, Chrome, Edge):** double-click the `.crt` file to add it to Keychain Access. Double-click **TradeLab local CA** there, open **Trust**, set **When using this certificate** to **Always Trust**, and close the window. Or in Terminal: `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain ~/Desktop/tradelab-local-ca.crt`
- **Windows:** double-click the `.crt` file, choose **Install Certificate**, **Current User**, **Place all certificates in the following store**, and pick **Trusted Root Certification Authorities**.
- **iPhone or iPad:** AirDrop or email the `.crt` file to it and open it. Install it in **Settings > General > VPN & Device Management**, then turn it on in **Settings > General > About > Certificate Trust Settings**.
- **Android:** **Settings > Security > Encryption & credentials > Install a certificate > CA certificate**, then pick the file. Menu names vary by phone.
- **Firefox** keeps its own list: **Settings > Privacy & Security > Certificates > View Certificates > Authorities > Import**, and tick "Trust this CA to identify websites".

Trust it only on your own devices, and keep `tradelab-local-ca-key.pem` private: anyone with that file could make certificates your devices would trust. When you no longer need it, remove **TradeLab local CA** from each device.

To let people type `http://`, add `--redirect-port 8080`: plain HTTP on port 8080 redirects to HTTPS. If phones can't connect at all, the venue's Wi-Fi may keep devices apart ("client isolation"); a phone's hotspot works instead.

## 3. A server with a domain name

For a public website, run TradeLab behind [Caddy](https://caddyserver.com/). Caddy gets a free certificate from Let's Encrypt, renews it, redirects HTTP to HTTPS and compresses responses. TradeLab listens only on `127.0.0.1`, so the internet reaches it only through Caddy. These steps are for Ubuntu or Debian; run them as root (with `sudo`).

1. **Point the domain at the server.** In your domain's DNS settings, add an `A` record for, say, `tradelab.example.com` with the server's IP address. Open ports 80 and 443 in the server's firewall.
2. **Install TradeLab** as its own user, with the commands at the top of [`deploy/tradelab.service`](../deploy/tradelab.service). If the version you want isn't on the repository's default branch, add `-b` and the branch name to `git clone`:

   ```bash
   useradd --system --home-dir /opt/tradelab --shell /usr/sbin/nologin tradelab
   git clone https://github.com/shadyvfx/InvestIQ /opt/tradelab
   python3 -m venv /opt/tradelab/.venv
   /opt/tradelab/.venv/bin/pip install -r /opt/tradelab/requirements.txt
   ```

3. **Start it as a service**, so it runs at boot and restarts if it stops. The service keeps accounts in `/var/lib/tradelab/tradelab.db` and can't write anywhere else:

   ```bash
   cp /opt/tradelab/deploy/tradelab.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now tradelab
   curl http://127.0.0.1:5001/api/health     # {"status":"ok"}
   ```

4. **Install Caddy** ([instructions](https://caddyserver.com/docs/install)), copy [`deploy/Caddyfile`](../deploy/Caddyfile) to `/etc/caddy/Caddyfile`, replace `tradelab.example.com` with your domain, and run `systemctl reload caddy`.

Open `https://tradelab.example.com`. The logs are in `journalctl -u tradelab -f` and `journalctl -u caddy -f`.

**The tutor's model.** Run llama-server on the same server and keep it on `127.0.0.1:8080`, or set `TRADELAB_LLAMA_URL` in the service file to a computer on a private network. Never open llama-server's port to the internet. The tutor answers signed-in accounts only. Without a model, it says it's unavailable; to use the prewritten answers instead, set `tutorApiEnabled: false` in `js/config.js`.

**Backups.** The accounts are one SQLite file. Copy it safely while TradeLab runs (`apt install sqlite3` first if needed):

```bash
sqlite3 /var/lib/tradelab/tradelab.db ".backup '/root/tradelab-$(date +%F).db'"
```

**Updating.** `cd /opt/tradelab && git pull && .venv/bin/pip install -r requirements.txt && systemctl restart tradelab`.

### Without Caddy

TradeLab can handle HTTPS itself with a Let's Encrypt certificate from [certbot](https://certbot.eff.org/):

```bash
sudo certbot certonly --standalone -d tradelab.example.com
sudo python3 serve.py --https --host 0.0.0.0 --port 443 --redirect-port 80 \
  --cert /etc/letsencrypt/live/tradelab.example.com/fullchain.pem \
  --key /etc/letsencrypt/live/tradelab.example.com/privkey.pem
```

This runs as root (ports 80 and 443, and the key file, need it), and TradeLab has to restart after each renewal to load the new certificate. Caddy avoids both, which is why it's the recommended setup.

## 4. Hosting platforms

Platforms such as Render, Railway and Fly.io handle HTTPS themselves and tell your app which port to use in `PORT`, which `serve.py` reads. Set:

- **Build command:** `pip install -r requirements.txt`
- **Start command:** `python serve.py --behind-proxy --host 0.0.0.0`
- **Health check path:** `/api/health`
- **`TRADELAB_DB`** to a file on a persistent disk or volume, such as `/data/tradelab.db`. Without one, every deploy starts with an empty database and all accounts are lost.
- **`TRADELAB_LLAMA_URL`** to a model the platform can reach, or turn the local tutor off as described in section 3.

## Options

Every option can also be set with an environment variable, which is how hosting platforms and the service file configure it.

| Option | Environment variable | What it does |
| --- | --- | --- |
| `--host` | `TRADELAB_HOST` | Address to listen on: `127.0.0.1` (default, this computer only) or `0.0.0.0` (every network) |
| `--port` | `PORT`, `TRADELAB_PORT` | Port: 5001 by default, 8443 with `--https` |
| `--https` | `TRADELAB_HTTPS=1` | Serve over HTTPS, with a certificate made for this computer unless `--cert` and `--key` are given |
| `--cert`, `--key` | `TRADELAB_TLS_CERT`, `TRADELAB_TLS_KEY` | Your own certificate (with its intermediate certificates) and private key, as PEM files |
| `--cert-name` | | Another hostname or IP address for the certificate made for this computer (repeatable) |
| `--redirect-port` | `TRADELAB_REDIRECT_PORT` | Also listen for plain HTTP on this port and redirect it to HTTPS |
| `--behind-proxy [N]` | `TRADELAB_PROXY_HOPS` | Trust `X-Forwarded-For`, `-Proto` and `-Host` from N reverse proxies (1 if you leave out N) |
| `--threads` | `TRADELAB_THREADS` | Requests handled at once (default 32; a streamed tutor answer uses one for up to two minutes) |
| `--db` | `TRADELAB_DB` | The user database file (default `tradelab.db` in the project folder) |
| `--model-url` | `TRADELAB_LLAMA_URL` | llama-server's address, such as `192.168.1.23:8080` |
| `--no-access-log` | `TRADELAB_NO_ACCESS_LOG=1` | Don't log a line per request |
| | `TRADELAB_CERT_DIR` | Where the made certificates are kept (default `~/.tradelab/certs`) |
| | `TRADELAB_RATE_LIMITS=off` | Turn off the rate limits, for load tests |

## Security

- **HTTPS.** Over HTTPS the sign-in and guest cookies are marked `Secure`, so browsers never send them unencrypted. On real domain names, every HTTPS response also carries `Strict-Transport-Security`, so browsers always use HTTPS there. It's never sent for `localhost`, `.local` names or IP addresses, where it would break plain-HTTP development on the same computer.
- **Rate limits.** Each network gets 20 sign-ins per 5 minutes, 20 new accounts per hour, 120 email checks per 5 minutes and 40 tutor questions per 5 minutes; after that, requests get `429` with the seconds to wait. A whole classroom on one Wi-Fi fits within these. Requests from the server's own computer are never limited.
- **Behind a proxy, always use `--behind-proxy`.** Without it, every request seems to come from the proxy, so TradeLab can't tell visitors apart and can't see that they use HTTPS. Use it only behind a proxy: otherwise visitors could fake the forwarded headers.
- **Content-Security-Policy.** The page runs only scripts from TradeLab's own files, plus the theme script in `index.html`, allowed by its hash. The hash is worked out from the file when it changes, so editing that script needs no other change. Forms and connections go only to TradeLab, and other websites can't show it in a frame.
- **Only the site's own files are served:** `index.html`, `assets/`, `css/` and `js/`. Everything else in the folder, including `server/lesson_content.json` (the account-only lessons, handed out only by `GET /api/lessons/<id>`), answers 404.
- **Keep private:** `tradelab.db` (emails and password hashes; it's in `.gitignore`), the private keys in `~/.tradelab/certs`, and llama-server's port.
- **Guest sessions.** Each new visitor gets a row in `guest_sessions` (a hash of the guest cookie and two counters), deleted after 365 days. Starting guest sessions isn't rate limited, so on a busy public site keep an eye on the database's size.
- **Not built yet:** confirmation emails and password reset. The email checker confirms that an address's domain receives mail, not that the mailbox exists.

## Troubleshooting

- **"Port 5001 is already in use":** another copy of TradeLab (or something else) is running. Stop it, or pick another port with `--port`.
- **"This computer won't let TradeLab use port 443":** ports below 1024 need administrator rights. Use `--port 8443`, or Caddy.
- **"Your connection is not private":** expected with the certificate TradeLab makes, until you trust its authority on that device (section 2).
- **Other devices can't connect:** check that you used `--host 0.0.0.0`, that they're on the same Wi-Fi, and that the computer's firewall allows Python. Some venue networks keep devices apart; a phone hotspot avoids that.
- **The tutor doesn't answer:** the server prints the model address it uses at startup (`Tutor model:`). Check that llama-server answers there, for example `curl http://192.168.1.23:8080/health`.
