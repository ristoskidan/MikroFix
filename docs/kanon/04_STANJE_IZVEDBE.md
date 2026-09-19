# MikroFIX — stanje izvedbe

Datum dokumenta: 19. 9. 2026

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
odjavi pozabi tudi ob izpadu povezave. Produkcijska zagona zdaj odpreta namenski
centralni prijavni ovoj ter ne kličeta več starega lokalnega Office programa.
Vsaka aplikacija zahteva lastno registrirano postajo in po uspešni prijavi
uporablja samo centralni API. Prisilna menjava prvega PIN-a je vključena.

Ta priklop je preverjen z avtomatiziranimi testi konfiguracije, dovoljenj in
zagonske usmeritve, še ne pa s celotnim ročnim preizkusom proti dejansko
registriranima postajama in nameščenemu centralnemu strežniku. Office in
Blagajna zato še nista razglašena za produkcijsko pripravljena.

Potekla ali centralno preklicana seja je ločena od poslovne zavrnitve
dovoljenja. HTTP 401 sproži enkraten zaklep samostojnega Office oziroma
Blagajne; uporabnik mora program znova zagnati in se ponovno prijaviti. Če se je
401 zgodil med zapisovalnim ukazom, njegov že shranjeni idempotentni zapis
ostane v lokalnem dnevniku za ponovitev po novi prijavi. HTTP 403 aplikacije ne
odjavi samodejno, ker lahko pomeni zavrnitev posamezne poslovne pravice.

Servisni interval pri dejanskem zagonu shrani stenski začetek, monotoni začetek
in identiteto zagona operacijskega sistema. Med istim zagonom računalnika se
trajanje zato meri z monotono uro in ostane pravilno tudi po ponovnem zagonu
aplikacije ali premiku sistemske ure. Zaključeni interval ohrani izračunane
sekunde. Gumb za tehnični zaključek ob kliku zajame en stenski in monotoni
marker ter isti marker uporabi za predhodni izračun in končno transakcijo; čas
zato ne vključuje trajanja varnostne kopije ali potrjevanja zaključka. Po
ponovnem zagonu računalnika se za še odprti interval uporabi trajni
stenski zapis; če bi dal negativen rezultat, se izračun izrecno blokira namesto
tihega popravka na nič. Strežniško označevanje nenavadnega razmerja med
stenskim in izmerjenim časom še ni izvedeno.

Centralna pogodba, strežnik in Blagajna podpirajo način `ZERO_BALANCE` samo pri
izdaji centralnega servisnega osnutka. Blagajnik ga izrecno izbere v plačilnem
koraku. Predogled ohrani vse pozitivne zaklenjene vrstice v prvotnem vrstnem
redu in za njimi pokaže enake negativne izravnalne vrstice v istem vrstnem
redu; seštevek je `0,00 EUR`, plačila in UPN ni. Izbira se hrani v lokalnem
varnostnem osnutku ter je del kontrolnega odtisa predogleda. Drugi F10 pošlje
samo `payments=[]` in `settlement_mode=ZERO_BALANCE`, brez odjemalskih vrstic;
strežnik končni posnetek sestavi iz svojega zaklenjenega osnutka. Neposredna
prodaja tega načina nima. Ciljani testi preverjajo vrstni red, predogled PDF,
prazna plačila, dejanski runtime klic in prepoved pri neposredni prodaji. Ročni
preizkus celotnega zaslona še ostaja potreben.

Centralni PostgreSQL del ima od 19. 9. 2026 trajno pripravljalno pregrado za
polno inventuro. Začetek seje `PREPARING` in njen preklic sta idempotentna,
preživita ponovni zagon strežnika in sta vezana na globalno transakcijsko
ključavnico. Med pripravo so novi servisni, prodajni, zalogovni in dokumentni
tokovi zavrnjeni; ostanejo samo izrecno določeni zaključni ukazi za zapiranje
že odprtega dneva. Vsaka registrirana Service postaja lahko za konkretno sejo
shrani podpisano poročilo o checkpointu, lokalni zapisovalni blokadi ter ločenih
`pending`, `uncertain` in konfliktnih ukazih. PostgreSQL integracijski testi
potrjujejo trajno blokado, enak rezultat ponovitve, preklic in ohranitev
blokirajočega poročila postaje.

To je samo varen temelj 8. sklopa. Prehod v `ACTIVE` še ni dovoljen, dokler niso
izvedeni centralni pregled vseh odprtih dokumentov in vseh aktivnih Service
postaj, inventurne vrstice, pravila manjkov/viškov, zaključek ter nova
inventurna epoha, ki zavrne zastarele offline ukaze.

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

## Lokalna pregledovalca

Lokalna modela ob posvetu prejmeta celoten urejeni kanonski sklop 01–06;
`06_LOKALNI_AGENTI.md` dodatno določa njune meje. Modela sta samo pregledovalca:
nimata izvajalnih pravic,
ne določata poslovnih pravil in njun odgovor ni dokaz izvedbe. Qwen 3.5 9B je
predviden za običajni tehnični pregled, GPT-OSS 20B pa za občasno drugo
skeptično mnenje. Codex ostane nadzornik, doda najmanjši potreben nalogi
specifičen izsek ter sam preveri rezultat.
