# Security Policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository
(**Security** tab → **Report a vulnerability**). Do not open a public issue for a
suspected secret leak or an exploitable defect.

## Scope and threat model

AI Photo Agent is a local-first application:

- The HTTP service binds to `127.0.0.1` by default and has no built-in
  authentication. Do not expose it to a LAN or the public internet. If you change
  `--host`, put an authenticating reverse proxy in front of it.
- `Host` headers are restricted to loopback names (`127.0.0.1`, `localhost`,
  `[::1]`); other names receive `400`. This is a DNS-rebinding defense, not
  authentication. A reverse proxy must preserve an allowed `Host` header or the
  service will reject the request.
- Uploaded photos, analysis sessions, generated assets and Lightroom bridge files
  live under ignored directories (`data/`, `uploads/`, `generated/`, `exports/`).
  They are not part of the repository.
- The Lightroom plug-in reads and writes only local files that the user selected
  or configured in `Config.local.lua`.
- The project does not include telemetry. Provider requests go only to the
  endpoints you configure (`api.deepseek.com`, an OpenAI-compatible endpoint, or
  a local Codex CLI).

## Secrets

- API keys are read from environment variables, an explicitly configured YAML
  credential file, or a local `.env` file loaded by `run_app.py`. See
  `.env.example` for the supported names.
- `.env`, `Config.local.lua`, `review-state.json` and `bridge-path.txt` are
  ignored by git. Never force-add them.
- If a key was committed, rotate it first, then remove it from history. Rewriting
  history alone does not make a leaked key safe again.
- The repository is a single sanitized snapshot; a local test artifact
  (`review-state.json`) that contained a personal photo path was removed from
  history before publication. `scripts/check_privacy.py --history` guards
  against this class of regression.

## Local data hygiene

Before publishing a fork or a screenshot, check that it does not contain:

- Windows profile paths (`C:\Users\<name>`), machine SIDs or other host identifiers;
- personal photo-library paths or file names;
- API keys, tokens, cookies or provider request/response dumps;
- Lightroom catalog paths or bridge logs.

Run the scanner locally:

```powershell
.\.venv\Scripts\python.exe scripts\check_privacy.py
.\.venv\Scripts\python.exe scripts\check_privacy.py --history
```

## Supported versions

Security fixes are applied to the current `main` branch.
