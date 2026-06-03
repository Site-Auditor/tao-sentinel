<!--
  deploy/README.md -- deployment guide for tao-sentinel.

  Covers: DNS, local quickstart with Docker Compose, production TLS (certbot on
  the host OR an external reverse proxy / Cloudflare), pulling the GHCR image,
  the GitHub Actions secrets, and security notes. This is the deployment doc;
  the project overview lives in the top-level README.md.
-->

# Deploying tao-sentinel

tao-sentinel ships as a single container image driven by the `tao-sentinel`
CLI. The Compose stack at the repo root runs three services on a private
network:

| Service     | Image                                  | Role                                                                 |
| ----------- | -------------------------------------- | -------------------------------------------------------------------- |
| `dashboard` | `tao-sentinel` (`serve`)               | Read-only web UI + JSON API on `:8787`. **Not** published to the host. |
| `watcher`   | `tao-sentinel` (`watch`)               | Background alert engine; polls and dispatches notifications.         |
| `proxy`     | `nginx:1.27-alpine`                    | Reverse proxy; the only service that exposes a port to the host.     |

Only `proxy` is reachable from outside the Compose network. It forwards to the
`dashboard` service over the internal DNS name `dashboard:8787`.

## 1. DNS

Point the production hostname at the server's public IP with an **A record**:

```
tao.insightfulbytes.com.   A   <your-server-ipv4>
```

(Add an `AAAA` record too if the host has a public IPv6 address.) The nginx
configs use `server_name tao.insightfulbytes.com`; the plain-HTTP config is
also `default_server`, so requests without a matching `Host` header (such as a
local `curl`) still reach the dashboard.

## 2. Local quickstart

Requirements: Docker with the Compose plugin (`docker compose`).

```bash
# From the repo root.
cp deploy/sentinel.example.yaml sentinel.yaml   # docker-ready config
docker compose up -d --build                     # build the image and start the stack
open http://localhost:8080                        # dashboard via the proxy
```

What this does:

- Builds `tao-sentinel:latest` from the repo `Dockerfile` (Compose uses
  `image: ${IMAGE:-tao-sentinel:latest}` with a `build: .` fallback, so the
  same `compose.yaml` builds locally and pulls on a server).
- Starts `dashboard`, `watcher`, and `proxy`. The proxy publishes `8080:80`, so
  the UI is at <http://localhost:8080>. The dashboard's own `:8787` is **not**
  published.
- Mounts `./sentinel.yaml` read-only at `/config/sentinel.yaml` in both
  `dashboard` and `watcher`. Both pass `--config /config/sentinel.yaml` so the
  portfolio card and state-backed alerts share one config.
- Creates the named volume `sentinel-state`, mounted at `/data` in `dashboard`
  and `watcher`. The example config writes state to `/data/state.json`.

### API key vs. mock mode

The image needs no secrets to run. `TAOSTATS_API_KEY` is passed through from the
host environment into the containers (the example config uses
`api_key: env:TAOSTATS_API_KEY`). If it is empty or unset, tao-sentinel falls
back to the **deterministic mock client**, so the whole stack runs offline with
realistic-looking data. Provide a real key to use live taostats data:

```bash
export TAOSTATS_API_KEY=sk-...     # before `docker compose up`
```

### Stopping

```bash
docker compose down            # stop and remove containers (keeps the volume)
docker compose down -v         # also delete the sentinel-state volume
```

The dashboard handles `SIGINT` cleanly (the image sets `STOPSIGNAL SIGINT`), so
`docker compose stop`/`down` shuts it down gracefully and it prints `Stopped`.

## 3. Production TLS

For a public deployment, terminate TLS at `tao.insightfulbytes.com`. Two
supported options:

### Option A -- certbot on the host (nginx terminates TLS)

1. Switch the proxy to the TLS config. Either mount
   `deploy/nginx/tao-sentinel-tls.conf` at
   `/etc/nginx/conf.d/default.conf` in the `proxy` service (replacing the
   `tao-sentinel.conf` mount), or uncomment the TLS block at the bottom of
   `tao-sentinel.conf`. Both define the same proxy.
2. Change the proxy's published ports in `compose.yaml` from `"8080:80"` to
   `"80:80"` and add `"443:443"` (there is a comment at that line).
3. Mount the certificate store and ACME webroot into the `proxy` service:
   `/etc/letsencrypt:/etc/letsencrypt:ro` and
   `/var/www/certbot:/var/www/certbot:ro`.
4. Issue a certificate (webroot challenge served by the port-80 server block):

   ```bash
   certbot certonly --webroot -w /var/www/certbot -d tao.insightfulbytes.com
   ```

   The TLS config expects certs at
   `/etc/letsencrypt/live/tao.insightfulbytes.com/fullchain.pem` and
   `.../privkey.pem`.
5. `docker compose up -d` and reload nginx (`docker compose exec proxy nginx -s reload`)
   after renewals.

