# vorarbeiter_core.py
"""Logik des Ahoi-Comic-Vorarbeiters.

Holt Bestellungen aus Shopify, liest zusaetzliche Dateien (Mail-Text, Screenshots, PDF, Word),
analysiert Fotos und Story per KI, prueft alles auf Luecken und baut die Vorarbeiter-Datei (ZIP),
die in der Comic-App (ComicPro) hochgeladen wird.
"""
from __future__ import annotations

import base64
import concurrent.futures
import datetime as _dt
import email
import email.policy
import io
import json
import re
import threading
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from PIL import Image, ImageOps

try:  # iPhone-Fotos (HEIC)
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
    HEIC_OK = True
except Exception:  # pragma: no cover
    HEIC_OK = False

FORMAT_NAME = "ahoi-vorarbeiter"
FORMAT_VERSION = 1

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_PHOTO_MODEL = "google/gemini-4-hydrogen"  # schnell, mit Bilderkennung
DEFAULT_STORY_MODEL = "google/gemini-4-carbon"  # gruendlich fuer die Story-Analyse
DEFAULT_SHOPIFY_API_VERSION = "2026-10"

COMIC_PAGE_OPTIONS = [4, 8, 12, 16, 18, 22]  # Seitenzahlen, die ComicPro anbietet
MIN_PHOTO_SIDE = 600  # darunter: Warnung "Foto zu klein"
MAX_PHOTO_SIDE = 2048  # groessere Fotos werden verkleinert gespeichert
AI_PHOTO_SIDE = 1024  # Bildgroesse fuer die KI-Analyse

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".gif", ".tif", ".tiff")
TEXT_EXT = (".txt", ".md", ".csv")

# ---------------------------------------------------------------------------
# Datenstrukturen
# ---------------------------------------------------------------------------


@dataclass
class Photo:
    id: str  # P1, P2, ...
    herkunft: str  # "Upload 1", "Datei: mail.pdf", ...
    jpeg: bytes = b""
    breite: int = 0
    hoehe: int = 0
    original_breite: int = 0
    original_hoehe: int = 0
    fehler: str = ""  # gesetzt, wenn das Foto nicht geladen werden konnte
    quelle_url: str = ""
    analyse: Dict[str, Any] = field(default_factory=dict)
    ist_screenshot: bool = False  # Bild mit Text (Mail-Screenshot) statt Personenfoto


@dataclass
class TextSource:
    quelle: str
    text: str


@dataclass
class Meldung:
    stufe: str  # "fehler" | "warnung" | "hinweis"
    text: str

    def as_dict(self) -> Dict[str, str]:
        return {"stufe": self.stufe, "text": self.text}


# ---------------------------------------------------------------------------
# Shopify (Dev-Dashboard-App, Client Credentials Grant)
# ---------------------------------------------------------------------------


