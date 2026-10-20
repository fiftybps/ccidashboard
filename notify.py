"""
notify.py — new-month email alert for the Credit Card Industry Dashboard.

Runs AFTER scraper.py in the GitHub Actions workflow. It does three things:

  1. Reads data.json and works out the latest reporting month.
  2. Compares that against .last_notified (a small file committed to the repo).
     If they match, nothing is new and it exits quietly.
  3. If the month IS new, builds a PDF summary and emails it, then records the
     month in .last_notified so the same month is never sent twice.

Deliberately standalone: it does not import from or modify scraper.py, so a
change to the scraper can't break the alert and vice versa.

All figures mirror the dashboard exactly:
  - source *_value fields are in Rs lakh; divided by 10,000 for Rs crore
  - spend = POS + Online + Others, EXCLUDING ATM cash withdrawals
  - transactions likewise exclude ATM

Environment variables (set as GitHub Secrets):
  SMTP_USER  - the Gmail address sending the mail
  SMTP_PASS  - a Gmail App Password, NOT the account password
  MAIL_TO    - recipient
"""

import json
import os
import smtplib
import ssl
import sys
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, KeepTogether)

DATA_FILE = "data.json"
MARKER_FILE = ".last_notified"
PDF_FILE = "monthly-report.pdf"
DASHBOARD_URL = "https://fiftybps.github.io/ccidashboard/"

# Brand palette, matching the dashboard's CSS variables
BLUE = colors.HexColor("#0052CC")
GREEN = colors.HexColor("#36B37E")
RED = colors.HexColor("#FF5630")
INK = colors.HexColor("#1B2436")
GREY = colors.HexColor("#5E6B82")
LINE = colors.HexColor("#E4E8F0")
PAPER = colors.HexColor("#F7F9FC")

IST = timezone(timedelta(hours=5, minutes=30))


# ----------------------------------------------------------------------------
# Number helpers — Indian digit grouping, matching the dashboard's formatting
# ----------------------------------------------------------------------------

def inr_group(n):
    """12345678 -> '1,23,45,678' (Indian grouping, not Western thousands)."""
    n = int(round(n))
    sign = "-" if n < 0 else ""
    s = str(abs(n))
    if len(s) <= 3:
        return sign + s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return sign + ",".join(parts) + "," + tail


def pct(v):
    if v is None:
        return "n/a"
    return ("+" if v >= 0 else "") + f"{v:.1f}%"


def crore(v):
    return "Rs " + inr_group(v) + " Cr"


# ----------------------------------------------------------------------------
# Aggregation — mirrors the dashboard's computeMonth logic
# ----------------------------------------------------------------------------

def compute(rows):
    """Turn raw scraper rows into per-bank computed figures, dashboard-style."""
    out = []
    for r in rows:
        pos_cr = (r.get("pos_value") or 0) / 10000
        online_cr = (r.get("online_value") or 0) / 10000
        others_cr = (r.get("others_value") or 0) / 10000
        atm_cr = (r.get("atm_value") or 0) / 10000
        # Spend EXCLUDES ATM: a cash advance is not purchase spend.
        spend = pos_cr + online_cr + others_cr
        vol = ((r.get("pos_volume") or 0) + (r.get("online_volume") or 0)
               + (r.get("others_volume") or 0))
        cards = r.get("cards_outstanding") or 0
        out.append({
            "bank": r.get("bank_name", "").strip(),
            "category": r.get("category", ""),
            "cards": cards,
            "spend": spend,
            "atm_cr": atm_cr,
            "vol": vol,
            "spend_per_card": (spend * 1e7 / cards) if cards else 0,
            "ticket": (spend * 1e7 / vol) if vol else 0,
        })
    return out


def totals(banks):
    t_cards = sum(b["cards"] for b in banks)
    t_spend = sum(b["spend"] for b in banks)
    t_vol = sum(b["vol"] for b in banks)
    return {
        "cards": t_cards,
        "spend": t_spend,
        "vol": t_vol,
        "spend_per_card": (t_spend * 1e7 / t_cards) if t_cards else 0,
        "ticket": (t_spend * 1e7 / t_vol) if t_vol else 0,
    }


def delta(now, prev):
    if not prev:
        return None
    return (now - prev) / prev * 100


# ----------------------------------------------------------------------------
# PDF
# ----------------------------------------------------------------------------