The TLS config keeps `/.well-known/acme-challenge/` on port 80 for renewals and
301-redirects all other HTTP traffic to HTTPS, and adds an HSTS header.

### Option B -- external reverse proxy / Cloudflare

Keep the plain-HTTP `tao-sentinel.conf` and let an upstream terminate TLS:

- Put the stack behind **Cloudflare** (orange-cloud the A record) or an existing
  reverse proxy (e.g. a shared nginx/Caddy/Traefik) that terminates HTTPS and
  forwards to the proxy's HTTP port.
- The proxy already forwards `X-Forwarded-Proto`, `X-Forwarded-For`,
  `X-Real-IP`, and `Host`, so the upstream terminator sees correct client info.
- Publish the proxy on a port your terminator targets (keep `8080:80`, or bind
  to localhost only, e.g. `"127.0.0.1:8080:80"`, when the terminator runs on the
  same host).

## 4. Server deployments: build from source (default) or pull from GHCR

The default deployment strategy is **git pull + build on the server** — the
repo is public, so this needs no registry credentials at all:

```bash
# On the server, in the project directory (a git clone of this repo holding
# your sentinel.yaml).
git pull --ff-only
docker compose up -d --build
```

Tip: put `TAOSTATS_API_KEY=...` in a `.env` file next to `compose.yaml`
(gitignored; docker compose reads it automatically) instead of exporting it —
non-interactive SSH sessions don't load `.bashrc` exports.

CI also publishes an image to GHCR on every push to `main`
(`ghcr.io/<lowercased-owner>/tao-sentinel:latest` and `:<git-sha>`), but note
it may be **private** depending on your org's package policy. To deploy by
pulling instead of building, the server's Docker must be logged in to ghcr.io
with a token that has `read:packages`; then:

```bash
export IMAGE=ghcr.io/<lowercased-owner>/tao-sentinel:latest
docker compose pull && docker compose up -d
```

`compose.yaml` reads `image: ${IMAGE:-tao-sentinel:latest}` with a `build: .`
fallback, so the same file serves both strategies.

## 5. GitHub Actions secrets

The CD workflow (`.github/workflows/deploy.yml`, `workflow_dispatch` only) SSHes
to the server, `cd`s into the project directory (`DEPLOY_PATH`), and runs
`git pull --ff-only && docker compose up -d --build`. The server must already
have a clone of the repo with a `sentinel.yaml` in the project directory.

| Secret           | Used by             | Purpose                                                       |
| ---------------- | ------------------- | ------------------------------------------------------------- |
| `DEPLOY_HOST`    | `deploy.yml`        | Hostname / IP of the deployment server.                       |
| `DEPLOY_USER`    | `deploy.yml`        | SSH user on that server.                                      |
| `DEPLOY_SSH_KEY` | `deploy.yml`        | Private SSH key (full PEM) authorized for `DEPLOY_USER`.      |
| `DEPLOY_PORT`    | `deploy.yml`        | Optional; SSH port if not 22.                                 |
| `GITHUB_TOKEN`   | `ci.yml` (built-in) | Auto-provided; pushes the image to GHCR (`packages: write`).  |

| Variable (not secret) | Used by      | Purpose                                                            |
| --------------------- | ------------ | ------------------------------------------------------------------ |
| `DEPLOY_PATH`         | `deploy.yml` | Project directory on the server (default `/opt/tao-sentinel`).     |

CI (`.github/workflows/ci.yml`) needs no extra secrets: the `test` job runs
pytest on the Python 3.10/3.12 matrix, and the `image` job (push to `main` only)
logs in to GHCR with the built-in `GITHUB_TOKEN` and pushes the `latest` and
`<sha>` tags.

## 6. Security notes

- **The dashboard is never exposed directly.** Only the `proxy` service
  publishes a host port; `dashboard:8787` is reachable solely on the Compose
  network. The proxy also sets `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, and `Referrer-Policy: strict-origin-when-cross-origin`.
- **The API key is only ever passed via the environment.** `TAOSTATS_API_KEY`
  flows from the host env into the containers; `sentinel.yaml` references it as
  `env:TAOSTATS_API_KEY` rather than storing a raw key. Keep `sentinel.yaml`
  out of version control if you ever inline a real key, and `chmod 600` it.
- **No key -> mock mode.** With no key set, tao-sentinel uses the deterministic
  mock client, so the stack runs without any secrets. This is safe for demos and
  CI but serves synthetic data.
- **Back up the state volume.** Watch-engine state lives in the named volume
  `sentinel-state` (`/data/state.json`). Back it up so alert de-duplication and
  baselines survive redeploys, e.g.:

  ```bash
  docker run --rm -v sentinel-state:/data -v "$PWD":/backup alpine \
    tar czf /backup/sentinel-state.tar.gz -C /data .
  ```
