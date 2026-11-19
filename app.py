# app.py – Ahoi Comic Vorarbeiter
"""Bereitet Comic-Bestellungen vor: Shopify-Daten + Mails/Dateien -> geprüfte Vorarbeiter-Datei (ZIP)
für die Comic-App (ComicPro)."""
from __future__ import annotations

import concurrent.futures
import hmac
import time
from typing import Any, Dict, List, Optional

import streamlit as st

import vorarbeiter_core as core

st.set_page_config(page_title="Ahoi Comic – Vorarbeiter", layout="wide")

st.markdown(
    """
<style>
.block-container { padding-top: 1.2rem; max-width: 1250px; }
.va-photo-cap { font-size: 0.82rem; color: rgba(0,0,0,0.65); }
</style>
""",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Zugang
# ---------------------------------------------------------------------------


def _secret(name: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(name, default) or default)
    except Exception:
        return default


def require_password() -> None:
    expected = _secret("APP_PASSWORD")
    if not expected:
        st.error("APP_PASSWORD ist nicht gesetzt. Bitte in den Streamlit-Secrets hinterlegen.")
        st.stop()
    if st.session_state.get("auth_ok"):
        return
    st.title("Ahoi Comic – Vorarbeiter")
    pwd = st.text_input("Passwort", type="password", key="auth_pwd")
    if st.button("Anmelden", type="primary"):
        if hmac.compare_digest(pwd or "", expected):
            st.session_state.auth_ok = True
            st.rerun()
        st.error("Falsches Passwort.")
    st.stop()


require_password()


@st.cache_resource
def _shopify_client(shop: str, cid: str, secret: str, version: str) -> core.ShopifyClient:
    return core.ShopifyClient(shop, cid, secret, version)


def shopify() -> Optional[core.ShopifyClient]:
    c = _shopify_client(
        _secret("SHOPIFY_SHOP", "vavedi-e6"),  # Ahoi Comic = vavedi-e6.myshopify.com
        _secret("SHOPIFY_CLIENT_ID"),
        _secret("SHOPIFY_CLIENT_SECRET"),
        _secret("SHOPIFY_API_VERSION", core.DEFAULT_SHOPIFY_API_VERSION),
    )
    return c if c.configured() else None


def ai() -> Optional[core.AI]:
    key = _secret("OPENROUTER_API_KEY")
    if not key:
        return None
    return core.AI(
        key,
        _secret("PHOTO_MODEL", core.DEFAULT_PHOTO_MODEL),
        _secret("STORY_MODEL", core.DEFAULT_STORY_MODEL),
    )


# ---------------------------------------------------------------------------
# Zustand
# ---------------------------------------------------------------------------

DEFAULTS: Dict[str, Any] = {
    "va_orders": [],  # Liste offener Bestellungen (Shopify-Rohdaten)
    "va_raw_order": None,  # gewählte Shopify-Bestellung (Rohdaten)
    "va_item_index": 0,
    "va_result": None,  # bearbeitbares Ergebnis
    "va_order": None,  # ausgelesene Bestelldaten
    "va_photos": [],
    "va_texts": [],
    "va_files": [],
    "va_upload_gen": 0,
}
for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v


def reset_all() -> None:
    for k in list(st.session_state.keys()):
        if k.startswith(("va_", "p_", "res_")) and k != "va_upload_gen":
            st.session_state.pop(k, None)
    st.session_state.va_upload_gen = int(st.session_state.get("va_upload_gen", 0)) + 1
    for k, v in DEFAULTS.items():
        if k != "va_upload_gen":
            st.session_state[k] = v


# ---------------------------------------------------------------------------
# Kopf
# ---------------------------------------------------------------------------

head_l, head_r = st.columns([4, 1])
with head_l:
    st.title("Ahoi Comic – Vorarbeiter")
    st.caption("Bestellung holen, Mail/Dateien ergänzen, prüfen lassen und die Vorarbeiter-Datei für die Comic-App herunterladen.")
with head_r:
    st.write("")
    if st.button("Neue Bestellung", width="stretch"):
        reset_all()
        st.rerun()

shop = shopify()
kiclient = ai()
status_bits = [
    "Shopify ✅" if shop else "Shopify ❌ (SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET fehlen)",
    "KI ✅" if kiclient else "KI ❌ (OPENROUTER_API_KEY fehlt)",
    "HEIC ✅" if core.HEIC_OK else "HEIC ❌",
]
st.caption(" · ".join(status_bits))

# ---------------------------------------------------------------------------
# 1) Eingabe
# ---------------------------------------------------------------------------

st.header("1) Eingabe")
col_shop, col_manual = st.columns([1, 1], gap="large")

with col_shop:
    st.subheader("Aus Shopify")
    if not shop:
        st.info("Shopify-Zugang ist noch nicht eingerichtet – du kannst trotzdem rechts manuell arbeiten.")
    else:
        b1, b2 = st.columns([1, 1])
        with b1:
            if st.button("Offene Bestellungen laden", width="stretch"):
                try:
                    with st.spinner("Lade offene Bestellungen ..."):
                        st.session_state.va_orders = shop.list_orders(only_open=True, limit=40)
                except Exception as e:
                    st.error(f"Shopify: {e}")
        with b2:
            show_all = st.toggle("auch erledigte", value=False, key="va_show_all")
            if show_all and st.button("Alle letzten 40 laden", width="stretch"):
                try:
                    st.session_state.va_orders = shop.list_orders(only_open=False, limit=40)
                except Exception as e:
                    st.error(f"Shopify: {e}")

        orders = st.session_state.va_orders or []
        if orders:
            labels = [core.order_summary_line(o) for o in orders]
            pick = st.selectbox("Bestellung auswählen", options=list(range(len(orders))), format_func=lambda i: labels[i], key="va_pick")
            if st.button("Diese Bestellung übernehmen", type="primary", width="stretch"):
                st.session_state.va_raw_order = orders[int(pick)]
                st.session_state.va_item_index = 0
                st.session_state.va_result = None
                st.rerun()

        num_col, btn_col = st.columns([2, 1])
        with num_col:
            num = st.text_input("…oder Bestellnummer", placeholder="z. B. 1270", key="va_num")
        with btn_col:
            st.write("")
            st.write("")
            if st.button("Holen", width="stretch"):
                try:
                    with st.spinner("Hole Bestellung ..."):
                        o = shop.get_order(num)
                    if not o:
                        st.error(f"Bestellung {num} nicht gefunden (ohne Zusatzrecht sieht die App nur die letzten 60 Tage).")
                    else:
                        st.session_state.va_raw_order = o
                        st.session_state.va_item_index = 0
                        st.session_state.va_result = None
                        st.rerun()
                except Exception as e:
                    st.error(f"Shopify: {e}")

    raw = st.session_state.va_raw_order
    if raw:
        od = core.extract_order(raw, int(st.session_state.va_item_index or 0))
        if od["anzahl_comic_artikel"] > 1:
            st.session_state.va_item_index = st.selectbox(
                "Mehrere Comics in der Bestellung – welcher?",
                options=list(range(od["anzahl_comic_artikel"])),
                format_func=lambda i: f"Comic {i + 1}",
                key="va_item_pick",
            )
            od = core.extract_order(raw, int(st.session_state.va_item_index))
        with st.container(border=True):
            st.markdown(f"**{od['bestellname']}** · {od['artikel']} · {od['variante']}")
            st.caption(
                f"Seiten: {od['seiten'] or '?'} · weitere Personen: {od['weitere_personen'] or '?'} · Fotos: {len(od['uploads'])}"
                + (f" · Zusatz: {', '.join(od['zusatzartikel'])}" if od["zusatzartikel"] else "")
            )
            for key, label in (
                ("hauptcharakter", "Hauptcharakter"),
                ("genre", "Genre"),
                ("titel", "Titel"),
                ("storyline", "Storyline"),
                ("bildbeschreibung", "Bildbeschreibung"),
            ):
                if od["felder"].get(key):
                    st.markdown(f"**{label}:** {od['felder'][key]}")
            for k, v in od["weitere_felder"].items():
                st.markdown(f"**{k}:** {v}")
            if not od["felder"] and not od["uploads"]:
                st.warning("Keine Angaben in der Bestellung – bitte rechts Mail/Dateien hinzufügen.")
            if od["uploads"]:
                thumbs = st.columns(min(5, len(od["uploads"])))
                for i, (n, url) in enumerate(od["uploads"]):
                    with thumbs[i % len(thumbs)]:
                        if url.lower().endswith((".heic", ".heif")):
                            st.caption(f"Upload {n}: HEIC (Vorschau nach Analyse)")
                        else:
                            st.image(url, caption=f"Upload {n}", width=110)

with col_manual:
    st.subheader("Manuell ergänzen")
    st.text_input("Bestellnummer (falls ohne Shopify)", key="va_manual_number", placeholder="z. B. 1271")
    st.text_area(
        "Mail-Text / Notizen des Kunden",
        key="va_manual_text",
        height=170,
        placeholder="Mail-Inhalt hier einfügen …",
    )
    st.file_uploader(
        "Dateien: Fotos, Screenshots, PDF, Word, .eml, .txt",
        type=["jpg", "jpeg", "png", "webp", "heic", "heif", "pdf", "docx", "eml", "txt"],
        accept_multiple_files=True,
        key=f"va_uploads_{st.session_state.va_upload_gen}",
    )

# ---------------------------------------------------------------------------
# Analyse
# ---------------------------------------------------------------------------


def run_analysis() -> None:
    if not kiclient:
        st.error("OPENROUTER_API_KEY fehlt in den Secrets.")
        return
    raw_order = st.session_state.va_raw_order
    order = core.extract_order(raw_order, int(st.session_state.va_item_index or 0)) if raw_order else None
    uploads = st.session_state.get(f"va_uploads_{st.session_state.va_upload_gen}") or []
    manual_text = (st.session_state.get("va_manual_text") or "").strip()
    if not order and not uploads and not manual_text:
        st.error("Bitte eine Bestellung holen oder Mail-Text/Dateien hinzufügen.")
        return

    status = st.status("Vorarbeit läuft …", expanded=True)
    photos: List[core.Photo] = []
    texts: List[core.TextSource] = []
    files: List[str] = []
    counter = [0]

    def next_id() -> str:
        counter[0] += 1
        return f"P{counter[0]}"

    # Shopify-Fotos laden
    if order and order["uploads"]:
        status.write(f"Lade {len(order['uploads'])} Fotos aus der Bestellung …")
        jobs = [(next_id(), f"Upload {n}", url) for n, url in order["uploads"]]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            photos.extend(ex.map(lambda j: core.download_photo(*j), jobs))

    # Manuelle Dateien
    if manual_text:
        texts.append(core.TextSource("Mail-Text (manuell eingefügt)", manual_text))
    for up in uploads:
        files.append(up.name)
        try:
            t2, imgs = core.extract_file(up.name, up.getvalue())
        except Exception as e:
            status.write(f"⚠️ {up.name}: {e}")
            continue
        texts.extend(t2)
        for herkunft, b in imgs:
            photos.append(core.make_photo(next_id(), herkunft, b))
    if files:
        status.write(f"{len(files)} Datei(en) gelesen.")

    # KI: Fotos
    good = [p for p in photos if p.jpeg]
    if good:
        status.write(f"Analysiere {len(good)} Bild(er): Personen, Alter, Screenshots …")
        core.analyse_photos(kiclient, photos, progress=lambda m: status.write(m))

    # KI: Story
    all_texts = core.collect_texts(order, texts, photos)
    status.write("Analysiere Story, Figuren und Foto-Zuordnung …")
    try:
        story = core.analyse_story(kiclient, order, all_texts, photos)
    except Exception as e:
        status.update(label="Fehler bei der Story-Analyse", state="error")
        st.error(str(e))
        return
    result = core.build_result(order, story, photos)
    if not result.get("bestellung"):
        result["bestellung"] = core.normalize_order_number(st.session_state.get("va_manual_number")) or ""

    st.session_state.va_order = order
    st.session_state.va_photos = photos
    st.session_state.va_texts = all_texts
    st.session_state.va_files = files
    load_result_into_widgets(result)
    st.session_state.va_result = result
    status.update(label="Vorarbeit fertig – bitte unten prüfen.", state="complete", expanded=False)
    time.sleep(0.3)
    st.rerun()


PERSON_FIELDS = ("name", "rolle", "art", "beziehung", "alter_text", "alter_foto", "beschreibung", "outfit", "foto_position", "aktiv")


def load_result_into_widgets(result: Dict[str, Any]) -> None:
    for k in list(st.session_state.keys()):
        if k.startswith(("p_", "res_")):
            st.session_state.pop(k, None)
    st.session_state.res_nummer = result.get("bestellung", "")
    st.session_state.res_titel = result.get("titel", "")
    st.session_state.res_genre = result.get("genre", "")
    st.session_state.res_seiten = int(result.get("seiten") or 8)
    st.session_state.res_briefing = result.get("briefing", "")
    st.session_state.res_widmung = result.get("widmung", "")
    st.session_state.res_zusatz = "\n".join(result.get("zusatzinfos") or [])
    for p in result.get("personen", []):
        pid = p["id"]
        for f in PERSON_FIELDS:
            val = p.get(f)
            if f in ("alter_text", "alter_foto"):
                val = "" if val is None else str(val)
            if f == "rolle" and val not in ("Hauptfigur", "Nebenfigur", "Bösewicht"):
                val = "Nebenfigur"
            if f == "art" and val not in ("Mensch", "Tier", "Fantasiefigur"):
                val = "Mensch"
            st.session_state[f"p_{pid}_{f}"] = val if val is not None else ""
        fotos = ([p["foto_id"]] if p.get("foto_id") else []) + list(p.get("weitere_foto_ids") or [])
        st.session_state[f"p_{pid}_fotos"] = fotos
        st.session_state[f"p_{pid}_haupt"] = p.get("foto_id") or ""
        st.session_state[f"p_{pid}_ok"] = bool(p.get("hauptfoto_bestaetigt"))


def read_widgets_into_result(result: Dict[str, Any]) -> None:
    result["bestellung"] = core.normalize_order_number(st.session_state.get("res_nummer")) or str(st.session_state.get("res_nummer") or "")
    result["titel"] = st.session_state.get("res_titel", "")
    if result.get("titel_ist_vorschlag") and st.session_state.get("res_titel_ok"):
        result["titel_ist_vorschlag"] = False
    result["genre"] = st.session_state.get("res_genre", "")
    result["seiten"] = int(st.session_state.get("res_seiten") or 8)
    result["briefing"] = st.session_state.get("res_briefing", "")
    result["widmung"] = st.session_state.get("res_widmung", "")
    result["zusatzinfos"] = [z.strip() for z in (st.session_state.get("res_zusatz") or "").splitlines() if z.strip()]
    for p in result.get("personen", []):
        pid = p["id"]
        for f in PERSON_FIELDS:
            key = f"p_{pid}_{f}"
            if key not in st.session_state:
                continue
            val = st.session_state[key]
            if f in ("alter_text", "alter_foto"):
                val = core._num(val)
            p[f] = val
        # Fotos dieser Person und Hauptfoto
        if f"p_{pid}_fotos" in st.session_state:
            alt = ([p["foto_id"]] if p.get("foto_id") else []) + list(p.get("weitere_foto_ids") or [])
            fotos = [x for x in st.session_state.get(f"p_{pid}_fotos") or [] if x]
            haupt = st.session_state.get(f"p_{pid}_haupt") or ""
            if haupt not in fotos:
                haupt = p.get("foto_id") if p.get("foto_id") in fotos else (fotos[0] if fotos else "")
            if set(fotos) != set(alt):  # manuell geändert -> Zuordnung gilt als geprüft
                p["zuordnung_sicherheit"] = "manuell" if fotos else "kein_foto"
            if haupt != p.get("foto_id"):
                p["hauptfoto_bestaetigt"] = True
                pos = (p.get("foto_positionen") or {}).get(haupt)
                if pos is not None:
                    p["foto_position"] = pos
                    st.session_state[f"p_{pid}_foto_position_next"] = pos
            if st.session_state.get(f"p_{pid}_ok"):
                p["hauptfoto_bestaetigt"] = True
            p["foto_id"] = haupt
            p["weitere_foto_ids"] = [x for x in fotos if x != haupt]
        # Zuschnitt des Hauptfotos (nur diese Figur im Bild)
        hid = p.get("foto_id") or ""
        if hid and f"p_{pid}_crop_on_{hid}" in st.session_state:
            crops = dict(p.get("zuschnitte") or {})
            if st.session_state.get(f"p_{pid}_crop_on_{hid}"):
                cx = st.session_state.get(f"p_{pid}_cx_{hid}") or (0, 100)
                cy = st.session_state.get(f"p_{pid}_cy_{hid}") or (0, 100)
                box = core.normalize_box([cx[0], cy[0], cx[1], cy[1]])
                if box:
                    crops[hid] = box
            else:
                crops.pop(hid, None)
            p["zuschnitte"] = crops


st.write("")
go = st.button("🔎 Vorarbeit starten (Fotos + Story analysieren)", type="primary", width="stretch")
if go:
    run_analysis()

# ---------------------------------------------------------------------------
# 2) Ergebnis prüfen
# ---------------------------------------------------------------------------

result = st.session_state.va_result
if result:
    photos: List[core.Photo] = st.session_state.va_photos or []
    order = st.session_state.va_order
    read_widgets_into_result(result)
    meldungen = core.check_result(result, order, photos)

    st.divider()
    st.header("2) Prüfen und korrigieren")

    n_err = sum(1 for m in meldungen if m.stufe == "fehler")
    n_warn = sum(1 for m in meldungen if m.stufe == "warnung")
    with st.container(border=True):
        st.subheader(f"Meldungen · {n_err} Fehler · {n_warn} Warnungen")
        if not meldungen:
            st.success("Alles vollständig – keine offenen Punkte.")
        for m in meldungen:
            if m.stufe == "fehler":
                st.error(m.text, icon="🛑")
            elif m.stufe == "warnung":
                st.warning(m.text, icon="⚠️")
            else:
                st.info(m.text, icon="ℹ️")

    # Fotos
    person_photos = [p for p in photos if not p.ist_screenshot]
    if photos:
        st.subheader("Fotos")
        cols = st.columns(5)
        for i, p in enumerate(photos):
            with cols[i % 5]:
                if p.jpeg:
                    st.image(core.to_jpeg(core.load_image_bytes(p.jpeg)[0], 360, 80), width="stretch")
                a = p.analyse or {}
                desc = []
                for x in a.get("personen") or []:
                    desc.append(f"{x.get('position', '')}: {x.get('geschlecht', '')}, ~{x.get('alter_geschaetzt', '?')} J.")
                for x in a.get("tiere") or []:
                    desc.append(f"{x.get('art', '')} {x.get('rasse', '')}".strip())
                label = "Screenshot/Text" if p.ist_screenshot else ("; ".join(desc) or "–")
                st.markdown(
                    f"<div class='va-photo-cap'><b>{p.id}</b> · {p.herkunft}<br>{p.original_breite}×{p.original_hoehe} px"
                    f"{' · ' + p.fehler if p.fehler else ''}<br>{label}</div>",
                    unsafe_allow_html=True,
                )

    # Comic-Daten
    st.subheader("Comic")
    c1, c2, c3, c4 = st.columns([1, 2, 1, 1])
    with c1:
        st.text_input("Bestellnummer", key="res_nummer")
    with c2:
        st.text_input("Titel", key="res_titel")
        if result.get("titel_ist_vorschlag"):
            st.checkbox("Titel-Vorschlag passt", key="res_titel_ok")
    with c3:
        st.text_input("Genre", key="res_genre")
    with c4:
        st.selectbox("Seiten", options=core.COMIC_PAGE_OPTIONS, key="res_seiten")
    st.text_area("Briefing (nur Story-relevantes)", key="res_briefing", height=220)
    w1, w2 = st.columns([1, 1])
    with w1:
        st.text_area("Widmung", key="res_widmung", height=110)
    with w2:
        st.text_area("Zusatzinfos (eine pro Zeile, gehen nicht in die Story)", key="res_zusatz", height=110)

    # Figuren
    st.subheader("Figuren und Foto-Zuordnung")
    photo_options = [p.id for p in person_photos if p.jpeg]
    photo_labels = {p.id: f"{p.id} · {p.herkunft}" for p in person_photos}
    photo_by_id = {p.id: p for p in photos}
    for pers in result.get("personen", []):
        pid = pers["id"]
        sicher = pers.get("zuordnung_sicherheit", "")
        badge = {"hoch": "🟢", "mittel": "🟡", "niedrig": "🔴", "kein_foto": "⚪", "manuell": "🔵"}.get(sicher, "⚪")
        n_fotos = (1 if pers.get("foto_id") else 0) + len(pers.get("weitere_foto_ids") or [])
        foto_badge = f" · 📷 {n_fotos} Fotos" if n_fotos > 1 else ""
        offen = sicher in ("niedrig", "kein_foto") or (n_fotos > 1 and not pers.get("hauptfoto_bestaetigt"))
        with st.expander(f"{badge} {pers.get('name')} · {pers.get('rolle')} · {pers.get('art')}{foto_badge}", expanded=offen):
            a1, a2, a3, a4 = st.columns([2, 1, 1, 1])
            with a1:
                st.text_input("Name", key=f"p_{pid}_name")
            with a2:
                st.selectbox("Rolle", ["Hauptfigur", "Nebenfigur", "Bösewicht"], key=f"p_{pid}_rolle")
            with a3:
                st.selectbox("Art", ["Mensch", "Tier", "Fantasiefigur"], key=f"p_{pid}_art")
            with a4:
                st.checkbox("im Comic", key=f"p_{pid}_aktiv")
            b1, b2, b3 = st.columns([2, 1, 1])
            with b1:
                st.text_input("Beziehung", key=f"p_{pid}_beziehung")
            with b2:
                st.text_input("Alter laut Text", key=f"p_{pid}_alter_text")
            with b3:
                st.text_input("Alter laut Foto (KI)", key=f"p_{pid}_alter_foto")
            # Fotos dieser Person + Hauptfoto (KI-Vorschlag, manuell änderbar)
            st.session_state[f"p_{pid}_fotos"] = [x for x in st.session_state.get(f"p_{pid}_fotos") or [] if x in photo_options]
            st.multiselect(
                "Fotos dieser Person",
                photo_options,
                format_func=lambda x: photo_labels.get(x, x),
                key=f"p_{pid}_fotos",
                help="Alle Fotos, die diese Person zeigen. Unten wählst du, welches die Comic-App als Vorlage nimmt.",
            )
            fotos_sel = list(st.session_state.get(f"p_{pid}_fotos") or [])
            if fotos_sel:
                ki = pers.get("ki_hauptfoto") or ""
                if st.session_state.get(f"p_{pid}_haupt") not in fotos_sel:
                    st.session_state[f"p_{pid}_haupt"] = pers.get("foto_id") if pers.get("foto_id") in fotos_sel else fotos_sel[0]
                tcols = st.columns(min(5, len(fotos_sel)))
                for i, fid in enumerate(fotos_sel):
                    ph = photo_by_id.get(fid)
                    with tcols[i % len(tcols)]:
                        if ph and ph.jpeg:
                            st.image(core.to_jpeg(core.load_image_bytes(ph.jpeg)[0], 300, 80), width="stretch")
                            st.caption(("⭐ KI-Vorschlag · " if fid == ki and len(fotos_sel) > 1 else "") + f"{fid} · " + core.photo_suitability(ph))
                st.radio(
                    "Hauptfoto für den Comic (Vorlage für das Charakter-Sheet)",
                    fotos_sel,
                    format_func=lambda x, ki=ki, n=len(fotos_sel): f"{x}{' ⭐' if x == ki and n > 1 else ''}",
                    horizontal=True,
                    key=f"p_{pid}_haupt",
                )
                if len(fotos_sel) > 1:
                    if pers.get("hauptfoto_grund"):
                        st.caption(f"KI: {pers['hauptfoto_grund']}")
                    st.checkbox("Hauptfoto passt", key=f"p_{pid}_ok")

                # Zuschneiden: maximal eine Person/Figur pro Bild
                hid = st.session_state.get(f"p_{pid}_haupt")
                hph = photo_by_id.get(hid)
                if hph and hph.jpeg:
                    crop = (pers.get("zuschnitte") or {}).get(hid)
                    n_subj = core.subject_count(hph)
                    on_key, cx_key, cy_key = f"p_{pid}_crop_on_{hid}", f"p_{pid}_cx_{hid}", f"p_{pid}_cy_{hid}"
                    if on_key not in st.session_state:
                        st.session_state[on_key] = bool(crop)
                    b0 = crop or [0, 0, 1, 1]
                    cx_default = (int(round(b0[0] * 100)), int(round(b0[2] * 100)))
                    cy_default = (int(round(b0[1] * 100)), int(round(b0[3] * 100)))
                    hint = f" – auf dem Foto sind {n_subj} Personen/Tiere" if n_subj > 1 else ""
                    st.checkbox(f"✂️ Hauptfoto zuschneiden, sodass nur {pers.get('name')} zu sehen ist{hint}", key=on_key)
                    if st.session_state.get(on_key):
                        z1, z2 = st.columns([2, 1])
                        with z1:
                            st.slider("Ausschnitt links → rechts (%)", 0, 100, value=st.session_state.get(cx_key, cx_default), key=cx_key)
                            st.slider("Ausschnitt oben → unten (%)", 0, 100, value=st.session_state.get(cy_key, cy_default), key=cy_key)
                            if crop:
                                st.caption("Vorschlag der KI vorausgefüllt – bei Bedarf anpassen.")
                        with z2:
                            cx = st.session_state.get(cx_key) or cx_default
                            cy = st.session_state.get(cy_key) or cy_default
                            box = core.normalize_box([cx[0], cy[0], cx[1], cy[1]])
                            if box:
                                st.image(core.crop_jpeg(core.to_jpeg(core.load_image_bytes(hph.jpeg)[0], 600, 80), box), caption="Ausschnitt für die Comic-App", width="stretch")
                            else:
                                st.warning("Ausschnitt zu klein.")
            if f"p_{pid}_foto_position_next" in st.session_state:
                st.session_state[f"p_{pid}_foto_position"] = st.session_state.pop(f"p_{pid}_foto_position_next")
            st.text_input("Position auf dem Hauptfoto", key=f"p_{pid}_foto_position", placeholder="z. B. links, mit Brille")
            st.text_area("Beschreibung (Aussehen, Beruf, Hobbys)", key=f"p_{pid}_beschreibung", height=80)
            st.text_input("Outfit (nur wenn vom Kunden vorgegeben – sonst schlägt die Comic-App eins vor)", key=f"p_{pid}_outfit")
            if pers.get("begruendung"):
                st.caption(f"KI-Begründung: {pers['begruendung']}")

    if st.button("➕ Figur hinzufügen"):
        new_id = max([p["id"] for p in result.get("personen", [])] + [0]) + 1
        result.setdefault("personen", []).append(
            {"id": new_id, "aktiv": True, "name": f"Person {new_id}", "rolle": "Nebenfigur", "art": "Mensch", "beziehung": "",
             "alter_text": None, "alter_foto": None, "beschreibung": "", "outfit": "", "foto_id": "", "foto_position": "",
             "weitere_foto_ids": [], "zuordnung_sicherheit": "kein_foto", "begruendung": ""}
        )
        for f in PERSON_FIELDS:
            st.session_state[f"p_{new_id}_{f}"] = {"name": f"Person {new_id}", "rolle": "Nebenfigur", "art": "Mensch", "aktiv": True}.get(f, "")
        st.session_state[f"p_{new_id}_fotos"] = []
        st.session_state[f"p_{new_id}_haupt"] = ""
        st.session_state[f"p_{new_id}_ok"] = False
        st.rerun()

    # Download
    st.divider()
    st.header("3) Vorarbeiter-Datei")
    if n_err:
        st.warning(f"Es sind noch {n_err} Fehler offen. Du kannst die Datei trotzdem herunterladen – die Meldungen stehen mit in der Datei.")
    try:
        fname, zbytes = core.build_zip(
            result, photos, meldungen, order, st.session_state.va_texts or [], st.session_state.va_files or []
        )
        st.download_button(
            f"⬇️ Vorarbeiter-Datei herunterladen ({fname})",
            data=zbytes,
            file_name=fname,
            mime="application/zip",
            type="primary",
            width="stretch",
        )
    except Exception as e:
        st.error(f"Datei konnte nicht erstellt werden: {e}")
    with st.expander("Briefing für die Comic-App (Vorschau)"):
        st.text(core.compose_order_text(result))
