"""Core funnel logic: pure Python, no Flask dependency, nothing is stored."""
import csv
import io
import re
from datetime import date, timedelta
from urllib.parse import quote

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 50000

EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
EMAIL_HEADERS = ("email", "e-mail", "email address", "email_address", "emailaddress",
                 "mail", "user_email", "user email", "customer_email", "customer email")
TRUE_VALUES = {"true", "yes", "y", "1", "done", "complete", "completed", "activated",
               "active", "finished", "x"}
NOT_PAYING = {"failed", "refunded", "canceled", "cancelled", "unpaid", "incomplete",
              "incomplete_expired", "past_due", "void", "expired", "disputed", "inactive",
              "ended", "churned"}


class InputError(Exception):
    """Raised when the uploaded data is not usable; message is user-facing."""


def plural(n, singular, plural_form=None):
    word = singular if n == 1 else (plural_form or singular + "s")
    return f"{n:,} {word}"


def decode_bytes(raw, label):
    """Turn uploaded bytes into text. Returns (text, warnings)."""
    warnings = []
    if not raw or not raw.strip():
        raise InputError(f"The {label} file is empty.")
    if len(raw) > MAX_BYTES:
        cut = raw[:MAX_BYTES]
        nl = cut.rfind(b"\n")
        raw = cut[:nl] if nl > 0 else cut
        warnings.append(f"The {label} file is larger than {MAX_BYTES // (1024 * 1024)} MB, so only the first part was analyzed.")
    head = raw[:4096]
    if b"\x00" in head or head.startswith((b"PK\x03\x04", b"%PDF", b"\x89PNG", b"\xff\xd8", b"\xd0\xcf\x11\xe0")):
        raise InputError(f"The {label} file does not look like text (it seems to be a binary file such as Excel, PDF or an image). Please export it as CSV.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("cp1252")
        except UnicodeDecodeError:
            raise InputError(f"The {label} file does not look like text. Please export it as CSV.")
    sample = text[:4096]
    bad = sum(1 for c in sample if ord(c) < 32 and c not in "\r\n\t")
    if sample and bad / len(sample) > 0.02:
        raise InputError(f"The {label} file does not look like text. Please export it as CSV.")
    return text, warnings


def _find(headers, exact=(), contains=()):
    for h in headers:
        if h in exact:
            return h
    for h in headers:
        if any(c in h for c in contains):
            return h
    return None


def parse_table(text, label):
    """Parse CSV text -> (headers, rows(list of dict), email_col, warnings)."""
    warnings = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise InputError(f"The {label} data is empty.")
    first = lines[0]
    delim = max(",;\t|", key=lambda d: first.count(d))
    if first.count(delim) == 0:
        delim = ","
    try:
        reader = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delim))
    except csv.Error:
        raise InputError(f"The {label} data could not be read as CSV.")
    if not reader:
        raise InputError(f"The {label} data is empty.")
    headers = [h.strip().lower() for h in reader[0]]
    body = reader[1:]
    email_col = _find(headers, EMAIL_HEADERS, ("mail",))
    if email_col is None:
        if len(reader[0]) == 1 and EMAIL_RE.match(reader[0][0].strip()):
            headers = ["email"]
            body = reader
            email_col = "email"
        else:
            raise InputError(f"The {label} data has no email column. The first row must be a header containing a column named \"email\".")
    if len(body) > MAX_ROWS:
        body = body[:MAX_ROWS]
        warnings.append(f"The {label} data has more than {MAX_ROWS:,} rows; only the first {MAX_ROWS:,} were analyzed.")
    rows = []
    for r in body:
        d = {}
        for i, h in enumerate(headers):
            d[h] = r[i].strip() if i < len(r) else ""
        rows.append(d)
    return headers, rows, email_col, warnings


def _safe_cell(v):
    v = v or ""
    if v and v[0] in "=+-@":
        return "'" + v
    return v


def _pct(a, b):
    return round(a * 100.0 / b, 1) if b else 0.0


