# MikroFix – kronološki pregled odločitev in dejanske izvedbe

Datum pregleda: 5. 9. 2026  
Stanje produkcije: **PREKLICANO DO KONCA REVIZIJE**  
Glavni register pravil: `MIKROFIX_v30_MASTER_SPECIFIKACIJA.md`

## Kako brati ta dokument

Ta dokument ne razglaša produkcijske pripravljenosti. Ločuje:

- **ZAKLENJENO** – zadnja jasna uporabnikova odločitev;
- **NAREJENO** – zahteva je razvidna iz trenutne lokalne kode;
- **DELNO** – del toka obstaja, vendar manjka pomemben del ali realna potrditev;
- **NI NAREJENO** – trenutna koda zahteve ne izvaja;
- **KONFLIKT** – koda izvaja starejšo odločitev ali nasprotno vedenje;
- **OHRANI** – obstoječa potrjena formula oziroma modul se ne spreminja brez
  nove izrecne odločitve.

Avtomatski testi dokazujejo samo preverjene lastnosti obstoječe kode. Ne dokazujejo,
da so bile zajete vse zahteve iz pogovorov.

## 1. Pregledani pogovori – od najstarejšega proti najnovejšemu

### 1.1 »Menjava earpiece iPhone X«

Tu je nastala prva dejanska aplikacija Service.

Zaklenjene odločitve:

- servisna baza hrani stranko, datum, identifikacijo naprave, vgrajeni del,
  številko računa in opravljeno delo;
- naprava se identificira neposredno prek USB; javni SN in IMEI sta maskirana,
  polni vrednosti ostaneta v bazi;
- Service ima ločene poglede naprave, servisov, zaloge in reklamacij;
- deli se v servis dodajajo iz zaloge z barkodo;
- zaloga postane ločena baza, Service jo samo bere in porablja;
- reklamacija je vezana na napravo in izvirni servis; zaključene in zavrnjene
  reklamacije niso več spremenljive;
- Office je ločena poslovna funkcija za dokumente, partnerje, nabavo, rabate,
  dostavo, marže in cene, vendar mora biti del enotne aplikacijske seje;
- Office samo bere servisne zapise; ne podvaja servisne baze;
- dokumentni tok mora biti uporaben s tipkovnico: sprotno iskanje, Enter naprej,
  jasna izbira vrste dokumenta in nato postavke;
- javni PDF vsebuje podatke podjetja in stranke, opravljeno delo ter uporabljene
  artikle, brez slik in brez internih cen;
- MicroDETECT ne sme zamrzniti GUI-ja.

Poznejše spremembe:

- zgodnja odločitev, da so testi privzeto `OK`, je pozneje razveljavljena s
  pravilom, da nepreverjeno ne pomeni delujoče;
- zgodnje priporočilo za cene, deljive s 5, je pozneje razveljavljeno z
  zaokroževanjem priporočene cene navzgor na naslednji cel EUR;
- zgodnja stroga prepoved servisa brez USB-identifikacije je bila najprej
  omiljena za poškodovan zaslon, nato pa je uporabnik povedal, da ta sklop zdaj
  ni prioriteta. Ne sme zadrževati trenutnega popravka časa dela.

Dejanska izvedba:

- **NAREJENO:** ločene SQLite baze, iskanje zaloge, barkode, servisna zgodovina,
  reklamacije, maskiranje javnih identifikatorjev in PDF-generiranje obstajajo;
- **DELNO:** Office in Service uporabljata isto prijavo, vendar Office še vedno
  teče kot ločen `runpy` modul in ni povsem enoten zaslon;
- **KONFLIKT:** testna polja v `mikrofix.py` so še vedno inicializirana z `OK`.

### 1.2 »MikroFix ideja in potencial« ter »Repair Assistant iPhone«

Zaklenjene odločitve:

- osnovna ponudba je mobilni servis in delavnica, s poudarkom na udobju,
  zanesljivosti in sledljivem kakovostnem popravilu;
- deli na zalogi so kakovostni aftermarket deli; originalni oziroma obnovljeni
  originalni deli so po naročilu;