class ShopifyClient:
    """Lesender Zugriff auf Bestellungen. Der Zugang wird alle 24 h automatisch erneuert."""

    def __init__(self, shop: str, client_id: str, client_secret: str, api_version: str = DEFAULT_SHOPIFY_API_VERSION):
        shop = (shop or "").strip().replace("https://", "").replace("http://", "").strip("/")
        if shop and "." not in shop:
            shop = f"{shop}.myshopify.com"
        self.shop = shop
        self.client_id = client_id
        self.client_secret = client_secret
        self.api_version = api_version or DEFAULT_SHOPIFY_API_VERSION
        self._token = ""
        self._token_until = 0.0
        self._lock = threading.Lock()

    def configured(self) -> bool:
        return bool(self.shop and self.client_id and self.client_secret)

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_until - 300:
                return self._token
            resp = requests.post(
                f"https://{self.shop}/admin/oauth/access_token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                timeout=30,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Shopify-Zugang fehlgeschlagen ({resp.status_code}): {resp.text[:300]}")
            data = resp.json()
            self._token = data.get("access_token", "")
            self._token_until = time.time() + float(data.get("expires_in") or 86399)
            if not self._token:
                raise RuntimeError("Shopify hat keinen Zugangsschluessel geliefert.")
            return self._token

    def graphql(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        resp = requests.post(
            f"https://{self.shop}/admin/api/{self.api_version}/graphql.json",
            json={"query": query, "variables": variables or {}},
            headers={"X-Shopify-Access-Token": self._access_token(), "Content-Type": "application/json"},
            timeout=45,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Shopify-Abfrage fehlgeschlagen ({resp.status_code}): {resp.text[:300]}")
        payload = resp.json()
        if payload.get("errors"):
            raise RuntimeError(f"Shopify-Fehler: {json.dumps(payload['errors'])[:400]}")
        return payload.get("data") or {}

    ORDER_FIELDS = """
      id
      name
      createdAt
      note
      displayFulfillmentStatus
      displayFinancialStatus
      tags
      customAttributes { key value }
      lineItems(first: 30) {
        nodes {
          name
          title
          variantTitle
          quantity
          customAttributes { key value }
          variant { selectedOptions { name value } }
        }
      }
    """

    def list_orders(self, only_open: bool = True, limit: int = 40) -> List[Dict[str, Any]]:
        q = "status:open AND fulfillment_status:unfulfilled" if only_open else ""
        data = self.graphql(
            "query($n:Int!,$q:String){ orders(first:$n, reverse:true, sortKey:CREATED_AT, query:$q){ nodes {"
            + self.ORDER_FIELDS
            + "} } }",
            {"n": int(limit), "q": q or None},
        )
        return ((data.get("orders") or {}).get("nodes")) or []

    def get_order(self, number: str) -> Optional[Dict[str, Any]]:
        num = normalize_order_number(number)
        if not num:
            return None
        data = self.graphql(
            "query($q:String){ orders(first:5, query:$q){ nodes {" + self.ORDER_FIELDS + "} } }",
            {"q": f"name:{num}"},
        )
        nodes = ((data.get("orders") or {}).get("nodes")) or []
        for node in nodes:
            if normalize_order_number(node.get("name", "")) == num:
                return node
        return nodes[0] if nodes else None


def normalize_order_number(value: Any) -> str:
    """'#1270', '1270', 'Bestellung 1270' -> '1270'."""
    m = re.search(r"(\d{3,})", str(value or ""))
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Bestellung auslesen
# ---------------------------------------------------------------------------

UPLOAD_KEY_RE = re.compile(r"^upload.*?(\d+)$", re.IGNORECASE)
KNOWN_KEYS = {
    "genre": "genre",
    "hauptcharakter name": "hauptcharakter",
    "storyline": "storyline",
    "titel": "titel",
    "charakterbeschreibung zu bildern": "bildbeschreibung",
    "name": "name",
    "anmerkung": "anmerkung",
    "ich beseitze die bildrechte": "bildrechte",
    "ich besitze die bildrechte": "bildrechte",
}


def _attrs_to_dict(attrs: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for a in attrs or []:
        if isinstance(a, dict):
            k = str(a.get("key") or a.get("name") or "").strip()
            v = a.get("value")
            if k:
                out[k] = "" if v is None else str(v)
    return out


def _is_comic_item(item: Dict[str, Any]) -> bool:
    props = _attrs_to_dict(item.get("customAttributes"))
    keys = {k.lower() for k in props}
    if keys & {"storyline", "genre", "hauptcharakter name"} or any(UPLOAD_KEY_RE.match(k) for k in keys):
        return True
    title = str(item.get("title") or item.get("name") or "")
    return title.lower().startswith("personalisierter") and "comic" in title.lower()


def _parse_options(item: Dict[str, Any]) -> Dict[str, str]:
    opts: Dict[str, str] = {}
    variant = item.get("variant") or {}
    for so in variant.get("selectedOptions") or []:
        if so.get("name"):
            opts[str(so["name"])] = str(so.get("value") or "")
    if not opts and item.get("variantTitle"):
        parts = [p.strip() for p in str(item["variantTitle"]).split(" / ")]
        for i, p in enumerate(parts):
            opts[f"Option {i + 1}"] = p
    return opts


def _clean_option_value(v: str) -> str:
    return re.sub(r"\s*\(\+[^)]*\)", "", v or "").strip()


def pages_from_options(opts: Dict[str, str]) -> Optional[int]:
    for k, v in opts.items():
        if "seite" in k.lower():
            m = re.search(r"\d+", _clean_option_value(v))
            if m:
                return int(m.group(0))
    for v in opts.values():  # Fallback: reine Zahl 8/12/16
        val = _clean_option_value(v)
        if re.fullmatch(r"(8|12|16|18|20|22|24)", val):
            return int(val)
    return None


def extra_persons_from_options(opts: Dict[str, str]) -> str:
    for k, v in opts.items():
        if "person" in k.lower() or "charakter" in k.lower():
            return _clean_option_value(v)
    for v in opts.values():  # Fallback: Nein / 1 / 2 / 3-5
        val = _clean_option_value(v)
        if re.fullmatch(r"(nein|\d|\d-\d|\d\+)", val, re.IGNORECASE):
            return val
    return ""


def expected_person_range(extra: str) -> Optional[Tuple[int, int]]:
    """Gebuchte Personenzahl inkl. Hauptperson."""
    e = (extra or "").strip().lower()
    if not e:
        return None
    if e == "nein":
        return (1, 1)
    m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", e)
    if m:
        return (1 + int(m.group(1)), 1 + int(m.group(2)))
    m = re.fullmatch(r"(\d+)\+", e)
    if m:
        return (1 + int(m.group(1)), 99)
    if e.isdigit():
        return (1 + int(e), 1 + int(e))
    return None


def nearest_page_option(pages: Optional[int]) -> int:
    if not pages:
        return 8
    return min(COMIC_PAGE_OPTIONS, key=lambda o: (abs(o - pages), o))


def extract_order(order: Dict[str, Any], item_index: int = 0) -> Dict[str, Any]:
    """Wandelt eine Shopify-Bestellung in die Rohdaten des Vorarbeiters um."""
    items = (order.get("lineItems") or {}).get("nodes") or order.get("line_items") or []
    comic_items = [it for it in items if _is_comic_item(it)]
    other_items = [it for it in items if not _is_comic_item(it)]
    item = comic_items[item_index] if 0 <= item_index < len(comic_items) else (comic_items[0] if comic_items else {})
    props = _attrs_to_dict(item.get("customAttributes"))
    opts = _parse_options(item) if item else {}

    felder: Dict[str, str] = {}
    weitere: Dict[str, str] = {}
    uploads: List[Tuple[int, str]] = []
    for k, v in props.items():
        if k.startswith("_"):
            continue  # versteckte App-Felder
        m = UPLOAD_KEY_RE.match(k.strip())
        if m and str(v).startswith("http"):
            uploads.append((int(m.group(1)), str(v).strip()))
            continue
        norm = KNOWN_KEYS.get(k.strip().lower())
        if norm:
            felder[norm] = str(v).strip()
        elif str(v).strip():
            weitere[k.strip()] = str(v).strip()
    uploads.sort()

    order_attrs = _attrs_to_dict(order.get("customAttributes"))
    return {
        "bestellung": normalize_order_number(order.get("name")),
        "bestellname": order.get("name") or "",
        "datum": order.get("createdAt") or "",
        "notiz": (order.get("note") or "").strip(),
        "bestell_attribute": {k: v for k, v in order_attrs.items() if not k.lower().startswith("zigpoll")},
        "artikel": item.get("title") or item.get("name") or "",
        "variante": item.get("variantTitle") or "",
        "optionen": opts,
        "seiten": pages_from_options(opts),
        "weitere_personen": extra_persons_from_options(opts),
        "felder": felder,
        "weitere_felder": weitere,
        "uploads": uploads,
        "zusatzartikel": [str(it.get("title") or it.get("name") or "") for it in other_items],
        "anzahl_comic_artikel": len(comic_items),
        "bildrechte": felder.get("bildrechte", ""),
    }


def order_summary_line(order: Dict[str, Any]) -> str:
    data = extract_order(order)
    when = ""
    try:
        when = _dt.datetime.fromisoformat(str(order.get("createdAt")).replace("Z", "+00:00")).strftime("%d.%m.%Y")
    except Exception:
        pass
    parts = [order.get("name") or "?", when]
    if data["seiten"]:
        parts.append(f"{data['seiten']} Seiten")
    parts.append(f"{len(data['uploads'])} Fotos")
    if data["felder"].get("hauptcharakter"):
        parts.append(data["felder"]["hauptcharakter"][:30])
    if not data["felder"] and not data["uploads"]:
        parts.append("keine Angaben")
    return " · ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Bilder
# ---------------------------------------------------------------------------


def load_image_bytes(raw: bytes) -> Tuple[Image.Image, int, int]:
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    w, h = img.size
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    return img, w, h


def to_jpeg(img: Image.Image, max_side: int = MAX_PHOTO_SIDE, quality: int = 90) -> bytes:
    im = img.copy()
    im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def make_photo(pid: str, herkunft: str, raw: bytes, url: str = "") -> Photo:
    p = Photo(id=pid, herkunft=herkunft, quelle_url=url)
    try:
        img, w, h = load_image_bytes(raw)
        p.original_breite, p.original_hoehe = w, h
        p.jpeg = to_jpeg(img)
        small = Image.open(io.BytesIO(p.jpeg))
        p.breite, p.hoehe = small.size
    except Exception as e:
        name = url.rsplit("/", 1)[-1] if url else herkunft
        if name.lower().endswith((".heic", ".heif")) and not HEIC_OK:
            p.fehler = "HEIC-Foto kann nicht gelesen werden (pillow-heif fehlt)."
        else:
            p.fehler = f"Foto nicht lesbar ({type(e).__name__})."
    return p


def download_photo(pid: str, herkunft: str, url: str) -> Photo:
    try:
        resp = requests.get(url, timeout=45)
        resp.raise_for_status()
    except Exception as e:
        return Photo(id=pid, herkunft=herkunft, quelle_url=url, fehler=f"Foto nicht ladbar ({type(e).__name__}).")
    return make_photo(pid, herkunft, resp.content, url)


def data_url_for_ai(p: Photo) -> str:
    img = Image.open(io.BytesIO(p.jpeg))
    return "data:image/jpeg;base64," + base64.b64encode(to_jpeg(img, AI_PHOTO_SIDE, 85)).decode("ascii")


# ---------------------------------------------------------------------------
# Dateien (manuelle Eingabe)
# ---------------------------------------------------------------------------


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", html)
    txt = re.sub(r"<[^>]+>", " ", html)
    txt = re.sub(r"&nbsp;", " ", txt)
    txt = re.sub(r"[ \t]+", " ", txt)
    return re.sub(r"\n\s*\n+", "\n\n", txt).strip()


def extract_file(name: str, raw: bytes) -> Tuple[List[TextSource], List[Tuple[str, bytes]]]:
    """Liest eine hochgeladene Datei. Rueckgabe: Texte und Bilder (Herkunft, Bytes)."""
    lower = name.lower()
    texts: List[TextSource] = []
    images: List[Tuple[str, bytes]] = []
    if lower.endswith(IMAGE_EXT):
        images.append((f"Datei: {name}", raw))
    elif lower.endswith(TEXT_EXT):
        texts.append(TextSource(f"Datei: {name}", raw.decode("utf-8", "ignore")))
    elif lower.endswith(".pdf"):
        try:
            import pymupdf as fitz
        except ImportError:  # aeltere Version
            import fitz  # type: ignore

        doc = fitz.open(stream=raw, filetype="pdf")
        page_texts = []
        for pno, page in enumerate(doc, start=1):
            t = page.get_text("text").strip()
            if t:
                page_texts.append(t)
            for img_info in page.get_images(full=True):
                try:
                    pix = doc.extract_image(img_info[0])
                    if pix and pix.get("width", 0) >= 300 and pix.get("height", 0) >= 300:
                        images.append((f"PDF {name}, Seite {pno}", pix["image"]))
                except Exception:
                    continue
            if not t and not page.get_images():
                # gescannte Seite ohne Text: als Bild weitergeben (die KI liest den Text)
                pm = page.get_pixmap(dpi=110)
                images.append((f"PDF {name}, Seite {pno} (Scan)", pm.tobytes("png")))
        if page_texts:
            texts.append(TextSource(f"PDF: {name}", "\n\n".join(page_texts)))
    elif lower.endswith(".docx"):
        import docx  # python-docx

        d = docx.Document(io.BytesIO(raw))
        parts = [p.text for p in d.paragraphs if p.text.strip()]
        for table in d.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        if parts:
            texts.append(TextSource(f"Word: {name}", "\n".join(parts)))
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for member in zf.namelist():
                if member.startswith("word/media/") and member.lower().endswith(IMAGE_EXT):
                    images.append((f"Word {name}: {member.rsplit('/', 1)[-1]}", zf.read(member)))
    elif lower.endswith(".eml"):
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        header = f"Von: {msg.get('From', '')}\nBetreff: {msg.get('Subject', '')}\nDatum: {msg.get('Date', '')}"
        body = ""
        try:
            part = msg.get_body(preferencelist=("plain", "html"))
            if part is not None:
                content = part.get_content()
                body = _html_to_text(content) if part.get_content_type() == "text/html" else content
        except Exception:
            body = ""
        texts.append(TextSource(f"Mail: {name}", (header + "\n\n" + (body or "")).strip()))
        for att in msg.iter_attachments():
            fname = att.get_filename() or "anhang"
            try:
                payload = att.get_payload(decode=True) or b""
            except Exception:
                payload = b""
            if payload:
                t2, i2 = extract_file(fname, payload)
                texts.extend(TextSource(f"{name} → {t.quelle}", t.text) for t in t2)
                images.extend((f"{name} → {h}", b) for h, b in i2)
    else:
        raise ValueError(f"Dateityp nicht unterstuetzt: {name}")
    return texts, images


# ---------------------------------------------------------------------------
# KI
# ---------------------------------------------------------------------------


def _json_from_text(text: str) -> Dict[str, Any]:
    t = (text or "").strip()
    fence = "\x60" * 3  # Markdown-Codeblock
    t = re.sub(r"^" + fence + r"(?:json)?\s*|\s*" + fence + r"$", "", t)
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            return json.loads(m.group(0))
        raise


class AI:
    def __init__(self, api_key: str, photo_model: str = DEFAULT_PHOTO_MODEL, story_model: str = DEFAULT_STORY_MODEL):
        from openai import OpenAI

        self.client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key, timeout=300)
        self.photo_model = photo_model or DEFAULT_PHOTO_MODEL
        self.story_model = story_model or DEFAULT_STORY_MODEL

    def ask_json(self, model: str, content: List[Dict[str, Any]], tries: int = 3) -> Dict[str, Any]:
        last: Optional[Exception] = None
        for attempt in range(tries):
            try:
                resp = self.client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": content}],
                    response_format={"type": "json_object"},
                    temperature=0.2,
                )
                return _json_from_text(resp.choices[0].message.content or "")
            except Exception as e:  # erneut anfragen
                last = e
                time.sleep(2 + 3 * attempt)
        raise RuntimeError(f"KI-Anfrage fehlgeschlagen: {last}")


PHOTO_PROMPT = """Du bist Vorarbeiter fuer personalisierte Comics. Analysiere dieses Bild nuechtern.
Antworte NUR mit JSON in genau diesem Format:
{
  "typ": "personenfoto" | "screenshot_text" | "sonstiges",
  "text": "falls Screenshot/Dokument: der vollstaendige lesbare Text, sonst leer",
  "personen": [
    {"position": "z.B. links, Mitte, vorne, einzige Person",
     "geschlecht": "maennlich" | "weiblich" | "unklar",
     "alter_geschaetzt": Zahl (geschaetztes Alter in Jahren),
     "merkmale": "Haare, Brille, Bart, Kleidung, Besonderheiten - kurz"}
  ],
  "tiere": [{"position": "...", "art": "z.B. Hund", "rasse": "falls erkennbar", "merkmale": "Fell, Farbe"}],
  "qualitaet": "gut" | "mittel" | "schlecht",
  "qualitaet_grund": "z.B. unscharf, Gesicht verdeckt, zu dunkel, Sonnenbrille - leer wenn gut",
  "gesicht_sichtbar": "gut" | "teilweise" | "nein",
  "eignung_charakter": Zahl 1-10 (wie gut eignet sich das Foto als Vorlage fuer eine Comicfigur: Gesicht frontal und
    scharf, Haare/Frisur erkennbar, moeglichst nur diese Person, gute Aufloesung und Licht),
  "eignung_grund": "kurz, z.B. Gesicht frontal und scharf / Gesicht klein, Gruppenfoto"
}
Schaetze das Alter realistisch. Erfinde nichts, was nicht zu sehen ist."""


STORY_PROMPT = """Du bist der Vorarbeiter von Ahoi Comic (personalisierte Comics). Du bereitest eine Kundenbestellung
so auf, dass daraus ohne Rueckfragen ein Comic erstellt werden kann. Arbeite genau, erfinde keine Fakten.

BESTELLDATEN (aus Shopify, kann leer sein):
{order_block}

TEXTE DES KUNDEN (Shopify-Felder, Mails, Dokumente, Screenshots):
{text_block}

FOTOS (von der Bilderkennung bereits beschrieben):
{photo_block}

AUFGABE:
1. Trenne Story-relevantes (Anlass, Handlung, Wuensche zur Geschichte, Orte, Hobbys, Beruf, Beziehungen,
   Eigenheiten der Personen) von Zusatzinfos (Liefertermin, Druck-/Versandwuensche, Widmung, Rechnung,
   organisatorische Hinweise, Fragen an das Team).
2. Erstelle eine Liste aller Figuren, die im Comic vorkommen sollen (Menschen, Tiere, ausdruecklich gewuenschte
   Fantasiefiguren/Boesewichte). Die Hauptperson zuerst. "Ich" im Kundentext ist die bestellende Person.
3. Ordne jedes Personenfoto (P1, P2, ...) der passenden Figur zu: nutze Namen, Altersangaben, Geschlecht,
   Beziehungen (Papa, Tochter, Ehemann ...), Bildbeschreibungen des Kunden und die Reihenfolge der Uploads.
   Bei Gruppenfotos gib die Position der Figur auf dem Foto an. Ein Foto kann mehreren Figuren gehoeren
   (Gruppenfoto). Wenn du unsicher bist, sage es offen (zuordnung_sicherheit "niedrig").
   Hat eine Figur MEHRERE Fotos, liste ALLE ihre Fotos auf und setze das Foto an die ERSTE Stelle, mit dem sich
   die Comicfigur am besten erstellen laesst (beste Qualitaet, Gesicht gut sichtbar, moeglichst allein im Bild,
   hohe "eignung_charakter"). Begruende die Wahl in "hauptfoto_grund".
4. Vergleiche das Alter laut Text mit dem geschaetzten Alter auf dem Foto.
5. Schreibe ein klares Briefing (Deutsch) fuer den Comic-Autor: nur Story-relevantes, gut gegliedert,
   alle Kundenwuensche erhalten, nichts dazuerfinden.

Antworte NUR mit JSON in genau diesem Format:
{
  "titel": "Titel des Kunden oder ein kurzer passender Vorschlag",
  "titel_ist_vorschlag": true | false,
  "genre": "Genre laut Kunde (z.B. Superheld, Abenteuer, Fantasy) oder bester Vorschlag",
  "briefing": "Briefing-Text",
  "widmung": "Widmungstext, falls der Kunde einen nennt, sonst leer",
  "zusatzinfos": ["..."],
  "personen": [
    {"name": "Name (oder Rolle wie 'Papa', falls kein Name genannt)",
     "rolle": "Hauptfigur" | "Nebenfigur" | "Boesewicht",
     "art": "Mensch" | "Tier" | "Fantasiefigur",
     "beziehung": "z.B. Ehemann der Kundin, Tochter, Hund der Familie",
     "alter_text": Zahl oder null (Alter laut Kundentext),
     "alter_foto": Zahl oder null (geschaetztes Alter auf dem zugeordneten Foto),
     "beschreibung": "Aussehen/Eigenschaften laut Text und Foto, Beruf, Hobbys - kurz",
     "outfit_kunde": "nur wenn der Kunde Kleidung ausdruecklich vorgibt, sonst leer",
     "fotos": [{"foto_id": "P1", "position": "z.B. links / einzige Person"}],
     "hauptfoto_grund": "nur bei mehreren Fotos: warum das erste Foto am besten ist",
     "zuordnung_sicherheit": "hoch" | "mittel" | "niedrig" | "kein_foto",
     "begruendung": "kurz, warum dieses Foto"}
  ],
  "fotos_ohne_zuordnung": [{"foto_id": "P3", "grund": "..."}],
  "meldungen": [{"stufe": "fehler" | "warnung" | "hinweis", "text": "konkret, was fehlt oder unklar ist"}]
}"""


def analyse_photos(
    ai: AI, photos: List[Photo], progress: Optional[Callable[[str], None]] = None, workers: int = 4
) -> None:
    todo = [p for p in photos if p.jpeg and not p.analyse]

    def run(p: Photo) -> None:
        try:
            p.analyse = ai.ask_json(
                ai.photo_model,
                [{"type": "text", "text": PHOTO_PROMPT}, {"type": "image_url", "image_url": {"url": data_url_for_ai(p)}}],
            )
        except Exception as e:
            p.analyse = {"typ": "unbekannt", "fehler": str(e)[:200]}
        p.ist_screenshot = str(p.analyse.get("typ", "")).startswith("screenshot")

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(run, p) for p in todo]
        for _ in concurrent.futures.as_completed(futures):
            done += 1
            if progress:
                progress(f"Fotos analysiert: {done}/{len(todo)}")


