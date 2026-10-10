# Wiki Web

A small web application for asking questions about a private Markdown notebook.
It works with a SilverBullet space or another directory of Markdown files.
The interface includes a public entrance, Cloudflare Access sign-in, asynchronous
questions, cancellation, follow-up conversations, and a source viewer with citations.

The frontend uses a dark green glass theme with locally bundled Wanted Sans and
D2Coding fonts. The entrance and sharing artwork use English; the question interface
and application messages currently use Korean.

## How it works

1. Cloudflare Access authenticates the visitor. The Python API independently
   validates the signed Access JWT, issuer, audience, and expiry.
2. The server reads a local snapshot of eligible Markdown notes. Recognized private
   values and explicit private blocks become random placeholders before model input.
3. A separate model worker runs Codex or AGY to select relevant documents and
   produce an answer with citations. It receives audited, masked prompts over a
   local Unix socket and has no access to the original notebook.
4. Codex reuses an isolated app-server with a fresh ephemeral conversation per call.
   The browser shows text as it is generated, with protected values still hidden.
5. After validating the answer, the server restores permitted values from selected evidence locally. The browser
   renders those values as literal text and can show the original cited source.

**Ordinary note text is still sent to the remote model.** Automatic recognition
cannot identify every kind of confidential prose. Mark content that must remain
local explicitly; see [PRIVACY.md](PRIVACY.md) for the protection boundary and examples.

## Requirements

- Linux and Python 3.11 or newer.
- An installed, authenticated Codex CLI or Antigravity CLI (`agy`) with a model
  available to its dedicated service account. Adapters target Codex 0.162.0 and
  AGY 1.3.2; see `backend/model_providers.py` and verify compatibility on upgrade.
- systemd and separate Linux service accounts for the isolated worker deployment.
- Cloudflare Access and an HTTPS hostname for a deployed application.
- Nginx (or an equivalent reverse proxy) and Cloudflare Tunnel for the provided
  deployment example.
- Node.js 20+ and Playwright only for browser tests or regenerating branding PNGs.

## Install and configure

For a private notebook, use the [isolated deployment guide](deploy/README.md).
It separates the web API from both CLI credentials and model execution, and supports
`ai.provider` set to `codex` or `agy`. The operator configures model and effort per
provider; failures never trigger an automatic retry or provider fallback.

The following commands retain the older direct Codex-only setup for compatibility.
That setup runs the CLI under the web process's identity and does not provide the
separate account boundary. AGY requires the isolated worker.

Run these commands from this checkout:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
mkdir -p "$HOME/.config/wiki-web"
cp config.example.json "$HOME/.config/wiki-web/config.json"
chmod 600 "$HOME/.config/wiki-web/config.json"
```

Edit that **external** JSON file. `config.example.json` contains placeholders and
cannot connect to a real installation without your values.

| Setting | Meaning |
| --- | --- |
| `origin` | Exact public HTTPS origin, such as `https://wiki.example.com` |
| `issuer` | Access team origin, such as `https://your-team.cloudflareaccess.com` |
| `audience` | Application Audience (AUD) from your Access application |
| `notes_root` | Absolute path to your private Markdown directory, outside the web root |
| `codex` | Codex executable; use an absolute path for a systemd service |
| `model` | Model identifier available to your authenticated CLI |
| `port` | Local API port; the server binds only to `127.0.0.1` |
| `notes_url` | Optional SilverBullet HTTPS link for the frontend build |

Keep Codex login credentials and Cloudflare Tunnel credentials in their normal
external locations. They are not application source files and must not be copied
into this checkout. Use a restricted Access policy for the people who may read the
configured notebook; authenticated users share the same notebook.

```bash
export WIKI_CONFIG="$HOME/.config/wiki-web/config.json"
.venv/bin/python backend/server.py
```

The direct Codex setup uses `medium` reasoning effort. Jobs are limited to one concurrent
model request globally and 20 admitted questions per owner per hour, with a
600-second job deadline. State and rate counters live in memory and reset on restart;
this is intended for a small trusted group, not a public multi-tenant service.