- stranki se jasno pove vrsta dela in možne razlike; ne daje se lažnih obljub o
  oznaki Genuine, odstranitvi opozoril ali biometriji;
- JCID je interno diagnostično/programsko orodje pri preverjeno združljivih
  popravilih, ne samostojna javno obljubljena storitev;
- običajna obraba ni isto kot napaka dela; napaka dela oziroma nepravilna
  vgradnja se obravnava kot reklamacija;
- prodaja obnovljenih naprav je drugi poslovni steber, z dokumentirano sledjo,
  testi in 12-mesečno komercialno garancijo brez zmanjšanja zakonskih pravic.

Dejanska izvedba:

- **DELNO:** baza loči rabljene naprave, odklope, karanteno, obnovo in dele;
- **DELNO:** sledljivost testov in delov obstaja, vendar celoten proces obnove,
  prodajnega zapisnika in vseh pravnih potrditev ni realno potrjen od začetka do
  konca;
- **NI NAREJENO:** JCID ni neposredno integriran v aplikacijo; to ni blokada za
  prvo izdajo, ker je bil potrjen kot interno orodje, ne obvezna avtomatika.

### 1.3 »Logika servisnega lista«

Odločitev je bila, da servis lahko začne pred uspešnim `Trust This Computer`,
če je zaslon poškodovan, neposredno branje pa mora uspeti pred končnim
zaključkom. Uporabnik je v trenutnem pogovoru ta problem izrecno odložil.

Dejanska izvedba:

- **KONFLIKT/ODLOŽENO:** trenutni `save()` še vedno vsebuje identifikacijsko
  blokado. Ker je uporabnik povedal, da ta točka zdaj ni pomembna, se je pri
  zadnjem posegu ni spreminjalo.

### 1.4 Veje »EU refurbished iPhone cene«

V teh dolgih vejah so nastali pravni, poslovni in dokumentni temelji.

Zaklenjene odločitve:

- polni registrirani naziv je **MikroFix, popravilo elektronskih in
  telekomunikacijskih naprav, Ristoski Daniel s.p.**;
- prikaz znamke je vedno **MikroFix** – ne `MIKROFIX` in ne `MikroFIX`;
- poslovna prostora sta MFD (delavnica) in MFT (teren), elektronska naprava ima
  ustrezno alfanumerično oznako;
- pri pravnih in davčnih odločitvah velja merilo **legalno, moralno in najlažja
  izvedljiva pot**;
- kadar rešitev ni vsaj 99-odstotno zanesljiva in obstaja uradna pot preverjanja,
  se ne odloča na pamet;
- račun je lahko A4 ali elektronski s soglasjem; termični POS-tiskalnik in
  tablica nista pravna obveznost;
- osnovni sistem je računalnik, A4/PDF, VKR za rezervo in pozneje lokalni
  Raspberry Pi strežnik za Pi-hole, SMS, bazo/backend ter USB modem;
- PIN-a naprave se ne zbira; stranka jo po potrebi odklene sama;
- dodatno delo ali del brez predhodne dokazljive odobritve se stranki ne
  zaračuna;
- veljavna odobritev ponudbe je podpis, e-pošta ali SMS; gola ročna oznaka in
  sam telefonski pogovor ne odkleneta plačljivega dela;
- odstranjeni deli privzeto ostanejo MikroFixu za nadaljnjo obravnavo oziroma
  reciklažo, če stranka vračila ne zahteva pred začetkom;
- neprevzeta naprava ne postane samodejno last MikroFixa;
- T3 je bil razdelan kot nujna spletna rezervacija, nato pa je bil **izrecno
  odstranjen iz začetne ponudbe**. Trenutno veljata samo T1 in T2;
- pri obnovljeni rabljeni napravi se kupec izrecno dogovori za enoletni rok
  odgovornosti za neskladnost, hkrati teče 12-mesečna komercialna garancija;
- vizualna zasnova spletne strani in iskanje naprave po imenu sta bila potrjena,
  vsebina pa je ostala za končno revizijo.

Dejanska izvedba:

- **NAREJENO:** ime MikroFix je v zadnjem popravku popravljeno v glavnih
  glavah/izpisih, kjer je bilo pregledano;
