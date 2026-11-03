"""Daily lead email: each person gets the new leads for their own products.

Sent by the daily job (cloud_worker.py) after the leads are saved.
- Only leads with score 5 or more that were not emailed before.
- One email per person, covering all their products that have emails turned on.
- No email on days without new leads.
- A lead is marked as emailed only after the email was sent.

Works with any email service that offers SMTP (Gmail, Brevo, Resend, ...).
GitHub Secrets:
  SMTP_HOST      for example smtp.gmail.com
  SMTP_PORT      587 (default)
  SMTP_USER      the login, for Gmail your Gmail address
  SMTP_PASSWORD  for Gmail an app password (not your normal password)
  EMAIL_FROM     optional, default SMTP_USER
  SITE_URL       optional, the address of the website
"""
import datetime as dt
import html
import os
import smtplib
import sys
from email.message import EmailMessage
from email.utils import formataddr

import requests

MIN_SCORE = 5
MAX_PER_PRODUCT = 10
SITE_URL = os.environ.get("SITE_URL") or "https://marshal-hakikat.github.io/leadfinder/app/"
SOURCE_NAMES = {"hackernews": "Hacker News", "bluesky": "Bluesky", "stackoverflow": "Stack Overflow"}


def source_name(src):
    src = src or ""
    if src.startswith("reddit/"):
        return src[len("reddit/"):]
    if src.startswith("mastodon/"):
        return "Mastodon"
    if src.startswith("dev.to/"):
        return "Dev.to"
    if src.startswith("lemmy/"):
        return "Lemmy"
    return SOURCE_NAMES.get(src, src.replace(".stackexchange", " Stack Exchange"))


def smtp_ready():
    return all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"))


