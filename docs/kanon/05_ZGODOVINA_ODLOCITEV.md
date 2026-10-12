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

## 5. Evidenca odprtih in razrešenih odločitev

1. **Aktivni cenovni profil in `L0`:** določiti začetno vrednost `L0`, lastnika
   cenovnega profila, njegovo različico ter postopek potrditve spremembe.
2. **Saldo vezanega storna:** določiti podatkovni model in uporabniški tok, s
   katerima se saldo uporabi na istem izvornem računu brez izgube kronološke in
   davčne sledi.
3. **SIGEN-CA:** pri veljavnem pravnem oziroma uradnem viru potrditi dovoljeni
   način uporabe Danielovega osebnega certifikata za podpis odkupne pogodbe
   oziroma drugega poslovnega dokumenta. To vprašanje je ločeno od namenskega
   digitalnega potrdila za davčno potrjevanje računov in centralizirano FURS
   komunikacijo.
4. **Trenutni DDV-status in datum prehoda:** iz preverljivega podatka FURS ali
   eDavkov ugotoviti režim domače prodaje, morebitni omejeni obseg identifikacije
   in datum začetka. Vnaprej določen datum prehoda ni nujen, ker ga lahko sproži
   tudi promet ali prostovoljna vključitev.
5. **Cenovna politika ob prehodu v DDV — RAZREŠENO 11. 10. 2026:** velja
   kombinacija po skupinah. Terenske storitve in delo ohranijo potrjene končne
   cene, pri rezervnih delih in paketnih popravilih pa se nova končna cena
   potrdi po preverjanju marže glede na novo nabavno stroškovno osnovo.
6. **Vrednotenje zaloge:** z računovodjo potrditi FIFO ali tehtano povprečje in
   metodo razporejanja odvisnih nabavnih stroškov.
7. **B2G obseg:** potrditi, ali MikroFIX izdaja račune proračunskim uporabnikom;
   če jih, je UJP e-račun produkcijska blokada.
8. **Ničelni račun:** računovodsko in davčno potrditi uporabo negativnih količin
   za garancijsko, brezplačno, zavrnjeno in drugo nezaračunano obravnavo ali
   določiti pravilen nadomestni prikaz po scenariju.
9. **Delni popravki in plačila:** določiti uporabniški tok delnega dobropisa,
   delnega vračila, več delnih plačil in kombiniranega plačila.
10. **Retencija in obnova:** potrditi odgovorne osebe, RPO/RTO, lokacije kopij,
    zakonske roke po vrsti dokumenta in pogostost dokazne testne obnove.

Točka 5 je razrešena in je del kanona. Preostale oštevilčene točke v tem
poglavju ostajajo odprte. Vse druge pred tem poglavjem zapisane točke so
potrjeno ciljno poslovno stanje. Njihova neizvedenost v trenutni kodi je
izvedbena vrzel, ne odprta poslovna odločitev.

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

## 7. Pravno-davčna uskladitev 11. 10. 2026

Novejša pravna preverba nadomešča prejšnji dvorežimski model. Potrjena je ciljna
podpora notranjim statusom `NON_VAT`, `VAT_ATYPICAL` in `VAT_REGISTERED` ter
ločitev režima domače prodaje od obsega DDV-identifikacije. `VAT_ATYPICAL` je
notranje ime za omejeno identifikacijo, ki jo FURS na seznamu označuje s `P`;
ne pomeni samodejnega obračuna DDV od domačih dobav ali splošne pravice do
odbitka. Podpora vsem statusom ne odloča, kateri trenutno velja za MikroFIX;
ta podatek in njegov začetek ostajata odprta odločitev iz 5. poglavja.

Potrjeno je tudi:

- ob prehodu v `VAT_REGISTERED` velja kombinirana cenovna politika: terenske
  storitve in delo ohranijo potrjene končne cene, rezervni deli in paketna
  popravila pa dobijo vnaprej potrjen, časovno veljaven cenik po preverjanju
  marže glede na novo nabavno stroškovno osnovo;
- prehod davčnega profila ne ustvari nove zaloge in ne spreminja zgodovinskih
  računov;
- pragova 60.000 EUR in 66.000 EUR se spremljata po koledarskem letu po
  zakonski opredelitvi prometa; pri preseganju 66.000 EUR je obdavčena že
  transakcija, s katero je prag presežen;
- datum izdaje sam ne določa davčne obravnave prehoda; hranijo se datum dobave
  oziroma opravljene storitve in predplačila, uporabi pa se 161. člen PZDDV-1;
- morebitni odbitek DDV od zaloge, nabavljene pred identifikacijo, ni samodejen,
  temveč ločena preverjena računovodska odločitev po konkretnih listinah;
- izdana vrstica ohrani davčni posnetek, popravek pa se sklicuje na izvor;
- kartica se za ZDavPR obravnava kot gotovinsko plačilo, neposredno nakazilo na
  TRR pa praviloma ne;
- interni stroškovni minimum ni zakonska prejšnja cena pri objavi znižanja;
- splošna domača obveznost B2B e-računov se začne 1. 1. 2028, medtem ko B2G
  obveznost že velja;
- PDF sam zase ni strukturirani e-račun in e-račun ne nadomesti morebitne
  obveznosti davčnega potrjevanja.

## 8. Sistem vizualnih identitet 11. 10. 2026

Lastnik je potrdil pomensko razmejitev imen: `MikroFIX` označuje programsko
družino in njene tri izdelke, `MikroFix` javno identiteto terenskega servisa,
polno registrirano ime podjetja pa pravno identifikacijo. Velikost črk zato ni
le slogovna različica.

Potrjeno je tudi skupno pikselizirano oblikovno načelo s poudarkom na delu
imena `FIX`. Načelo povezuje obe identitetni poti, vendar ne določa enakega
logotipa ali enake uporabe vseh elementov. Končni logotipi, pisave, motiv
razbitega stekla in stopnja vizualne sorodnosti ostajajo odprte oblikovalske
odločitve. Operativni zemljevid vodi Piksi v
`/home/mikrofix/Codex_Agenti/PIKSI.md`.

## 9. Barvna paleta in spletna vloga 12. 10. 2026

Lastnik je kot osnovno skupno paleto potrdil sivo `#929CAA`, modro `#4EA3E3`,
turkizno `#35C9C1`, rumeno `#F2B84B` in rdečo `#FF6B67`. Potrditev nadomešča
prejšnjo navedbo, da natančne barve še niso izbrane; način uporabe posameznih
barv ostaja predmet konkretnih oblikovalskih pravil.

V aktivno skupino je vključen Svit kot skrbnik javne spletne prisotnosti
`MikroFix`, gostovanja in nadzorovanega postopka objave. Njegova operativna meja
je zapisana v `/home/mikrofix/Codex_Agenti/SVIT.md`; vključitev ne daje samodejnega
dovoljenja za produkcijske ali varnostno občutljive posege.
