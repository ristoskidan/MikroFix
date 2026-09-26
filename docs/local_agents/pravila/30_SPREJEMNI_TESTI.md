<!-- GENERIRANO iz docs/kanon/03_SPREJEMNI_TESTI.md; ne urejaj ročno. -->

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
