# MikroFIX — veljavna poslovna pravila

Datum konsolidacije: 12. 10. 2026

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

- Trgovsko blago ima ime, mersko enoto in sledljivo količino ter povezavo do
  številke in datuma prevzemnega dokumenta, dobavitelja ter številke in datuma
  dobaviteljevega dokumenta.
- Barkoda določi konkretno zalogovno identiteto.
- Kosi z isto barkodo se lahko vodijo količinsko znotraj sledljivega LOT-a;
  serijsko voden kos zahteva svojo identiteto.
- Servis ob tehničnem zaključku knjiži dejansko uporabljeni del.
- Izdani račun uporabljeni del samo obračuna in ga ne odšteje ponovno.
- Blagajna lahko neposredno prodaja razpoložljive dele neodvisno od Servisa.
- Rezervacija je dovoljena kot neobvezna možnost pred uporabo. Rezerviran kos je
  še fizična zaloga; porabljen oziroma vgrajen kos ni več zaloga.
- `SERVICE_RESERVATION` ne sme biti prikrito ime za že vgrajen del.
- Vsa povečanja in zmanjšanja zaloge nastanejo kot nespremenljivi dogodki
  enotne knjige gibanj: prejem, pulled del, rezervacija, poraba, prodaja,
  vračilo, prenos, odpis, inventurna razlika ali drug izrecno dovoljen tip.
- Vrednotenje uporablja eno potrjeno metodo, FIFO ali tehtano povprečje. Aktivna
  metoda se ne menja brez računovodske odločitve, datuma začetka in revizijske
  sledi. Izbira začetne metode je odprta odločitev.
- Neposredni nabavni stroški, nevračljive dajatve in nepovračljivi DDV so del
  nabavne vrednosti, kadar tako zahtevajo računovodska pravila. Poškodovana,
  zastarela ali neprodajljiva zaloga se vodi ločeno in se po potrjenem postopku
  oslabi oziroma odpiše.

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
- Plačilo z bankovci, kovanci, plačilno, kreditno ali debetno kartico oziroma
  drugim načinom, ki ni neposredno nakazilo na transakcijski račun, se za ZDavPR
  obravnava kot gotovinsko plačilo. Neposredno nakazilo na TRR praviloma ni
  gotovinsko plačilo.
- Če ob izdaji neplačanega računa poznejši način plačila ni znan in je račun
  nato plačan gotovinsko, se podatki pošljejo FURS v desetih delovnih dneh od
  plačila.
- Ob izpadu povezave se račun izda z ZOI in drugimi obveznimi podatki brez EOR.
  Povezava se ponovno vzpostavi in račun pošlje praviloma v dveh delovnih dneh;
  pozneje prejeti EOR se shrani skupaj s kopijo računa.
- Če FURS zahtevek zavrne zaradi napake, se račun izda brez EOR, napaka odpravi
  in popravljeni podatki pošljejo praviloma v dveh delovnih dneh. Račun je
  davčno potrjen šele po prejemu EOR.
- VKR se uporabi samo, ko računa objektivno ni mogoče izdati po nobeni dovoljeni
  elektronski poti. Gre za skrajni rezervni postopek, ne običajni odgovor na
  kratkotrajno motnjo ene povezave ali ene naprave.
- Ob okvari elektronske naprave se uporabi predhodno potrjena VKR, podatki pa se
  po ponovni vzpostavitvi praviloma sporočijo v dveh delovnih dneh. Podrobne
  izjeme zaradi nepredvidljivih in neodvrnljivih razlogov ter morebitne novele se
  pred produkcijo ponovno preverijo pri uradnem viru.

## 14. Vidna in notranja stanja servisa

Glavno poslovno stanje ostane preprosto:

- `V obdelavi`;
- `Zaključeno`, izključno po izdaji računa.

Tehnična faza in stanje sinhronizacije sta ločena podatka. Zato so
`Pripravljeno za obračun`, `Čaka sinhronizacijo` in `SYNC_CONFLICT` vidna
pojasnila oziroma podstanja, ne dodatna končna poslovna stanja.

## 15. Interni cenovni minimum, zakonska prejšnja cena in rabat

- Najnižja dovoljena prodajna cena artikla je dokumentirani skupni strošek,
  povečan za `5 %`, nato zaokrožen navzgor na naslednjih `5 EUR`.
- `L0` je ločen, različicen interni minimum dela, ki ga določa aktivni potrjeni
  cenovni profil. Njegova začetna vrednost še ni potrjena.
