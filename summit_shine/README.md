# Summit Shine

A self-hosted, free job-tracking app for a small cleaning business. Like Jobber, but no monthly fee — runs on your own free-tier hosting.

**Features in v1**
- Clients (CRM) — name, address, contact, property type, notes, full job/quote/invoice history
- Jobs — schedule, assign to team member, status (scheduled / in progress / done / cancelled), price
- Quotes — line items, tax, status (draft / sent / accepted / declined), printable PDF, one-click "convert to invoice"
- Invoices — line items, due date, status (draft / sent / paid / overdue), printable PDF
- Customer quote-request form — public page customers fill in, lands in your dashboard inbox
- Shared password login for you and your partner

**Stack**
- FastAPI + Jinja2 + SQLite (zero external services)
- Tailwind via CDN (no build step)
- Browser "Print → Save as PDF" for quote/invoice export
- Single SQLite file for storage

---

## Run locally

```bash
# Python deps
pip install -r summit_shine/requirements.txt
# Build the bundled Tailwind CSS (only needed once, or when you change templates / tailwind.src.css)
cd summit_shine && npm install && npx tailwindcss -i ./tailwind.src.css -o ./static/tailwind.css --minify && cd ..
# Run
SUMMIT_PASSWORD=test SUMMIT_SECRET=dev uvicorn summit_shine.app:app --reload
```

Open http://localhost:8000 and sign in with `test`.

The built CSS (`summit_shine/static/tailwind.css`) is checked into the repo, so on a deploy host you only need Python — the Tailwind build is just for local iteration.

## Environment variables

See `.env.example`. The important ones:

| Var | What |
| --- | --- |
| `SUMMIT_PASSWORD` | Shared password — anyone with it can sign in |
| `SUMMIT_SECRET` | Random secret for signing session cookies (`python -c "import secrets;print(secrets.token_hex(32))"`) |
| `SUMMIT_BUSINESS_NAME` | Shown on nav, quotes, invoices, public form (default: Summit Shine) |
| `SUMMIT_BUSINESS_EMAIL` / `SUMMIT_BUSINESS_PHONE` | Shown on quotes / invoices header |
| `SUMMIT_CURRENCY` | Currency symbol (default: `$`) |
| `SUMMIT_TAX_RATE` | Default tax rate, e.g. `0.10` for 10% |
| `SUMMIT_TEAM` | Comma-separated names for the "assigned to" dropdown (default: `Aidan,Partner`) |
| `SUMMIT_DB` | Path to the SQLite file (default: `summit_shine.db`) |

## Deploy (free options)

**Railway** — easiest if you're already using it. Create a new service for this directory:
- Root directory: `summit_shine`
- Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
- Add a persistent volume mounted at the working directory (so `summit_shine.db` survives restarts), and point `SUMMIT_DB` at a path on that volume, e.g. `/data/summit_shine.db`.
- Set the env vars above.

**Fly.io** — `fly launch`, attach a volume, set the SUMMIT_DB path to live on that volume.

**Render** — free web service works but sleeps; pair with `render-disk` for SQLite persistence.

**Self-host on a $5 VPS** — `pip install -r requirements.txt && uvicorn summit_shine.app:app --host 0.0.0.0 --port 80` behind nginx with TLS via certbot.

⚠️ **SQLite + free serverless platforms don't mix** — the DB file gets wiped on every cold start. Always deploy with a persistent disk/volume.

## Attaching the quote form to your existing website

Two options:

1. **Link to it** — your site's "Get a quote" button links to `https://your-summit-shine-domain.com/quote-request`. Done.
2. **Embed it** — drop this into your website where you want the form:
   ```html
   <iframe src="https://your-summit-shine-domain.com/quote-request?embed=1"
           width="100%" height="800" style="border:0;border-radius:12px;"></iframe>
   ```
   The `?embed=1` flag strips the logo header so it blends into the surrounding page.

## Roadmap (next versions)

- Photo uploads on jobs (before/after shots)
- Email quote/invoice straight from the app (configurable SMTP)
- Stripe payment links on invoices
- Recurring jobs (auto-generate weekly/fortnightly bookings)
- Calendar view with drag-to-reschedule
- Per-user accounts instead of a shared password
