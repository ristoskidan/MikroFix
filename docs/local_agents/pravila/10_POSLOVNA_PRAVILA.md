<!-- GENERIRANO iz docs/kanon/01_POSLOVNA_PRAVILA.md; ne urejaj ročno. -->

# MikroFIX — veljavna poslovna pravila

Datum konsolidacije: 26. 9. 2026

## 1. Programi in odgovornosti

MikroFIX sestavljajo trije samostojni programi:

### MikroFIX Office

- prevzame blago na podlagi dokumentiranega izvora;
- ga pregleda, ovrednoti in določi prodajno ceno;
- potrjene dele sprosti v skupno zalogo;
- vodi partnerje, dobavitelje, dokumente, cenike, poročila, administracijo,
  dovoljenja in inventuro.

### MikroFIX Servis

- sprejme in vodi servisni nalog;
- meri dejanski čas, vodi delo, ugotovitve in teste;
- evidentira uporabljene dele;
- tehnično zaključi servis in pripravi obračunski paket;
- vsebuje tudi odkup, obnovo, razstavljanje donorskih naprav in nastanek
  `pulled part` delov.

### MikroFIX Blagajna

- izdaja račune iz zaklenjenih servisnih osnutkov;
- neposredno prodaja artikle;
- izbira in evidentira način plačila;
- izvaja davčno potrditev;
- povezuje račune s servisi in reklamacijami;
- z izdanim računom obračunsko zaključi servis.

Programi so ločeni procesi, vendar niso vsak trajno vezani na svoj fizični
računalnik. Na isti registrirani delovni postaji lahko hkrati teče več dovoljenih
programov, na primer Servis in Blagajna na terenskem računalniku.

## 2. Prijava in seja

- Vsak zagon vsakega programa zahteva novo prijavo.
- Seja se ne shranjuje na disk in se po ponovnem zagonu ne obnovi.
- Vsak nadzorovani izhod samodejno odjavi uporabnika in odstrani žeton,
  identiteto ter dovoljenja iz pomnilnika.
- Vsak uporabnik ima svojo identiteto in PIN; skupni računi niso dovoljeni.
- Samodejni zaklep zaslona ni izhod in ne ustavi servisnega časa.
- Ročna odjava in nadzorovano zaprtje Servisa sta blokirana, dokler ima uporabnik
  vsaj en tekoči servisni interval.

## 3. Dovoljeni izvor pozitivne zaloge

Pozitivna razpoložljiva zaloga ima samo dva dovoljena izvora:

1. pregledan in potrjen dobavni oziroma nabavni postopek;
2. potrjen uporaben del iz dokumentirano odkupljene in razstavljene donorske
   naprave.

Splošni `STOCK_EXCEPTION`, negativna zaloga, ročno ustvarjanje količine in
inventurni višek kot samostojen tretji vir niso dovoljeni.

Inventura lahko odkrije višek, vendar ga ne sme sprostiti v prodajo ali servis.
Kos se lahko sprosti šele, ko je povezan z enim od dveh dovoljenih izvornih
procesov. Če izvora ni mogoče dokazati, ostane nerazpoložljiv.

## 4. Artikli, barkode, LOT-i in poraba

- Barkoda določi konkretno zalogovno identiteto.
- Kosi z isto barkodo se lahko vodijo količinsko znotraj sledljivega LOT-a;
  serijsko voden kos zahteva svojo identiteto.
- Servis ob tehničnem zaključku knjiži dejansko uporabljeni del.
- Izdani račun uporabljeni del samo obračuna in ga ne odšteje ponovno.
- Blagajna lahko neposredno prodaja razpoložljive dele neodvisno od Servisa.
- Rezervacija je dovoljena kot neobvezna možnost pred uporabo. Rezerviran kos je
  še fizična zaloga; porabljen oziroma vgrajen kos ni več zaloga.
- `SERVICE_RESERVATION` ne sme biti prikrito ime za že vgrajen del.

## 5. Servisni čas

- Čas je trajen zapis intervalov, ne stanje okna ali zavihka.
- Preklop zavihka, skrivanje okna ali prehod na drug nalog časa ne ustavi.
- Isti serviser ima lahko več hkrati tekočih nalogov.
- Pred preklopom se celotni odprti osnutek shrani.
- Čas ustavi uporabnik ali tehnični zaključek.
- Po ponovnem zagonu se odprti interval prepozna iz lokalne baze.