def _order_block(order: Optional[Dict[str, Any]]) -> str:
    if not order:
        return "(keine Shopify-Bestellung)"
    lines = [
        f"Bestellung: {order.get('bestellname') or order.get('bestellung')}",
        f"Artikel: {order.get('artikel')}",
        f"Variante: {order.get('variante')}",
        f"Gebuchte zusaetzliche Personen: {order.get('weitere_personen') or 'unbekannt'}",
        f"Seitenanzahl: {order.get('seiten') or 'unbekannt'}",
    ]
    if order.get("zusatzartikel"):
        lines.append("Zusatzartikel: " + "; ".join(order["zusatzartikel"]))
    if order.get("notiz"):
        lines.append("Bestellnotiz: " + order["notiz"])
    return "\n".join(lines)


def collect_texts(order: Optional[Dict[str, Any]], extra_texts: List[TextSource], photos: List[Photo]) -> List[TextSource]:
    texts: List[TextSource] = []
    if order:
        labels = {
            "hauptcharakter": "Hauptcharakter Name",
            "genre": "Genre",
            "titel": "Titel",
            "storyline": "Storyline",
            "bildbeschreibung": "Charakterbeschreibung zu Bildern",
            "name": "Name",
            "anmerkung": "Anmerkung",
        }
        for key, label in labels.items():
            if order["felder"].get(key):
                texts.append(TextSource(f"Shopify-Feld '{label}'", order["felder"][key]))
        for k, v in (order.get("weitere_felder") or {}).items():
            texts.append(TextSource(f"Shopify-Feld '{k}'", v))
        for k, v in (order.get("bestell_attribute") or {}).items():
            texts.append(TextSource(f"Bestellattribut '{k}'", v))
        if order.get("notiz"):
            texts.append(TextSource("Bestellnotiz", order["notiz"]))
    texts.extend(t for t in extra_texts if t.text.strip())
    for p in photos:
        if p.ist_screenshot and str(p.analyse.get("text", "")).strip():
            texts.append(TextSource(f"Screenshot {p.id} ({p.herkunft})", str(p.analyse["text"]).strip()))
    return texts