def build_pdf(month, banks, prev_banks, generated_at, path=PDF_FILE):
    body = ParagraphStyle("body", fontName="Helvetica", fontSize=9,
                          leading=13, textColor=INK)
    small = ParagraphStyle("small", fontName="Helvetica", fontSize=7.5,
                           leading=10, textColor=GREY)
    h1 = ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=20,
                        leading=24, textColor=INK, spaceAfter=2)
    h2 = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11,
                        leading=14, textColor=BLUE, spaceBefore=12, spaceAfter=5)
    eyebrow = ParagraphStyle("eyebrow", fontName="Helvetica-Bold", fontSize=8,
                             leading=11, textColor=BLUE)

    doc = SimpleDocTemplate(path, pagesize=A4,
                            leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=15 * mm, bottomMargin=15 * mm,
                            title=f"Credit Card Industry Report - {month}",
                            author="Numan Ahmad")
    el = []

    el.append(Paragraph("RBI CREDIT CARD STATISTICS", eyebrow))
    el.append(Paragraph(f"Industry Report &mdash; {month}", h1))
    el.append(Paragraph(
        f"Generated {generated_at} &nbsp;|&nbsp; Source: RBI monthly bank-wise "
        f"card statistics &nbsp;|&nbsp; {DASHBOARD_URL}", small))
    el.append(Spacer(1, 8))

    now, prev = totals(banks), totals(prev_banks) if prev_banks else None

    # ---- headline KPIs -----------------------------------------------------
    def kpi_cell(label, value, d):
        if d is None:
            sub = "no prior month"
            col = GREY
        else:
            sub = f"{pct(d)} vs prior month"
            col = GREEN if d >= 0 else RED
        return [Paragraph(f'<font size=7 color="#5E6B82"><b>{label}</b></font>', small),
                Paragraph(f'<font size=13 color="#1B2436"><b>{value}</b></font>', body),
                Paragraph(f'<font size=7.5 color="#{col.hexval()[2:]}"><b>{sub}</b></font>', small)]

    kpis = [
        ("CARDS OUTSTANDING", inr_group(now["cards"]),
         delta(now["cards"], prev["cards"]) if prev else None),
        ("INDUSTRY SPEND (EXCL. ATM)", crore(now["spend"]),
         delta(now["spend"], prev["spend"]) if prev else None),
        ("TRANSACTIONS (EXCL. ATM)", inr_group(now["vol"]),
         delta(now["vol"], prev["vol"]) if prev else None),
        ("AVG TICKET (EXCL. ATM)", "Rs " + inr_group(now["ticket"]),
         delta(now["ticket"], prev["ticket"]) if prev else None),
    ]
    cells = [kpi_cell(*k) for k in kpis]
    kpi_table = Table([[c[0] for c in cells], [c[1] for c in cells],
                       [c[2] for c in cells]], colWidths=[44 * mm] * 4)
    kpi_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PAPER),
        ("BOX", (0, 0), (-1, -1), 0.5, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    el.append(kpi_table)

    # ---- leaderboard -------------------------------------------------------
    ranked = sorted(banks, key=lambda b: b["cards"], reverse=True)
    prev_by_name = {b["bank"]: b for b in prev_banks} if prev_banks else {}
    t_cards = now["cards"] or 1

    el.append(Paragraph("Top 15 issuers by cards outstanding", h2))
    head = ["#", "Bank", "Cards", "Share", "Net adds", "Spend (Cr)", "MoM"]
    rows = [head]
    for i, b in enumerate(ranked[:15], 1):
        p = prev_by_name.get(b["bank"])
        net = b["cards"] - p["cards"] if p else None
        mom = delta(b["cards"], p["cards"]) if p else None
        rows.append([
            str(i),
            b["bank"][:34],
            inr_group(b["cards"]),
            f'{b["cards"] / t_cards * 100:.1f}%',
            ("+" if net and net >= 0 else "") + inr_group(net) if net is not None else "n/a",
            inr_group(b["spend"]),
            pct(mom),
        ])
    tbl = Table(rows, colWidths=[8 * mm, 52 * mm, 26 * mm, 16 * mm, 24 * mm, 24 * mm, 18 * mm],
                repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), BLUE),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.4, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PAPER]),
    ]
    for r in range(1, len(rows)):
        if rows[r][6] != "n/a":
            style.append(("TEXTCOLOR", (6, r), (6, r),
                          GREEN if rows[r][6].startswith("+") else RED))
    tbl.setStyle(TableStyle(style))
    el.append(tbl)

    # ---- movers ------------------------------------------------------------
    if prev_by_name:
        moves = []
        for b in banks:
            p = prev_by_name.get(b["bank"])
            if p and p["cards"] > 0:
                moves.append((b["bank"], b["cards"] - p["cards"],
                              (b["cards"] - p["cards"]) / p["cards"] * 100))
        gain = sorted(moves, key=lambda x: x[1], reverse=True)[:5]
        lose = [m for m in sorted(moves, key=lambda x: x[1]) if m[1] < 0][:5]

        def mover_table(title, data, positive):
            r = [[title, "Net adds", "MoM"]]
            for name, net, p in data:
                r.append([name[:28], ("+" if net >= 0 else "") + inr_group(net), pct(p)])
            if len(r) == 1:
                r.append(["None this month", "", ""])
            t = Table(r, colWidths=[46 * mm, 24 * mm, 18 * mm], repeatRows=1)
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), GREEN if positive else RED),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("GRID", (0, 0), (-1, -1), 0.4, LINE),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            return t

        el.append(Paragraph("Biggest movers by net card additions", h2))
        side = Table([[mover_table("Largest gains", gain, True),
                       mover_table("Largest declines", lose, False)]],
                     colWidths=[90 * mm, 90 * mm])
        side.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                  ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                  ("RIGHTPADDING", (0, 0), (0, 0), 6)]))
        el.append(side)

    # ---- methodology -------------------------------------------------------
    el.append(Spacer(1, 10))
    el.append(KeepTogether([
        Paragraph("Methodology", h2),
        Paragraph(
            "Spend is POS + Online/e-com + Others. It <b>excludes ATM cash "
            "withdrawals</b>, which are a cash advance rather than purchase spend "
            "and are reported separately. Transactions exclude ATM transactions on "
            "the same basis. Average ticket is spend divided by transactions, both "
            "excluding ATM. Credit card figures only; debit card columns in the "
            "source file are excluded. Figures are bank-reported to RBI and "
            "republished without adjustment.", small),
        Spacer(1, 4),
        Paragraph(
            f"Full interactive dashboard: {DASHBOARD_URL} &nbsp;|&nbsp; "
            "Built by Numan Ahmad. Informational only, not financial advice.", small),
    ]))

    doc.build(el)
    return path


