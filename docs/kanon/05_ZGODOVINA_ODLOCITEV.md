# MikroFIX — zgodovina odločitev in odprte blokade

Ta dokument hrani izvor in nadomeščene smeri. Ne uporablja se samostojno za
implementacijo.

## 1. Odločitve, konsolidirane 14. 9. 2026

- cilj so trije samostojni programi, ne en monolitni uporabniški ovoj;
- več programov lahko teče na istem računalniku;
- Odkup in obnova ostaneta v programu Servis;
- vsak zagon vsakega programa zahteva prijavo, izhod pa odjavo;
- polna inventura zahteva zaprto prodajo, vse dokumente zaključene in knjižene
  ter vse Service postaje sinhronizirane;
- garancijski list vsebuje polni IMEI, drugi javni dokumenti ga lahko maskirajo;
- po končni matriki naprav se po potrebi spremeni prijava FURS in izdela nov
  interni akt;
- VKR je samo skrajna možnost, ko ni druge dovoljene elektronske poti;
- naprave brez podpore MicroDETECT morajo imeti varen ločen sprejemni tok.

## 2. Nadomeščene smeri

Naslednje stare smeri niso več aktivne zahteve:

- en program z glavnim menijem 2 × 2 in eno skupno sejo;
- četrti samostojni modul oziroma program Odkup in obnova;
- samodejni prehod ali ponudba za odprtje Blagajne po zaključku servisa;
- dodatna neposredna prodaja na istem zaklenjenem servisnem računu;
- opustitev servisnega računa samo zato, ker je znesek `0,00 EUR`;
- inventurni višek kot samostojen pozitivni vir zaloge;
- obvezna servisna rezervacija kot nadomestilo za dejansko porabo;
- lokalni podatkovni imenik kot edina varnostna kopija celotnega razdeljenega
  sistema.

## 3. Zgodovinski dokumenti

- `MIKROFIX_v30_MASTER_SPECIFIKACIJA.md` — obsežen dnevnik starih in novih
  odločitev; ni več neposredna aktivna specifikacija;
- `/home/mikrofix/MikroFIX-kanonicni-poslovni-tokovi.md` — neposredni predhodnik
  današnje konsolidacije;
- `/home/mikrofix/MikroFIX-arhitekturni-kontekst.md` — arhitekturna raziskava;
- `/home/mikrofix/MikroFIX-prva-faza-tehnicni-nacrt.md` — prvi tehnični načrt;
- `/home/mikrofix/MikroFIX-stanje-2026-09-11.md` — zgodovinski posnetek izvedbe.

Stari pogovori uporabniškega računa `ristoski.dan` so kontekst poslovnih
odločitev. Dostopni arhiv je bil 14. 9. 2026 pregledan v prijavljenem računu.
Odločitev iz pogovora se pri prenosu v novo arhitekturo presodi glede varnosti,
sledljivosti in nove centralne zapisovalne meje.

## 4. Produkcijske blokade, ki zahtevajo zunanjo potrditev

### FURS in interni akt

Pred produkcijo je treba določiti končno matriko:

- fizična naprava;
- nameščeni program;
- dejanski poslovni prostor `MFD` ali `MFT`;
- FURS oznaka elektronske naprave;
- uporabljeno številčno zaporedje.

Po potrebi se spremeni prijava FURS in izdela nov interni akt. Sedanja podpisana
različica 1.2 ostane zgodovinski veljavni dokument za doslej prijavljeno
postavitev in se je ne prepisuje.

### Ničelni, garancijski in brezplačni račun

Poslovno je odločeno, da se vsak servis zaključi z računom in da ničelni račun
ohrani pozitivne obračunske vrstice ter enake negativne izravnalne vrstice.
Pred produkcijo je treba pri računovodji oziroma uradnem viru potrditi samo še
davčno/računovodsko obravnavo tako oblikovanega dokumenta.

### Garancija in neskladnost

Pred produkcijo se ponovno preverijo trenutno veljavni roki, začetek teka z
dobavo, obvezna vsebina garancijskega lista, razmerje med komercialno garancijo
in zakonsko neskladnostjo ter pravila za rabljeno blago.

### VKR in izredna izdaja

Pred produkcijo se uradno preveri razmejitev med začasno prekinjeno povezavo,
nedelovanjem posameznega odjemalca, nedosegljivim centralnim strežnikom in
primerom, ko je dejansko treba uporabiti VKR.

## 5. Odprte izvedbene podrobnosti

- podrobnosti uporabniškega toka reklamacije po obvezni izbiri računa;
- dokončna FURS matrika delovnih postaj in programov.

## 6. Dodatno potrjeno 14. 9. 2026

- ničelni servisni račun vsebuje pozitivne obračunske vrstice in enake negativne
  izravnalne vrstice; negativne vrstice ne vračajo zaloge;
- začasna offline oznaka je
  `MF-OFF-<POSTAJA>-<LETO>-<ZAPOREDJE>`, centralna številka pa se dodeli ob
  sinhronizaciji;
- servisne fotografije se hranijo najmanj do poteka najdaljše pripadajoče
  garancije in dlje samo ob veljavnem zadržanju;
- reklamacija se vedno odpre iz izdanega računa; račun ohrani povezavo s
  servisom in, kadar obstaja, garancijskim listom.
- offline uporabniški posnetek velja največ sedem dni od zadnje centralne
  prijave; vsaka offline prijava mora prikazati opozorilo in čas izteka;
- obstoječi preprosti fizični prepis identitete nepodprte naprave je zadosten;
  dodatnih polj za razčlenjevanje vira odčitka se ne uvaja.
