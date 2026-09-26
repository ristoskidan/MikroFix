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

Celotni preverljivi indeks 25 pogovorov, najdenih formul in dokumentov je v
[`../POGOVORI_RISTOSKI_DAN_INDEKS_2026-09-18.md`](../POGOVORI_RISTOSKI_DAN_INDEKS_2026-09-18.md).
Ločeno pisno pooblastilo je bilo pozneje izdelano in je sledljivo v
`docs/pravni/POOBLASTILO_ZA_PREGLED_IN_PREVZEM_NAPRAV.docx`; njegov generator
je `tools/build_pooblastilo_docx.py`. Pooblastilo ne prenese pravice do končne
odobritve ali sklenitve pogodbe in ne dovoljuje plačila.

## 4. Potrjene odločitve 26. 9. 2026

- vsak sken ali izbor artikla ustvari novo kronološko vrstico `x1`; vrstice se
  ne združujejo in ne brišejo;
- pred izdajo je količina neposredno spremenljiva na pozitivno ali negativno
  celo število, nikoli na `0`; sprememba ima notranjo revizijsko sled in ne
  ustvari avtomatske korekcijske vrstice;
- ročna negativna vrstica se poveže s konkretno neizravnano pozitivno vrstico
  in prevzame njen cenovni, davčni ter rabatni posnetek;
- ob enem kandidatu ga sistem jasno izbere, ob več kandidatih mora izbrati
  uporabnik; sistem ne ugiba;
- izdani račun ohrani vse končne vrstice kronološko in je nespremenljiv;
- vsak neprazen neposredni ali servisni račun se lahko izravna na `0,00 EUR` in
  izda; negativni račun ni dovoljen;
- po izdaji je dovoljen samo vezan storno, njegov saldo pa se uporabi na istem
  izvornem računu;
- minimum artikla je dokumentirani skupni strošek + `5 %`, zaokrožen navzgor na
  naslednjih `5 EUR`; `L0` je ločen različicen minimum dela, cena pa se v
  Blagajni spreminja samo prek rabata;
- Blagajna sprejme reklamacijo in jo poveže z računom, Office jo administrira,
  Servis pa opravi tehnično delo;
- pooblaščenka sme pregledati in prevzeti napravo ter pripraviti samo osnutek
  pogodbe. Daniel Ristoski jo dokončno odobri in sklene ter izvede plačilo.

## 5. Tri odprte odločitve

1. **Aktivni cenovni profil in `L0`:** določiti začetno vrednost `L0`, lastnika
   cenovnega profila, njegovo različico ter postopek potrditve spremembe.
2. **Saldo vezanega storna:** določiti podatkovni model in uporabniški tok, s
   katerima se saldo uporabi na istem izvornem računu brez izgube kronološke in
   davčne sledi.
3. **SIGEN-CA:** pred produkcijo pri veljavnem pravnem oziroma uradnem viru
   potrditi dovoljeni tehnični in organizacijski način uporabe namenskega
   digitalnega potrdila v centralizirani FURS arhitekturi.

Vse druge zgoraj zapisane točke so potrjeno ciljno poslovno stanje. Njihova
neizvedenost v trenutni kodi je izvedbena vrzel, ne odprta poslovna odločitev.

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
