"""Summit Shine FastAPI app — single file with all routes for simplicity."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.middleware.base import BaseHTTPMiddleware

from . import auth, db

BASE_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))

CURRENCY = os.environ.get("SUMMIT_CURRENCY", "$")
TAX_RATE = float(os.environ.get("SUMMIT_TAX_RATE", "0") or 0)
BUSINESS_NAME = os.environ.get("SUMMIT_BUSINESS_NAME", "Summit Shine")
BUSINESS_EMAIL = os.environ.get("SUMMIT_BUSINESS_EMAIL", "")
BUSINESS_PHONE = os.environ.get("SUMMIT_BUSINESS_PHONE", "")
BUSINESS_TAGLINE = os.environ.get("SUMMIT_TAGLINE", "Naturally spotless.")
SERVICE_AREA = os.environ.get("SUMMIT_SERVICE_AREA", "")
TEAM = [t.strip() for t in os.environ.get("SUMMIT_TEAM", "Aidan,Partner").split(",") if t.strip()]

JOB_STATUSES = ["scheduled", "in_progress", "done", "cancelled"]
QUOTE_STATUSES = ["draft", "sent", "accepted", "declined"]
INVOICE_STATUSES = ["draft", "sent", "paid", "overdue"]
REQUEST_STATUSES = ["new", "contacted", "quoted", "converted", "dismissed"]

SERVICE_TYPES = [
    "Standard / regular clean",
    "Deep clean",
    "Move-in / move-out clean",
    "End-of-lease clean",
    "Office / commercial clean",
    "Retail / storefront clean",
    "Airbnb / short-term turnover",
    "One-off / special occasion clean",
]
FREQUENCIES = ["One-off", "Weekly", "Fortnightly", "Monthly"]
PROPERTY_TYPES = ["House", "Apartment / condo", "Townhouse", "Office", "Retail / storefront", "Other"]


def money(value) -> str:
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        v = 0
    return f"{CURRENCY}{v:,.2f}"


def fmt_date(value) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "")).strftime("%d %b %Y")
    except ValueError:
        return str(value)


def fmt_status(value: str) -> str:
    return (value or "").replace("_", " ").title()


def logo(size: int = 40, ring: bool = True) -> Markup:
    """Render the Summit Shine mountain-and-leaf logo as inline SVG."""
    ring_svg = (
        f'<circle cx="50" cy="50" r="48" fill="#f5f3ee" stroke="#15803d" stroke-width="2.5"/>'
        if ring else ''
    )
    return Markup(
        f'<svg viewBox="0 0 100 100" width="{size}" height="{size}" xmlns="http://www.w3.org/2000/svg" '
        f'aria-label="{BUSINESS_NAME} logo" role="img">'
        f'{ring_svg}'
        # back range (light sage)
        '<path d="M12 78 L36 38 L50 56 L62 42 L88 78 Z" fill="#84cc16" opacity="0.5"/>'
        # front mountain (forest)
        '<path d="M8 82 L32 42 L46 62 L54 52 L74 82 Z" fill="#15803d"/>'
        # leaf cresting the highest peak
        '<path d="M32 42 C28 30 36 18 46 18 C46 28 40 38 32 42 Z" fill="#84cc16"/>'
        '<path d="M34 40 L42 24" stroke="#15803d" stroke-width="1.2" stroke-linecap="round" fill="none"/>'
        '</svg>'
    )


TEMPLATES.env.filters["money"] = money
TEMPLATES.env.filters["fmt_date"] = fmt_date
TEMPLATES.env.filters["fmt_status"] = fmt_status
TEMPLATES.env.globals.update(
    BUSINESS_NAME=BUSINESS_NAME,
    BUSINESS_EMAIL=BUSINESS_EMAIL,
    BUSINESS_PHONE=BUSINESS_PHONE,
    BUSINESS_TAGLINE=BUSINESS_TAGLINE,
    SERVICE_AREA=SERVICE_AREA,
    CURRENCY=CURRENCY,
    TAX_RATE=TAX_RATE,
    TEAM=TEAM,
    JOB_STATUSES=JOB_STATUSES,
    QUOTE_STATUSES=QUOTE_STATUSES,
    INVOICE_STATUSES=INVOICE_STATUSES,
    REQUEST_STATUSES=REQUEST_STATUSES,
    SERVICE_TYPES=SERVICE_TYPES,
    FREQUENCIES=FREQUENCIES,
    PROPERTY_TYPES=PROPERTY_TYPES,
    today=lambda: date.today().isoformat(),
    logo=logo,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Summit Shine", lifespan=lifespan)

STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


PUBLIC_PATHS = ("/", "/login", "/logout", "/quote-request", "/health", "/static", "/favicon.ico")


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if any(path == p or path.startswith(p + "/") for p in PUBLIC_PATHS):
            return await call_next(request)
        if not auth.is_authenticated(request):
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)


app.add_middleware(AuthMiddleware)


def render(name: str, request: Request, **ctx) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request, name, {"user": auth.current_user(request), **ctx}
    )


def _redirect(url: str, msg: str | None = None) -> RedirectResponse:
    if msg:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}msg={msg}"
    return RedirectResponse(url, status_code=303)


# ============================================================
# Health + auth
# ============================================================

@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    if auth.is_authenticated(request):
        return RedirectResponse("/", status_code=303)
    return render("login.html", request, error=None)


@app.post("/login")
async def login_post(request: Request, password: str = Form(...)):
    token = auth.login(password)
    if not token:
        return render("login.html", request, error="Wrong password.")
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        auth.SESSION_COOKIE, token,
        httponly=True, samesite="lax",
        max_age=auth.SESSION_TTL_SECONDS,
    )
    return resp


@app.post("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


# ============================================================
# Dashboard
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Root: marketing landing for visitors, dashboard for the team."""
    if not auth.is_authenticated(request):
        return render("public_landing.html", request)
    today = date.today().isoformat()
    upcoming = db.query(
        """SELECT j.*, c.name AS client_name
             FROM jobs j JOIN clients c ON c.id = j.client_id
            WHERE j.status IN ('scheduled','in_progress')
              AND (j.scheduled_for IS NULL OR j.scheduled_for >= ?)
            ORDER BY COALESCE(j.scheduled_for, '9999-12-31') ASC
            LIMIT 8""",
        (today,),
    )
    new_requests = db.query(
        "SELECT * FROM quote_requests WHERE status = 'new' ORDER BY created_at DESC LIMIT 5"
    )
    new_request_count = db.query_one(
        "SELECT COUNT(*) AS c FROM quote_requests WHERE status = 'new'"
    )["c"]
    outstanding = db.query_one(
        "SELECT COALESCE(SUM(total), 0) AS t FROM invoices WHERE status IN ('sent','overdue')"
    )["t"]
    paid_this_month = db.query_one(
        """SELECT COALESCE(SUM(total), 0) AS t FROM invoices
           WHERE status = 'paid' AND substr(paid_at,1,7) = ?""",
        (date.today().strftime("%Y-%m"),),
    )["t"]
    return render(
        "dashboard.html", request,
        active="dashboard",
        upcoming=upcoming,
        new_requests=new_requests,
        new_request_count=new_request_count,
        outstanding=outstanding,
        paid_this_month=paid_this_month,
        client_count=db.query_one("SELECT COUNT(*) AS c FROM clients")["c"],
    )