def _photo_block(photos: List[Photo]) -> str:
    lines = []
    for p in photos:
        if p.fehler or not p.jpeg or p.ist_screenshot:
            continue
        a = p.analyse or {}
        pers = "; ".join(
            f"{x.get('position', '?')}: {x.get('geschlecht', '?')}, ca. {x.get('alter_geschaetzt', '?')} J., {x.get('merkmale', '')}"
            for x in a.get("personen") or []
        )
        tiere = "; ".join(f"{x.get('art', '?')} {x.get('rasse', '')} ({x.get('position', '')})" for x in a.get("tiere") or [])
        lines.append(
            f"{p.id} [{p.herkunft}, {p.original_breite}x{p.original_hoehe} px]: Personen: {pers or 'keine'} | Tiere: {tiere or 'keine'}"
            f" | Qualitaet: {a.get('qualitaet', '?')} | Gesicht: {a.get('gesicht_sichtbar', '?')}"
            f" | Eignung Charakter: {a.get('eignung_charakter', '?')}/10"
        )
    return "\n".join(lines) or "(keine Fotos)"


def analyse_story(ai: AI, order: Optional[Dict[str, Any]], texts: List[TextSource], photos: List[Photo]) -> Dict[str, Any]:
    text_block = "\n\n".join(f"[{t.quelle}]\n{t.text.strip()}" for t in texts) or "(keine Texte)"
    prompt = (
        STORY_PROMPT.replace("{order_block}", _order_block(order))
        .replace("{text_block}", text_block[:60000])
        .replace("{photo_block}", _photo_block(photos))
    )
    return ai.ask_json(ai.story_model, [{"type": "text", "text": prompt}])


