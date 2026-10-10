# MikroFIX — stanje izvedbe

Datum dokumenta: 11. 10. 2026

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

Centralna pogodba, strežnik in Blagajna trenutno podpirajo način
`ZERO_BALANCE` samo pri izdaji centralnega servisnega osnutka. Predogled ohrani
pozitivne zaklenjene vrstice in doda negativne izravnalne vrstice, plačil in UPN
ni. Neposredna prodaja je v trenutni kodi iz tega načina izrecno izključena in
to potrjuje tudi aktivni test `test_direct_sale_cannot_select_zero_balance`.
To ni več ciljno poslovno pravilo: potrjeno ciljno stanje dovoljuje vsak
neprazen neposredni ali servisni račun z vsoto `0,00 EUR`. Koda in test morata
biti spremenjena šele v ločenem blagajniškem izvedbenem sklopu.

Sedanja Blagajna prav tako še ni skladna z novo potrjenimi pravili vrstic:
ponovni sken lahko poveča obstoječo vrstico, sprememba količine lahko vrstico
prepiše ali odstrani, javni prikaz pa lahko korekcije združi. Ciljno stanje
zahteva novo vrstico za vsak sken, prepoved količine `0`, notranjo revizijsko
sled sprememb, prepoved brisanja, povezano negativno vrstico z izvirnim
cenovnim/davčnim/rabatnim posnetkom in kronološki izpis. To je dokumentirana
izvedbena vrzel, ne trditev o že narejeni funkciji.

Prav tako še niso kot celota izvedeni in dokazani novi minimum cene artikla,
različicen `L0`, delitev reklamacijskih odgovornosti ter preverjanje obsega
pooblastila pri agentskem odkupu. Obstoječa datoteka pooblastila in njen
generator sta prisotna, vendar aplikacijska uveljavitev vseh novih omejitev ni
razglašena za dokončano.

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

## Statična pravno-tehnična revizija 10.–11. 10. 2026

Pregled je bil izveden samo za branje. Produkcijska baza, FURS okolje in celotni
avtomatizirani testi niso bili zagnani, zato spodnje ugotovitve niso produkcijski
sprejem.

Potrjene dobre osnove so centralno atomsko številčenje, idempotentne identitete
operacij, trajno pripravljen podpisani zahtevek FURS, revizijski dogodki,
dokumentirani prevzemi, polni storno in pripravljalna inventurna pregrada.

Potrjene blokade P0:

- centralna izdaja trenutno uporablja `NON_VAT` in oprostitev po 94. členu brez
  splošnega časovno veljavnega profila podjetja;
- ni ločenega modela za režim domače prodaje in omejeno DDV-identifikacijo z
  oznako `P`; statusa `VAT_ATYPICAL` in njegove čezmejne obveznosti niso
  izvedeni;
- ni dokazano spremljanje prometa po koledarskem letu in 94.a členu ZDDV-1,
  preverjanje pragov 60.000 EUR in 66.000 EUR v atomski izdaji ali upoštevanje
  prometa predhodnega leta;
- davčna odločitev še ni dokazano vezana na datum dobave oziroma opravljene
  storitve in predplačila po prehodnih pravilih 161. člena PZDDV-1;
- artikel in račun nimata celovitega modela davčnih klasifikacij, davčnih vrstic
  in več stopenj na istem računu;
- centralna A4 in termična predstavitev nista dokazano poenoteni glede ZOI, EOR,
  strojno berljive kode in kopij;
- ni dokazan produkcijski izvajalec FURS vrste z obravnavo obstalega pošiljanja,
  backoffom, alarmi zakonskih rokov in ročnim pregledom;
- za DDV-zavezanca niso najdeni zaključeni izvozi evidenc obračunanega in
  odbitka DDV;
- B2G e-račun ni izveden; to je takojšnja blokada, če MikroFIX izdaja račune
  proračunskim uporabnikom.

Potrjene vrzeli P1:

- ni splošne knjige vseh gibanj zaloge, dokončane polne inventure ali potrjene
  računovodske metode FIFO oziroma tehtanega povprečja;
