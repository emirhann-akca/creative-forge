# Security policy

## Reporting a vulnerability

Please report security problems privately, through GitHub's private vulnerability reporting: open this
repository's **Security** tab and choose **Report a vulnerability** (the `security/advisories/new` page of the
repository).

Do not open a public issue, pull request or discussion about a vulnerability before a fix is released. Include what
you found, how to reproduce it (a spec, a playable, a request to `forge ui`) and what an attacker gains. A proof of
concept against your own local install is enough.

What to expect: this is a small project maintained by one studio. A report is acknowledged within 5 working days,
triaged within 10, and a confirmed issue is fixed in a release and published as a GitHub security advisory (with a
CVE when it qualifies), crediting you unless you ask otherwise. If a fix takes longer than 90 days, the advisory says
why.

Good-faith research that follows this policy (no access to other people's data or accounts, no service disruption,
no public disclosure before the fix) is welcome and will not be pursued.

## Supported versions

| Version | Supported |
|---|---|
| `main` and the latest `0.x` release | yes |
| older releases | no: upgrade |

## Scope

In scope: the `creative-forge` Python package (the `forge` CLI, the `forge ui` web app and the desktop app, the
pipeline and its providers, playable checking and packaging, the performance importers, `forge ideate`), the
installers (`install.sh`, `install.ps1`, `forge setup ffmpeg`) and the GitHub workflows.

Out of scope:

- Attacks that need control of the machine running `forge` (its files, its environment, its user account) or of the
  operator's own spec files and configuration.
- Vulnerabilities in third-party services (Google Cloud, ad networks, ComfyUI itself) and in dependencies or ffmpeg,
  unless creative-forge uses them in a vulnerable way; report those upstream.
- Missing hardening without a demonstrated impact.

How the project is defended, and what it does not defend against, is written down in
[docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md).
