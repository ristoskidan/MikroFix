# MikroFIX — ciljna arhitektura

Datum konsolidacije: 11. 10. 2026

## 1. Topologija

```text
MikroFIX Servis   (lokalni SQLite + outbox) ─┐
MikroFIX Blagajna (centralni odjemalec) ─────┼─ API ─ PostgreSQL
MikroFIX Office   (centralni odjemalec) ─────┘        └─ centralne priloge
```

Program je samostojen proces, ne nujno samostojen računalnik. Na začetku je
dovoljena postavitev z enim ali dvema računalnikoma; na terenskem računalniku
lahko hkrati delujeta Servis in Blagajna. Končni strežnik bo ločen, do takrat sta
razvojni API in PostgreSQL lahko na sedanjem razvojnem računalniku.

## 2. Avtoritativna zapisovalna meja

- Skupne poslovne podatke spreminja samo centralni API.
- PostgreSQL ni neposredno uporabniško programsko vmesniško mesto.
- Office in Blagajna sta centralna odjemalca.
- Servis zapisuje lokalne offline dogodke in jih prek podpisanih idempotentnih
  ukazov sinhronizira z API-jem.
- Trajno dvojno zapisovanje istega poslovnega dejstva v dve avtoritativni bazi ni
  dovoljeno.

## 3. Identitete ukaza

Vsak občutljiv ukaz vsebuje najmanj:

- `operation_id`;
- uporabnika;
- fizično delovno postajo;
- namestitev programa in vrsto odjemalca;
- poslovni prostor, kadar je pomemben;
- lokalno zaporedje pri offline Servisu;
- različico podatkov oziroma poslovnega pravila.

Pri računu se dodatno shrani registrirana blagajniška/FURS identiteta. Isti
`operation_id` z isto vsebino vrne isti rezultat; z drugo vsebino se zavrne.

Offline uporabniška seja uporablja strežniško podpisan posnetek identitete,
vlog in dovoljenj. Posnetek ne nadomesti prijave: ob vsakem zagonu Servisa mora
uporabnik ponovno vnesti svoje poverilnice. Posnetek velja največ sedem dni od
zadnje uspešne centralne prijave. Vsaka offline prijava uporabnika opozori, da
strežnik ni dosegljiv, in pokaže konec veljavnosti. Po poteku je offline prijava
blokirana do ponovne centralne prijave. Odvzem uporabnika ali pravic se na že
odklopljeni postaji lahko uveljavi najpozneje ob poteku posnetka.

## 4. Stanja servisa

Stanje se ne tlači v en sam stolpec:

- `business_status`: `IN_PROGRESS` ali `COMPLETED`;
- `technical_phase`: sprejem, diagnostika, delo, test, pripravljeno za obračun;
- `sync_status`: lokalno, čaka prenos, sinhronizirano, konflikt;
- `billing_status`: brez osnutka, osnutek pripravljen, račun izdan;
- `custody_status`: pri MikroFIX ali vrnjeno.

Uporabniški vmesnik lahko kaže samo dve glavni poslovni stanji, vendar mora
jasno prikazati pomembna podstanja in blokade.

## 5. Čas

Lokalni Servis shrani začetni in končni stenski čas, trajanje pa med delom meri
z monotono uro procesa. Ob zaključku shrani izračunano trajanje in revizijske
podatke. Strežnik trajanja ne preračuna iz svojega časa, zabeleži pa čas prejema
in opozori na nenavadna odstopanja ali premik sistemske ure.

## 6. Tehnični zaključek

Lokalni marker zaključka, intervali, testi, vrstice dela, uporabljeni deli in
outbox ukaz nastanejo v eni SQLite transakciji. Sekundarna opravila, kot so PDF,
osvežitev ali varnostna kopija, ne smejo povzročiti izgube že potrjenega
zaključka.

Centralni strežnik v eni transakciji preveri vrstni red, identitete, količine in
ponovitev ter nato ustvari zalogovne dogodke in centralni servisni osnutek.

## 7. Izdaja računa

- Odpiranje osnutka ne ustvari poslovnega zakupa `CLAIMED`.
- Predizdajni račun hrani kronološke vrstice z lastno stabilno identiteto.
  Urejanje količine je audit dogodek `old → new`, ne prepis brez sledi; vrednost
  `0` je zavrnjena in vrstic ni mogoče brisati.
- Vsaka negativna vrstica nosi obvezni sklic na pozitivno vrstico, ki jo
  izravnava, ter njen nespremenljivi cenovni, davčni in rabatni posnetek. Če je
  kandidatov več, je izrecna uporabnikova izbira del ukaza.
- Strežnik preveri, da povezava pripada istemu predizdajnemu računu, da
  pozitivna količina še ni prekomerno izravnana in da prevzeti posnetek ni bil
  spremenjen.