# ---------------------------------------------------------------------------
# Ergebnis aufbauen, pruefen, ZIP
# ---------------------------------------------------------------------------

ROLE_MAP = {"hauptfigur": "Hauptfigur", "nebenfigur": "Nebenfigur", "boesewicht": "Bösewicht", "bösewicht": "Bösewicht"}


def _num(v: Any) -> Optional[int]:
    try:
        if v is None or v == "":
            return None
        return int(round(float(str(v).replace(",", ".").split()[0])))
    except Exception:
        m = re.search(r"\d+", str(v or ""))
        return int(m.group(0)) if m else None


def build_result(order: Optional[Dict[str, Any]], story: Dict[str, Any], photos: List[Photo]) -> Dict[str, Any]:
    """Baut das bearbeitbare Ergebnis aus Shopify-Daten und KI-Antwort."""
    valid_ids = {p.id for p in photos if p.jpeg and not p.ist_screenshot}
    personen = []
    for i, raw in enumerate(story.get("personen") or [], start=1):
        fotos = [f for f in raw.get("fotos") or [] if isinstance(f, dict) and f.get("foto_id") in valid_ids]
        personen.append(
            {
                "id": i,
                "aktiv": True,
                "name": str(raw.get("name") or f"Person {i}").strip(),
                "rolle": ROLE_MAP.get(str(raw.get("rolle") or "").strip().lower(), "Hauptfigur" if i == 1 else "Nebenfigur"),
                "art": str(raw.get("art") or "Mensch"),
                "beziehung": str(raw.get("beziehung") or ""),
                "alter_text": _num(raw.get("alter_text")),
                "alter_foto": _num(raw.get("alter_foto")),
                "beschreibung": str(raw.get("beschreibung") or ""),
                "outfit": str(raw.get("outfit_kunde") or ""),
                "foto_id": fotos[0]["foto_id"] if fotos else "",
                "foto_position": str(fotos[0].get("position") or "") if fotos else "",
                "weitere_foto_ids": [f["foto_id"] for f in fotos[1:]],
                "foto_positionen": {f["foto_id"]: str(f.get("position") or "") for f in fotos},
                "ki_hauptfoto": fotos[0]["foto_id"] if fotos else "",
                "hauptfoto_grund": str(raw.get("hauptfoto_grund") or ""),
                "hauptfoto_bestaetigt": len(fotos) <= 1,
                "zuordnung_sicherheit": str(raw.get("zuordnung_sicherheit") or ("kein_foto" if not fotos else "mittel")),
                "begruendung": str(raw.get("begruendung") or ""),
            }
        )
    pages = (order or {}).get("seiten")
    titel = str(story.get("titel") or (order or {}).get("felder", {}).get("titel") or "").strip()
    return {
        "bestellung": (order or {}).get("bestellung", ""),
        "titel": titel,
        "titel_ist_vorschlag": bool(story.get("titel_ist_vorschlag")) and not (order or {}).get("felder", {}).get("titel"),
        "genre": str(story.get("genre") or (order or {}).get("felder", {}).get("genre") or "").strip(),
        "seiten": nearest_page_option(pages),
        "seiten_gebucht": pages,
        "briefing": str(story.get("briefing") or "").strip(),
        "widmung": str(story.get("widmung") or "").strip(),
        "zusatzinfos": [str(z) for z in story.get("zusatzinfos") or [] if str(z).strip()],
        "personen": personen,
        "ki_meldungen": [m for m in story.get("meldungen") or [] if isinstance(m, dict) and m.get("text")],
        "fotos_ohne_zuordnung_ki": story.get("fotos_ohne_zuordnung") or [],
    }


