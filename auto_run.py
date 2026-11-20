# auto_run.py – täglicher Automatik-Lauf des Ahoi-Comic-Vorarbeiters
"""Läuft jeden Morgen um 5:00 Uhr (GitHub Actions):

1. holt alle Comic-Bestellungen der letzten 24 h aus Shopify,
2. liest alle Mails der letzten 24 h aus dem Postfach (IMAP),
3. ordnet Mails den Bestellungen zu (Bestellnummer im Text oder Absender-Adresse),
4. erkennt Comic-Anfragen, die nur per Mail kamen,
5. analysiert alles wie die Vorarbeiter-App und
6. schickt pro Bestellung/Anfrage die Vorarbeiter-Datei (ZIP) plus eine Tagesübersicht ins Postfach.

Wichtig: Das Repo ist öffentlich – die Logs von GitHub Actions auch. Deshalb werden hier keine
Kundendaten ausgegeben, nur Zahlen.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import email
import email.policy
import imaplib
import os
import re
import smtplib
import sys
import time
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import vorarbeiter_core as core

BERLIN = ZoneInfo("Europe/Berlin")
MARKER_HEADER = "X-Ahoi-Vorarbeiter"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
SKIP_SENDER_PARTS = ("shopify.com", "noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster", "klaviyo", "notifications@")
MIN_MAIL_IMAGE_SIDE = 300  # kleinere Bilder in Mails sind meist Logos/Signaturen


def log(msg: str) -> None:
    """Nur Zahlen/Status ausgeben – keine Kundendaten (öffentliche Logs)."""
    print(f"[{dt.datetime.now(BERLIN):%H:%M:%S}] {msg}", flush=True)


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


# ---------------------------------------------------------------------------
# Zeitsteuerung: GitHub startet um 03:00 und 04:00 UTC – nur der Lauf um 5 Uhr Berlin zählt
# ---------------------------------------------------------------------------


def should_run(now_utc: dt.datetime, schedule: str, force: bool) -> bool:
    if force:
        return True
    if not schedule:
        return True
    hour_utc = 4 if now_utc.astimezone(BERLIN).utcoffset() == dt.timedelta(hours=1) else 3
    return schedule.strip() == f"0 {hour_utc} * * *"


# ---------------------------------------------------------------------------
# Shopify
# ---------------------------------------------------------------------------

ORDER_FIELDS_MAIL = core.ShopifyClient.ORDER_FIELDS.replace("      createdAt\n", "      createdAt\n      email\n", 1)


def shopify_orders_since(shop: core.ShopifyClient, since: dt.datetime) -> Tuple[List[Dict[str, Any]], bool]:
    """Alle Bestellungen seit since. Zweiter Wert: ob die Kunden-E-Mail mitgeliefert wurde."""
    q = f"created_at:>={since.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"
    for fields, with_mail in ((ORDER_FIELDS_MAIL, True), (core.ShopifyClient.ORDER_FIELDS, False)):
        try:
            data = shop.graphql(
                "query($q:String){ orders(first:100, sortKey:CREATED_AT, query:$q){ nodes {" + fields + "} } }", {"q": q}
            )
            return shop.add_customer_info(((data.get("orders") or {}).get("nodes")) or []), with_mail
        except Exception as e:
            if not with_mail:
                raise
            log(f"Shopify: E-Mail-Feld nicht freigegeben, Zuordnung nur über Bestellnummer ({type(e).__name__}).")
    return [], False


def shopify_order_by_email(shop: core.ShopifyClient, addr: str) -> Optional[Dict[str, Any]]:
    try:
        data = shop.graphql(
            "query($q:String){ orders(first:1, reverse:true, sortKey:CREATED_AT, query:$q){ nodes {" + ORDER_FIELDS_MAIL + "} } }",
            {"q": f"email:{addr}"},
        )
        nodes = ((data.get("orders") or {}).get("nodes")) or []
        return shop.add_customer_info(nodes[:1])[0] if nodes else None
    except Exception:
        return None


def is_comic_order(order: Dict[str, Any]) -> bool:
    items = (order.get("lineItems") or {}).get("nodes") or []
    return any(core._is_comic_item(it) for it in items)


# ---------------------------------------------------------------------------
# Postfach (IMAP lesen, SMTP senden)
# ---------------------------------------------------------------------------


class Mail:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.msg = email.message_from_bytes(raw, policy=email.policy.default)
        self.subject = str(self.msg.get("Subject", "") or "")
        self.sender = parseaddr(str(self.msg.get("From", "") or ""))[1].lower()
        try:
            self.date = parsedate_to_datetime(str(self.msg.get("Date")))
            if self.date.tzinfo is None:
                self.date = self.date.replace(tzinfo=dt.timezone.utc)
        except Exception:
            self.date = None
        self._texts: Optional[List[core.TextSource]] = None
        self._images: Optional[List[Tuple[str, bytes]]] = None

    def parts(self) -> Tuple[List[core.TextSource], List[Tuple[str, bytes]]]:
        if self._texts is None:
            try:
                texts, images = core.extract_file("mail.eml", self.raw)
            except Exception:
                texts, images = [], []
            label = f"Mail vom {self.date.astimezone(BERLIN):%d.%m. %H:%M}" if self.date else "Mail"
            self._texts = [core.TextSource(f"{label}: {t.quelle}", t.text) for t in texts]
            self._images = [(f"{label}: {h}", b) for h, b in images]
        return self._texts, self._images or []

    def body_text(self) -> str:
        texts, _ = self.parts()
        return "\n\n".join(t.text for t in texts)

    def is_own_or_system(self, own_addr: str) -> bool:
        if self.msg.get(MARKER_HEADER):
            return True
        if self.subject.lower().startswith("vorarbeiter"):
            return True
        if self.sender == own_addr.lower():
            return True
        if self.msg.get("List-Unsubscribe"):
            return True  # Newsletter / Marketing
        return any(p in self.sender for p in SKIP_SENDER_PARTS)


def fetch_mails_since(host: str, user: str, password: str, since: dt.datetime, folder: str = "INBOX") -> List[Mail]:
    day = since - dt.timedelta(days=1)
    criteria = f"(SINCE {day.day:02d}-{MONTHS[day.month - 1]}-{day.year})"
    box = imaplib.IMAP4_SSL(host, 993)
    try:
        box.login(user, password)
        box.select(folder, readonly=True)  # nur lesen – Mails bleiben ungelesen
        typ, data = box.search(None, criteria)
        ids = (data[0] or b"").split() if typ == "OK" else []
        mails: List[Mail] = []
        for mid in ids:
            typ, msg_data = box.fetch(mid, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            m = Mail(msg_data[0][1])
            if m.date and m.date >= since:
                mails.append(m)
        return mails
    finally:
        try:
            box.logout()
        except Exception:
            pass


def send_mail(host: str, user: str, password: str, to: str, subject: str, body: str, attachments: List[Tuple[str, bytes]]) -> None:
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = subject
    msg[MARKER_HEADER] = "1"
    msg.set_content(body)
    for name, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="zip", filename=name)
    last: Optional[Exception] = None
    for attempt in range(3):
        try:
            with smtplib.SMTP_SSL(host, 465, timeout=120) as s:
                s.login(user, password)
                s.send_message(msg)
            return
        except Exception as e:
            last = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Mailversand fehlgeschlagen: {type(last).__name__}")


# ---------------------------------------------------------------------------
# Zuordnung Mail -> Bestellung, Erkennung von Comic-Anfragen
# ---------------------------------------------------------------------------

ORDER_REF_RE = re.compile(r"(?:#|bestell(?:ung|nummer|nr)?\.?\s*:?\s*#?|order\s*#?)\s*(\d{4,6})", re.IGNORECASE)


def order_numbers_in(text: str) -> List[str]:
    return list(dict.fromkeys(m.group(1) for m in ORDER_REF_RE.finditer(text or "")))


CLASSIFY_PROMPT = """Du arbeitest fuer Ahoi Comic (personalisierte Comics). Pruefe diese E-Mail.
Ist sie eine Comic-Bestellung oder Comic-Anfrage bzw. liefert sie Infos/Fotos fuer einen personalisierten Comic
(Personen, Story-Wuensche, Anlass, Fotos)? Werbung, Rechnungen, Versandinfos, Lieferanten- oder Systemmails sind KEINE.
Antworte NUR mit JSON: {"comic": true|false, "bestellnummer": "Nummer falls genannt, sonst leer", "grund": "kurz"}

