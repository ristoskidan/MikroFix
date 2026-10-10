# MikroFIX — kanonično kazalo

Status: **glavna izhodiščna točka; pravno-davčno usklajeno 11. 10. 2026**

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

## Uradni pravni in računovodski viri

Pravno-davčna uskladitev je bila opravljena po stanju uradnih virov na
11. 10. 2026. Uporabljeni so zlasti:

- [ZT-1](https://pisrs.si/Pis.web/pregledPredpisa?id=ZAKO5327) — evidence
  trgovskega blaga in podatki, dostopni na prodajnem mestu;
- [SRS 2024](https://www.uradni-list.si/glasilo-uradni-list-rs/vsebina/2023-01-3897/slovenski-racunovodski-standardi-2024)
  — pripoznanje, vrednotenje, oslabitve in odprava zalog;
- [ZGD-1](https://pisrs.si/pregledNpb?idPredpisa=ZAKO4291) — poslovne knjige,
  letna uskladitev in hramba;
- [ZDavP-2](https://pisrs.si/Pis.web/pregledPredpisa?id=ZAKO4703) — izvorni
  elektronski podatki, vse spremembe in dokumentacija sistema;
- [ZDDV-1](https://pisrs.si/Pis.web/pregledPredpisa?id=ZAKO4701) in
  [PZDDV-1](https://pisrs.si/Pis.web/pregledPredpisa?id=PRAV7542) — računi,
  DDV, evidence in hramba;
- [SPOT — DDV](https://spot.gov.si/sl/teme/davek-na-dodano-vrednost-ddv/) —
  pragova 60.000 EUR in 66.000 EUR, DDV-P2 ter prostovoljna vključitev;
- [FURS — identifikacija za DDV](https://www.fu.gov.si/poslovni_dogodki_podjetja/identifikacija_za_ddv)
  in [seznami davčnih zavezancev](https://www.fu.gov.si/e_storitve/seznami_davcnih_zavezancev/)
  — omejena identifikacija, pridobitve in storitve iz EU ter oznaka `P`;
- [FURS — DDV evidence](https://www.fu.gov.si/davki_in_druge_dajatve/podrocja/davek_na_dodano_vrednost_ddv/)
  — elektronski evidenci obračunanega DDV in odbitka DDV;
- [ZDavPR](https://pisrs.si/Pis.web/pregledPredpisa?id=ZAKO7195) in
  [izvedbeni pravilnik](https://pisrs.si/Pis.web/pregledPredpisa?id=PRAV12531)
  — številčenje, ZOI, EOR, izpadi, VKR in poznejše spremembe računov;
- [FURS tehnična dokumentacija](https://edavki.durs.si/EdavkiPortal/OpenPortal/CommonPages/Opdynp/PageD.aspx?category=dpr_teh_spec)
  — aktualne sheme in tehnični protokoli davčnega potrjevanja;
- [ZVPot-1](https://pisrs.si/pregledPredpisa?id=ZAKO7054) in
  [pravilnik o označevanju cen](https://www.uradni-list.si/glasilo-uradni-list-rs/vsebina/2023-01-0160/pravilnik-o-nacinu-oznacevanja-cen-blaga-digitalnih-vsebin-in-storitev-ter-objavi-znizanja-cene-blaga)
  — B2C cene, cena na enoto in objava znižanj;
- [ZPJS](https://www.uradni-list.si/glasilo-uradni-list-rs/vsebina/2025-01-3030/zakon-o-placilnih-in-javnofinancnih-storitvah-zpjs)
  — e-računi za proračunske uporabnike;
- [ZIERDED](https://www.uradni-list.si/glasilo-uradni-list-rs/vsebina/2025-01-3032)
  — domači B2B e-računi od 1. 1. 2028 in soglasje pri B2C.

Prečiščena besedila PISRS so delovni vir; ob pravnem sporu je merodajna objava
posameznega predpisa v Uradnem listu. Pred produkcijsko izdajo se preveri, ali
je bil kateri od navedenih predpisov ali tehničnih dokumentov spremenjen.

## Obvezno pravilo za nadaljnje delo

Pred spremembo kode se določi prizadeto poslovno pravilo in sprejemni test.
Če pravilo manjka ali sta dva zapisa v konfliktu, se najprej popravi ta
dokumentacija. Stare oznake `POTRJENO` v zgodovinskem Manifestu same po sebi ne
prevladajo nad to mapo.
