# MikroFIX — stanje izvedbe

Datum dokumenta: 14. 9. 2026

## Namen

Ta dokument ne določa poslovnih pravil. Pove samo, kaj je bilo dejansko
preverjeno v kodi. Oznaka `izvedeno` brez testa ne pomeni `sprejeto`.

## Trenutna varna ugotovitev

- Razvoj poteka v Git-repozitoriju `MikroFIX_Service_v30_R9_LINUX_DEV`.
- Stari monolit in dosedanja v30 izvedba vsebujeta uporabne preverjene funkcije,
  vendar uporabniški ovoj še izvira iz modela ene aplikacije.
- Centralni API, PostgreSQL razvojna smer, idempotentni ukazi, Service outbox,
  zalogovni dogodki in centralna izdaja so bili izdelovani in testirani po
  posameznih sklopih.
- Zadnji predhodni pregled stanja je v datoteki
  `/home/mikrofix/MikroFIX-stanje-2026-09-11.md`.
- Današnja kanonična konsolidacija še ni celovita ponovna revizija vse kode in
  vseh testov. Zato noben spodnji produkcijsko občutljiv sklop ni razglašen za
  pripravljen samo na podlagi starega poročila.

## Naslednja revizija kode

Kodo je treba primerjati po tem vrstnem redu:

1. zagoni treh programov in prijavne seje;
2. servisni čas in lokalna trajnost;
3. identifikacija podprtih in nepodprtih naprav;
4. tehnični zaključek brez odpiranja Blagajne;
5. enkratna poraba delov in sinhronizacijski konflikt;
6. zaklenjen servisni osnutek ter ločena neposredna prodaja;
7. ničelni servisni račun;
8. polna inventura in stanje vseh Service postaj;
9. centralne priloge in nova strategija varnostnih kopij;
10. FURS matrika, novi interni akt in produkcijski sprejemni test.

## Produkcijski status

Produkcijska izdaja ostaja blokirana, dokler niso opravljeni kanonični sprejemni
testi in ločeno potrjene pravno-davčne točke iz zgodovine odločitev.

