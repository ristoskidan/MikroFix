# Kompaktni zemljevid MikroFIX

## Glavni deli

- `mikrofix_v29_core.py`: skupno lokalno jedro, povezava z lokalno SQLite bazo,
  zalogovne strukture in starejše skupne pogodbe.
- `mikrofix_v30_pos.py` in `mikrofix_v30_pos_ui.py`: blagajniška domena in UI.
- `mikrofix_cashier_runtime.py`: pretvorbe med blagajno in centralnimi pogodbami.
- `mikrofix_v30_receipts.py`: izdaja in predstavitev računov.
- `mikrofix_server/`: centralni API, modeli, operacije in trajna strežniška stanja.
- `alembic/versions/`: spremembe centralne podatkovne sheme.
- `tests/`: aplikacijski in domenski regresijski testi.
- `tools/`, `config/local_agents/` in `docs/local_agents/`: orodja ter navodila
  lokalnih coderjev in zgodovinski artefakti prejšnje nadzorne verige.

## Običajna podatkovna pot

Uporabniško dejanje nastane v Servisu ali Blagajni. UI pokliče lokalno domensko
funkcijo, ta izdela pogodbeni payload, runtime adapter ga pošlje centralnemu API,
strežniška operacija pa šele nato spremeni trajni podatkovni model. Vrnjen
slovar, izračun ali UI vrednost zato sam zase ni dokaz trajnega zapisa. Pri
raziskavi je treba slediti poti do SQL operacije, ORM spremembe ali drugega
dejanskega zapisovalnega mehanizma ter preveriti transakcijski rob.

## Testi in kanon

- Poslovna pravila: `docs/kanon/01_POSLOVNA_PRAVILA.md`.
- Ciljna arhitektura: `docs/kanon/02_CILJNA_ARHITEKTURA.md`.
- Sprejemni testi: `docs/kanon/03_SPREJEMNI_TESTI.md`.
- Stanje izvedbe: `docs/kanon/04_STANJE_IZVEDBE.md`.
- Strokovne vloge Codex: `/home/mikrofix/Codex_Agenti/README.md`.
- Lokalni coderji in projektna orodja: `docs/local_agents/README.md`.
- Zgodovinska nadzorovana veriga: `docs/local_agents/PRAVILNIK_VERIGE.md`.

## Nespremenljive raziskovalne omejitve

- Ne ugibaj izbire, kadar obstaja več primernih vrstic; potrebna je uporabnikova
  izrecna izbira.
- Negativna vrstica mora biti vezana na konkretno pozitivno vrstico in prevzeti
  njen cenovni, davčni in rabatni posnetek.
- Poslovnih vrstic se ne briše, izdani račun pa ostane nespremenljiv.
- Račun je lahko ničeln, ne sme pa biti negativen.
- Storno izdanega računa je nov, vezan dokument.
- Dejanska koda in testi imajo prednost pred imeni datotek ali domnevami.