# ============================================================
# Clients
# ============================================================

@app.get("/clients", response_class=HTMLResponse)
async def clients_list(request: Request, q: str = ""):
    if q:
        like = f"%{q}%"
        clients = db.query(
            """SELECT * FROM clients
                WHERE name LIKE ? OR email LIKE ? OR phone LIKE ? OR address LIKE ?
                ORDER BY name ASC""",
            (like, like, like, like),
        )
    else:
        clients = db.query("SELECT * FROM clients ORDER BY name ASC")
    return render("clients_list.html", request, active="clients", clients=clients, q=q)


@app.get("/clients/new", response_class=HTMLResponse)
async def clients_new_get(request: Request):
    return render("clients_form.html", request, active="clients", client=None)


@app.post("/clients/new")
async def clients_new_post(
    request: Request,
    name: str = Form(...),
    email: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
    property_type: str = Form(""),
    notes: str = Form(""),
):
    cid = db.execute(
        """INSERT INTO clients(name, email, phone, address, property_type, notes)
           VALUES(?, ?, ?, ?, ?, ?)""",
        (name.strip(), email.strip(), phone.strip(), address.strip(), property_type, notes.strip()),
    )
    return _redirect(f"/clients/{cid}", "Client added")


@app.get("/clients/{cid}", response_class=HTMLResponse)
async def clients_detail(request: Request, cid: int):
    client = db.query_one("SELECT * FROM clients WHERE id = ?", (cid,))
    if not client:
        raise HTTPException(404)
    jobs = db.query("SELECT * FROM jobs WHERE client_id = ? ORDER BY COALESCE(scheduled_for, created_at) DESC", (cid,))
    quotes = db.query("SELECT * FROM quotes WHERE client_id = ? ORDER BY created_at DESC", (cid,))
    invoices = db.query("SELECT * FROM invoices WHERE client_id = ? ORDER BY created_at DESC", (cid,))
    return render(
        "clients_detail.html", request,
        active="clients", client=client, jobs=jobs, quotes=quotes, invoices=invoices,
    )