## 6. Identifikacija naprave

### Naprava, ki jo MicroDETECT podpira

- Servisni nalog se sme odpreti tudi, kadar poškodovana naprava še ne omogoča
  branja ali potrditve zaupanja računalniku.
- Do uspešnega branja uporablja notranji UUID in lastno nalepko naloga.
- Modela, IMEI-ja in serijske številke se ne vnaša ročno kot preverjene podatke.
- Neposredno branje lahko uspe pred posegom ali po njem.
- Običajni tehnični zaključek je blokiran, dokler zahtevana identiteta ni
  neposredno prebrana iz naprave.
- PIN-a ali gesla naprave MikroFIX ne zbira in ne hrani; napravo po potrebi
  odklene stranka.

### Naprava, ki je MicroDETECT ne podpira

Takšno napravo je dovoljeno sprejeti. Uporabi se ločen postopek
`FIZIČNO PREVERJENA IDENTITETA`, ki se nikoli ne prikazuje kot MicroDETECT
branje. Zapis vsebuje:

- kategorijo, proizvajalca in model;
- serijsko številko ali drug stabilni identifikator, kadar obstaja;
- potrditev uporabnika ter datum in čas fizičnega prepisa.

Če naprava nima uporabnega proizvajalčevega identifikatorja, se ji dodeli samo
notranji UUID in fizična MikroFIX nalepka. Sistem tega podatka ne predstavlja kot
serijsko številko proizvajalca. Prosto besedilo stranke samo po sebi ni preverjena
identiteta.

## 7. Tehnični zaključek in osnutek računa

Ukaz `ZAKLJUČI SERVIS IN PRIPRAVI RAČUN` v eni lokalni atomski operaciji:

1. ustavi pripadajoči tekoči interval;
2. shrani delo, ugotovitve, teste in neobvezne fotografije;
3. shrani uporabljene dele in obračunski posnetek;
4. tehnično zaključi servis;
5. ustvari nespremenljiv lokalni paket zaključka;
6. ob povezavi ga pošlje centralnemu strežniku.

Servis ne odpre, ne aktivira in ne ponudi programa Blagajna. Uporabniku pokaže
samo eno od potrditev:

- `Osnutek računa je pripravljen`, če ga je strežnik sprejel;
- `Zaključek je shranjen in čaka sinhronizacijo`, če povezave ni;
- jasno napako oziroma `KONFLIKT SINHRONIZACIJE`, če strežnik zaključek zavrne.

Centralni servisni osnutek ostane nespremenljiva izvorna referenca. Blagajna iz
njega pripravi sledljiv predizdajni račun in ne prepisuje izvornega servisnega
paketa. Neposredna prodaja vedno nastane na ločenem računu.

## 8. Blagajniške vrstice, servis in račun

- Vsak sken barkode oziroma ročna izbira artikla ustvari novo kronološko
  vrstico s količino `1`. Sistem enakih artiklov ne združuje samodejno.
- Pred izdajo je količino obstoječe vrstice dovoljeno neposredno spremeniti na
  pozitivno ali negativno celo število, nikoli na `0`.
- Sprememba količine ne ustvari avtomatske korekcijske vrstice. V notranjo
  revizijsko sled se zapišejo dokument, vrstica, stara in nova vrednost,
  uporabnik ter čas.
- Vrstica se ne briše. Če ni več potrebna, se popravi z dovoljenim sledljivim
  postopkom; izdani račun vedno ohrani končne vrstice v kronološkem vrstnem
  redu in jih ne združi v neto prikaz.
- Ročna negativna vrstica nastane z novim skenom oziroma izborom artikla in
  nato ročno negativno količino. Negativna vrstica mora biti povezana s
  konkretno neizravnano pozitivno vrstico istega artikla.
- Če obstaja natanko ena primerna pozitivna vrstica, jo sistem izbere in
  povezavo jasno pokaže. Če jih je več, mora uporabnik izbrati; sistem ne sme
  ugibati po starosti, ceni ali podobnosti.
- Negativna vrstica prevzame nespremenljiv cenovni, davčni in rabatni posnetek
  povezane pozitivne vrstice. Povezava in prevzeti posnetek sta del revizijske
  sledi. Negativna vrstica sama ne vrača zaloge.

- Vsak servis se obračunsko zaključi z izdanim povezanim računom.
- Pred računom je lahko tehnično zaključen oziroma pripravljen za obračun, ni pa
  dokončno zaključen.