BETREFF: {subject}
ANHAENGE: {n_images} Bild(er)
TEXT:
{text}"""


def classify_mail(ai: core.AI, mail: Mail) -> Dict[str, Any]:
    _, images = mail.parts()
    prompt = (
        CLASSIFY_PROMPT.replace("{subject}", mail.subject[:300])
        .replace("{n_images}", str(len(images)))
        .replace("{text}", mail.body_text()[:6000])
    )
    try:
        return ai.ask_json(ai.photo_model, [{"type": "text", "text": prompt}])
    except Exception:
        return {"comic": False, "grund": "KI nicht erreichbar"}


# ---------------------------------------------------------------------------
# Verarbeitung eines Auftrags (wie „Vorarbeit starten“ in der App)
# ---------------------------------------------------------------------------


def run_pipeline(
    ai: core.AI, order_raw: Optional[Dict[str, Any]], mails: List[Mail], fallback_number: str = ""
) -> Tuple[str, bytes, List[core.Meldung], Dict[str, Any]]:
    order = core.extract_order(order_raw) if order_raw else None
    photos: List[core.Photo] = []
    counter = [0]

    def next_id() -> str:
        counter[0] += 1
        return f"P{counter[0]}"

    if order and order["uploads"]:
        jobs = [(next_id(), f"Upload {n}", url) for n, url in order["uploads"]]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            photos.extend(ex.map(lambda j: core.download_photo(*j), jobs))

    texts: List[core.TextSource] = []
    for m in mails:
        t, imgs = m.parts()
        texts.extend(t)
        for herkunft, b in imgs:
            p = core.make_photo(next_id(), herkunft, b)
            if p.jpeg and min(p.original_breite, p.original_hoehe) < MIN_MAIL_IMAGE_SIDE:
                counter[0] -= 1  # Logo/Signatur – ignorieren
                continue
            photos.append(p)

    if any(p.jpeg for p in photos):
        core.analyse_photos(ai, photos)
    all_texts = core.collect_texts(order, texts, photos)
    story = core.analyse_story(ai, order, all_texts, photos)
    result = core.build_result(order, story, photos)
    if not result.get("bestellung"):
        result["bestellung"] = fallback_number
    meldungen = core.check_result(result, order, photos)
    if mails:
        meldungen.append(core.Meldung("hinweis", f"{len(mails)} Mail(s) des Kunden wurden mit ausgewertet."))
    if not order:
        meldungen.insert(0, core.Meldung("warnung", "Anfrage nur per Mail – keine Shopify-Bestellung gefunden."))
    fname, zbytes = core.build_zip(result, photos, meldungen, order, all_texts, [m.subject or "Mail" for m in mails])
    return fname, zbytes, meldungen, result


def mail_body_for(result: Dict[str, Any], meldungen: List[core.Meldung], source: str) -> str:
    lines = [
        f"Vorarbeiter-Datei für {source}",
        f"Titel: {result.get('titel', '')}   Genre: {result.get('genre', '')}   Seiten: {result.get('seiten', '')}",
        "",
        "MELDUNGEN",
    ]
    if not meldungen:
        lines.append("Keine – alles vollständig.")
    for m in meldungen:
        lines.append(f"[{m.stufe.upper()}] {m.text}")
    lines += ["", "FIGUREN"]
    for p in result.get("personen", []):
        if p.get("aktiv", True):
            lines.append(f"- {p.get('name')} ({p.get('rolle')}, {p.get('art')}) – Foto: {p.get('foto_id') or 'keins'}")
    lines += [
        "",
        "Die ZIP-Datei im Anhang in der Comic-App unter „Vorarbeiter-Datei hochladen“ einlesen",
        "oder vorher im Vorarbeiter prüfen: https://ahoicomic-vorarbeiter.streamlit.app",
    ]
    return "\n".join(lines)


def count(meldungen: List[core.Meldung], stufe: str) -> int:
    return sum(1 for m in meldungen if m.stufe == stufe)


# ---------------------------------------------------------------------------
# Hauptablauf
# ---------------------------------------------------------------------------


def main() -> int:
    now = dt.datetime.now(dt.timezone.utc)
    force = env("FORCE_RUN") in ("1", "true", "yes")
    if not should_run(now, env("RUN_SCHEDULE"), force):
        log("Nicht 5 Uhr in Berlin – dieser Start wird übersprungen.")
        return 0

    hours = float(env("LOOKBACK_HOURS", "24") or 24)
    since = now - dt.timedelta(hours=hours)
    mail_user = env("MAIL_USER", "moin@ahoicomic.de")
    mail_pw = env("MAIL_PASSWORD")
    mail_to = env("MAIL_TO", mail_user)
    imap_host = env("IMAP_HOST", "imap.ionos.de")
    smtp_host = env("SMTP_HOST", "smtp.ionos.de")
    if not env("OPENROUTER_API_KEY") or not mail_pw:
        log("OPENROUTER_API_KEY oder MAIL_PASSWORD fehlt – Abbruch.")
        return 1

    ai = core.AI(env("OPENROUTER_API_KEY"), env("PHOTO_MODEL", core.DEFAULT_PHOTO_MODEL), env("STORY_MODEL", core.DEFAULT_STORY_MODEL))
    shop = core.ShopifyClient(
        env("SHOPIFY_SHOP", "vavedi-e6"), env("SHOPIFY_CLIENT_ID"), env("SHOPIFY_CLIENT_SECRET"), env("SHOPIFY_API_VERSION", core.DEFAULT_SHOPIFY_API_VERSION)
    )
    problems: List[str] = []

    # 1) Shopify
    orders: List[Dict[str, Any]] = []
    with_mail = False
    if shop.configured():
        try:
            orders, with_mail = shopify_orders_since(shop, since)
            orders = [o for o in orders if is_comic_order(o)]
        except Exception as e:
            problems.append(f"Shopify nicht erreichbar: {type(e).__name__}")
    else:
        problems.append("Shopify-Zugang fehlt (SHOPIFY_CLIENT_ID/SECRET).")
    log(f"Shopify: {len(orders)} neue Comic-Bestellung(en).")

    # 2) Postfach
    mails: List[Mail] = []
    try:
        mails = [m for m in fetch_mails_since(imap_host, mail_user, mail_pw, since) if not m.is_own_or_system(mail_user)]
    except Exception as e:
        problems.append(f"Postfach nicht lesbar: {type(e).__name__}")
    log(f"Postfach: {len(mails)} relevante Mail(s).")

    # 3) Aufträge bilden
    jobs: Dict[str, Dict[str, Any]] = {}
    for o in orders:
        num = core.normalize_order_number(o.get("name"))
        jobs[num] = {"order": o, "mails": [], "quelle": f"Bestellung {o.get('name')}"}
    by_email = {str(o.get("email") or "").lower(): core.normalize_order_number(o.get("name")) for o in orders if o.get("email")}

    mail_only: List[Mail] = []
    ignoriert = 0
    for m in mails:
        nums = order_numbers_in(m.subject + "\n" + m.body_text())
        target = next((n for n in nums if n in jobs), None)
        if not target:
            for n in nums:  # Mail zu einer älteren Bestellung
                try:
                    o = shop.get_order(n) if shop.configured() else None
                except Exception:
                    o = None
                if o and is_comic_order(o):
                    target = core.normalize_order_number(o.get("name"))
                    jobs.setdefault(target, {"order": o, "mails": [], "quelle": f"Bestellung {o.get('name')} (Nachtrag per Mail)"})
                    break
        if not target and m.sender in by_email:
            target = by_email[m.sender]
        if not target and with_mail and shop.configured():
            o = shopify_order_by_email(shop, m.sender)
            if o and is_comic_order(o):
                target = core.normalize_order_number(o.get("name"))
                jobs.setdefault(target, {"order": o, "mails": [], "quelle": f"Bestellung {o.get('name')} (Nachtrag per Mail)"})
        if target:
            jobs[target]["mails"].append(m)
            continue
        verdict = classify_mail(ai, m)
        if verdict.get("comic"):
            mail_only.append(m)
        else:
            ignoriert += 1

    # Mail-Anfragen ohne Bestellung: pro Absender zusammenfassen
    by_sender: Dict[str, List[Mail]] = {}
    for m in mail_only:
        by_sender.setdefault(m.sender or "unbekannt", []).append(m)
    for i, (sender, ms) in enumerate(by_sender.items(), start=1):
        jobs[f"MAIL-{i}"] = {"order": None, "mails": ms, "quelle": f"Mail-Anfrage von {sender}", "sender": sender}
    log(f"Aufträge: {len(jobs)} (davon {len(by_sender)} nur per Mail), {ignoriert} Mail(s) ohne Comic-Bezug.")

    # 4) verarbeiten und verschicken
    summary: List[str] = []
    for key, job in jobs.items():
        try:
            fallback = key if key.startswith("MAIL-") else ""
            if job.get("sender"):
                fallback = f"MAIL-{re.sub(r'[^a-z0-9]+', '-', job['sender'].split('@')[0])[:20]}-{now.astimezone(BERLIN):%Y%m%d}"
            fname, zbytes, meldungen, result = run_pipeline(ai, job["order"], job["mails"], fallback)
            e_, w_ = count(meldungen, "fehler"), count(meldungen, "warnung")
            label = f"#{key}" if not key.startswith("MAIL-") else "Mail-Anfrage"
            subject = f"Vorarbeiter {label} – {e_} Fehler, {w_} Warnung{'en' if w_ != 1 else ''}"
            if job.get("sender"):
                subject += f" ({job['sender']})"
            send_mail(smtp_host, mail_user, mail_pw, mail_to, subject, mail_body_for(result, meldungen, job["quelle"]), [(fname, zbytes)])
            summary.append(f"✓ {job['quelle']}: {e_} Fehler, {w_} Warnungen – {fname}")
            log(f"Auftrag verarbeitet: {e_} Fehler, {w_} Warnungen, {len(zbytes) // 1024} KB.")
        except Exception as e:
            summary.append(f"✗ {job['quelle']}: nicht verarbeitet ({type(e).__name__}: {str(e)[:150]})")
            problems.append(f"{job['quelle']}: {type(e).__name__}")
            log(f"Auftrag fehlgeschlagen: {type(e).__name__}")

    # 5) Tagesübersicht
    body = [
        f"Vorarbeiter – Tagesübersicht {now.astimezone(BERLIN):%d.%m.%Y %H:%M}",
        f"Zeitraum: letzte {hours:g} Stunden",
        "",
        f"Neue Shopify-Comic-Bestellungen: {len(orders)}",
        f"Relevante Mails im Postfach: {len(mails)} (davon {len(mail_only)} Comic-Anfragen ohne Bestellung, {ignoriert} ohne Comic-Bezug)",
        "",
        "AUFTRÄGE",
    ] + (summary or ["Keine neuen Aufträge."])
    if not with_mail and orders:
        body += ["", "Hinweis: Shopify liefert die Kunden-E-Mail nicht – Mails werden nur über die Bestellnummer zugeordnet."]
    if problems:
        body += ["", "PROBLEME"] + [f"- {p}" for p in problems]
    try:
        send_mail(
            smtp_host, mail_user, mail_pw, mail_to,
            f"Vorarbeiter Tagesübersicht {now.astimezone(BERLIN):%d.%m.} – {len(jobs)} Auftrag/Aufträge",
            "\n".join(body), [],
        )
    except Exception as e:
        log(f"Übersicht nicht versendet: {type(e).__name__}")
        return 1
    log("Fertig.")
    return 0 if not problems else 2


if __name__ == "__main__":
    sys.exit(main())
