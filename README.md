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
3. A Codex CLI subprocess selects relevant documents and produces an answer with
   citations. Tool access is disabled by the invocation arguments.
4. The server restores permitted values from selected evidence locally. The browser
   renders those values as literal text and can show the original cited source.

**Ordinary note text is still sent to the remote model.** Automatic recognition
cannot identify every kind of confidential prose. Mark content that must remain
local explicitly; see [PRIVACY.md](PRIVACY.md) for the protection boundary and examples.

## Requirements

- Linux and Python 3.11 or newer.
- An installed, authenticated Codex CLI with a model available to your account.
  The CLI must support the options in `backend/server.py`'s `invoke` function;
  model and CLI availability depend on the installation.
- Cloudflare Access and an HTTPS hostname for a deployed application.
- Nginx (or an equivalent reverse proxy) and Cloudflare Tunnel for the provided
  deployment example.
- Node.js 20+ and Playwright only for browser tests or regenerating the OG PNG.

## Install and configure

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

The reasoning effort is currently `medium`. Jobs are limited to one concurrent
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

[deploy/nginx.conf.example](deploy/nginx.conf.example) and
[deploy/wiki-web.service.example](deploy/wiki-web.service.example) are templates.
Set their paths and hostname for your machine; these files do not automatically
modify any service or other website. The service is a **user** systemd unit.

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

Tests create temporary notes, signing keys, and fake model subprocesses. They do
not read a personal notebook, load production credentials, or make AI requests.
Browser scenarios use local/mock HTTP responses. These checks do not verify an
actual Codex account, model availability, or a real Cloudflare Access policy.

To regenerate the generic 1200×630 OG PNG from its SVG source:

```bash
node scripts/render_branding.cjs
```

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
