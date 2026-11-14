# Security model

What creative-forge protects, from whom, where the trust boundaries are, how each one is defended (with the code that
does it) and what is knowingly not defended. Reporting a problem: [SECURITY.md](../SECURITY.md).

## Assets

| Asset | Where it lives |
|---|---|
| Cloud credentials and budget: Google ADC / service account, the Vertex AI project (paid image, video and music calls) | the environment, the operator's ADC file |
| Ad-network credentials: Meta access token, AppLovin Report Key; the ComfyUI proxy token | environment variables only (never files or the command line) |
| Review decisions and what gets published (an approval is tied to the file's SHA-256) | `review.json` and the publish manifest of each run |
| Unreleased creatives and performance data (spend, installs per creative) | run folders under `--root`, the perf SQLite store |
| The UI session: the per-process CSRF token, the access token of a network bind | memory of the `forge ui` process, pages it serves |
| The operator's machine: files next to a playable, local services (ComfyUI has no authentication) | the machine that runs `forge` |

## Trust boundaries and attackers

| # | Boundary | Attacker |
|---|---|---|
| B1 | Browser → `forge ui` / the desktop app | **A1** a web page the operator visits (CSRF, DNS rebinding, embedding) |
| B2 | Network → a UI bound to a non-loopback address | **A2** another machine on the LAN |
| B3 | Files from elsewhere → the pipeline: playables (HTML/JS/zip) from vendors, run folders and reports, CSV exports | **A3** whoever made the file |
| B4 | API responses → the importers and providers (Meta, AppLovin, Vertex AI, ComfyUI) | **A4** a compromised or spoofed service, a worker on the LAN |
| B5 | Model output → specs, prompts and tools (`forge ideate`, the QA judge) | **A5** text the model reads (library entries, lessons) that tells it what to do |
| B6 | Dependencies, ffmpeg builds, GitHub Actions → the package and CI | **A6** a compromised release, download or action; a malicious pull request |

Trusted: the operator, the machine and account that run `forge`, the spec files and the environment.

## Mitigations

**B1/B2, the UI** (`ui/security.py`, one middleware in front of every route, `/static` and error page):

- binds 127.0.0.1 by default; a Host header that is not this machine is refused (DNS rebinding);
- bound to any other address, every request needs a random access token (printed once in the URL, then an HttpOnly
  SameSite=Strict cookie);
- CSRF: every POST needs the per-process token (header or form field, `require_token`), an `Origin` equal to the
  server's own and no cross-site Fetch Metadata; a test enumerates every POST route (`tests/test_security.py`);
- cross-site subresource requests are refused (Fetch Metadata);
- headers on every response: a Content-Security-Policy with scripts from `/static` only (no inline script, no
  `'unsafe-inline'` for scripts, htmx with `allowEval` off), `frame-ancestors 'none'`, `nosniff`, no Referer,
  COOP/CORP, `no-store` on HTML; a run's `report.html` (a file that may come from someone else's run) is served with
  `default-src 'none'` and `sandbox`, so it runs no script in the UI's origin;
- files are served only from inside `--root`, only a run's own exports, by name (`load_run`, `creative_or_404`);
  uploads are reduced to a bare file name in a fresh temporary folder;
- the desktop app's `/__app/*` endpoints refuse other hosts and cross-site requests; `app.json` may only name
  `http://127.0.0.1:<port>/`.

**B3, files from elsewhere**: a playable zip is read in memory, never extracted, after its member names (no absolute
or `..` paths) and total size are checked (`playable/static.py`); the dynamic check serves it from a private
127.0.0.1 server to a headless browser that aborts every other request and every WebSocket (`playable/dynamic.py`);
packing a folder skips symlinks that lead out of it; `forge setup ffmpeg` installs only archives that match a pinned
size and SHA-256, and copies only the named members under its own file names; HTML reports escape every value;
YAML is read with `yaml.safe_load`; ffmpeg and other tools run with argument lists (no shell), and a spec's file
paths are resolved against the spec's own folder.

**B4, API responses**: tokens travel in headers (AppLovin's key, which must be a query parameter, is redacted from
every message), and a pagination link is followed only to the API's own https host (`perf/http.py`); the ComfyUI
worker URL must be http(s) with no credentials in it and not a link-local or metadata address
(`providers/comfyui.py`); a paid Vertex call is never re-submitted on an ambiguous failure.

**B5, model output**: `forge ideate` acts only through five declared tools, a gameplay clip must be one the operator
listed, and a concept is accepted only after the same validation and lint as `forge run`; what it writes is a spec
for a person to review, not a run.

**B6, supply chain** (`.github/`): actions pinned to commit SHAs, tool binaries to SHA-256s, Python tools to hashes;
least-privilege `permissions:` per job, `persist-credentials: false`; CodeQL (Python, Actions), osv-scanner on the
newest and the lowest dependency versions `pyproject.toml` allows (its floors are the first releases without a known
vulnerability), gitleaks over the whole history, bandit, dependency review on pull requests, OpenSSF Scorecard,
Dependabot with a 7-day cooldown; htmx is vendored with its license.

## Known gaps (accepted for now)

- One operator: no user accounts or roles; anyone who has the UI (or its access token) can approve exports.
- `style-src 'unsafe-inline'` remains: the templates use style attributes.
- The ComfyUI worker URL is not resolved before use, and ComfyUI itself has no authentication: run it on a trusted
  network or behind the reverse proxy whose token creative-forge sends.
- Packing a single HTML file reads the files next to it; only those the HTML references are inlined, so pack a
  playable from its own folder.
- Model prompts (image, video, music) are paid calls whose content is the operator's to review before `forge run`.
