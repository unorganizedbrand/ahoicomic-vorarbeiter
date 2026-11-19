# Ahoi Comic – Vorarbeiter

Separate Streamlit-App, die Comic-Bestellungen für die Comic-App (ComicPro) vorbereitet.

**Ablauf**
1. Bestellung aus Shopify holen (Liste offener Bestellungen oder Bestellnummer) – liest Genre, Hauptcharakter,
   Storyline, Titel, Bildbeschreibung und die Fotos aus `Upload Add To Cart_1` … `_5`.
2. Optional manuell ergänzen: Mail-Text einfügen, Fotos, Screenshots, PDF, Word, `.eml`, `.txt` hochladen.
3. „Vorarbeit starten“: die KI analysiert jedes Foto (Personen, Alter, Tiere, Qualität, Screenshot-Text) und die Story
   (Story-relevantes vs. Zusatzinfos, Figurenliste, Foto-Zuordnung, Alter Text vs. Foto).
4. Meldungen prüfen (Foto ohne Person, Person ohne Foto, Gruppenfoto, Alter passt nicht, Personenzahl ≠ Buchung,
   Foto zu klein …), korrigieren.
5. `AhoiComic_<Nr>_vorarbeiter.zip` herunterladen und in der Comic-App unter „Vorarbeiter-Datei hochladen“ einlesen.

**Datei-Format** (`vorarbeiter.json`, `format: ahoi-vorarbeiter`, `version: 1`): `bestellung`, `comic` (titel, genre,
seiten, briefing, order_text, widmung), `personen` (name, rolle, art, alter, hinweis, outfit, foto), `zusatzinfos`,
`meldungen`, `fotos`, `quellen`. Fotos liegen unter `fotos/`.

**Shopify-Zugang**: App „Ahoi Vorarbeiter“ im Shopify Dev Dashboard, Rechte `read_orders`, `read_products`,
installiert im Shop *ahoicomic*. Die App holt sich mit Client-ID + Secret selbst alle 24 h einen Zugang
(Client Credentials Grant). Secrets siehe `secrets.example.toml`.