def photo_suitability(p: Photo) -> str:
    """Kurzbewertung eines Fotos fuer die Anzeige (Eignung fuer das Charakter-Sheet)."""
    a = p.analyse or {}
    teile = []
    if a.get("eignung_charakter") not in (None, ""):
        teile.append(f"Eignung {a.get('eignung_charakter')}/10")
    if a.get("gesicht_sichtbar"):
        teile.append(f"Gesicht {a.get('gesicht_sichtbar')}")
    if p.original_breite:
        teile.append(f"{p.original_breite}×{p.original_hoehe} px")
    if a.get("eignung_grund"):
        teile.append(str(a.get("eignung_grund")))
    return " · ".join(teile)


def _age_mismatch(text_age: Optional[int], photo_age: Optional[int]) -> bool:
    if text_age is None or photo_age is None:
        return False
    tol = 4 if min(text_age, photo_age) < 16 else (10 if min(text_age, photo_age) < 50 else 15)
    return abs(text_age - photo_age) > tol


def check_result(result: Dict[str, Any], order: Optional[Dict[str, Any]], photos: List[Photo]) -> List[Meldung]:
    """Feste Pruefregeln (laufen ohne KI und nach jeder Aenderung erneut)."""
    out: List[Meldung] = []
    by_id = {p.id: p for p in photos}
    aktive = [p for p in result.get("personen", []) if p.get("aktiv", True)]

    if order is not None and not order["felder"] and not order["uploads"]:
        out.append(
            Meldung("fehler", "Die Shopify-Bestellung enthält keine Angaben und keine Fotos – Infos bitte manuell ergänzen (z. B. aus der Mail).")
        )
    if not result.get("briefing", "").strip():
        out.append(Meldung("fehler", "Kein Briefing / keine Story vorhanden."))
    if not aktive:
        out.append(Meldung("fehler", "Keine Figuren erkannt."))

    for p in photos:
        if p.fehler:
            out.append(Meldung("fehler", f"{p.id} ({p.herkunft}): {p.fehler}"))
        elif not p.ist_screenshot and min(p.original_breite, p.original_hoehe) < MIN_PHOTO_SIDE:
            out.append(
                Meldung("warnung", f"{p.id} ({p.herkunft}): Foto sehr klein ({p.original_breite} × {p.original_hoehe} px) – Gesicht evtl. ungenau.")
            )
        q = (p.analyse or {}).get("qualitaet")
        if q == "schlecht" and not p.ist_screenshot:
            grund = (p.analyse or {}).get("qualitaet_grund") or ""
            out.append(Meldung("warnung", f"{p.id} ({p.herkunft}): schlechte Bildqualität {('– ' + grund) if grund else ''}".strip()))

    used: Dict[str, List[str]] = {}
    for pers in aktive:
        name = pers.get("name") or "?"
        fid = pers.get("foto_id") or ""
        art = pers.get("art") or "Mensch"
        if not fid:
            if art == "Fantasiefigur" or pers.get("rolle") == "Bösewicht":
                out.append(Meldung("hinweis", f"{name}: kein Foto – wird frei gezeichnet."))
            else:
                out.append(Meldung("fehler", f"{name} ({art}) wird beschrieben, aber es gibt kein Foto dazu."))
            continue
        if fid not in by_id:
            out.append(Meldung("fehler", f"{name}: zugeordnetes Foto {fid} existiert nicht."))
            continue
        used.setdefault(fid, []).append(name)
        n_fotos = 1 + len([f for f in pers.get("weitere_foto_ids") or [] if f in by_id])
        if n_fotos > 1 and not pers.get("hauptfoto_bestaetigt"):
            vorschlag = pers.get("ki_hauptfoto") or fid
            out.append(Meldung("warnung", f"{name}: {n_fotos} Fotos – bitte Hauptfoto wählen (KI-Vorschlag: {vorschlag})."))
        if pers.get("zuordnung_sicherheit") == "niedrig":
            out.append(Meldung("warnung", f"{name}: Zuordnung zu Foto {fid} ist unsicher – bitte prüfen."))
        if _age_mismatch(pers.get("alter_text"), pers.get("alter_foto")):
            out.append(
                Meldung(
                    "warnung",
                    f"{name}: Alter passt nicht – laut Text {pers['alter_text']} Jahre, auf Foto {fid} ca. {pers['alter_foto']} Jahre.",
                )
            )
        ana = by_id[fid].analyse or {}
        n_people = len(ana.get("personen") or []) + len(ana.get("tiere") or [])
        if n_people > 1 and not (pers.get("foto_position") or "").strip():
            out.append(Meldung("warnung", f"{name}: Foto {fid} ist ein Gruppenfoto – bitte Position der Person angeben."))
        if art == "Mensch" and ana and not ana.get("personen") and ana.get("tiere"):
            out.append(Meldung("warnung", f"{name}: Foto {fid} zeigt ein Tier, aber {name} ist als Mensch eingetragen."))
        if art == "Tier" and ana and ana.get("personen") and not ana.get("tiere"):
            out.append(Meldung("warnung", f"{name}: Foto {fid} zeigt einen Menschen, aber {name} ist als Tier eingetragen."))

    for fid, names in used.items():
        if len(names) > 1:
            out.append(Meldung("hinweis", f"Foto {fid} wird für mehrere Figuren genutzt ({', '.join(names)}) – Gruppenfoto."))

    all_used = set(used) | {f for p in aktive for f in p.get("weitere_foto_ids") or []}
    for p in photos:
        if p.jpeg and not p.fehler and not p.ist_screenshot and p.id not in all_used:
            out.append(Meldung("fehler", f"{p.id} ({p.herkunft}) ist keiner Person zugeordnet."))

    if order is not None:
        rng = expected_person_range(order.get("weitere_personen", ""))
        humans = [p for p in aktive if (p.get("art") or "Mensch") == "Mensch" and p.get("rolle") != "Bösewicht"]
        if rng and not (rng[0] <= len(humans) <= rng[1]):
            soll = f"{rng[0]}" if rng[0] == rng[1] else f"{rng[0]}–{rng[1] if rng[1] < 99 else 'mehr'}"
            out.append(
                Meldung(
                    "warnung",
                    f"Personenzahl passt nicht zur Bestellung: gebucht {soll} Person(en) inkl. Hauptperson, erkannt {len(humans)} (Tiere nicht mitgezählt).",
                )
            )
        if order["uploads"] and not str(order.get("bildrechte") or "").strip():
            out.append(Meldung("warnung", "Bildrechte wurden in der Bestellung nicht bestätigt."))
        if not order.get("seiten"):
            out.append(Meldung("warnung", "Seitenanzahl nicht in der Bestellung gefunden – bitte prüfen."))
        elif order["seiten"] not in COMIC_PAGE_OPTIONS:
            out.append(
                Meldung("hinweis", f"Gebucht sind {order['seiten']} Seiten; in der Comic-App wird {result.get('seiten')} eingestellt.")
            )
        if order.get("anzahl_comic_artikel", 0) > 1:
            out.append(Meldung("warnung", f"Die Bestellung enthält {order['anzahl_comic_artikel']} Comics – jeden einzeln vorbereiten."))
        if order.get("zusatzartikel"):
            out.append(Meldung("hinweis", "Zusatzartikel: " + "; ".join(order["zusatzartikel"])))

    if result.get("titel_ist_vorschlag"):
        out.append(Meldung("hinweis", f"Kein Titel vom Kunden – Vorschlag: „{result.get('titel', '')}“."))
    if not result.get("genre"):
        out.append(Meldung("warnung", "Kein Genre angegeben."))

    # KI-Meldungen ergaenzen (ohne Dopplungen)
    seen = {m.text for m in out}
    for m in result.get("ki_meldungen") or []:
        stufe = str(m.get("stufe") or "hinweis").lower()
        if stufe not in ("fehler", "warnung", "hinweis"):
            stufe = "hinweis"
        text = "KI: " + str(m.get("text")).strip()
        if text not in seen:
            out.append(Meldung(stufe, text))
            seen.add(text)
    order_rank = {"fehler": 0, "warnung": 1, "hinweis": 2}
    out.sort(key=lambda m: order_rank.get(m.stufe, 3))
    return out