- Blagajnik transakcijske cene ne prepisuje neposredno. Spremeni jo lahko samo
  z rabatom, ki ne sme prebiti veljavnega minimuma artikla ali `L0`.
- Na računu se ob izdaji shrani uporabljena različica cenovnega profila in
  odločilni cenovni posnetek.
- Interni minimum iz tega poglavja ni prejšnja cena po pravilih varstva
  potrošnikov. Pri objavi znižanja se ločeno prikažeta znižana in prejšnja cena;
  prejšnja je praviloma najnižja cena v najmanj 30 dneh pred znižanjem v istem
  prodajnem kanalu oziroma lokaciji.
- Cena za potrošnika je v EUR in vključuje DDV, kadar je MikroFIX DDV-zavezanec.
  Kjer je zahtevano, se poleg prodajne cene prikaže tudi cena na običajno mersko
  enoto. Sistem upošteva zakonske izjeme, ne pa splošne izjeme po lastni presoji.

## 16. Omejeno zastopanje pri odkupu

- Pooblaščenka sme v okviru veljavnega pisnega pooblastila pregledati in
  prevzeti napravo ter pripraviti samo osnutek pogodbe.
- Pooblaščenka ne sme zavezati MikroFix, pogodbe dokončno odobriti ali skleniti
  in ne sme izvesti plačila. Končno odobritev, sklenitev in plačilo opravi Daniel
  Ristoski.
- Sistem pred sprejemom takega dejanja preveri veljavnost in obseg pooblastila
  ter v revizijsko sled zapiše uporabljeno različico.

## 17. Davčni profil, artikel in prehod v DDV

- Sistem podpira tri notranje statuse `NON_VAT`, `VAT_ATYPICAL` in
  `VAT_REGISTERED`. `VAT_ATYPICAL` je programsko ime za omejeno identifikacijo,
  ki je v uradnem seznamu FURS označena s `P`, in ni samostojen zakonski izraz.
- Model loči režim domače prodaje od obsega identifikacije za DDV.
  `VAT_ATYPICAL` praviloma še ne pomeni obračuna DDV od domačih dobav ali
  pravice do odbitka, povzroči pa obveznosti za zadevne pridobitve blaga oziroma
  čezmejne storitve. Prisotnost predpone `SI` zato sama ne sme vključiti DDV na
  domačem računu.
- Aktivacija ali sprememba statusa je nadzorovana operacija odgovorne osebe z
  datumom začetka veljavnosti, virom potrditve, datumom zadnjega preverjanja in
  revizijsko sledjo; spreminjanje za nazaj ni dovoljeno.
- Uradni vir davčnega profila se ponovno preveri najpozneje en koledarski mesec
  po datumu in času zadnjega preverjanja. Rok zapade na isti koledarski dan v
  naslednjem mesecu oziroma na njegov zadnji dan, če takega dne ni. Po izteku
  roka je profil zastarel in produkcijska izdaja računa je blokirana do novega
  sledljivega preverjanja; status se zaradi poteka roka ne spremeni samodejno.
- Dejanski status MikroFIX in datum njegove veljavnosti nista določena samo s
  programsko privzeto vrednostjo. Pred produkcijsko izdajo se ugotovita iz
  dejanskih pogojev ter preverljivih podatkov FURS oziroma eDavkov; potrditev
  računovodje je priporočljiva, ni pa nadomestilo za uradni status.
- Sistem po koledarskem letu vodi zakonsko opredeljeni promet na ozemlju
  Slovenije iz 94.a člena ZDDV-1 in upošteva tudi predhodno koledarsko leto.
  Opozori pred pragovoma 60.000 EUR in 66.000 EUR. Če tekoči promet preseže
  60.000 EUR, vendar ne 66.000 EUR, podpre začetek rednega obračunavanja
  najpozneje 1. januarja naslednjega leta. Transakcija, s katero promet preseže
  66.000 EUR, mora že uporabiti ustrezno DDV-obravnavo; preverjanje je zato del
  atomske izdaje računa, ne le naknadno opozorilo.
- Podprta je tudi prostovoljna vključitev z datumom začetka in obdobjem vezanosti
  po veljavnih pravilih ter omejena identifikacija zaradi pridobitev blaga iz EU
  oziroma čezmejnih storitev. Sistem statusa ne aktivira samodejno, ampak ob
  doseženem pogoju blokira nejasno izdajo in zahteva potrjeno odločitev oziroma
  uradni podatek.