- **DELNO:** MFD/MFT in FURS struktura obstajata v nastavitvah in kodi;
- **DELNO:** pravila ponudbe, odkupa, garancije in fotografij so razpršena po
  kodi in PDF-generatorjih, vendar niso vsa potrjena v enem realnem scenariju;
- **NI PREVERJENO:** spletna stran ni del te lokalne kode in je v tem pregledu
  ni mogoče šteti za zaključeno.

### 1.5 »Zahteve za delavnico«

Zaklenjene odločitve:

- terenski servis se izvaja v namenskem, ESD-urejenem delu vozila, ne na mizi v
  strankinem domu;
- MikroFix sprejema tudi računalnike, prenosnike, konzole, tablice in drugo
  elektroniko;
- MFD/MFT in namensko digitalno potrdilo so bili dejansko nastavljani v
  MiniBlagajni;
- pred produkcijo je potreben dejanski zaključni tok od dokumenta do računa.

Dejanska izvedba:

- **NAREJENO:** Service podpira splošno vrsto naprave poleg Apple mobilnih;
- **KONFLIKT:** testi PRED/PO so še vedno vidni tudi tam, kjer uporabnik zahteva,
  da se za »Drugo napravo« odstranijo;
- **DELNO:** FURS produkcijska konfiguracija vsebuje potrdilo in je vključena,
  vendar izdaja ostaja pravilno blokirana z revizijskim release-gateom.

### 1.6 Cenovne niti: »Cena menjave …«, »Izdelava PDF cenika« in
»Zakleni cenik v1.0«

Zadnja veljavna cenovna pravila:

- `L0 >= 22 EUR` je interni trdi prag za rabat na storitveni postavki T1/T2;
  ni javna cena in se ne uporablja kot minimum fizičnega artikla;
- T1: delo v delavnici, prva začeta ura 30 EUR, nato 15 EUR na vsako začeto
  polurno enoto;
- T2: teren, prvih 0–60 minut 90 EUR, nato 15 EUR na vsako začeto polurno
  enoto;
- deli so zalogovni artikli z lastno nabavno vrednostjo in prodajno ceno;
- rabat fizičnega dela ne sme pod dejansko nabavno vrednost konkretnega LOT-a
  + 5 %;
- garancijska rezerva je notranji dejavnik kategorijskega cenovnega profila;
- ciljna marža je prava marža: `stroškovna osnova / (1 - ciljna marža)`;
- cenovni profil ima tri stroškovne razrede in je različicen; nova formula velja
  le za prihodnja priporočila;
- priporočena cena se zaokroži navzgor na naslednji cel EUR;
- uporabnik ob prejemu potrdi končno prodajno ceno; priporočilo je pomoč, ne
  prisila;
- dejanski potrošni material je lasten artikel/postavka in ni skrit pavšal v
  ceni dela;
- pri kombiniranem posegu se skupnih korakov odpiranja, tesnjenja in testiranja
  ne zaračuna dvakrat.

Dejanska izvedba:

- **NAREJENO:** POS ima ločeni spodnji varovalki: artikel nabava + 5 % in
  storitev L0;
- **DELNO:** strukturirani T1/T2 podatki, čas v minutah in zaklenjeni tarifni
  snapshot obstajajo;
- **DELNO:** kategorijski cenovni profili obstajajo, vendar celoten zaključek
  prejema in vse različice formul še niso dokazano skladne z vsako zadnjo
  odločitvijo;
- **KONFLIKT:** deli stare kode in starih PDF-jev še omenjajo prejšnje
  priporočilo `/5` oziroma stare cenovne predloge. Ti ne smejo biti aktivni vir
  cene.

### 1.7 »SI price report ready email blocked« in zadnja odločitev o ohranitvi

Razvoj odločitve:

1. prvi veljavni izpis je imel pravilno formulo;
2. drugi izpis je imel po uporabnikovi oceni odlično sestavo;
3. dogovorjeno je bilo, da obvestilo ostane samo v pogovoru in se nič ne pošilja
   samodejno;
