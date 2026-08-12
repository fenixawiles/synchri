# Synchri.com on GitHub Pages

The public Synchri website is a standalone static site in `website/`. It has
no connection to the local browser app in `synchri/ui/static/app.html`.
Opening that app file directly cannot work: it is the local interface and
expects the local Synchri API to be running.

The `Deploy Synchri.com` workflow publishes `website/` whenever it changes.
The one-time setup below is all that is needed.

## One-time GitHub setup

1. In `fenixawiles/synchri`, open **Settings → Pages**.
2. Under **Build and deployment**, select **GitHub Actions**. The repository
   workflow then deploys the site automatically.
3. Under **Custom domain**, enter `synchri.com` and save it **before** adding
   DNS records. GitHub recommends this order to avoid a domain-takeover window.

## DNS records

At the company where the domain is managed, remove conflicting `@` or `www`
web-hosting records, then add these:

| Type | Name | Value |
|---|---|---|
| A | `@` | `185.199.108.153` |
| A | `@` | `185.199.109.153` |
| A | `@` | `185.199.110.153` |
| A | `@` | `185.199.111.153` |
| CNAME | `www` | `fenixawiles.github.io` |

Do not create a wildcard record such as `*.synchri.com`.

When GitHub shows that DNS has checked successfully, enable **Enforce HTTPS**.
GitHub provisions the certificate; there is no separate certificate to buy.
DNS may take up to 24 hours to settle, though it commonly finishes sooner.
