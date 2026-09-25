<!-- GENERIRANO iz docs/kanon/00_KAZALO.md; ne urejaj ročno. -->

# MikroFIX — kanonično kazalo

Status: **glavna izhodiščna točka od 14. 9. 2026**

## Namen

Ta mapa ločuje veljavna pravila, ciljno arhitekturo, sprejemne teste, dejansko
stanje izvedbe in zgodovino odločitev. Stari pogovori, obstoječa koda in prejšnje
specifikacije so pomemben kontekst, vendar nove poti ne prepišejo samodejno.
Poslovno pravilo iz starega pogovora se v novi arhitekturi izvede varno in
sledljivo, ne z obnovitvijo stare tehnične zasnove.

## Vrstni red veljavnosti

Ob nasprotju velja naslednji vrstni red:

1. zadnja izrecna odločitev uporabnika, vpisana v `01_POSLOVNA_PRAVILA.md`;
2. `01_POSLOVNA_PRAVILA.md`;
3. `02_CILJNA_ARHITEKTURA.md`;
4. `03_SPREJEMNI_TESTI.md`;
5. `04_STANJE_IZVEDBE.md`;
6. `05_ZGODOVINA_ODLOCITEV.md` in starejši dokumenti.

Koda ni dokaz pravilnega poslovnega pravila. Če je koda v nasprotju s prvima
dvema dokumentoma, je to odstopanje izvedbe.

## Dokumenti

- [`01_POSLOVNA_PRAVILA.md`](01_POSLOVNA_PRAVILA.md) — kaj mora sistem poslovno
  zagotavljati;
- [`02_CILJNA_ARHITEKTURA.md`](02_CILJNA_ARHITEKTURA.md) — kako se pravila varno
  izvedejo v treh programih;
- [`03_SPREJEMNI_TESTI.md`](03_SPREJEMNI_TESTI.md) — dokaz, da izvedba spoštuje
  pravila;
- [`04_STANJE_IZVEDBE.md`](04_STANJE_IZVEDBE.md) — kaj je dejansko preverjeno v
  kodi in kaj še ni;
- [`05_ZGODOVINA_ODLOCITEV.md`](05_ZGODOVINA_ODLOCITEV.md) — izvor, nadomeščene
  odločitve in nerešene pravno-davčne točke.
- [`06_LOKALNI_AGENTI.md`](06_LOKALNI_AGENTI.md) — meja med poslovnim kanonom
  in vlogovno neodvisno lokalno agentno verigo; ne določa novih poslovnih pravil.

## Obvezno pravilo za nadaljnje delo

Pred spremembo kode se določi prizadeto poslovno pravilo in sprejemni test.
Če pravilo manjka ali sta dva zapisa v konfliktu, se najprej popravi ta
dokumentacija. Stare oznake `POTRJENO` v zgodovinskem Manifestu same po sebi ne
prevladajo nad to mapo.