@app.get("/clients/{cid}/edit", response_class=HTMLResponse)
async def clients_edit_get(request: Request, cid: int):
    client = db.query_one("SELECT * FROM clients WHERE id = ?", (cid,))
    if not client:
        raise HTTPException(404)
    return render("clients_form.html", request, active="clients", client=client)


@app.post("/clients/{cid}/edit")
async def clients_edit_post(
    request: Request, cid: int,
    name: str = Form(...),
    email: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
    property_type: str = Form(""),
    notes: str = Form(""),
):
    with db.connect() as conn:
        conn.execute(
            """UPDATE clients
                  SET name=?, email=?, phone=?, address=?, property_type=?, notes=?
                WHERE id=?""",
            (name.strip(), email.strip(), phone.strip(), address.strip(), property_type, notes.strip(), cid),
        )
    return _redirect(f"/clients/{cid}", "Client updated")


@app.post("/clients/{cid}/delete")
async def clients_delete(cid: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM clients WHERE id = ?", (cid,))
    return _redirect("/clients", "Client deleted")


# ============================================================
# Jobs
# ============================================================

@app.get("/jobs", response_class=HTMLResponse)
async def jobs_list(request: Request, status: str = ""):
    if status and status in JOB_STATUSES:
        jobs = db.query(
            """SELECT j.*, c.name AS client_name
                 FROM jobs j JOIN clients c ON c.id = j.client_id
                WHERE j.status = ?
                ORDER BY COALESCE(j.scheduled_for, j.created_at) DESC""",
            (status,),
        )
    else:
        jobs = db.query(
            """SELECT j.*, c.name AS client_name
                 FROM jobs j JOIN clients c ON c.id = j.client_id
                ORDER BY COALESCE(j.scheduled_for, j.created_at) DESC"""
        )
    return render("jobs_list.html", request, active="jobs", jobs=jobs, status=status)


@app.get("/jobs/new", response_class=HTMLResponse)
async def jobs_new_get(request: Request, client_id: Optional[int] = None):
    clients = db.query("SELECT id, name FROM clients ORDER BY name ASC")
    return render("jobs_form.html", request, active="jobs", job=None, clients=clients, preselect_client=client_id)


@app.post("/jobs/new")
async def jobs_new_post(
    request: Request,
    client_id: int = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    status: str = Form("scheduled"),
    assigned_to: str = Form(""),
    scheduled_for: str = Form(""),
    price: str = Form(""),
    notes: str = Form(""),
):
    if status not in JOB_STATUSES:
        status = "scheduled"
    price_val = float(price) if price.strip() else None
    jid = db.execute(
        """INSERT INTO jobs(client_id, title, description, status, assigned_to, scheduled_for, price, notes)
           VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
        (client_id, title.strip(), description.strip(), status,
         assigned_to.strip(), scheduled_for or None, price_val, notes.strip()),
    )
    return _redirect(f"/jobs/{jid}", "Job created")


@app.get("/jobs/{jid}", response_class=HTMLResponse)
async def jobs_detail(request: Request, jid: int):
    job = db.query_one(
        """SELECT j.*, c.name AS client_name
             FROM jobs j JOIN clients c ON c.id = j.client_id WHERE j.id = ?""",
        (jid,),
    )
    if not job:
        raise HTTPException(404)
    return render("jobs_detail.html", request, active="jobs", job=job)


@app.get("/jobs/{jid}/edit", response_class=HTMLResponse)
async def jobs_edit_get(request: Request, jid: int):
    job = db.query_one("SELECT * FROM jobs WHERE id = ?", (jid,))
    if not job:
        raise HTTPException(404)
    clients = db.query("SELECT id, name FROM clients ORDER BY name ASC")
    return render("jobs_form.html", request, active="jobs", job=job, clients=clients, preselect_client=None)


@app.post("/jobs/{jid}/edit")
async def jobs_edit_post(
    request: Request, jid: int,
    client_id: int = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    status: str = Form("scheduled"),
    assigned_to: str = Form(""),
    scheduled_for: str = Form(""),
    price: str = Form(""),
    notes: str = Form(""),
):
    if status not in JOB_STATUSES:
        status = "scheduled"
    price_val = float(price) if price.strip() else None
    completed_at = datetime.utcnow().isoformat(timespec="seconds") if status == "done" else None
    with db.connect() as conn:
        existing = conn.execute("SELECT completed_at, status FROM jobs WHERE id = ?", (jid,)).fetchone()
        # Preserve completed_at if already set and status is still done
        if existing and existing["status"] == "done" and status == "done":
            completed_at = existing["completed_at"]
        conn.execute(
            """UPDATE jobs SET client_id=?, title=?, description=?, status=?,
                    assigned_to=?, scheduled_for=?, price=?, notes=?, completed_at=?
                WHERE id=?""",
            (client_id, title.strip(), description.strip(), status, assigned_to.strip(),
             scheduled_for or None, price_val, notes.strip(), completed_at, jid),
        )
    return _redirect(f"/jobs/{jid}", "Job updated")


@app.post("/jobs/{jid}/delete")
async def jobs_delete(jid: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (jid,))
    return _redirect("/jobs", "Job deleted")


# ============================================================
# Quotes
# ============================================================

def _save_items(table: str, fk_col: str, parent_id: int,
                descriptions: list[str], quantities: list[str], unit_prices: list[str]):
    """Replace all line items for the given quote/invoice."""
    with db.connect() as conn:
        conn.execute(f"DELETE FROM {table} WHERE {fk_col} = ?", (parent_id,))
        for i, desc in enumerate(descriptions):
            if not desc or not desc.strip():
                continue
            try:
                q = float(quantities[i]) if i < len(quantities) and quantities[i] else 1
            except ValueError:
                q = 1
            try:
                up = float(unit_prices[i]) if i < len(unit_prices) and unit_prices[i] else 0
            except ValueError:
                up = 0
            conn.execute(
                f"INSERT INTO {table}({fk_col}, description, quantity, unit_price, sort_order) VALUES(?, ?, ?, ?, ?)",
                (parent_id, desc.strip(), q, up, i),
            )


@app.get("/quotes", response_class=HTMLResponse)
async def quotes_list(request: Request, status: str = ""):
    if status and status in QUOTE_STATUSES:
        rows = db.query(
            """SELECT q.*, c.name AS client_name
                 FROM quotes q JOIN clients c ON c.id = q.client_id
                WHERE q.status = ? ORDER BY q.created_at DESC""",
            (status,),
        )
    else:
        rows = db.query(
            """SELECT q.*, c.name AS client_name
                 FROM quotes q JOIN clients c ON c.id = q.client_id
                ORDER BY q.created_at DESC"""
        )
    return render("quotes_list.html", request, active="quotes", quotes=rows, status=status)


@app.get("/quotes/new", response_class=HTMLResponse)
async def quotes_new_get(request: Request, client_id: Optional[int] = None, from_request: Optional[int] = None):
    clients = db.query("SELECT id, name FROM clients ORDER BY name ASC")
    prefill_items = []
    notes = ""
    if from_request:
        req = db.query_one("SELECT * FROM quote_requests WHERE id = ?", (from_request,))
        if req:
            notes = (
                f"From quote request: {req['service_type'] or ''} "
                f"({req['frequency'] or 'one-off'}) — {req['details'] or ''}"
            ).strip()
            prefill_items.append({
                "description": f"{req['service_type'] or 'Cleaning service'}" + (f" ({req['frequency']})" if req['frequency'] else ""),
                "quantity": 1,
                "unit_price": 0,
            })
    return render(
        "quotes_form.html", request,
        active="quotes", quote=None, items=prefill_items, clients=clients,
        preselect_client=client_id, prefill_notes=notes, from_request=from_request,
    )


@app.post("/quotes/new")
async def quotes_new_post(
    request: Request,
    client_id: int = Form(...),
    notes: str = Form(""),
    valid_until: str = Form(""),
    tax_rate: str = Form(""),
    item_description: list[str] = Form([]),
    item_quantity: list[str] = Form([]),
    item_unit_price: list[str] = Form([]),
    from_request: str = Form(""),
):
    rate = float(tax_rate) if tax_rate.strip() else TAX_RATE
    number = db.next_number("Q", "quotes")
    qid = db.execute(
        """INSERT INTO quotes(client_id, number, status, notes, valid_until, tax_rate)
           VALUES(?, ?, 'draft', ?, ?, ?)""",
        (client_id, number, notes.strip(), valid_until or None, rate),
    )
    _save_items("quote_items", "quote_id", qid, item_description, item_quantity, item_unit_price)
    db.recompute_totals("quotes", qid)
    if from_request.strip().isdigit():
        with db.connect() as conn:
            conn.execute("UPDATE quote_requests SET status = 'quoted' WHERE id = ?", (int(from_request),))
    return _redirect(f"/quotes/{qid}", "Quote created")


@app.get("/quotes/{qid}", response_class=HTMLResponse)
async def quotes_detail(request: Request, qid: int):
    quote = db.query_one(
        """SELECT q.*, c.name AS client_name, c.email AS client_email,
                  c.phone AS client_phone, c.address AS client_address
             FROM quotes q JOIN clients c ON c.id = q.client_id WHERE q.id = ?""",
        (qid,),
    )
    if not quote:
        raise HTTPException(404)
    items = db.query("SELECT * FROM quote_items WHERE quote_id = ? ORDER BY sort_order ASC, id ASC", (qid,))
    return render("quotes_detail.html", request, active="quotes", quote=quote, items=items)


@app.get("/quotes/{qid}/edit", response_class=HTMLResponse)
async def quotes_edit_get(request: Request, qid: int):
    quote = db.query_one("SELECT * FROM quotes WHERE id = ?", (qid,))
    if not quote:
        raise HTTPException(404)
    items = db.query("SELECT * FROM quote_items WHERE quote_id = ? ORDER BY sort_order ASC, id ASC", (qid,))
    clients = db.query("SELECT id, name FROM clients ORDER BY name ASC")
    return render(
        "quotes_form.html", request,
        active="quotes", quote=quote, items=items, clients=clients,
        preselect_client=None, prefill_notes="", from_request=None,
    )


@app.post("/quotes/{qid}/edit")
async def quotes_edit_post(
    request: Request, qid: int,
    client_id: int = Form(...),
    notes: str = Form(""),
    valid_until: str = Form(""),
    tax_rate: str = Form(""),
    item_description: list[str] = Form([]),
    item_quantity: list[str] = Form([]),
    item_unit_price: list[str] = Form([]),
):
    rate = float(tax_rate) if tax_rate.strip() else TAX_RATE
    with db.connect() as conn:
        conn.execute(
            "UPDATE quotes SET client_id=?, notes=?, valid_until=?, tax_rate=? WHERE id=?",
            (client_id, notes.strip(), valid_until or None, rate, qid),
        )
    _save_items("quote_items", "quote_id", qid, item_description, item_quantity, item_unit_price)
    db.recompute_totals("quotes", qid)
    return _redirect(f"/quotes/{qid}", "Quote updated")


@app.post("/quotes/{qid}/status")
async def quotes_set_status(qid: int, status: str = Form(...)):
    if status not in QUOTE_STATUSES:
        raise HTTPException(400, "Invalid status")
    stamp_col = {"sent": "sent_at", "accepted": "accepted_at"}.get(status)
    with db.connect() as conn:
        if stamp_col:
            conn.execute(
                f"UPDATE quotes SET status = ?, {stamp_col} = COALESCE({stamp_col}, datetime('now')) WHERE id = ?",
                (status, qid),
            )
        else:
            conn.execute("UPDATE quotes SET status = ? WHERE id = ?", (status, qid))
    return _redirect(f"/quotes/{qid}", f"Marked {status}")


@app.post("/quotes/{qid}/convert")
async def quotes_convert_to_invoice(qid: int):
    quote = db.query_one("SELECT * FROM quotes WHERE id = ?", (qid,))
    if not quote:
        raise HTTPException(404)
    items = db.query("SELECT * FROM quote_items WHERE quote_id = ? ORDER BY sort_order ASC, id ASC", (qid,))
    number = db.next_number("INV", "invoices")
    inv_id = db.execute(
        """INSERT INTO invoices(client_id, quote_id, number, status, notes, tax_rate)
           VALUES(?, ?, ?, 'draft', ?, ?)""",
        (quote["client_id"], qid, number, quote["notes"], quote["tax_rate"]),
    )
    with db.connect() as conn:
        for it in items:
            conn.execute(
                """INSERT INTO invoice_items(invoice_id, description, quantity, unit_price, sort_order)
                   VALUES(?, ?, ?, ?, ?)""",
                (inv_id, it["description"], it["quantity"], it["unit_price"], it["sort_order"]),
            )
    db.recompute_totals("invoices", inv_id)
    return _redirect(f"/invoices/{inv_id}", "Invoice created from quote")


@app.post("/quotes/{qid}/delete")
async def quotes_delete(qid: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM quotes WHERE id = ?", (qid,))
    return _redirect("/quotes", "Quote deleted")


# ============================================================
# Invoices
# ============================================================

@app.get("/invoices", response_class=HTMLResponse)
async def invoices_list(request: Request, status: str = ""):
    if status and status in INVOICE_STATUSES:
        rows = db.query(
            """SELECT i.*, c.name AS client_name
                 FROM invoices i JOIN clients c ON c.id = i.client_id
                WHERE i.status = ? ORDER BY i.created_at DESC""",
            (status,),
        )
    else:
        rows = db.query(
            """SELECT i.*, c.name AS client_name
                 FROM invoices i JOIN clients c ON c.id = i.client_id
                ORDER BY i.created_at DESC"""
        )
    return render("invoices_list.html", request, active="invoices", invoices=rows, status=status)


@app.get("/invoices/new", response_class=HTMLResponse)
async def invoices_new_get(request: Request, client_id: Optional[int] = None):
    clients = db.query("SELECT id, name FROM clients ORDER BY name ASC")
    return render(
        "invoices_form.html", request,
        active="invoices", invoice=None, items=[], clients=clients, preselect_client=client_id,
    )


@app.post("/invoices/new")
async def invoices_new_post(
    request: Request,
    client_id: int = Form(...),
    notes: str = Form(""),
    due_date: str = Form(""),
    tax_rate: str = Form(""),
    item_description: list[str] = Form([]),
    item_quantity: list[str] = Form([]),
    item_unit_price: list[str] = Form([]),
):
    rate = float(tax_rate) if tax_rate.strip() else TAX_RATE
    number = db.next_number("INV", "invoices")
    inv_id = db.execute(
        """INSERT INTO invoices(client_id, number, status, notes, due_date, tax_rate)
           VALUES(?, ?, 'draft', ?, ?, ?)""",
        (client_id, number, notes.strip(), due_date or None, rate),
    )
    _save_items("invoice_items", "invoice_id", inv_id, item_description, item_quantity, item_unit_price)
    db.recompute_totals("invoices", inv_id)
    return _redirect(f"/invoices/{inv_id}", "Invoice created")


@app.get("/invoices/{iid}", response_class=HTMLResponse)
async def invoices_detail(request: Request, iid: int):
    inv = db.query_one(
        """SELECT i.*, c.name AS client_name, c.email AS client_email,
                  c.phone AS client_phone, c.address AS client_address
             FROM invoices i JOIN clients c ON c.id = i.client_id WHERE i.id = ?""",
        (iid,),
    )
    if not inv:
        raise HTTPException(404)
    items = db.query("SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY sort_order ASC, id ASC", (iid,))
    return render("invoices_detail.html", request, active="invoices", invoice=inv, items=items)


@app.get("/invoices/{iid}/edit", response_class=HTMLResponse)
async def invoices_edit_get(request: Request, iid: int):
    inv = db.query_one("SELECT * FROM invoices WHERE id = ?", (iid,))
    if not inv:
        raise HTTPException(404)
    items = db.query("SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY sort_order ASC, id ASC", (iid,))
    clients = db.query("SELECT id, name FROM clients ORDER BY name ASC")
    return render(
        "invoices_form.html", request,
        active="invoices", invoice=inv, items=items, clients=clients, preselect_client=None,
    )


@app.post("/invoices/{iid}/edit")
async def invoices_edit_post(
    request: Request, iid: int,
    client_id: int = Form(...),
    notes: str = Form(""),
    due_date: str = Form(""),
    tax_rate: str = Form(""),
    item_description: list[str] = Form([]),
    item_quantity: list[str] = Form([]),
    item_unit_price: list[str] = Form([]),
):
    rate = float(tax_rate) if tax_rate.strip() else TAX_RATE
    with db.connect() as conn:
        conn.execute(
            "UPDATE invoices SET client_id=?, notes=?, due_date=?, tax_rate=? WHERE id=?",
            (client_id, notes.strip(), due_date or None, rate, iid),
        )
    _save_items("invoice_items", "invoice_id", iid, item_description, item_quantity, item_unit_price)
    db.recompute_totals("invoices", iid)
    return _redirect(f"/invoices/{iid}", "Invoice updated")


@app.post("/invoices/{iid}/status")
async def invoices_set_status(iid: int, status: str = Form(...)):
    if status not in INVOICE_STATUSES:
        raise HTTPException(400, "Invalid status")
    stamps = {"sent": "sent_at", "paid": "paid_at"}
    col = stamps.get(status)
    with db.connect() as conn:
        if col:
            conn.execute(
                f"UPDATE invoices SET status = ?, {col} = COALESCE({col}, datetime('now')) WHERE id = ?",
                (status, iid),
            )
        else:
            conn.execute("UPDATE invoices SET status = ? WHERE id = ?", (status, iid))
    return _redirect(f"/invoices/{iid}", f"Marked {status}")


@app.post("/invoices/{iid}/delete")
async def invoices_delete(iid: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM invoices WHERE id = ?", (iid,))
    return _redirect("/invoices", "Invoice deleted")


# ============================================================
# Quote requests (incoming from public form)
# ============================================================

@app.get("/requests", response_class=HTMLResponse)
async def requests_list(request: Request, status: str = ""):
    if status and status in REQUEST_STATUSES:
        rows = db.query("SELECT * FROM quote_requests WHERE status = ? ORDER BY created_at DESC", (status,))
    else:
        rows = db.query("SELECT * FROM quote_requests ORDER BY created_at DESC")
    return render("requests_list.html", request, active="requests", requests=rows, status=status)


@app.get("/requests/{rid}", response_class=HTMLResponse)
async def requests_detail(request: Request, rid: int):
    req = db.query_one("SELECT * FROM quote_requests WHERE id = ?", (rid,))
    if not req:
        raise HTTPException(404)
    return render("requests_detail.html", request, active="requests", req=req)


@app.post("/requests/{rid}/status")
async def requests_set_status(rid: int, status: str = Form(...)):
    if status not in REQUEST_STATUSES:
        raise HTTPException(400, "Invalid status")
    with db.connect() as conn:
        conn.execute("UPDATE quote_requests SET status = ? WHERE id = ?", (status, rid))
    return _redirect(f"/requests/{rid}", f"Marked {status}")


@app.post("/requests/{rid}/convert-to-client")
async def requests_convert(rid: int):
    req = db.query_one("SELECT * FROM quote_requests WHERE id = ?", (rid,))
    if not req:
        raise HTTPException(404)
    if req["client_id"]:
        return _redirect(f"/clients/{req['client_id']}", "Already linked to a client")
    cid = db.execute(
        """INSERT INTO clients(name, email, phone, address, property_type, notes)
           VALUES(?, ?, ?, ?, ?, ?)""",
        (
            req["name"], req["email"] or "", req["phone"] or "",
            req["address"] or "", req["property_type"] or "",
            f"Imported from quote request #{rid}\n"
            f"Service: {req['service_type'] or ''}\n"
            f"Frequency: {req['frequency'] or ''}\n"
            f"Details: {req['details'] or ''}",
        ),
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE quote_requests SET status = 'converted', client_id = ? WHERE id = ?",
            (cid, rid),
        )
    return _redirect(f"/clients/{cid}", "Client created from request")


@app.post("/requests/{rid}/delete")
async def requests_delete(rid: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM quote_requests WHERE id = ?", (rid,))
    return _redirect("/requests", "Request deleted")


# ============================================================
# Public quote-request form (no auth)
# ============================================================

@app.get("/quote-request", response_class=HTMLResponse)
async def public_quote_request_get(request: Request, embed: int = 0):
    return render("public_quote_request.html", request, embed=bool(embed), submitted=False, error=None)


@app.post("/quote-request")
async def public_quote_request_post(
    request: Request,
    name: str = Form(...),
    phone: str = Form(...),
    email: str = Form(""),
    address: str = Form(""),
    property_type: str = Form(""),
    service_type: str = Form(""),
    frequency: str = Form(""),
    details: str = Form(""),
    embed: int = Form(0),
):
    if not name.strip() or not phone.strip():
        return render(
            "public_quote_request.html", request,
            embed=bool(embed), submitted=False,
            error="Name and phone are required so we can get back to you.",
        )
    db.execute(
        """INSERT INTO quote_requests
           (name, email, phone, address, property_type, service_type, frequency, details)
           VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            name.strip(), email.strip(), phone.strip(), address.strip(),
            property_type, service_type, frequency, details.strip(),
        ),
    )
    return render("public_quote_request.html", request, embed=bool(embed), submitted=True, error=None)
