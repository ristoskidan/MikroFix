# MikroFIX — kanonični sprejemni testi

Ta dokument opisuje najmanjši obvezni dokaz pred produkcijo. Podrobni tehnični
testi se lahko dodajo, ne smejo pa oslabiti teh pravil.

## 1. Ločeni programi in prijava

- Office, Servis in Blagajna se zaženejo kot trije ločeni procesi.
- Servis in Blagajna lahko hkrati delujeta na istem računalniku.
- Vsak proces ob vsakem zagonu zahteva prijavo.
- Izhod odstrani sejo; ponovni zagon je ne obnovi.
- Ročna odjava in izhod iz Servisa sta ob tekočem intervalu blokirana.

## 2. Čas

- Isti uporabnik začne čas na dveh različnih nalogih; oba tečeta.
- Preklop naloga, zavihka in skrivanje okna časa ne ustavijo.
- Pavza zaključi samo izbrani interval.
- Po nenadzorovanem ponovnem zagonu se odprti interval prepozna iz baze.
- Premik sistemske ure ne ustvari negativnega trajanja ali tihe spremembe že
  zaključenega trajanja.

## 3. Identifikacija naprave

- Podprt iPhone z nedelujočim zaslonom je mogoče sprejeti brez začetnega branja.
- Zaključek ostane blokiran, dokler MicroDETECT pred ali po posegu ne prebere
  zahtevane identitete.
- Ročni IMEI se ne more prikazati kot neposredno preverjen.
- Nepodprta naprava se sprejme prek preprostega postopka
  `FIZIČNO PREVERJENA IDENTITETA`; sistem ne zahteva dodatnega razčlenjevanja
  vira fizičnega prepisa.
- Naprava brez lastne serijske številke dobi notranji UUID in MikroFIX nalepko,
  ne izmišljene proizvajalčeve serijske številke.

## 4. Tehnični zaključek

- En klik atomsko shrani čas, delo, teste, dele in lokalni ukaz zaključka.
- Fotografija ni obvezna.
- Servis po uspehu ne odpre Blagajne.
- Online zaključek pokaže pripravljen osnutek; offline zaključek pokaže čakanje
  na sinhronizacijo.
- Ponovljeni klik vrne isti rezultat in ne ustvari druge porabe ali osnutka.

## 5. Zaloga

- Zaloga nastane samo iz potrjenega prejema ali potrjenega pulled dela.
- `STOCK_EXCEPTION`, negativna zaloga in ročno pozitivno knjiženje so zavrnjeni.
- Isti zadnji kos lahko uspešno porabi samo ena sočasna operacija.
- Druga operacija ne knjiži ničesar delno in pokaže konkretni artikel ter
  barkodo.
- Račun servisnega dela zaloge ne zmanjša ponovno.
- Inventurni nepojasnjeni višek ne postane razpoložljiv.

## 6. Offline sinhronizacija

- Servis offline odpre in zaključi več nalogov v določenem vrstnem redu.
- Vsak dobi enolično oznako oblike `MF-OFF-<POSTAJA>-<LETO>-<ZAPOREDJE>` in
  UUID; po sinhronizaciji dobi še centralno številko, lokalna oznaka pa ostane
  iskalna in revizijska referenca.
- Po ponovni povezavi se vsak dogodek prenese natanko enkrat in v pravilnem
  zaporedju.
- Prekinitev odgovora strežnika in ponovitev ne podvojita dogodka.
- Konflikt zadnjega kosa ustvari `SYNC_CONFLICT`, brez centralnega osnutka.
- Po sinhronizaciji lokalni pogled prejme novejši centralni posnetek.
- Ponovni zagon brez povezave vedno zahteva prijavo. Veljaven podpisan
  uporabniški posnetek jo omogoči, potekel ali spremenjen posnetek pa jo zavrne.
- Vsaka uspešna offline prijava pokaže opozorilo in čas izteka. Posnetek je ob
  dopolnjenih sedmih dneh zavrnjen; premik lokalne ure pred čas zadnje centralne
  prijave se ne uporabi za podaljšanje veljavnosti.

## 7. Blagajna

- Dva blagajnika istočasno izdata isti servisni osnutek: nastane največ en račun
  in ena številka.
- Ponovitev iste operacije vrne že znani rezultat.
- Neposredna prodaja nastane na ločenem računu.
- Prazna neposredna prodaja se lahko opusti brez številke.
- Dva zaporedna skena istega artikla ustvarita dve vrstici `x1`; nobena se ne
  združi ali izbriše.
- Pred izdajo se količina vrstice spremeni na pozitivno in negativno celo
  vrednost, sprememba na `0` pa je zavrnjena. Revizijska sled vsebuje staro in
  novo vrednost, uporabnika, čas, dokument ter vrstico.
- Nova negativna vrstica se lahko poveže samodejno samo, kadar obstaja natanko
  ena primerna neizravnana pozitivna vrstica. Pri dveh kandidatih izdaja ostane
  blokirana do izrecne uporabnikove izbire.
