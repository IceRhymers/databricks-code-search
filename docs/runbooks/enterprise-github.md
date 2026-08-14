# Runbook: pointing the indexer at GitHub Enterprise Cloud (data residency)

What an operator needs to point the indexing job at a GitHub Enterprise Cloud
deployment with data residency instead of public `github.com`: the one config
key, the web-vs-API host relationship it encodes, the token requirement that is
the most common failure, and what is deliberately out of scope.

---

## 1. What this changes

By default the indexer talks to `https://api.github.com`. GitHub Enterprise
Cloud with data residency gives an enterprise its own hostnames:

- **Web host:** `https://<enterprise>.ghe.com` — where a human browses repos, and
  the host you write in a `repos:` URL entry.
- **API host:** `https://api.<enterprise>.ghe.com` — where the REST API lives. It
  is the web host with a leading `api.` label, and it is the value you set for
  `github_api_base`.

Setting `github_api_base` makes the job build its one `httpx.Client` against that
API base; every GitHub call (enumeration, branch listing, ref resolution, tarball
download) then egresses to the enterprise host and nothing leaves to
`github.com`. Leaving it unset is byte-identical to today.

## 2. Configuring it

In `config.yaml`, near the top:

```yaml
version: 1

github_api_base: https://api.acme.ghe.com

connections:
  - type: github
    orgs:
      - acme-eng
    # URL-form repos: entries use the WEB host, not the API host:
    # repos:
    #   - https://acme.ghe.com/acme-eng/widgets
```

Rules, all enforced at config-parse time (a bad value fails the run immediately
with a `ConfigError` naming the value, never a mid-run surprise):

- **https only** — a non-https scheme is rejected.
- **Origin only** — no path, so a GitHub Enterprise Server style
  `https://ghe.internal/api/v3` base is rejected (GHES is out of scope; see §5).
- **No userinfo** — a `user@host` form is rejected, because it would ship the
  bearer token to an attacker-controlled host.
- **No query or fragment.**
- **Host allowlist** — the host must be `api.github.com` or a `*.ghe.com`
  enterprise host. Every request carries the bearer token, so this forecloses a
  mistyped or hostile base pointing it at a metadata/loopback/internal address
  (`169.254.169.254`, `localhost`, …). Widen this list in `_validate_github_api_base`
  if a new deployment shape is ever supported.
- A lone trailing slash is normalized off (`.../` becomes `...`).

Explicit `repos:` entries written as URLs are validated against the **web** host
derived from this base (`acme.ghe.com` and `www.acme.ghe.com` when the base is
`https://api.acme.ghe.com`). A `https://github.com/...` entry is rejected once an
enterprise base is set; bare `org/repo` entries are host-agnostic and always
accepted.

## 3. The token MUST be issued by that enterprise

This is the failure to check first. The PAT the job reads from its secret scope
must be a token **issued by the enterprise** whose API you are targeting. A
`github.com` personal access token will not authenticate against
`api.<enterprise>.ghe.com` — it fails with a 401, not a helpful "wrong host"
message. Reseed the secret scope with an enterprise-issued token that has the
same read scopes the public-github setup needed (org/repo read for enumeration,
repo contents for the tarball).

The base URL itself is not a secret and is logged once at job startup
(`indexing against GitHub API base: https://api.acme.ghe.com`) so an operator can
confirm from the run log which host a given run targeted. The token is never
logged — it lives only in the `Authorization` header, per this job's existing
token-redaction discipline.

## 4. Tarball downloads and cross-host redirects

The tarball endpoint answers with a 302 to a signed download URL. On public
`github.com` that host is `codeload.github.com`; on GHE Cloud it is an
enterprise-specific signed host. The code assumes **no** particular download
hostname — `httpx` follows whatever `Location` the API returns, and because the
signed URL is a different origin it strips the `Authorization` header on the hop
(the signed URL carries its own auth in the query string). No configuration is
needed for this; it works on both public and enterprise deployments.

## 5. Out of scope

- **GitHub Enterprise Server (self-hosted, `/api/v3` path-style base).** The
  origin-only validation deliberately rejects a base with a path. Supporting
  GHES would mean threading a separate API-path prefix through every fetch URL;
  it is not part of this change.
- **Plain-`github.com` EMU (Enterprise Managed Users).** EMU orgs live under
  `github.com` and need no base override; this key is only for the
  `<enterprise>.ghe.com` data-residency shape.

---

## Reference

- `config.yaml` — the commented `github_api_base:` block.
- [`semantic-enablement.md`](semantic-enablement.md) / [`multi-branch.md`](multi-branch.md)
  — sibling operator runbooks in the same style.