- Pravilo velja tudi za garancijsko, brezplačno, zavrnjeno ali drugače
  nezaračunano obravnavo.
- Prazno neposredno prodajno košarico je mogoče opustiti. Vsak neprazen
  predizdajni račun, neposredni ali servisni, je dovoljeno pravilno izravnati na
  `0,00 EUR` in ga izdati. Končni negativni znesek je vedno prepovedan.
- Ničelni servisni račun ni prazen. Vsebuje prvotne obračunske vrstice in enake
  izravnalne vrstice z negativno količino. Potrjeni primer je:

  - `Zaslon OLED iPhone 11`, količina `1`, znesek `110,00 EUR`;
  - `T2`, količina `1`, znesek `30,00 EUR`;
  - `Zaslon OLED iPhone 11`, količina `-1`, znesek `-110,00 EUR`;
  - `T2`, količina `-1`, znesek `-30,00 EUR`.

  Skupni znesek je `0,00 EUR`. Negativni vrstici sta sledljiva razloga za
  izravnavo istega dela in storitve; ne pomenita vračila kosa v zalogo in ne
  ustvarita pozitivne zaloge.
- Po izdaji so vrstice nespremenljive. Popravek izdanega računa je dovoljen samo
  z vezanim storno dokumentom; saldo storna se uporabi na istem izvornem
  računu, natančni podatkovni model in uporabniški tok te poravnave pa sta še
  odprta.

## 9. Offline Servis in sinhronizacija

- Servis uporablja lokalni SQLite in zadnji potrjeni centralni posnetek.
- Ob vsakem zagonu je tudi brez povezave obvezna nova prijava z lokalnim PIN-om.
  Centralno potrjeni uporabniški posnetek velja največ sedem dni od zadnje
  uspešne centralne prijave. Vsaka offline prijava pokaže jasno opozorilo in
  čas, do katerega posnetek še velja; po poteku je prijava blokirana do povezave.
- Nima lastne fizične zaloge.
- Brez povezave lahko odpre, vodi in tehnično zaključi poljubno število nalogov.
- Vsak nalog in dogodek imata UUID, identiteto izvorne namestitve in lokalno
  zaporedje.
- Offline nalog do prve potrjene centralne sinhronizacije uporablja začasno
  uporabniško oznako `MF-OFF-<POSTAJA>-<LETO>-<ZAPOREDJE>`, na primer
  `MF-OFF-MFT01-2026-000001`. Oznaka je nespremenljiva lokalna referenca in se
  hrani tudi po sinhronizaciji. Strežnik ob prvem sprejemu dodeli končno
  centralno številko `MF-<LETO>-<ZAPOREDJE>`; začasna oznaka se nikoli ne
  preštevilči ali ponovno uporabi.
- Dogodki se pošiljajo po vrstnem redu in z isto identiteto operacije.
- Ponovitev iste operacije ne podvoji naloga, porabe ali osnutka.
- Če je centralna količina ob sinhronizaciji nezadostna, se nič ne knjiži delno,
  osnutek ne nastane, zaloga se ne izmisli, problematična barkoda pa se blokira.
- Lokalni servis ostane tehnično zaključen s stanjem sinhronizacije
  `SYNC_CONFLICT`. Opozorilo pokaže nalog, artikel, barkodo in količino.

Blagajna in Office v prvi fazi nimata offline zapisovalnega načina.

## 10. Inventura

- Na dan polne inventure je prodaja zaprta.
- Pred začetkom morajo biti vsi odprti dokumenti zaključeni in knjiženi.
- Vse registrirane Service postaje morajo biti sinhronizirane, brez čakajočih ali
  konfliktnih dogodkov.
- Če katera postaja ni dosegljiva ali ima nepotrjen outbox, polne inventure ni
  mogoče začeti.
- Po začetku polne inventure so blokirani vsi poslovni postopki, ki bi spremenili
  zalogo ali ustvarili nov odprt dokument.
- Delna inventura ne knjiži in sama ne spreminja zaloge.

## 11. Fotografije in identifikatorji na dokumentih

- Servisne fotografije niso obvezne.
- Fotografije posameznega servisa se hranijo sorazmerno z garancijo: najmanj do
  poteka najdaljše pripadajoče garancije za ta servis. Po poteku se odstranijo v
  nadzorovanem postopku, če niso predmet odprte reklamacije, spora, zakonske
  hrambe ali izrecnega zadržanja.