- ni dokazljivega izračuna zakonske prejšnje cene v 30-dnevnem obdobju po
  lokaciji in kanalu;
- ni zaključenih delnih dobropisov, več delnih poravnav ter celovitega izvoza
  izvora in vseh sprememb za davčni nadzor;
- koledar delovnih dni še ni dokazano dopolnjen s slovenskimi prazniki;
- ni e-SLOG/EN 16931 generatorja, validatorja in transporta za B2B obveznost od
  1. 1. 2028;
- retencija in varnostne kopije so arhitekturno opisane, niso pa še dokazano
  uveljavljene po posamezni zakonski vrsti dokumenta.

Ničelni račun z negativnimi količinami ostaja potrjeno interno ciljno pravilo,
vendar pred produkcijo zahteva računovodsko-davčno potrditev, da prikaz ustreza
dejanskemu poslovnemu dogodku v vseh predvidenih scenarijih.

## Naslednja revizija kode

Kodo je treba primerjati po tem vrstnem redu:

1. zagoni treh programov in prijavne seje;
2. servisni čas in lokalna trajnost;
3. identifikacija podprtih in nepodprtih naprav;
4. tehnični zaključek brez odpiranja Blagajne;
5. enkratna poraba delov in sinhronizacijski konflikt;
6. zaklenjen servisni osnutek ter ločena neposredna prodaja;
7. kronološke blagajniške vrstice, njihove audit spremembe, povezane negativne
   vrstice ter ničelni neposredni in servisni račun;
8. polna inventura in stanje vseh Service postaj;
9. reklamacija, ki se vedno začne iz računa in ohrani povezave;
10. centralne priloge, garancijsko vezana retencija in nova strategija
    varnostnih kopij;
11. časovno veljavni davčni profil, klasifikacije in večstopenjski račun;
12. fiskalni izpisi, kopije in produkcijski izvajalec FURS vrste;
13. DDV evidence, davčni in računovodski izvozi ter zakonska retencija;
14. zakonska 30-dnevna prejšnja cena in cena na mersko enoto;
15. B2G e-račun ter priprava B2B e-računov za 1. 1. 2028;
16. FURS matrika, novi interni akt in produkcijski sprejemni test.

## Produkcijski status

Produkcijska izdaja ostaja blokirana, dokler niso opravljeni kanonični sprejemni
testi in ločeno potrjene pravno-davčne točke iz zgodovine odločitev.

## Lokalna agentska veriga

Aktivna veriga ločuje vlogo od izbranega modela. `/research` ločeno zamrzne
model vodje in lastnikovo izbiro worker modela. `conversation_lead` brez
projektnih orodij razdeli vprašanja na največ tri podnaloge, zaporedni
`research_workerji` pa v svežih read-only sejah zbirajo dokaze. Manager iz
dejanskih rezultatov orodij izda evidence kartice in sestavi verzioniran dosje.
`/prepare-plan` je dovoljen šele po Codexovi potrditvi točno trenutnega hasha
dosjeja in zažene ločeno sejo `plan_preparer`, ki nima skritega spomina.
Načrt se prikaže v istem uporabniškem pogovoru in ga je mogoče popraviti; šele
`/plan-dispatch` ga pošlje v `plan_validator`, lastnikov `/approve-plan` pa
dovoli izvedbo. Nato `executor` dela v omejenem izoliranem worktreeju,
deterministični gate preveri dovoljene poti in teste, `local_reviewer` pa
neodvisno pregleda dejanski diff. Rezultat se vrne lastniku in ostane kandidat,
dokler ga Codex ne pregleda, isti preverjeni kandidat prenese v zaščiteni
repozitorij in `/codex-approved` ne potrdi enakih poti ter končnih blob hashov.

Vloge niso vezane na Qwen, GPT-OSS ali drug model. Izvajalna pravica pripada
omejeni vlogi `executor`, ne modelu kot takemu. Modeli ne določajo poslovnih
pravil in njihov odgovor sam ni dokaz izvedbe. Vsaka vloga prejme najmanjši
nalogi potreben izbor generiranih kanoničnih modulov, ne celotnega zgodovinskega
spomina.