def analyze(signup_text, payment_text):
    """Match signups with payments by email and compute the funnel."""
    warnings = []
    s_headers, s_rows, s_email, w = parse_table(signup_text, "signups")
    warnings += w
    p_headers, p_rows, p_email, w = parse_table(payment_text, "payments")
    warnings += w

    name_col = _find(s_headers, ("name", "full_name", "full name", "first_name"), ("name",))
    date_col = _find(s_headers, ("signup_date", "created_at", "signed_up_at", "date"), ("signup", "signed", "created", "date"))
    onb_col = _find(s_headers, ("onboarding_completed", "onboarded", "activated"), ("onboard", "activat", "setup"))
    status_col = _find(p_headers, ("status", "subscription_status", "payment_status", "state"))

    signups = {}
    invalid_s = 0
    dup_s = 0
    for r in s_rows:
        e = r.get(s_email, "").strip().lower()
        if not EMAIL_RE.match(e):
            invalid_s += 1
            continue
        if e in signups:
            dup_s += 1
            if onb_col and r.get(onb_col, "").lower() in TRUE_VALUES:
                signups[e]["onboarded"] = True
            continue
        signups[e] = {
            "email": e,
            "name": r.get(name_col, "") if name_col else "",
            "date": r.get(date_col, "") if date_col else "",
            "onboarded": bool(onb_col and r.get(onb_col, "").lower() in TRUE_VALUES),
        }
    if not signups:
        raise InputError("No valid email addresses were found in the signups data. Check that the email column contains real addresses.")
    if invalid_s:
        warnings.append(f"{plural(invalid_s, 'signup row')} had no valid email and {'was' if invalid_s == 1 else 'were'} skipped.")
    if dup_s:
        warnings.append(f"{plural(dup_s, 'duplicate signup row')} {'was' if dup_s == 1 else 'were'} merged by email.")

    paid = set()
    invalid_p = 0
    excluded = 0
    for r in p_rows:
        e = r.get(p_email, "").strip().lower()
        if not EMAIL_RE.match(e):
            invalid_p += 1
            continue
        if status_col and r.get(status_col, "").strip().lower().replace(" ", "_") in NOT_PAYING:
            excluded += 1
            continue
        paid.add(e)
    if not paid:
        raise InputError("No paying customers were found in the payments data (no valid emails, or every row has a non-paying status).")
    if invalid_p:
        warnings.append(f"{plural(invalid_p, 'payment row')} had no valid email and {'was' if invalid_p == 1 else 'were'} skipped.")
    if excluded:
        warnings.append(f"{plural(excluded, 'payment row')} with a failed, refunded or canceled status {'was' if excluded == 1 else 'were'} not counted as paid.")

    total = len(signups)
    has_onb = bool(onb_col)
    unmatched_paid = sorted(e for e in paid if e not in signups)
    if unmatched_paid:
        warnings.append(f"{plural(len(unmatched_paid), 'paying email')} did not match any signup and {'is' if len(unmatched_paid) == 1 else 'are'} excluded from the funnel (check for different email addresses).")

    paid_signups = [u for e, u in signups.items() if e in paid]
    stages = [{"label": "Signed up", "count": total}]
    unconverted = []
    note = ""
    if has_onb:
        onboarded = [u for u in signups.values() if u["onboarded"]]
        paid_onb = [u for u in onboarded if u["email"] in paid]
        paid_not_onb = len(paid_signups) - len(paid_onb)
        stages.append({"label": "Completed onboarding", "count": len(onboarded)})
        stages.append({"label": "Paid subscription", "count": len(paid_onb)})
        if paid_not_onb:
            note = f"{plural(paid_not_onb, 'paying user')} did not complete onboarding, so {'is' if paid_not_onb == 1 else 'are'} not counted in the last funnel stage but {'is' if paid_not_onb == 1 else 'are'} included in the overall conversion."
        for u in signups.values():
            if u["email"] in paid:
                continue
            seg = "Onboarded, not paid" if u["onboarded"] else "Did not complete onboarding"
            unconverted.append(dict(u, segment=seg))
        unconverted.sort(key=lambda x: (x["segment"] != "Onboarded, not paid", x["email"]))
    else:
        stages.append({"label": "Paid subscription", "count": len(paid_signups)})
        note = "No onboarding column was found in the signups data (e.g. \"onboarding_completed\"), so the funnel has two stages."
        for u in signups.values():
            if u["email"] not in paid:
                unconverted.append(dict(u, segment="Signed up, not paid"))
        unconverted.sort(key=lambda x: x["email"])

    prev = None
    for st in stages:
        st["pct_total"] = _pct(st["count"], total)
        if prev is None:
            st["drop_pct"] = None
            st["lost"] = None
        else:
            st["lost"] = max(prev - st["count"], 0)
            st["drop_pct"] = _pct(st["lost"], prev)
        prev = st["count"]

    for u in unconverted:
        u["name_disp"] = u["name"] or "—"
        u["date_disp"] = u["date"] or "—"

    buf = io.StringIO()
    wr = csv.writer(buf)
    wr.writerow(["email", "name", "signup_date", "segment"])
    for u in unconverted:
        wr.writerow([_safe_cell(u["email"]), _safe_cell(u["name"]), _safe_cell(u["date"]), u["segment"]])
    download = "data:text/csv;charset=utf-8," + quote(buf.getvalue())

    shown_limit = 500
    return {
        "total": total,
        "paid_total": len(paid_signups),
        "overall_pct": _pct(len(paid_signups), total),
        "stages": stages,
        "has_onboarding": has_onb,
        "note": note,
        "unconverted": unconverted[:shown_limit],
        "unconverted_count": len(unconverted),
        "hidden_count": max(len(unconverted) - shown_limit, 0),
        "download": download,
        "warnings": warnings,
    }


def demo_data():
    """Embedded demo CSVs with dates relative to today."""
    today = date.today()
    first_names = ["Ava", "Liam", "Noah", "Emma", "Mia", "Lucas", "Zoe", "Ethan", "Ivy", "Leo",
                   "Nora", "Owen"]
    s_lines = ["email,name,signup_date,onboarding_completed"]
    p_lines = ["email,plan,status,amount"]
    n = 36
    for i in range(1, n + 1):
        email = f"user{i}@example.com"
        name = f"{first_names[i % len(first_names)]} Demo"
        d = (today - timedelta(days=i * 2 + 1)).isoformat()
        onboarded = i % 3 != 0
        s_lines.append(f"{email},{name},{d},{'true' if onboarded else 'false'}")
        if onboarded and i % 4 == 1:
            p_lines.append(f"{email},Pro,active,29")
    return "\n".join(s_lines), "\n".join(p_lines)