- Davčni profil podjetja ni lastnost zaloge. Prehod v sistem DDV ne ustvari nove
  zaloge, ne spremeni količin in ne spreminja že izdanih računov.
- Artikel oziroma storitev ima časovno veljavno davčno klasifikacijo. Končno
  pravilo ob izdaji upošteva tudi vrsto transakcije, kupca, kraj, oprostitev in
  druge dovoljene okoliščine; stopnja DDV ni samo statično polje artikla.
- Vsaka izdana vrstica in račun hranita nespremenljiv posnetek uporabljenega
  davčnega profila, klasifikacije, osnove, stopnje, zneska DDV in pravne navedbe.
- Vračilo, storno ali dobropis uporablja davčni posnetek izvornega računa, tudi
  če se je profil podjetja ali artikla pozneje spremenil.
- Davčna obravnava prehoda ni določena samo z datumom izdaje. Sistem hrani datum
  dobave oziroma opravljene storitve in podatke o predplačilu. Za dobavo,
  dejansko opravljeno pred prehodom, vendar z računom po prehodu, uporabi pravilo
  161. člena PZDDV-1 in nadzoruje poseben rok izdaje; pri predplačilu pred
  prehodom in dobavi po njem davčno obravnavo določi obdobje opravljene dobave.
- Obstoječa fizična zaloga se ob spremembi statusa ne podvoji ali prevrednoti na
  skrivaj. Morebitni odbitek DDV za blago, nabavljeno pred identifikacijo, je
  ločen, sledljiv računovodski popravek na podlagi konkretne listine in potrjene
  upravičenosti; sistem ga ne predpostavi samodejno.
- Ob prehodu v `VAT_REGISTERED` velja dokončno potrjena kombinirana cenovna
  politika po skupinah. Pri terenskih storitvah in delu se ohranijo potrjene
  končne cene za kupca, zato vključitev DDV zmanjša neto prihodek. Pri rezervnih
  delih in paketnih popravilih se pred aktivacijo novega cenika preveri marža
  glede na novo nabavno stroškovno osnovo ter potrdi nova končna cena.
- Vsak prehodni cenik je različicno voden, vnaprej potrjen in ima datum začetka
  veljavnosti. Ne spreminja že izdanih računov ali cen že dogovorjenih naročil
  brez ustrezne pogodbene podlage oziroma soglasja kupca.

## 18. Obvezna vsebina računa in fiskalni izpis

- Polni račun hrani najmanj datum izdaje, enolično zaporedno številko, zahtevane
  identifikacijske podatke izdajatelja in kupca, ime in naslov, količino in vrsto
  blaga oziroma obseg in vrsto storitve, datum dobave, davčno osnovo po stopnjah,
  ceno na enoto brez DDV, nevključene popuste, stopnjo in znesek DDV ter vse
  pogojne zakonske navedbe.
- Poenostavljeni račun se uporabi samo v zakonsko dovoljenem primeru in vsebuje
  vse podatke, zahtevane za tak račun. Izbira oblike ne sme izpustiti podatkov,
  potrebnih za preveritev količine, vrste in zaračunanega zneska.
- Račun podpira več davčnih stopenj in oprostitev na različnih vrsticah ter
  pravilne seštevke po davčni kategoriji.
- A4 in termični izpis davčno potrjenega računa dosledno prikažeta ZOI, EOR in
  predpisano strojno berljivo kodo. Dovoljeni račun brez EOR jasno ohrani ZOI in
  vse druge zahtevane podatke.
- Vsaka kopija je vsebinsko enaka izvirniku, označena `KOPIJA` in zaporedno
  številko kopije. Evidenca hrani čas nastanka vseh kopij.

## 19. Popravki, DDV evidence in davčni izvoz

- Izdani račun se ne briše ali prepisuje. Vsaka poznejša sprememba je nov vezan
  dokument oziroma dogodek z nedvoumnim sklicem na izvorni račun in se, kadar
  tako zahteva ZDavPR, ponovno davčno potrdi.
- Če je MikroFIX identificiran za DDV, za vsako davčno obdobje vodi in v roku za
  DDV-O elektronsko predloži evidenco obračunanega DDV in evidenco odbitka DDV
  po posamezni listini. Neskladnost z že predloženimi evidencami se odpravi
  nemudoma oziroma najpozneje v treh delovnih dneh.
