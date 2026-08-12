# Synchri.com on GitHub Pages

The public Synchri website is a standalone static site in `website/`. It has
no connection to the local browser app in `synchri/ui/static/app.html`.
Opening that app file directly cannot work: it is the local interface and
expects the local Synchri API to be running.

The `Deploy Synchri.com` workflow publishes `website/` whenever it changes.
GitHub Pages and the `synchri.com` custom-domain setting are already configured
for this repository. The only remaining step is DNS.

## Switch the domain to GitHub Pages

`synchri.com` is currently pointing at a different hosted website. Replacing
the records below will make the GitHub Pages site the live Synchri website, so
do this only when that existing website is ready to be replaced.

## DNS records

At the company where the domain is managed, replace the existing `@` and `www`
web-hosting records with these:

| Type | Name | Value |
|---|---|---|
| A | `@` | `185.199.108.153` |
| A | `@` | `185.199.109.153` |
| A | `@` | `185.199.110.153` |
| A | `@` | `185.199.111.153` |
| CNAME | `www` | `fenixawiles.github.io` |

Do not create a wildcard record such as `*.synchri.com`.

When GitHub Pages shows that DNS has checked successfully, enable **Enforce
HTTPS**. GitHub provisions the certificate; there is no separate certificate to
buy. DNS may take up to 24 hours to settle, though it commonly finishes sooner.