## Build the frontend

The checked-in frontend contains only example hostnames. Generate an installation
copy using the same private configuration:

```bash
.venv/bin/python scripts/build_frontend.py --config "$WIKI_CONFIG"
```

This writes `dist/public/`, which is ignored by Git. It inserts only the public
origin and optional notes link, never the complete configuration. The output must
be a new directory; for later builds select a new `--output` directory. Deploy the
generated **public directory only** as your web root.

For a synthetic build without any private configuration:

```bash
.venv/bin/python scripts/build_frontend.py --origin https://wiki.example.com --output /tmp/wiki-web-example-public
```

## Deployment

The [isolated deployment guide](deploy/README.md) provides two **system** service
templates. [deploy/nginx.conf.example](deploy/nginx.conf.example) describes the
reverse proxy. Set their paths and hostname for your machine; these files do not
automatically modify any service or other website. The older
[user service](deploy/wiki-web.service.example) supports only direct Codex mode.

With the example topology, Cloudflare Tunnel forwards the hostname to
`http://127.0.0.1:8081`. Nginx serves the generated static files and forwards `/api/`
to the API at `127.0.0.1:8093`. Keep both listeners on loopback.

Configure Access so that:

- The exact `/` entrance and `/assets/branding/*` are public, so link previews and
  the entrance work before sign-in.
- `/app/`, `/api/*`, and the remaining application paths require your Access policy.
- `HttpOnly` and `Secure` are enabled for the Access application cookie.
- The protected application AUD matches `audience` in the private JSON settings.

The entrance probes `/api/status` without following sign-in redirects. A valid
session enters the application automatically; otherwise the visitor uses Open Wiki.
Both pages link to the public install manifest at `/assets/branding/manifest.json`.
It provides the Wiki name, green W icons, and a standalone app window. Installed
apps start at `/` and follow the same sign-in flow. The manifest and icons must stay
public; application and API authentication still apply. No service worker or offline
note cache is added.
Check [Access application path matching](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/app-paths/)
carefully when creating the public exceptions; a hostname-level bypass can also
match child paths. See [Access cookie settings](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/)
for the authentication cookie options.

Before using personal notes, check that the entrance is public, unauthenticated
protected paths redirect to Access, and direct unauthenticated API requests return
401. `/health` reports process health only; it does not prove model authentication.
Use the fictional notes in `examples/notes/` for initial installation checks.

## Tests

```bash
.venv/bin/python -B run_tests.py
npm ci
npx playwright install chromium
.venv/bin/python -B run_tests.py --browser
```

Tests create temporary notes, signing keys, Unix sockets and fake model subprocesses. They do
not read a personal notebook, load production credentials, or make AI requests.
Browser scenarios use local/mock HTTP responses. These checks do not verify an
actual provider account, model availability, a deployed OS sandbox, or a real
Cloudflare Access policy. Worker tests exercise both adapters, local restoration,
peer identity, output limits, cancellation and detached-helper cleanup.

To regenerate the generic 1200×630 OG PNG from its SVG source:

```bash
node scripts/render_branding.cjs
```

To regenerate the install icons and manifest from the existing Wanted Sans W:

```bash
node scripts/render_app_icons.cjs
```

This also refreshes the manifest URL in both HTML pages. Icon filenames include
content hashes, and a separate maskable icon keeps the W inside the platform crop.

## Repository boundaries

This repository contains application code, synthetic tests and examples, bundled
font licenses, and generic deployment templates. It starts with a new Git history.
Personal notes, private settings, authentication profiles, logs, screenshots,
backups, and deployment reports belong outside it. `.gitignore` is an additional
safeguard, not a substitute for reviewing staged files.

Use synthetic data in issues, screenshots, tests, and pull requests. Follow
[SECURITY.md](SECURITY.md) when reporting security concerns.

## Licensing

No project-wide software license has been selected. Public availability alone
does not grant an open-source license. The bundled fonts retain their original
SIL Open Font License notices; see
[the font documentation](frontend/public/assets/branding/fonts/README.md).