def _slug(s: str) -> str:
    s = (s or "").lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s[:30] or "person"


def compose_hint(pers: Dict[str, Any]) -> str:
    parts = []
    if pers.get("art") and pers["art"] != "Mensch":
        parts.append(pers["art"])
    if pers.get("beziehung"):
        parts.append(pers["beziehung"])
    age = pers.get("alter_text") if pers.get("alter_text") is not None else pers.get("alter_foto")
    if age is not None:
        parts.append("Baby (unter 1 Jahr)" if age == 0 else ("ca. 1 Jahr" if age == 1 else f"ca. {age} Jahre"))
    if pers.get("foto_position"):
        parts.append(f"auf dem Foto: {pers['foto_position']}")
    if pers.get("beschreibung"):
        parts.append(pers["beschreibung"])
    return ", ".join(p.strip().rstrip(".") for p in parts if str(p).strip())


def compose_order_text(result: Dict[str, Any]) -> str:
    """Briefing fuer das Feld 'Order Text' in ComicPro (Story + Figurenliste)."""
    lines = [result.get("briefing", "").strip()]
    aktive = [p for p in result.get("personen", []) if p.get("aktiv", True)]
    if aktive:
        lines.append("\nFiguren:")
        for p in aktive:
            hint = compose_hint(p)
            lines.append(f"- {p['name']} ({p.get('rolle', 'Hauptfigur')}){': ' + hint if hint else ''}")
    return "\n".join(lines).strip()