# ----------------------------------------------------------------------------
# Email
# ----------------------------------------------------------------------------

def send_email(month, pdf_path, now, prev):
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    to = os.environ.get("MAIL_TO")
    if not (user and password and to):
        print("SKIP: SMTP_USER / SMTP_PASS / MAIL_TO not all set.")
        return False

    d_cards = delta(now["cards"], prev["cards"]) if prev else None
    d_spend = delta(now["spend"], prev["spend"]) if prev else None

    msg = EmailMessage()
    msg["Subject"] = f"RBI credit card data for {month} is live"
    msg["From"] = user
    msg["To"] = to
    msg.set_content(
        f"New RBI credit card data has been published and the dashboard has "
        f"already updated.\n\n"
        f"{month}\n"
        f"  Cards outstanding : {inr_group(now['cards'])}  ({pct(d_cards)})\n"
        f"  Industry spend    : {crore(now['spend'])}  ({pct(d_spend)})\n"
        f"  Transactions      : {inr_group(now['vol'])}\n"
        f"  Avg ticket        : Rs {inr_group(now['ticket'])}\n\n"
        f"Spend and transactions exclude ATM cash withdrawals.\n\n"
        f"Full report attached. Dashboard: {DASHBOARD_URL}\n"
    )
    with open(pdf_path, "rb") as f:
        msg.add_attachment(f.read(), maintype="application", subtype="pdf",
                           filename=f"RBI-Credit-Card-Report-{month.replace(' ', '-')}.pdf")

    # A mail failure must not lose the month: the caller leaves the marker
    # untouched on False, so the next scheduled run retries automatically.
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
            s.login(user, password)
            s.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        print("ERROR: Gmail rejected the login. SMTP_PASS must be a 16-character "
              "App Password (not the account password), and 2-Step Verification "
              "must be on for that account.")
        return False
    except Exception as exc:
        print(f"ERROR: could not send mail ({type(exc).__name__}: {exc})")
        return False

    print(f"Email sent to {to}")
    return True


# ----------------------------------------------------------------------------

def main():
    if not os.path.exists(DATA_FILE):
        print(f"No {DATA_FILE}; nothing to do.")
        return 0

    with open(DATA_FILE) as f:
        data = json.load(f)

    months = data.get("months") or []
    if not months:
        print("data.json has no months; nothing to do.")
        return 0

    latest = months[0]
    previous = months[1] if len(months) > 1 else None

    last_sent = ""
    if os.path.exists(MARKER_FILE):
        last_sent = open(MARKER_FILE).read().strip()

    if last_sent == latest:
        print(f"Latest month is {latest}, already notified. Nothing to send.")
        return 0

    # First ever run: record the month without emailing, so enabling this
    # doesn't fire a mail about data that was already there.
    if not last_sent:
        with open(MARKER_FILE, "w") as f:
            f.write(latest)
        print(f"First run - baseline set to {latest}. No email sent.")
        return 0

    print(f"New month detected: {latest} (was {last_sent})")

    banks = compute(data["history"][latest])
    prev_banks = compute(data["history"][previous]) if previous else []
    gen = data.get("generated_at")
    try:
        stamp = datetime.fromisoformat(gen.replace("Z", "+00:00")).astimezone(IST) \
            .strftime("%d %b %Y, %I:%M %p IST")
    except Exception:
        stamp = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")

    build_pdf(latest, banks, prev_banks, stamp)
    print(f"Built {PDF_FILE}")

    sent = send_email(latest, PDF_FILE, totals(banks),
                      totals(prev_banks) if prev_banks else None)

    # Only advance the marker once the mail actually went out, so a transient
    # SMTP failure retries on the next run instead of silently skipping a month.
    if sent:
        with open(MARKER_FILE, "w") as f:
            f.write(latest)
    else:
        print("Marker not advanced; will retry next run.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
