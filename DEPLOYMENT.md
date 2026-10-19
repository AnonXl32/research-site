# GitHub Pages deployment

Repository: `AnonXl32/research-site`

Canonical address: `https://research.elbaz.ch`

## 1. Repository and authentication

Create the public repository without an automatically generated README, license, or gitignore. Sign in using GitHub's normal browser or Git Credential Manager flow; never paste credentials into project files, workflow secrets, or command arguments. Use the GitHub-provided no-reply commit identity to avoid publishing a personal email.

Push this source tree to the repository's `main` branch. Under **Settings → Pages → Build and deployment**, select **GitHub Actions**. The workflow validates all pull requests and deploys only `main` or manual runs. It grants deployment permissions only to its deployment job.

## 2. Verify domain ownership

In the account's **Settings → Pages → Add a domain**, request verification for `research.elbaz.ch`. GitHub generates a TXT hostname and value. Copy those exact values into the `elbaz.ch` DNS zone in Cloudflare and complete GitHub verification. Keep the TXT record afterward.

Use the exact TXT hostname GitHub displays; Cloudflare may display it relative to the zone. Do not invent a verification value. Verify only the intended research subdomain rather than changing ownership settings for existing unrelated services.

## 3. Configure Pages, then DNS

In the repository's **Settings → Pages**, set **Custom domain** to `research.elbaz.ch`. Do this before pointing the public hostname to Pages.

In Cloudflare, inspect all records for the exact `research` name and resolve any conflict deliberately. Create:

| Type | Name | Target | Proxy | TTL |
|---|---|---|---|---|
| CNAME | `research` | `anonxl32.github.io` | DNS only | Auto |

The target has no `https://`, repository path, or trailing slash. This does not change the apex domain's hosting or mail records. Do not use a wildcard record. With a custom Actions deployment, the custom-domain setting is managed in GitHub Pages settings, not a generated `CNAME` file.

## 4. Enable and verify HTTPS

Wait for DNS validation and certificate provisioning in GitHub Pages. Select **Enforce HTTPS** when available. Provisioning or propagation can take up to 24 hours. Do not work around certificate errors by disabling browser security or adding a second TLS proxy.

Verify public DNS, the custom-domain certificate, and HTTP redirection. On Windows:

```powershell
Resolve-DnsName research.elbaz.ch -Type CNAME -Server 1.1.1.1
curl.exe -I https://research.elbaz.ch
curl.exe -I http://research.elbaz.ch
```

The CNAME should point to `anonxl32.github.io`. HTTPS should return a successful response, and HTTP should redirect to HTTPS. The home page and both research articles must use canonical URLs under `https://research.elbaz.ch`.

## 5. Rollback and retirement

Use `git revert` on `main` for content rollback. Leave DNS and the custom-domain setting intact during a content rollback. If the site is permanently retired, remove its Cloudflare CNAME before removing the GitHub Pages domain or deleting the repository; retain the ownership-verification TXT record.

## Documentation

- [Custom GitHub Pages workflows](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)
- [Verify a custom domain](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/verifying-your-custom-domain-for-github-pages)
- [Manage a custom domain](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/managing-a-custom-domain-for-your-github-pages-site)
- [Enforce HTTPS](https://docs.github.com/en/pages/getting-started-with-github-pages/securing-your-github-pages-site-with-https)