- Negativna vrstica prevzame točno ceno, davek in rabat povezane pozitivne
  vrstice tudi, če se je trenutni cenik medtem spremenil; ne poveča zaloge.
- Izdani račun pokaže vse končne vrstice kronološko in jih ne netira.
- Servisni in neposredni neprazni račun se lahko izravnata na `0,00 EUR` ter se
  vseeno izdata. Negativni končni znesek je zavrnjen.
- Po izdaji ni mogoče spremeniti vrstice; dovoljen je samo vezan storno, ki
  ohrani revizijsko povezavo do izvornega računa.

## 8. Cena in rabat

- Za več različnih stroškov se minimum artikla izračuna kot dokumentirani skupni
  strošek + `5 %` in vedno zaokroži navzgor na naslednjih `5 EUR`.
- Sprememba cene v Blagajni je mogoča samo prek rabata. Poskus preboja minimuma
  artikla ali veljavnega `L0` je zavrnjen.
- Izdani račun ohrani različico profila in cenovni posnetek tudi po poznejši
  spremembi cenika.
- Ob aktivaciji `VAT_REGISTERED` terenska storitev in delo ohranita potrjeno
  končno ceno, sistem pa pravilno razdeli davčno osnovo in DDV znotraj te cene.
- Rezervni del in paketno popravilo uporabita vnaprej potrjen novi cenik,
  izračunan po preverjanju marže glede na novo nabavno stroškovno osnovo.
- Aktivacija prehodnega cenika ne spremeni že izdanega računa ali cenovnega
  posnetka že dogovorjenega naročila brez zabeležene pogodbene podlage oziroma
  soglasja kupca.

## 9. Inventura

- Polna inventura se ne začne, če prodaja ni zaprta.
- Ne začne se ob odprtem, neknjiženem ali konfliktnem dokumentu.
- Ne začne se, če katera Service postaja ni sinhronizirana ali ima outbox.
- Med polno inventuro so vsi zalogovni in dokumentni vpisi blokirani.
- Delna inventura ne spremeni zaloge.

## 10. Dokumenti in zasebnost

- Garancijski list konkretne naprave vsebuje polni IMEI.
- Drugi javni dokumenti uporabljajo maskirano vrednost, kadar ni izrecno
  določeno drugače.
- Javni dokumenti ne vsebujejo PIN-a, gesla, nabavne cene, marže ali interne
  opombe.
- Dolga besedila in več strani ne prekrijejo postavk, zneskov ali podpisov.
- Fotografija ni samodejno izbrisana pred potekom najdaljše pripadajoče
  garancije. Po poteku jo nadzorovana retencija izbriše, razen ob reklamaciji,
  sporu ali drugem veljavnem zadržanju.

## 11. Reklamacije

- Blagajna sprejme predmet in izbere račun, Office vodi reklamacijski zapis,
  Servis pa izvede tehnični del. Servis brez računa reklamacije ne more odpreti.
- Reklamacije ni mogoče ustvariti brez izbire konkretnega izdanega računa.
- Reklamacija za servisni račun prek računa kaže isti servis in pripadajoči
  garancijski list.
- Storno in nadomestni račun ohranita celotno verigo povezav in ne ustvarita
  nepovezane reklamacije.

## 12. Omejeno pooblastilo

- Veljavno pooblastilo pooblaščenki dovoli pregled in prevzem naprave ter
  pripravo osnutka pogodbe.
- Poskus končne odobritve, sklenitve pogodbe ali plačila z njeno identiteto je
  zavrnjen. Enaka dejanja z Danielovo potrditvijo so sledljivo dovoljena.
- Poteklo, preklicano ali vsebinsko neustrezno pooblastilo dejanje zavrne.

## 13. FURS in izredni postopki

- Dvojni klik ne ustvari druge številke, ZOI ali FURS dogodka.
- Nejasen odgovor se usklajuje z natančno istim podpisanim zahtevkom.
- Poslovni prostor in elektronska naprava na računu se ujemata z veljavno FURS
  matriko in internim aktom.
- VKR je dostopen samo v dokumentiranem skrajnem primeru, ko ni mogoče uporabiti
  nobene dovoljene elektronske poti.
- Produkcijski preizkus se izvede šele po novi prijavi naprav in podpisu novega
  internega akta.

## 14. Davčni profil in račun

- Profil `NON_VAT` izda račun brez obračunanega DDV in s pravilno zakonsko
  navedbo; ne sme uporabljati navedbe, za katero izdajatelj nima pogojev.
- Profil `VAT_ATYPICAL` pri domači dobavi ne obračuna DDV samo zaradi obstoja
  številke `SI`, ne dovoli splošnega odbitka vstopnega DDV in pravilno evidentira
  zadevno pridobitev blaga oziroma čezmejno storitev. Uradna oznaka `P` se
  preslika v ta notranji status brez zamenjave z `VAT_REGISTERED`.
- Profil `VAT_REGISTERED` izda račun z ločenimi postavkami 22 %, 9,5 %, 5 % in
  oprostitvijo ter pravilnimi osnovami in seštevki po kategorijah.