- Fotografije osebnih dokumentov niso dovoljene.
- Interna evidenca hrani polne dovoljene identifikatorje.
- Na garancijskem listu konkretne naprave je prikazan polni IMEI.
- Na drugih javnih dokumentih je IMEI praviloma maskiran na zadnje štiri znake,
  razen če je za posamezni dokument pozneje izrecno potrjena druga obveznost.
- PIN-i, gesla, nabavne cene, marže in interne opombe se ne izpisujejo na javne
  dokumente.

## 12. Reklamacije in povezave dokumentov

- Blagajna fizično sprejme reklamacijo, izbere konkretni račun in odpre
  sledljivo povezavo. Office vodi in administrira reklamacijski zapis. Servis
  opravi tehnično diagnostiko in delo, ne odpira pa reklamacije brez računa.
- Reklamacija se vedno odpre iz konkretnega izdanega računa in mora hraniti
  njegovo nespremenljivo identiteto.
- Če račun izvira iz servisa, je povezan tudi s konkretnim servisnim nalogom.
- Kadar za predmet računa obstaja garancijski list, je račun povezan tudi s
  konkretnim garancijskim listom; reklamacija lahko prek računa nedvoumno najde
  oba dokumenta.
- Garancijski list brez pripadajočega računa ni samostojna vstopna točka za
  reklamacijo.
- Storno ali nadomestni račun ne sme pretrgati revizijske povezave do prvotnega
  računa, servisa, garancijskega lista in reklamacije.

## 13. FURS, poslovni prostori in VKR

- Ciljna tehnična rešitev centralizira namensko digitalno potrdilo, uradno
  številčenje, podpisovanje zahtevka in komunikacijo s FURS.
- Blagajna mora podati dejanskega uporabnika, poslovni prostor, registrirano
  delovno postajo oziroma elektronsko napravo in identiteto operacije.
- Sedanja podpisana različica internega akta 1.2 ni nespremenljiva podlaga za
  novo topologijo. Ko bo znana končna matrika naprav, se prijava FURS po potrebi
  spremeni in izdela ter podpiše nov interni akt.
- `MFD` pomeni delavnico oziroma pisarno, `MFT` pa terenski poslovni prostor.
- VKR se uporabi samo, ko računa objektivno ni mogoče izdati po nobeni dovoljeni
  elektronski poti. Gre za skrajni rezervni postopek, ne običajni odgovor na
  kratkotrajno motnjo ene povezave ali ene naprave.
- Natančen izredni postopek in roki se pred produkcijo ponovno preverijo pri
  uradnem viru.

## 14. Vidna in notranja stanja servisa

Glavno poslovno stanje ostane preprosto:

- `V obdelavi`;
- `Zaključeno`, izključno po izdaji računa.

Tehnična faza in stanje sinhronizacije sta ločena podatka. Zato so
`Pripravljeno za obračun`, `Čaka sinhronizacijo` in `SYNC_CONFLICT` vidna
pojasnila oziroma podstanja, ne dodatna končna poslovna stanja.

## 15. Najnižja cena in rabat

- Najnižja dovoljena prodajna cena artikla je dokumentirani skupni strošek,
  povečan za `5 %`, nato zaokrožen navzgor na naslednjih `5 EUR`.
- `L0` je ločen, različicen interni minimum dela, ki ga določa aktivni potrjeni
  cenovni profil. Njegova začetna vrednost še ni potrjena.
- Blagajnik transakcijske cene ne prepisuje neposredno. Spremeni jo lahko samo
  z rabatom, ki ne sme prebiti veljavnega minimuma artikla ali `L0`.
- Na računu se ob izdaji shrani uporabljena različica cenovnega profila in
  odločilni cenovni posnetek.

## 16. Omejeno zastopanje pri odkupu

- Pooblaščenka sme v okviru veljavnega pisnega pooblastila pregledati in
  prevzeti napravo ter pripraviti samo osnutek pogodbe.
- MikroFIX ne sme zavezati, pogodbe dokončno odobriti ali skleniti in ne sme
  izvesti plačila. Končno odobritev, sklenitev in plačilo opravi Daniel
  Ristoski.
- Sistem pred sprejemom takega dejanja preveri veljavnost in obseg pooblastila
  ter v revizijsko sled zapiše uporabljeno različico.