- Strežniški ukaz izdaje zaklene osnutek in atomsko zagotovi največ en veljaven
  račun ter eno porabljeno številko.
- Strežniški ukaz dovoljuje končni znesek `0,00 EUR` pri neposrednem in
  servisnem računu, zavrne pa negativni končni znesek.
- Pred prvim FURS klicem se trajno shranijo natančni podpisani zahtevek,
  `MessageID`, ZOI in kontrolni odtis.
- Nejasen rezultat nadaljuje isto operacijo; ne ustvari novega računa ali ZOI.
- Servisna poraba zaloge se ob računu ne ponovi.
- Izdani posnetek ohrani vse vrstice kronološko. Poznejša sprememba je možna
  samo z vezanim storno dokumentom; model uporabe salda na istem izvornem
  računu ostaja odprta ciljna podrobnost.

## 8. Cenovni posnetki

Centralni cenovni profil je različicen. Za artikel hrani dokumentirani skupni
strošek in izračunani minimum `strošek + 5 %`, zaokrožen navzgor na naslednjih
`5 EUR`; za delo hrani ločen minimum `L0`. Računska vrstica ob nastanku zajame
ceno, davek, rabat, odločilni minimum in različico profila. Blagajna zahteva
rabat, ne poljubnega prepisa cene. Strežnik pred izdajo ponovno preveri prag.

Interni minimum in zakonska prejšnja cena sta ločena podatka. Strežnik vodi
časovno zgodovino bruto in neto cene po artiklu, lokaciji in prodajnem kanalu ter
iz nje reproducibilno določi najnižjo ceno v zahtevanem 30-dnevnem obdobju.

Cenovni profil vsebuje skupino, način prehoda `KEEP_GROSS` ali
`REPRICE_FROM_COST`, datum začetka veljavnosti, odgovorno odobritev in izvorno
različico. Terenske storitve in delo ob prehodu v `VAT_REGISTERED` uporabljajo
`KEEP_GROSS`; rezervni deli in paketna popravila uporabljajo
`REPRICE_FROM_COST` s preverjanjem marže. Že dogovorjeno naročilo hrani svoj
cenovni posnetek in se zaradi poznejše aktivacije profila ne preračuna samo.

## 9. Priloge in varnostne kopije

Centralno potrjeni dokumenti in priloge imajo centralno avtoritativno hrambo.
Offline Servis lahko hrani lokalne delovne kopije do potrjene sinhronizacije.
Centralna hramba fotografij vsebuje datum poteka hrambe, izračunan iz najdaljše
pripadajoče garancije, in pravno/poslovno zadržanje, ki avtomatski izbris ustavi.

Varnostne kopije so ločene na:

1. PostgreSQL;
2. centralne priloge;
3. strežniško konfiguracijo in zaščitene ključe;
4. lokalne Service baze z dogodki, ki še niso potrjeno sinhronizirani.

Obnova se šteje za preverjeno šele po dejanski testni obnovi združljive celote.

## 10. Polna inventura

Pred inventurnim zaklepom strežnik preveri:

- da ni odprtih ali neknjiženih dokumentov;
- da so vse registrirane Service postaje dosegljive in sinhronizirane;
- da ni čakajočih ali konfliktnih zalogovnih dogodkov;
- da so Blagajna, prejemi in drugi zapisovalni postopki zaprti.

Šele nato izda inventurni zaklep. Med njim noben program ne sme ustvariti
poslovnega dogodka, ki bi vplival na zamrznjeno stanje.

## 11. Reklamacije in pooblastila

Reklamacijski zapis je centralen in ga Office administrira. Blagajna ga odpre
iz konkretnega računa ob fizičnem sprejemu, Servis pa nanj dodaja tehnične
ugotovitve. Povezave do računa, servisa, garancijskega lista, storna in
nadomestnega računa so nespremenljive.

Pooblastilo je različicen centralni zapis obsega in veljavnosti. Dejanja
pooblaščenke morajo dokazati veljavno pooblastilo; dovoljen je pregled, prevzem
in priprava osnutka pogodbe, medtem ko odobritev, sklenitev in plačilo zahtevajo
Danielovo identiteto.

## 12. Postopna migracija

- Stari monolit ostane primerjalna osnova in se ne prepiše naenkrat.
- Preverjene poslovne funkcije se izločajo za centralni API po posameznih
  vertikalah.
- Vsaka vertikala ima eno avtoritativno zapisovalno pot.
- Združljivostni adapter je začasen in ne postane trajna druga poslovna baza.
- Nov odjemalec se priključi šele, ko so njegovi pogodbeni testi z API-jem
  uspešni.

## 13. Davčni model

Centralni model ločuje:

1. časovno veljavni režim domače prodaje;
2. časovno veljavni obseg identifikacije za DDV;
3. časovno veljavno davčno klasifikacijo artikla ali storitve;
4. transakcijsko pravilo oziroma dovoljeno izjemo;
5. nespremenljiv davčni posnetek računske vrstice;
6. seštevke računa po davčnih kategorijah.

Notranji statusi so najmanj `NON_VAT`, `VAT_ATYPICAL` in `VAT_REGISTERED`.
Profil hrani začetek in morebitni konec veljavnosti, vir potrditve, čas zadnjega
preverjanja in odgovorno odobritev. Prekrivajoči se profili, status brez
dokazljivega vira in retroaktivna sprememba izdanega računa so zavrnjeni.
Veljavnost preverjanja vira traja en koledarski mesec od `verified_at`; rok je
isti koledarski dan naslednjega meseca oziroma njegov zadnji dan, kadar takega
dne ni. Zastarel profil blokira produkcijsko izdajo, ne povzroči pa samodejne
spremembe davčnega statusa.

Ločen projekcijski modul računa promet tekočega in predhodnega koledarskega leta
po 94.a členu ZDDV-1. Pred potrditvijo izdaje v isti transakciji preveri tudi
vpliv novega računa na pragova 60.000 EUR in 66.000 EUR. Opozorilo ne sme
samodejno ustvariti uradnega statusa, pri možnem prehodu pa izdaja ne sme nadaljevati
z nejasnim davčnim pravilom.

Davčni odločevalnik poleg profila in klasifikacije uporabi datum dobave oziroma
opravljene storitve, datum izdaje, predplačila, kupca, kraj in vrsto transakcije.
S tem izvede tudi prehodna pravila 161. člena PZDDV-1. Prehod profila ne
spreminja artiklov, količin zaloge ali zgodovinskih dokumentov; morebitna
prilagoditev odbitka DDV je ločen knjižen dogodek.

## 14. Knjiga gibanja zaloge in vrednotenje

Vsako poslovno povečanje ali zmanjšanje je nespremenljiv centralni zalogovni
dogodek z vrsto, količino, mersko enoto, izvorno listino, uporabnikom, časom in
predhodnikom, kadar gre za popravek. Razpoložljivo stanje je izpeljano iz knjige
dogodkov, ne iz nepovezanega ročnega prepisa količine.

Vrednotenje podpira FIFO in tehtano povprečje, vendar je v posameznem obdobju
aktivna samo potrjena računovodska metoda. Sistem hrani sestavo nabavne
vrednosti, razporeditev odvisnih stroškov, oslabitve, odpise in inventurne
razlike brez izgube izvorne listine.

## 15. FURS vrsta in fiskalni dokument

Centralni izvajalec vrste FURS uporablja trajna stanja najmanj `PREPARED`,
`SENDING`, `CONFIRMED`, `RETRYABLE`, `REJECTED` in `MANUAL_REVIEW`. Ob ponovitvi
uporabi isti pripravljeni podpisani zahtevek. Nadzira obstale operacije, uporablja
nadzorovan backoff, opozori pred potekom dveh oziroma desetih delovnih dni in ne
ustvari nove številke ali ZOI.

Koledar rokov upošteva slovenske dela proste dni, ne samo vikendov. Ročna
intervencija je revizijsko zabeležena in ne sme prepisati izvornega zahtevka.

Osrednja predstavitvena storitev iz istega nespremenljivega posnetka pripravi A4
in termični izpis z enakimi davčnimi zneski, ZOI, EOR, strojno berljivo kodo ter
evidentirano številko kopije.

## 16. Davčne evidence, izvoz in e-računi

Iz nespremenljivih listin se izdelajo evidence obračunanega in odbitka DDV po
posamezni listini, dogovorjeni računovodski izvozi ter ločena izvoza izdanih
računov in njihovih poznejših sprememb za davčni nadzor. Izvoz je ponovljiv in
vsebinsko vezan na uporabljeno različico sheme.

Podsistem e-računov loči vsebino od transporta. Validira e-SLOG oziroma dovoljeno
sintakso EN 16931, hrani strukturirani izvirnik in vizualizacijo ter podpira UJP,
registriranega ponudnika, PEPPOL ali dovoljeno neposredno sistemsko povezavo.
Statusi dostave in zavrnitve so idempotentni poslovni dogodki.

## 17. Retencija in dokazljivost

Politika retencije je različicena po vrsti dokumenta in pravni podlagi. Brisanje
je mogoče šele po poteku najdaljšega veljavnega roka in brez aktivnega pravnega
zadržanja. Izvorni dokument, njegove spremembe, sistemska dokumentacija in
revizijski dogodki ostanejo dostopni ter preverljivo povezani skozi celoten rok.
