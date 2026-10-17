# Security Policy

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue for a
vulnerability. Use GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on the repository ("Security" → "Report a vulnerability"), or contact the
maintainers listed in `pyproject.toml`.

Please include: affected version/commit, a reproduction, and the impact you
believe it has. We aim to acknowledge reports within a few days.

## Threat model

This is a **local, single-user MCP server**. The primary threat is a
**prompt-injected model driving the tools**: the model is untrusted input, so
every tool argument must be treated as attacker-controlled.

Assets at risk if the model is manipulated:

| Asset | Exposure |
|---|---|
| Local files (`~/.ssh`, `~/.aws`, dotfiles) | path-taking tools (`read_document`, `ocr_image`, `list_images`, `transcribe_local`, `convert_media`, `upload_to_s3`) |
| Internal/cloud-metadata services | URL-taking tools (`visit_page`, `archive_webpage`, `shorten_url`, feed/podcast URLs) |
| Credentials | tool arguments, subprocess command lines, logs |
| The user's mailbox | `fetch_emails` (IMAP) |

Out of scope: a malicious *local user* (who can read the process environment and
data files regardless), and remote network attackers (the server is not designed
to be exposed).

## Controls

### SSRF / URL guard (`core/netguard.py`)

Applied to `visit_page`, `archive_webpage`, `shorten_url` and custom feed URLs:

- Non-`http(s)` schemes are **always rejected** — notably `file://` (a local file
  read primitive), plus `data:` and `javascript:`.
- Link-local addresses and the cloud-metadata endpoint (`169.254.169.254`) are
  **always rejected**.
- Loopback / private / reserved / multicast ranges are rejected **when**
  `NOAPI_MCP_ALLOW_PRIVATE_URLS=0`.
- Hostnames are resolved and **every** returned address is checked, which defeats
  a hostname that resolves to a private address (DNS-rebinding style bypass).

> Default is permissive for private/LAN addresses (`ALLOW_PRIVATE_URLS=1`) so
> self-hosted / LAN / GPU setups keep working. If this server is reachable by
> anything other than you, set `NOAPI_MCP_ALLOW_PRIVATE_URLS=0`.

### Filesystem guard (`core/fsguard.py`)

Path-taking tools are confined to `NOAPI_MCP_ALLOWED_ROOTS` (default: the user's
home directory plus the system temp directory). Paths are resolved (symlinks
followed) before the containment check, so a symlink cannot be used to escape a
root.

### Secrets

- Prefer environment variables over tool arguments:
  `NOAPI_MCP_IMAP_USER` / `NOAPI_MCP_IMAP_PASSWORD`, `AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY` / `AWS_ENDPOINT_URL`. Tool arguments still work for
  backward compatibility, but they end up in the client's transcript.
- Secrets are **never placed on a subprocess command line**: the S3 upload path
  passes credentials to `mc` via the `MC_HOST_<alias>` environment variable.
- Tool-call logging redacts argument names that look like secrets
  (`password`, `secret`, `token`, `key`, `auth`, `credential`, …) and truncates
  long values (e.g. base64 images).

### Read-only operations stay read-only

`fetch_emails` fetches messages with `BODY.PEEK[]`, so reading mail does **not**
mark it as read. Tools advertise this via MCP annotations
(`readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint`), which
all 42 tools now carry.

### Output limits

Document and email bodies are capped
(`NOAPI_MCP_MAX_DOCUMENT_CHARS`, `NOAPI_MCP_MAX_EMAIL_BODY_CHARS`) to prevent
memory/context exhaustion from a hostile or merely huge input.

### Upstream-abuse controls

Rate limiting, singleflight, caching and conditional GET deliberately **reduce**
the number of requests sent upstream. The project does not strengthen bot
evasion or CAPTCHA solving; the existing stealth/CAPTCHA code is unchanged.

### Logging hygiene

Logs go to **stderr** only. On the stdio transport stdout is the MCP protocol
channel, so a stray `print()` would corrupt the stream — this is enforced by
`tests/unit/test_stdout_hygiene.py`.

## Hardening checklist for operators

1. Set `NOAPI_MCP_ALLOW_PRIVATE_URLS=0` unless you genuinely need LAN targets
   (the default is permissive, for self-hosted/GPU setups).
2. Narrow `NOAPI_MCP_ALLOWED_ROOTS` to the directories you actually use.
3. Put credentials in the environment (or an `EnvironmentFile=` with mode
   `0600` for systemd), not in prompts.
4. Run as an unprivileged user, and do not expose the `/health` port publicly.
5. Review which tools you expose to the model; `upload_to_s3` and `unsubscribe`
   are the destructive ones (`destructiveHint=true`).