4. poročilo pridobiva dejanske javne cene iz več evropskih refurbished virov ter
   javno ceno relevantnega dela in iz njih pripravi pregled za lažji nakup in
   prodajo;
5. zadnja uporabnikova odločitev 5. 9. 2026 je, da formula in modul ostaneta
   takšna, kot sta bila dogovorjena. Modula se ne odstrani in formule se ne
   spreminja.

Dejanska izvedba:

- **OHRANI:** `mikrofix_weekly_report.py` uporablja potrjeno formulo
  `MF-ODKUP-1.0.1` za `PRIPOROČEN ODKUP / MAX ODKUP`;
- **NAREJENO:** viri vključujejo najmanj Swappie, Refurbed in FixShop, vsak z
  javnim URL-jem ter datumom zajema;
- **DELNO:** modul in Office prikaz obstajata, toda sandbox ga ima v nastavitvah
  trenutno izključenega; v sandboxu ga je treba vključiti in ohraniti njegovo
  formulo nespremenjeno;
- poročilo oziroma obvestilo se ne pošilja nobenemu brez izrecnega naročila.

### 1.8 »Locate project history«

Tu so bile po dejanskem GUI-preizkusu ponovno najdene vrzeli:

- Blagajna se ni odprla zaradi manjkajoče tabele;
- dokument je ob knjiženju vračal nejasno napako za skupni znesek;
- tiskalniki niso bili povezani z vsemi tokovi;
- QR je odpiral SMS/klic in prikazal samo številko;
- F10 potek plačila, UPN, gotovina in vračilo niso bili dokončani;
- Office je še uporabljal 58 mm namesto dogovorjenih 80 mm;
- Service za »Drugo napravo« ni odstranil iPhone testov;
- zahtevano je bilo dejansko pregledati nastale PDF-je in vse poti do napak.

Dejanska izvedba danes:

- **NAREJENO, UPORABNIK POTRDIL:** UPN QR zdaj deluje z bančno aplikacijo;
- **NAREJENO:** jedro gotovina/kartica/TRR, vračilo in dvostopenjski tisk
  obstajajo;
- **DELNO:** tisk A4 in 80 mm deluje, vendar sta bila opažena težava s šumniki
  na A4 in invertiran 80-mm izpis; po popravkih ni dokumentiranega ponovnega
  realnega odtisa obeh formatov;
- **NAREJENO V KODI:** pri »Drugi napravi« se iPhone testi skrijejo, dobijo
  vrednost `N/A` in se ne zapisujejo; iPhone testi se začnejo kot
  `NI TESTIRANO`, ne kot `OK`;
- **DELNO:** napake tabel so bile migrirane, vendar je treba realni zagon obeh
  okolij ponoviti šele po koncu revizije.

### 1.9 Trenutni pogovor po selitvi na Linux

Zadnje veljavne odločitve:

- lokalni disk hrani produkcijsko in sandbox različico; Kingston je samo backup;
- sandbox je za testiranje in načrtovanje posodobitev, stable pa mora biti brez
  testnega opozorila ter dejansko produkcijsko povezan;
- noben račun se ne sme izdati brez odprtega blagajniškega dnevnika, ne glede na
  način plačila;
- ob izpadu interneta se račun izda z ZOI, brez EOR in z jasno oznako naknadne
  potrditve; isti nepotrjeni račun ostane v čakalni vrsti do potrditve;
- pri servisu uporabnik med delom dodaja samo fizične artikle;
- gumb **Začni servis** v Office in Service mora zagnati isti časovnik;
- čas se ne vpisuje ročno: od prvega začetka do zaključka ga izračuna program;
- ponovno odprtje istega servisa ne ponastavi začetka;
- ob zaključku program iz časa sam izbere T1/T2, izdela eno postavko **Delo** in
  jo skupaj z deli prenese na račun;
- produkcije se ne odpre, dokler pregled ne pokaže, da je res pripravljena.

Dejanska izvedba danes:

- **NAREJENO V KODI:** produkcijski launcher počisti testne okoljske spremenljivke;
- **NAREJENO V KODI:** stable in sandbox kažeta različni okoljski oznaki;
- **NAREJENO V KODI:** produkcijski release je nastavljen na
  `REVOKED_PENDING_REQUIREMENTS_AUDIT`, zato Stable trenutno pravilno ne sme
  skozi;
