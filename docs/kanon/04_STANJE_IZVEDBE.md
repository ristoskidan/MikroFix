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

## Preverjeno 14. 9. 2026 po konsolidaciji

- Dodani so trije ločeni produkcijski procesni vhodi:
  `zazeni_mikrofix_service.py`, `zazeni_mikrofix_office.py` in
  `zazeni_mikrofix_blagajna.py`.
- Vsak vhod zažene svoj proces; Servis uporablja namenski način
  `--service-only`, Office in Blagajna pa se zaženeta samostojno in zato ne
  podedujeta seje Servisa.
- Samostojni Servis po prijavi odpre servisni delovni prostor, vsebuje tudi
  Odkup, ne kaže skupnega menija in ne ponuja gumba za odpiranje Blagajne.
- Neposredno ustvarjanje reklamacije iz izbranega servisa je v samostojnem
  Servisu skrito, dokler ni izveden novi tok, ki se začne iz računa.
- Ciljani testi ločenih vhodov, sveže prijave, odjave, servisnega časa in
  tehničnega zaključka so uspešni.

To še ni končna razdelitev podatkovnih poti. Samostojna Office in Blagajna
trenutno še vsebujeta stari lokalni način; njuno obvezno centralno prijavo in
API-only zapisovanje je treba preveriti ter dokončati pred produkcijo.

Pri identifikaciji nepodprte naprave je obstoječi preprosti fizični prepis
potrjen kot zadosten. Dodatno razčlenjevanje vira odčitka se ne uvaja.

Offline centralni uporabniški posnetek je omejen na sedem dni. Po izteku in ob
premiku sistemske ure pred zadnjo centralno prijavo se dostop varno zavrne.
Vsaka uspešna offline prijava pokaže opozorilo in natančen čas izteka.

Za samostojna Office in Blagajno je izdelano ter testirano skupno centralno
identitetno jedro. Preveri uporabnika, vrsto programa, registrirano delovno
postajo in zahtevano dovoljenje; dostopni žeton hrani samo v pomnilniku in ga ob
odjavi pozabi tudi ob izpadu povezave. Produkcijska zagonska mostova sprejmeta
samo tako preverjeno sejo, skupni razvojni žeton pa v produkciji še naprej
zavrneta. Prijavni zaslon in priklop samostojnih uporabniških ovojev še nista
dokončana, zato Office in Blagajna s tem še nista produkcijsko pripravljena.

## Naslednja revizija kode

Kodo je treba primerjati po tem vrstnem redu:

1. zagoni treh programov in prijavne seje;
2. servisni čas in lokalna trajnost;
3. identifikacija podprtih in nepodprtih naprav;
4. tehnični zaključek brez odpiranja Blagajne;
5. enkratna poraba delov in sinhronizacijski konflikt;
6. zaklenjen servisni osnutek ter ločena neposredna prodaja;
7. ničelni servisni račun z izravnalnimi negativnimi vrsticami;
8. polna inventura in stanje vseh Service postaj;
9. reklamacija, ki se vedno začne iz računa in ohrani povezave;
10. centralne priloge, garancijsko vezana retencija in nova strategija
    varnostnih kopij;
11. FURS matrika, novi interni akt in produkcijski sprejemni test.

## Produkcijski status

Produkcijska izdaja ostaja blokirana, dokler niso opravljeni kanonični sprejemni
testi in ločeno potrjene pravno-davčne točke iz zgodovine odločitev.