def build_zip(
    result: Dict[str, Any],
    photos: List[Photo],
    meldungen: List[Meldung],
    order: Optional[Dict[str, Any]],
    texts: List[TextSource],
    dateien: List[str],
) -> Tuple[str, bytes]:
    by_id = {p.id: p for p in photos}
    files: Dict[str, bytes] = {}
    photo_name: Dict[str, str] = {}
    aktive = [p for p in result.get("personen", []) if p.get("aktiv", True)]
    # Dateinamen nach Zuordnung
    for idx, pers in enumerate(aktive, start=1):
        for fid in [pers.get("foto_id")] + list(pers.get("weitere_foto_ids") or []):
            if fid and fid in by_id and by_id[fid].jpeg and fid not in photo_name:
                photo_name[fid] = f"fotos/{fid.lower()}_{idx:02d}_{_slug(pers['name'])}.jpg"
    for p in photos:
        if p.jpeg and not p.ist_screenshot and p.id not in photo_name:
            photo_name[p.id] = f"fotos/{p.id.lower()}_nicht_zugeordnet.jpg"
        if p.jpeg and p.ist_screenshot:
            photo_name[p.id] = f"screenshots/{p.id.lower()}.jpg"
    for fid, name in photo_name.items():
        files[name] = by_id[fid].jpeg

    personen_out = []
    for idx, pers in enumerate(aktive, start=1):
        personen_out.append(
            {
                "id": idx,
                "name": pers["name"],
                "rolle": pers.get("rolle", "Hauptfigur"),
                "art": pers.get("art", "Mensch"),
                "beziehung": pers.get("beziehung", ""),
                "alter": pers.get("alter_text") if pers.get("alter_text") is not None else pers.get("alter_foto"),
                "alter_text": pers.get("alter_text"),
                "alter_foto": pers.get("alter_foto"),
                "beschreibung": pers.get("beschreibung", ""),
                "hinweis": compose_hint(pers),
                "outfit": pers.get("outfit", ""),
                "foto": photo_name.get(pers.get("foto_id") or "", ""),
                "foto_id": pers.get("foto_id") or "",
                "foto_position": pers.get("foto_position", ""),
                "foto_herkunft": by_id[pers["foto_id"]].herkunft if pers.get("foto_id") in by_id else "",
                "weitere_fotos": [photo_name[f] for f in pers.get("weitere_foto_ids") or [] if f in photo_name],
                "zuordnung_sicherheit": pers.get("zuordnung_sicherheit", ""),
            }
        )
    nummer = result.get("bestellung") or (order or {}).get("bestellung") or "ohne_nummer"
    data = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "erstellt_am": _dt.datetime.now().isoformat(timespec="seconds"),
        "bestellung": {
            "nummer": nummer,
            "shopify": bool(order),
            "datum": (order or {}).get("datum", ""),
            "artikel": (order or {}).get("artikel", ""),
            "variante": (order or {}).get("variante", ""),
            "weitere_personen": (order or {}).get("weitere_personen", ""),
            "seiten_gebucht": (order or {}).get("seiten"),
            "zusatzartikel": (order or {}).get("zusatzartikel", []),
            "bildrechte_bestaetigt": bool(str((order or {}).get("bildrechte") or "").strip()),
        },
        "comic": {
            "titel": result.get("titel", ""),
            "titel_ist_vorschlag": bool(result.get("titel_ist_vorschlag")),
            "genre": result.get("genre", ""),
            "seiten": int(result.get("seiten") or 8),
            "briefing": result.get("briefing", ""),
            "order_text": compose_order_text(result),
            "widmung": result.get("widmung", ""),
        },
        "personen": personen_out,
        "zusatzinfos": result.get("zusatzinfos", []),
        "meldungen": [m.as_dict() for m in meldungen],
        "fotos": [
            {
                "id": p.id,
                "datei": photo_name.get(p.id, ""),
                "herkunft": p.herkunft,
                "original_groesse": [p.original_breite, p.original_hoehe],
                "fehler": p.fehler,
                "analyse": p.analyse,
            }
            for p in photos
        ],
        "quellen": {
            "dateien": dateien,
            "texte": [{"quelle": t.quelle, "text": t.text} for t in texts],
            "shopify_felder": (order or {}).get("felder", {}),
            "shopify_weitere_felder": (order or {}).get("weitere_felder", {}),
        },
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("vorarbeiter.json", json.dumps(data, ensure_ascii=False, indent=2))
        zf.writestr("briefing.txt", _readable_summary(data))
        for name, content in files.items():
            zf.writestr(name, content)
    return f"AhoiComic_{nummer}_vorarbeiter.zip", buf.getvalue()


def _readable_summary(data: Dict[str, Any]) -> str:
    c = data["comic"]
    lines = [
        f"Ahoi Comic – Vorarbeiter – Bestellung {data['bestellung']['nummer']}",
        f"Titel: {c['titel']}{' (Vorschlag)' if c['titel_ist_vorschlag'] else ''}",
        f"Genre: {c['genre']}   Seiten: {c['seiten']}",
        "",
        "BRIEFING",
        c["briefing"],
        "",
        "FIGUREN",
    ]
    for p in data["personen"]:
        lines.append(f"- {p['name']} ({p['rolle']}, {p['art']}) – Foto: {p['foto'] or 'keins'} – {p['hinweis']}")
    if c.get("widmung"):
        lines += ["", "WIDMUNG", c["widmung"]]
    if data["zusatzinfos"]:
        lines += ["", "ZUSATZINFOS"] + [f"- {z}" for z in data["zusatzinfos"]]
    if data["meldungen"]:
        lines += ["", "MELDUNGEN"] + [f"[{m['stufe'].upper()}] {m['text']}" for m in data["meldungen"]]
    return "\n".join(lines) + "\n"