- **NAREJENO V KODI:** `issue_invoice()` zahteva odprt blagajniški dnevnik pri
  vsakem novem računu; idempotenten ponovni klic že izdanega računa ostane
  dovoljen;
- **NAREJENO V KODI:** skupni časovnik je dodan v `service_orders` in ohrani
  prvi začetek;
- **NAREJENO V KODI:** zaključek izračuna pretečene minute, izbere T2 samo za
  `MFT_TEREN`, sicer T1, zaokroži obračunske enote in ustvari eno postavko
  `Delo`;
- **NAREJENO V KODI:** ročni zavihek storitvenih postavk je skrit, ob odprtju
  servisa se pokaže zavihek vgrajenih delov;
- **NAREJENO V KODI:** Office prikazuje odprta servisna naročila in uporablja
  isto pot **ZAČNI / NADALJUJ SERVIS** do skupnega časovnika kot Service;
- **NI NAMENSKO TESTIRANO:** po uporabnikovi zahtevi po koncu zadnjega zagona
  niso bili zagnani novi testi. Zadnji že tekoči paket se je končal 36/36 OK,
  vendar nima posebnega novega testa za Office gumb časovnika.

## 2. Trenutna slika po modulih

| Sklop | Stanje | Glavni razlog |
|---|---|---|
| Prijava, PIN, dovoljenja | DELNO/NAREJENO | Jedro in testi obstajajo; realni nadaljevalni tok po zaklepu ni ponovno pregledan v vseh modulih. |
| Enotna aplikacija in navigacija | DELNO | Skupni meni obstaja, Office je še vedno ločen `runpy` modul. |
| Office – partnerji in dokumenti | DELNO | Veliko jedra obstaja; niso zaključene vse tipkovniške poti, filtri in pregled vsake napake. |
| Prejem blaga in zaloga | DELNO | Decimalni prejemi, LOT/FIFO in priponke obstajajo; celotna zadnja cenovna logika ni potrjena na realnih dokumentih. |
| Service | DELNO | Avtomatski časovnik, skupni Office/Service vstop in LOT rezervacija so v kodi; manjka še celotni realni GUI preizkus. |
| Reklamacije | DELNO | Baza in stanja obstajajo; vseh zaklenjenih dokaznih in PDF pravil ni realno potrjenih. |
| Odkup/obnova | DELNO | Jedro in atomski tokovi obstajajo; končni uporabniški postopek in javni dokumenti niso celovito potrjeni. |
| Blagajna | DELNO | Načini plačila, UPN, FURS jedro in tisk obstajajo; odprti dnevnik je pravkar popravljen, realni celotni tok še ni ponovljen. |
| FURS produkcija/offline | DELNO | Produkcijska konfiguracija in čakalna vrsta obstajata; release je namerno preklican do revizije. |
| Javni PDF A4/80 mm | DELNO | Generatorji obstajajo; po znanih napakah šumnikov/inverzije manjka zadnji realni odtis. |
| SI Price Report | OHRANI/DELNO | Formula in več evropskih virov ostanejo; sandbox ga mora vključiti brez spreminjanja formule. |
| Backup/šifriranje | DELNO | Jedro obstaja; operativna nastavitev nosilca, gesla in realna obnova niso zaključeni kot produkcijski postopek. |
| Produkcijski Stable | BLOKIRAN | `production-release.json` je pravilno preklican do konca pregleda. |

## 3. Najpomembnejše nespremenjene ali preskočene zahteve

1. Skupni Office/Service vstop in časovnik sta izvedena, vendar še čakata na
   celotni ročni GUI scenarij z dejanskim servisnim nalogom.
2. Servisni deli se zdaj rezervirajo po konkretnem FIFO/LOT-u; fizični odpis se
   izvede ob računu, storno pa obnovi isti LOT. Manjka še ročni sprejemni scenarij.
3. Testi PRED/PO se začnejo kot `NI TESTIRANO`; pri drugih napravah se ne kažejo
   in ne shranjujejo. Manjka še vizualna potrditev v realnem GUI.