def owner_email(supabase_url, service_key, user_id):
    """Look up the sign-in email of a user (needs the service key)."""
    r = requests.get(supabase_url.rstrip("/") + "/auth/v1/admin/users/" + user_id,
                     headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"}, timeout=30)
    r.raise_for_status()
    return (r.json() or {}).get("email")


def build(products):
    """products: list of (name, leads, extra). Returns (subject, text, html)."""
    total = sum(len(l) + extra for _, l, extra in products)
    if len(products) == 1:
        subject = f"{products[0][0]}: {total} new lead{'s' if total != 1 else ''}"
    else:
        subject = f"{total} new leads for " + " and ".join(p[0] for p in products)
    text, parts = [], []
    for name, leads, extra in products:
        text.append(f"{name}\n" + "=" * len(name))
        parts.append(f'<h2 style="font:700 18px/1.3 Arial,sans-serif;color:#233142;margin:24px 0 8px">{html.escape(name)}</h2>')
        for m in leads:
            post = m.get("raw_posts") or {}
            title = post.get("title") or "(no title)"
            url = m["url"]
            why = (m.get("reason") or "").replace("AI: ", "")
            src = source_name(post.get("source"))
            text.append(f"[{m['score']}] {title}\n    {src} - {url}\n    Why: {why}")
            if m.get("angle"):
                text.append(f"    Draft first message: {m['angle']}")
            color = "#15803d" if m["score"] >= 7 else "#b45309"
            draft = (f'<div style="margin-top:6px;padding:8px 10px;background:#E4F0FD;border-radius:8px;'
                     f'font:14px/1.4 Arial,sans-serif;color:#233142"><b>Draft first message:</b> '
                     f'{html.escape(m["angle"])}</div>') if m.get("angle") else ""
            parts.append(
                f'<div style="padding:12px 0;border-bottom:1px solid #E5E7EB">'
                f'<span style="display:inline-block;min-width:26px;text-align:center;font:700 14px Arial,sans-serif;'
                f'color:#fff;background:{color};border-radius:6px;padding:2px 6px;margin-right:8px">{m["score"]}</span>'
                f'<a href="{html.escape(url, quote=True)}" style="font:700 16px/1.4 Arial,sans-serif;color:#0A6CFF;'
                f'text-decoration:none">{html.escape(title)}</a>'
                f'<div style="font:13px/1.5 Arial,sans-serif;color:#4A6179;margin-top:4px">{html.escape(src)}'
                f'{" · Why: " + html.escape(why) if why else ""}</div>{draft}</div>')
        if extra:
            text.append(f"...and {extra} more on your Leads page.")
            parts.append(f'<p style="font:14px Arial,sans-serif;color:#4A6179">...and {extra} more on your Leads page.</p>')
        text.append("")
    text.append(f"See all leads: {SITE_URL}")
    text.append("You get this email because email alerts are on for your products. "
                "To stop them: My products > Edit > untick 'Email me new leads'.")
    body = (
        '<div style="max-width:640px;margin:0 auto;padding:16px;background:#ffffff">'
        '<div style="font:800 22px Arial,sans-serif;color:#0B2540">Lead Finder</div>'
        f'<p style="font:15px/1.5 Arial,sans-serif;color:#233142">New posts from people who may need your product. '
        f'Reply as yourself, and help first.</p>{"".join(parts)}'
        f'<p style="margin:24px 0"><a href="{html.escape(SITE_URL, quote=True)}" style="display:inline-block;'
        'background:#0A6CFF;color:#fff;font:700 15px Arial,sans-serif;padding:10px 18px;border-radius:8px;'
        'text-decoration:none">See all leads and drafts</a></p>'
        '<p style="font:12px/1.5 Arial,sans-serif;color:#6B7280">You get this email because email alerts are on '
        "for your products. To stop them: My products &gt; Edit &gt; untick ‘Email me new leads’.</p></div>")
    return subject, "\n".join(text), body


def send(to, subject, text, body):
    msg = EmailMessage()
    sender = os.environ.get("EMAIL_FROM") or os.environ["SMTP_USER"]
    msg["From"] = formataddr(("Lead Finder", sender))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(body, subtype="html")
    port = int(os.environ.get("SMTP_PORT") or 587)
    host = os.environ["SMTP_HOST"]
    if port == 465:
        s = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        s = smtplib.SMTP(host, port, timeout=30)
        s.starttls()
    try:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.send_message(msg)
    finally:
        try:
            s.quit()
        except Exception:
            pass


def send_all(db, project_rows, now_iso, supabase_url, service_key):
    """Email every owner the new leads of their products. Never raises."""
    if not smtp_ready():
        print("  email: not set up yet (add the SMTP secrets) - no emails sent.")
        return
    owners = {}
    for row in project_rows:
        if row.get("email_alerts") is False:
            continue
        owners.setdefault(row["owner_id"], []).append(row)
    sent = 0
    for owner, rows in owners.items():
        try:
            products, ids = [], []
            for row in rows:
                leads = db.select("matches", {
                    "select": "url,score,reason,angle,raw_posts(title,source)",
                    "project_id": f"eq.{row['id']}", "score": f"gte.{MIN_SCORE}",
                    "emailed_at": "is.null", "order": "score.desc"})
                handled = {x["url"] for x in db.select("lead_status", {
                    "select": "url", "project_id": f"eq.{row['id']}", "status": "not.is.null"})}
                fresh = [m for m in leads if m["url"] not in handled]
                ids.append(row["id"])
                if fresh:
                    products.append((row["name"], fresh[:MAX_PER_PRODUCT], max(0, len(fresh) - MAX_PER_PRODUCT)))
            if not products:
                continue
            to = owner_email(supabase_url, service_key, owner)
            if not to:
                print("  ! email: a user has no email address; skipped")
                continue
            subject, text, body = build(products)
            send(to, subject, text, body)
            for pid in ids:   # mark only after the email went out
                db.update("matches", {"project_id": f"eq.{pid}", "emailed_at": "is.null",
                                      "score": f"gte.{MIN_SCORE}"}, {"emailed_at": now_iso})
            sent += 1
            print(f"  email sent: {subject}")
        except Exception as e:  # one person's problem must not stop the others
            print(f"  ! email failed for one user: {str(e)[:200]}")
    print(f"Emails: {sent} sent.")