- Sistem omogoča izvoz izvornih podatkov izdanih računov in vseh njihovih
  poznejših sprememb v urejeni obliki, ki jo zahteva davčni nadzor, ter ločen
  dogovorjeni računovodski izvoz.
- Delni dobropis, delno vračilo, več delnih plačil in kombinirano plačilo morajo
  ohraniti izvorne zneske, povezave, davčne posnetke in celotno časovno sled.

## 20. Hramba in nespremenljivost dokumentacije

- Kopije vseh izdanih in vsi prejeti računi se hranijo najmanj deset let po
  poteku leta, na katero se nanašajo; računi v zvezi z nepremičninami najmanj
  dvajset let. Za drugo davčno dokumentacijo se uporablja daljši veljavni rok,
  kadar ga zahtevajo ZDavP-2 ali posebni predpisi.
- Elektronsko ustvarjeni ali prejeti podatki ostanejo v elektronski obliki.
  Izvor je berljiv, dostopen in reproducirljiv skupaj s podatki za dokazovanje
  pristnosti izvora in celovitosti vsebine.
- Sistem ne omogoča brisanja, popravljanja, razveljavitve, nadomestitve,
  dodajanja ali skrivanja knjiženega zapisa brez hrambe izvornega podatka in vseh
  poznejših sprememb.
- Vsaka sprememba elektronske rešitve, ki vpliva na davčne evidence, se
  kronološko dokumentira z vzrokom, vrsto, posledico in datumom.

## 21. E-računi

- Dobava proračunskemu uporabniku se dokumentira z veljavnim strukturiranim
  e-računom in izmenja prek UJP skladno z ZPJS.
- Za domače B2B dobave, opravljene v Sloveniji, se sistem pripravi na izključno
  izmenjavo strukturiranih e-računov od 1. 1. 2028. Podprta oblika je e-SLOG,
  sintaksa evropskega standarda oziroma drug zakonsko dovoljen dogovorjeni
  standard; PDF sam zase ni e-račun.
- B2C e-račun se izda samo s predhodnim izrecnim soglasjem potrošnika. Zagotovi
  se vizualizacija in možnost, da potrošnik kadar koli zahteva papirni račun.
- E-račun in davčno potrjevanje sta ločeni obveznosti. E-račun, plačan z
  gotovino ali kartico, se davčno potrdi, kadar so izpolnjeni pogoji ZDavPR.
- Hrani se strukturirani izvirnik, uporabljena sintaksa, enolična identiteta,
  transportna pot ter statusi sprejema, zavrnitve in dostave.

## 22. Imena in sistem vizualnih identitet

- `MikroFIX` je identiteta programske družine. Njeni izdelki se dosledno
  imenujejo **MikroFIX Office**, **MikroFIX Servis** in **MikroFIX Blagajna**.
- `MikroFix` je javna identiteta terenskega servisa. Ime `MikroFIX Servis`
  označuje programski izdelek in ni zamenljivo z javno storitveno identiteto
  `MikroFix`.
- Polno registrirano ime podjetja je pravna identifikacija, ne tretja prosto
  oblikovana znamka. Na pravno pomembnih dokumentih se vedno prevzame iz
  veljavnega poslovnega vira oziroma nastavitev.
- Velikost črk je pomenska. Zapisov `MikroFIX` in `MikroFix` ni dovoljeno
  poenotiti samo zaradi vizualne doslednosti.
- Programsko in servisno identiteto povezuje pikselizirani oblikovni jezik. Del
  imena `FIX` je njegov glavni poudarek; mreža, kvadratni moduli, rastrski
  prehodi, fragmenti, robovi in ikone so dovoljeni načini njegove izvedbe.
- Pikselizirani jezik ne sme zmanjšati čitljivosti, uporabnosti, resnosti ali
  zakonsko zahtevane vsebine programske opreme, računov in servisnih gradiv.
- Potrjena osnovna skupna barvna paleta je: siva `#929CAA`, modra `#4EA3E3`,
  turkizna `#35C9C1`, rumena `#F2B84B` in rdeča `#FF6B67`. Posamezna identiteta
  ali gradivo lahko uporablja le potreben del palete; konkretne pomenske vloge,
  kontrasti in razmerja barv se določijo v pripadajočih oblikovalskih pravilih.
- Končni logotipi, pisave, gostota vzorca, motiv razbitega stekla in stopnja
  vizualne sorodnosti identitet niso določeni s tem pravilom. Postanejo kanon
  šele po izrecni lastnikovi potrditvi.