4. Za »Drugo napravo« iPhone testi PRED/PO še niso odstranjeni.
5. Price Report je v sandbox nastavitvah izključen, čeprav morata obstoječi
   modul in potrjena formula ostati dostopna.
6. Nekateri stari tokovi in dokumenti še uporabljajo zastarele cenovne pojme
   `/5`, ročne servisne postavke ali stare tarife.
7. Celotni fizični tok A4 in 80-mm tiska po zadnjih popravkih ni ponovno potrjen
   na obeh tiskalnikih.
8. Produkcijski FURS ima konfigurirano potrdilo in je vključen, toda dejanski
   produkcijski zagon ne sme biti dovoljen samo na podlagi ECHO ali enotnih
   testov; potreben je zaključni nadzorovani račun in preveritev izpisa.
9. Partner/AJPES iskanje po davčni številki in samodejni vnos novega podjetja ni
   dokazano zaključeno v produkcijskem toku.
10. Odobritev dodatnega dela s pripetim dokazom (podpis/e-pošta/SMS) ni dokazano
    zaključena kot enoten tok, ki dejansko odklene plačljivo delo.
11. Celotni tok neprevzete naprave, hramba in končni poziv ni implementiran kot
    zaključen modul.
12. Popolna inventura, sklep o razlikah in atomsko knjiženje vseh odločitev niso
    dokazani v realnem GUI scenariju.
13. Realna obnovitev šifrirane varnostne kopije na drugem okolju ni zaključena
    kot sprejemni produkcijski postopek.
14. Spletna stran potrebuje dogovorjeno vsebinsko revizijo; lokalna koda tega ne
    dokazuje.
15. Glavna specifikacija pri servisnem scenariju še opisuje ročne storitvene
    postavke in jo je treba uskladiti z zadnjo odločitvijo o avtomatskem času ter
    eni postavki `Delo`.

## 4. Kaj je dejansko potrjeno in se ne odpira ponovno

- zapis znamke **MikroFix**;
- polni registrirani podatki podjetja;
- MFD/MFT;
- T3 ni del začetne ponudbe;
- L0 je trdi rabatni prag samo za T1/T2;
- fizični artikel ima spodnjo mejo dejanska nabavna vrednost LOT-a + 5 %;
- T1 = 30 EUR prva začeta ura, nato 15 EUR / začete 30 min;
- T2 = 90 EUR prvih 0–60 min, nato 15 EUR / začete 30 min;
- deli in delo so ločene postavke;
- javni dokument ne kaže nabavnih cen, notranjih marž ali priporočil;
- račun zahteva odprt blagajniški dnevnik;
- UPN QR je namenjen dejanskemu plačilu in je uporabnik njegovo delovanje že
  potrdil;
- offline FURS ne blokira dela: ZOI takoj, EOR naknadno iz nespremenljive vrste;
- čas servisa se meri samodejno od začetka do zaključka;
- med servisom se dodajajo samo artikli, ob zaključku nastane ena postavka Delo;
- Stable je produkcija, Sandbox pa ločeno testno okolje; Kingston je backup;
- formula in modul SI Price Report ostaneta nespremenjena ter uporabljata
  dejansko pridobljene javne cene več evropskih refurbished virov;
- pri resni pravni/davčni nejasnosti se uporabi uradni vir in se ne ugiba.

## 5. Pošten sklep

Trenutna verzija ni pripravljena za produkcijski zagon. Ima veliko delujočega
jedra, vendar so med pogovori in kodo ostali pomembni konflikti. Najkrajša pot do
uporabne prve produkcije je:

1. ciljno preveriti enoten začetek/zaključek servisa iz Office in Service;
2. ciljno preveriti rezervacijo, račun in storno servisnih delov;
3. vizualno potrditi teste za iPhone in druge naprave;
4. potrditi vključeni Price Report v sandboxu brez spreminjanja formule;
5. opraviti en ročni celotni scenarij Service → Blagajna → dnevnik → FURS →
   A4/80 mm, ko uporabnik znova dovoli testiranje;
6. šele nato izdati nov, datumsko sledljiv produkcijski release.
