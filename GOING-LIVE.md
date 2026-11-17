# Going live and getting found

**The site is already online.** It has been since GitHub Pages was switched on.

```
https://alexbrunelli08-lgtm.github.io/legislative-alpha/
```

Verified 16 Nov 2026: `HTTP 200`, HTTPS enforced, Pages status `built`, serving
data generated the same day. Every `git push` to `main` republishes it within
about a minute, and the Actions cron pushes fresh data four times a day.

What is *not* done is **being findable**. A search for the site's own address
returns nothing — Google has never been told it exists. That is what the rest
of this file is for.

Do Part A before Part B. If you register a domain after setting up Search
Console, you have to set Search Console up again from scratch.

---

## Part A — Decide the address

Skip to Part B if you are happy staying on `github.io`. But the site is called
Vedetta Analytics and the address says `legislative-alpha`, so this is worth
doing first.

### A1. Register a domain

Roughly $10–15/year for a `.com`. Registrars that do not mark up or upsell:

- **Cloudflare Registrar** — sells at wholesale cost, no renewal markup
- **Porkbun**
- **Namecheap**

### A2. Add the DNS records

At the registrar's DNS panel. These values are from GitHub's official docs
(verified 16 Nov 2026):

| Type  | Host  | Value |
|-------|-------|-------|
| A     | `@`   | `185.199.108.153` |
| A     | `@`   | `185.199.109.153` |
| A     | `@`   | `185.199.110.153` |
| A     | `@`   | `185.199.111.153` |
| AAAA  | `@`   | `2606:50c0:8000::153` |
| AAAA  | `@`   | `2606:50c0:8001::153` |
| AAAA  | `@`   | `2606:50c0:8002::153` |
| AAAA  | `@`   | `2606:50c0:8003::153` |
| CNAME | `www` | `alexbrunelli08-lgtm.github.io` |

Two things that catch people out:

- The `www` CNAME points at `alexbrunelli08-lgtm.github.io` **with no
  `/legislative-alpha` path**. A CNAME cannot contain a path.
- **If you use Cloudflare for DNS**, set these records to *DNS only* (grey
  cloud), not *Proxied* (orange cloud). Proxying blocks GitHub from issuing
  the TLS certificate, and HTTPS will fail until you turn it off.

### A3. Tell GitHub about the domain

Repo → **Settings** → **Pages** → **Custom domain** → type the domain → **Save**.

This writes a `CNAME` file into the repo root. Run `git pull` locally
afterwards or your next push will be working from a stale tree.

### A4. Wait for the DNS check, then force HTTPS

The Settings → Pages panel shows the check. Usually minutes; occasionally a few
hours. Once it passes, tick **Enforce HTTPS**. Do not skip this — without it the
site answers on plain HTTP and Google treats `http://` and `https://` as two
different sites.

### A5. Hand it back to me

Say the word and I will update, in one pass:

- `<link rel="canonical">`, `og:url`, `og:image` and `twitter:image`
- `sitemap.xml` → `<loc>` and the `Sitemap:` line in `robots.txt`
- `README.md` and the fetcher's declared User-Agent

A bonus that falls out of this: **`robots.txt` starts working.** On a project
Pages site it serves at `/legislative-alpha/robots.txt`, and crawlers only ever
read `/robots.txt` at the domain root — so right now it is ignored entirely.
On a real domain the repo root *is* the origin root and the file takes effect
unchanged.

---

## Part B — Tell Google it exists

### B1. Open Search Console

<https://search.google.com/search-console> — sign in with any Google account.

### B2. Add the property

**If you did Part A** → choose **Domain** property, enter the bare domain.
Verify by adding the TXT record it gives you at your registrar. This one
property covers `www`, non-`www`, http and https together.

**If you stayed on github.io** → choose **URL prefix**, and enter the full path
including the trailing slash:

```
https://alexbrunelli08-lgtm.github.io/legislative-alpha/
```

Verify with the **HTML tag** method. Google gives you a `<meta>` tag — paste it
to me and I will put it in `<head>` and push it. The file-upload method also
works. *Do not* pick "Google Analytics" or "Tag Manager" — the site has neither,
deliberately.

### B3. Submit the sitemap

Search Console → **Sitemaps** → enter `sitemap.xml` → Submit.

### B4. Ask for the homepage to be crawled

Search Console → **URL Inspection** → paste the homepage URL → **Request
indexing**. This is a nudge, not a guarantee.

### B5. Bing, while you are here

<https://www.bing.com/webmasters> lets you import everything from Search
Console in one click. Two minutes, and it covers Bing, Yahoo and DuckDuckGo.

### B6. Wait

A brand-new site with no inbound links typically takes **days to a few weeks**
to appear, and longer to rank for anything but its own name. Search Console's
Coverage report tells you what Google has actually done, which is better
information than refreshing a search box.

---

## Part C — Give it a reason to rank

Being indexed and being *found* are different problems. Two honest constraints:

### The hash-routing ceiling

The whole site lives at **one URL**. `#learn`, `#about` and `#insiders` are
fragments, and no crawler treats a fragment as a separate page. That means:

- Google will index exactly **one page** for this site.
- The twelve Learn modules — the content most likely to attract search traffic,
  since people genuinely search "does day trading work" — **cannot rank**,
  because they have no address of their own.

Fixing this means pre-rendering the evergreen sections as real static HTML at
real paths (`/learn/why-day-trading-doesnt-work/` and so on) with the text baked
in rather than assembled by JavaScript. The fetch script already runs on every
update and could emit them. This is the single highest-leverage SEO change
available and it is a real piece of work — ask and I will scope it properly.

### Links are what actually move the needle

For a new site, inbound links from places real people read matter more than
anything on this list. One link from somewhere relevant beats a hundred
directory submissions. Realistically: post it where the audience already is.

Never buy links. It is the one thing that can get a site actively penalised
rather than merely ignored.

---

## Quick status check

```bash
curl -s -o /dev/null -w "HTTP %{http_code}\n" https://alexbrunelli08-lgtm.github.io/legislative-alpha/
```