- Manjkajoč ali zastarel vir potrditve statusa blokira produkcijsko izdajo; sama
  programska privzeta vrednost testa ne opravi.
- Preverjanje vira je veljavno do istega koledarskega dne naslednjega meseca
  oziroma do zadnjega dne tega meseca, če istega dne ni. Izdaja tik pred rokom
  uspe, izdaja po roku je blokirana, novo sledljivo preverjanje pa profil znova
  omogoči brez samodejne spremembe njegovega statusa.
- Pri prometu do 60.000 EUR, nad 60.000 EUR do vključno 66.000 EUR in pri
  transakciji, ki preseže 66.000 EUR, sistem uporabi ustrezno pravilo. Posebej se
  preverita meja koledarskega leta in promet predhodnega leta. Transakcija čez
  66.000 EUR ne sme biti najprej napačno izdana kot `NON_VAT` in popravljena
  šele naknadno.
- Prehod profila na določen datum ne spremeni zaloge ali računa pred datumom
  prehoda. Dobava, opravljena pred prehodom in zaračunana po njem, ter
  predplačilo pred prehodom za dobavo po njem prestaneta ločena testa po 161.
  členu PZDDV-1; datum izdaje sam ne odloči režima.
- Vračilo po prehodu uporabi davčni posnetek izvornega računa.
- Morebitni odbitek DDV od obstoječe zaloge nastane samo kot sledljiv, odobren
  računovodski popravek in ne spremeni fizične količine.
- Polni in poenostavljeni račun vsebujeta vse podatke, zahtevane za konkretni
  tip računa; mešani račun ohrani davčne podatke vsake vrstice.

## 15. Plačila, fiskalni izpis in kopije

- Plačilo s kartico sproži davčno potrjevanje enako kot plačilo z gotovino;
  neposredno nakazilo na TRR ga brez drugega gotovinskega dela ne sproži.
- Poznejše gotovinsko plačilo prej neplačanega računa ustvari pravočasen FURS
  dogodek in ne novega računa.
- Izpad povezave, zavrnitev zahtevka, restart izvajalca in obstali `SENDING`
  nadaljujejo isti račun, številko, ZOI, `MessageID` in podpisani zahtevek.
- Alarm se sproži dovolj zgodaj pred potekom roka dveh oziroma desetih delovnih
  dni, vključno s pravilnim koledarjem praznikov.
- A4 in termični izpis vsebujeta pravilne ZOI, EOR in strojno berljivo kodo.
- Vsaka ponovna izdaja je označena `KOPIJA`, ima naslednjo zaporedno številko,
  čas nastanka in nespremenljivo vsebino izvirnika.

## 16. Cene, merske enote in zaloga

- Pri objavi znižanja se prejšnja cena izračuna kot zakonsko zahtevana najnižja
  cena v istem kanalu oziroma lokaciji v zadnjih najmanj 30 dneh; interni
  stroškovni minimum na rezultat ne vpliva.
- Potrošniška cena vključuje DDV, prodajna cena in zahtevana cena na enoto pa sta
  skladni z mersko enoto artikla.
- Iz konkretnega prevzema je mogoče dokazati dobavitelja, obe številki in datuma
  dokumentov, ime, mersko enoto, količino, prodajno ceno in njene spremembe.
- Prejem, prodaja, servisna poraba, vračilo, prenos, odpis in inventurna razlika
  ustvarijo ustrezne povezane dogodke ter reproducibilno stanje in vrednost po
  aktivni računovodski metodi.

## 17. DDV evidence, izvoz in hramba

- Iz znanega nabora listin se reproducibilno izdelata evidenci obračunanega in
  odbitka DDV; tehnična zavrnitev oddaje ne ustvari lažnega uspeha.
- Popravek evidence ohrani prvotno oddajo, razlog in čas ter je mogoče dokazati,
  ali je bil izveden v treh delovnih dneh.
- Izvoz za davčni nadzor ločeno vsebuje izvorne izdane račune in vse njihove
  poznejše spremembe. Računovodski izvoz se številčno ujema z računi in DDV
  evidencami.
- Testna obnovitev dokaže dostopnost, čitljivost in celovitost računov, prilog,
  sprememb ter revizijske sledi. Politika prepreči prezgodnji izbris dokumenta z
  deset-, dvajsetletnim ali daljšim veljavnim rokom.

## 18. E-računi

- B2G račun se uspešno validira in izmenja prek UJP; zavrnitev je sledljiva in
  ponovitev ne podvoji dokumenta.
- Domači B2B račun se validira v podprti strukturirani sintaksi; PDF brez
  strukturiranega izvirnika je zavrnjen kot e-račun.
- B2C e-račun brez dokazljivega predhodnega izrecnega soglasja je zavrnjen;
  potrošnik prejme vizualizacijo in lahko zahteva papir.
- Gotovinsko oziroma kartično plačan e-račun opravi tudi zahtevani neodvisni tok
  davčnega potrjevanja.
