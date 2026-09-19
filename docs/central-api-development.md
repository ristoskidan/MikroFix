# MikroFIX central API — razvojni zagon

To je postopno grajen centralni razvojni rez. Sprejema `CreateServiceOrder`,
`CompleteServiceTechnically`, življenjski cikel partnerja, `CreateInventoryItem`,
izdajo servisnega ali neposrednega računa, blagajniški dnevnik,
`ConfirmPurchaseReceipt` in `AttachPurchaseInvoice`. Razvojni
in testni bazi imata centralni model uporabnikov, dovoljenj, registriranih
delovnih postaj, sej in revizijske sledi. Produkcijska centralna identiteta je
priklopljena za Servis; Blagajna in Office sta še v postopni migraciji.

Centralna Blagajna podpira tudi `OpenCashSession`, `RecordManualCashEntry`,
`CloseCashSession`, `SettleUnpaidBankInvoice` in `StornoInvoice`. Za posamezen poslovni prostor in elektronsko napravo je
dovoljena samo ena odprta seja. Gotovinska izdaja računa in pripadajoča knjižba
sta ena transakcija; kartično ali TRR plačilo fizičnega blagajniškega salda ne
spreminja. Zaporedje knjižb, tekoči saldo in končni PDF se vedno sestavijo iz
avtoritativnega centralnega posnetka.

Neplačan račun, prvotno izdan za TRR, se pozneje potrdi na istem računu. Dejansko
nakazilo na TRR samo označi plačilo in ne ustvari FURS ali gotovinske knjižbe.
Poznejša gotovina oziroma kartica isti račun prestavi v naknadno davčno
potrditev; gotovina se hkrati natanko enkrat vpiše v odprto blagajno. Strežnik
shrani čas prejema in varni rok desetih delovnih dni, prvotni posnetek postavk in
zneskov pa ostane nespremenjen.

Centralno zgodovino izdanih računov bereta Blagajna in Office iz istega vira.
Iskanje pokriva številko in datum računa, kupca in davčno številko, opis artikla,
barkodo ter identiteto oziroma številko povezanega servisa. Office je pri tem
samo bralec; izdaja in potrjevanje plačila ostajata odgovornost Blagajne.

`StornoInvoice` je različično varovan, atomski in idempotenten ukaz Blagajne z
ločenim dovoljenjem `STORNO_DOCUMENT`. Sprejme samo identiteto dokončno izdanega
računa, pričakovano različico, razlog in izrecno potrditev. Ustvari nov negativni
račun v istem centralnem zaporedju in ga poveže z izvirnikom. Gotovinski del
ustvari natanko eno negativno knjižbo v odprti blagajni istega prostora in
naprave; gotovinski ali kartični storno se dokonča šele po FURS. Storno nima
zalogovnega ali servisnega učinka. Fizično vračilo artikla, reklamacija in
ponovno odprtje servisa so ločeni poslovni dogodki.

Knjiženje prejema oziroma pripenjanje poznejšega dobaviteljevega računa zahteva
poleg dostopa do Office tudi ločeno centralno dovoljenje `CONFIRM_DOCUMENT`.
Strežnik obe pravici preveri znova ob poslovnem ukazu.

## Enkratna priprava

Kot `root`:

```sh
/home/mikrofix/setup_mikrofix_postgres_dev.sh
```

Kot uporabnik `mikrofix`, iz korena repozitorija:

```sh
.venv-server/bin/alembic upgrade head
MIKROFIX_DATABASE_URL=postgresql+psycopg:///mikrofix_test \
  .venv-server/bin/alembic upgrade head
```

## Integracijski testi

```sh
MIKROFIX_RUN_PG_TESTS=1 \
  .venv-server/bin/python -m unittest -v test_v32_central_api.py
```

Testi pred vsakim primerom pobrišejo samo vrstice iz tabel `*_v32` v bazi
`mikrofix_test`. Razvojne baze se ne dotaknejo.

Paket preverja tudi idempotenten tehnični zaključek, enkratno porabo po barkodi,
en obračunski osnutek, atomsko zavrnitev nezadostne offline zaloge ter sočasni
poskus dveh servisov za zadnji kos. Preverjeni so tudi seznam vsebinsko
zaklenjenih osnutkov brez zakupa, sočasna izdaja istega osnutka, eno centralno
številčenje, natančno ujemanje plačil, TRR zaključek in gotovinski prehod skozi
`PENDING_FISCALIZATION` oziroma `FISCAL_RECONCILIATION`. Zaloga je v nekaterih
starejših testih pripravljena neposredno. P8 testi uporabljajo dovoljeni tok
`DOB`/`DRA`/`PRA`: blokirana količina ni razpoložljiva, sprostitev se zgodi
natanko enkrat, servisna poraba pa se FIFO poveže s konkretnim LOT-om.

Integracijski paket preverja še sočasno odpiranje iste blagajne, sočasno
knjiženje dveh dogodkov, nespremenljivo zaporedje salda, idempotentno ponovitev
odprtja, ročne knjižbe in zaprtja ter zavrnitev gotovinskega računa brez odprte
seje. Preverjen je tudi sočasni poskus dveh Office odjemalcev sprostiti isti
prejem: uspe samo eden, zaloga se poveča enkrat in porabi se ena številka PRA.
Preizkusi storna dodatno dokazujejo eno negativno številko, eno gotovinsko
vračilo, referenco na izvirni FURS račun, nespremenjeno zalogo in nespremenjene
LOT-razporeditve ter varno ponovitev istega ukaza. Sočasna storna dveh
blagajnikov ustvarita največ en negativni dokument in porabita največ eno novo
številko. Po nejasnem rezultatu FURS ponovitev pošlje isti trajno shranjeni
zahtevek z isto identiteto; izvirnik do potrditve ostane v stanju
`REVERSAL_PENDING`.

Fizični prejem vrnjenega prodajnega artikla oziroma odstranjenega servisnega
dela je ločen od storna. Centralni zapis se vedno poveže z izvirnim računom,
vrstico in LOT-om ter najprej dobi stanje `QUARANTINED`; razpoložljive zaloge
ne poveča. Strežnik pod zaklepom zavrne skupno vrnjeno količino, večjo od
dejansko porabljene količine iste vrstice in LOT-a. Office nato z dovoljenjem
`MANAGE_INVENTORY` enkrat izbere sprostitev v isti LOT, označitev okvare ali
usmeritev v reklamacijski pregled. Samo sprostitev poveča zalogo, pri čemer LOT
ne sme preseči prvotno prejete količine. Trenutna migracijska glava je
`20260914_21`.

Blagajna podrobnosti vračila ne sestavlja iz prikazane vrstice računa. Pred
prejemom od strežnika prebere še dovoljeno količino in natančno identiteto
izvirnega LOT-a, nato pa fizični prejem najprej trajno zapiše v lokalni dnevnik
ukazov. Centralna zgodovina računa ponuja ukaz `FIZIČNO VRAČILO ARTIKLA` in
jasno pove, da kos preide v karanteno. Office ima ločen pregled `KARANTENA
VRAČIL`; odločitev z razlogom in ponovno potrditvijo uporablja isti trajni
dnevnik. Izgubljen odgovor zato ne ustvari drugega prejema ali druge razrešitve.

Service prejete inventurne spremembe hrani ločeno od starih lokalnih zalogovnih
tabel. `mikrofix_service_inventory.py` iz njih izračuna razpoložljivost in do
prihoda ustreznega centralnega dogodka odšteva lokalno že zaključene porabe.
Potrdilo HTTP samo še ni dokaz osveženega posnetka. Pot
`/v1/service/inventory-snapshot` zagotovi začetni celotni popis iz ene
`REPEATABLE READ` transakcije, skupaj z zaporedno številko in markerjem
`complete`. UI preklopi na centralni iskalnik šele po atomskem lokalnem zapisu
tega markerja. Sprotni dogodek z isto različico in starejšo zaporedno številko
posnetka ne more prepisati.

Enako strogo je ločena evidenca prej vgrajenih delov. Ob obračunskem zaključku
centralni dogodek `ServiceBillingCompleted` posreduje Service računalniku samo
strežniško potrjene povezave servis–račun–vrstica–LOT in še odstranljivo
količino. Ker posamezni računalnik ob nadgradnji lahko že ima checkpoint za
starejše dogodke, `/v1/service/returnable-parts-snapshot` vrne tudi popoln,
`REPEATABLE READ` začetni posnetek z njegovo centralno zaporedno številko.
Service fizično odstranitev zapiše v lokalni outbox tudi brez povezave; lokalni
prikaz jo takoj odšteje kot čakajočo, vendar zaloge ne poveča. Šele sprejet
centralni dogodek jo potrdi v projekciji in jo uvrsti v karanteno. Zavrnitev
ustvari viden konflikt z računom, vrstico, LOT-om, količino in razlogom; ne
ustvari `STOCK_EXCEPTION`, ne odpre starega servisa in ne spremeni računa.

Uporabniška seja velja samo za trenutni proces. Vsak zagon zahteva novo prijavo,
ob ročni odjavi ali nadzorovanem izhodu pa aplikacija zabeleži odjavo, odstrani
identiteto in dovoljenja iz pomnilnika ter centralno sejo prekliče, če je
strežnik dosegljiv. Dostopnega žetona ni dovoljeno zapisati v SQLite, nastavitve
ali dnevnik. Offline dovoljena lokalna prijava zato ni obnovitev stare seje;
ustvari novo lokalno sejo brez centralnega žetona.

Office ima bralne poti za partnerje, artikle in njihove dobaviteljeve identitete,
prejeme ter uporabniške vrste dokumentov. `mikrofix_office_client.py` pred vsakim
poslovnim pošiljanjem trajno shrani nespremenljiv ukaz. Digitalne priloge so
neobvezne; pogodba sprejme njihov kontrolni opis, dejanski prenos datotek v
centralni upravljani arhiv pa še ni produkcijsko priklopljen.

Nov artikel lahko ustvari samo Office z dovoljenjem `MANAGE_INVENTORY`. Nastane
z ničelno količino in ničelno ceno; količine ni mogoče vnesti mimo dobavnice ali
drugega dovoljenega dokumentiranega izvora. Glavna barkoda je centralno
enolična. Prvi potrjeni prejem nato ustvari dobaviteljevo identiteto, LOT in
preverjeno prodajno ceno.

Office lahko prodajno ceno pozneje spremeni samo z dovoljenjem `MANAGE_PRICES`,
pričakovano različico artikla in obveznim razlogom. Strežnik zaklene artikel,
ponovno izračuna najvišji nabavni strošek še razpoložljivih LOT-ov in zavrne
ceno pod varovalko strošek + 5 %. Sprejeta odločitev ohrani staro in novo ceno,
stroškovno osnovo, uporabnika, razlog ter identiteto operacije; sprememba je
vidna Servisu in Blagajni prek istega inventurnega dogodka. Office lahko celotno
zgodovino cene prebere na artiklu; nabavna stroškovna osnova se s tem ne razkrije
Blagajni ali Servisu.

Office lahko partnerja tudi ustvari, popravi, deaktivira ali ponovno aktivira.
Vsaka sprememba zahteva pričakovano različico zapisa; sočasna popravka istega
partnerja zato nimata tihega zadnjega zmagovalca. Davčna številka ostaja enolična,
deaktiviran partner ostane v Office zgodovini, Blagajna pa ga ne ponuja za nove
račune. Že izdani računi vedno ohranijo svoj prvotni posnetek kupca.

`mikrofix_office_runtime.py` združuje centralne Office bralne poti in spremembe v
en UI-neodvisen potek. Vsak potrjen prejem, pozneje pripeti račun ali sprememba
partnerja se najprej nespremenljivo shrani v lokalni dnevnik ukazov. Nejasen
omrežni rezultat blokira nove Office spremembe na isti postaji, dokler se ne
ponovi isti ukaz z isto identiteto. Lokalni dnevnik ni druga poslovna baza in ne
omogoča dela Office brez strežnika. Razvojni zagonski most je pripravljen; priklop
velikega obstoječega Office zaslona se izvede po posameznih zaključenih
funkcionalnih rezih, da se lokalno in centralno knjiženje ne pomešata.

Glavni namizni program ob nastavljenem `database.server_url` zdaj odpre prvi
centralni Office pregled za zalogo, prejeme, izdane račune in partnerje.
Partnerje je mogoče ustvariti, urediti, deaktivirati in znova aktivirati z
različico zapisa ter razlogom spremembe. Zaslon omogoča tudi izrecno uskladitev
istega nedokončanega ukaza po izgubljenem odgovoru. Prejem je mogoče sestaviti,
po barkodi poiskati ali ustvariti artikel ter ga centralno potrditi. Pri `DOB`
ostane zaloga blokirana do pozneje pripetega računa; `DRA` jo sprosti v isti
transakciji. Poznejši račun ne sme zamenjati artikla, količine, barkode, LOT-a ali
vrstnega reda že fizično potrjenih postavk.

Nedokončan obrazec se s kontrolnim odtisom samodejno hrani v lokalni SQLite
datoteki samo za istega uporabnika in postajo. To ni zalogovna baza in ne knjiži
nobenega poslovnega dogodka. Ob izgubljenem odgovoru ohrani isto identiteto
ukaza; poslovno zavrnjen in popravljen obrazec dobi novo identiteto. Prazen URL
odpre stari lokalni Office. Neposreden samostojni zagon stare datoteke
`mikrofix_office.py` še ni centralni zaganjalnik in ni del centralnega testnega
postopka.

Centralni seznam prejemov vrne tudi izvirne finančne podatke vsake vrstice
(neto ceno, rabat, DDV, dobaviteljev znesek in razporeditev prevoza) ter podatke
povezanega računa. Obrazec poznejšega računa to osnovo prebere s strežnika;
odjemalec je ne rekonstruira in ne ugiba. Strežnik pred sprostitvijo zaloge
ponovno preveri finančne vsote, prodajno cenovno varovalko in identiteto fizično
prejetih postavk.

Centralni P7 model je že prisoten: odpiranje osnutka ne ustvari `CLAIMED`, ukaz
izdaje pa sprejme samo plačilno izbiro in identitete blagajne. Vsebina računa se
izračuna iz strežniškega posnetka. Enolične omejitve in zaklep vrstice preprečijo
dvojno izdajo in dvojno številko. Gotovinski račun se ne šteje za dokončno
izdanega in servis se ne zapre, dokler isti fiskalni dogodek nima potrjenega EOR.
Dejanski centralni omrežni izvajalnik s produkcijskim potrdilom ostaja izključen.

Centralni servisni osnutek ima v plačilnem koraku tudi izrecno izbiro
`ZERO_BALANCE`. Blagajna pri prvem F10 izdela neknjižen predogled z vsemi
pozitivnimi vrsticami in nato pripadajočimi negativnimi vrsticami v istem
vrstnem redu. Pri drugem F10 ne pošlje teh vrstic ali plačila, temveč samo
strežniško izbiro `settlement_mode=ZERO_BALANCE` in `payments=[]`. Izbira je
del lokalnega varnostnega osnutka in kontrolnega odtisa predogleda. Neposredna
prodaja tega načina ne ponuja.

F9 lahko išče aktivne centralne partnerje in pridobi nepotrjen AJPES-predlog.
Ločeni `CreatePartner` zahteva izrecno potrditev pregleda, je idempotenten in ne
omogoča spremembe ali deaktivacije obstoječega partnerja. Pred izdajo se izbrana
različica partnerja še enkrat preveri. UI-neodvisni blagajniški odjemalec ukaz
pred pošiljanjem zapiše v lokalni varnostni dnevnik; ta dnevnik ni offline
poslovni način Blagajne, ampak zaščita pred izgubljenim HTTP-odgovorom.

Neposredna prodaja uporablja `IssueDirectSaleInvoice`. Ukaz vsebuje prodajno
identiteto, barkode, količine, zahtevani rabat, kupca in plačilo, ne vsebuje pa
zaupanja vrednih cen, nabavnih vrednosti ali FIFO razporeditve. Strežnik vse to
izračuna in knjiži v eni transakciji. Javni posnetek hrani samo uporabljeni
rabat; zahtevani rabat ter odločitev cenovne varovalke sta v
`direct_sale_discount_audit_v32`, porabljeni LOT-i pa v
`direct_sale_stock_allocations_v32`. Blagajna išče prodajno zalogo prek
`GET /v1/cashier/inventory`, ki ne vrača nabavnih cen.

Blagajna lahko isti račun po prekinitvi vedno znova prebere prek centralne
identitete računa in pregleda centralno zgodovino po stanju. Starejši lokalno
nedokončan ukaz blokira pripravo novega ukaza, dokler ni usklajen v prvotnem
vrstnem redu. Pri FURS se pred omrežnim klicem v PostgreSQL nespremenljivo
shranijo podpisani zahtevek, njegov kontrolni odtis, `MessageID` in ZOI;
ponovitev zato ne more sestaviti nove davčne identitete. Izvajalnik obstaja kot
ločena strežniška meja, vendar sam ne vključi produkcijskega okolja in ne nalaga
certifikata brez izrecne konfiguracije.

## Lokalni zagon

Žeton je trenutno samo razvojna zaščita transporta in ni končna prijava:

```sh
export MIKROFIX_DEV_SYNC_TOKEN='zamenjaj-z-dolgim-lokalnim-zetonom'
.venv-server/bin/python -m mikrofix_server
```

Strežnik se privzeto veže samo na `127.0.0.1:8765`. Pred uporabo na drugih
računalnikih so obvezni TLS, prava identiteta uporabnika in naprave, rotacija
skrivnosti ter omejitve omrežnega dostopa.

Razvojni žeton je v okolju `production` prepovedan in niti v razvoju nima
dostopa do `/v1/admin/*`. Prvi centralni skrbnik in Office računalnik se na
prazni migrirani bazi ustvarita interaktivno:

```sh
.venv-server/bin/python -m mikrofix_server.bootstrap
```

Ukaz PIN prebere brez izpisa in skrivnost Office računalnika pokaže samo enkrat.
Ni bil zagnan nad dejanskimi uporabniškimi podatki. Nadaljnje uporabnike in
računalnike registrira skrbnik prek administrativnega API-ja. Service računalnik
ob registraciji pošlje samo javni Ed25519 ključ; zasebni ključ z dovoljenji 0600
ostane lokalno.

## Priklop Servisa

V načinu TEST lahko Servis še uporablja izključno razvojni transportni žeton:

```sh
export MIKROFIX_SYNC_TOKEN="$MIKROFIX_DEV_SYNC_TOKEN"
export MIKROFIX_SYNC_INTERVAL_SECONDS=15
```

V aktivni datoteki nastavitev mora biti `database.server_url` na primer
`http://127.0.0.1:8765`. Prazen URL pomeni `SYNC: NI NASTAVLJEN`; nastavljen URL
brez okoljske skrivnosti pomeni `SYNC: MANJKA SKRIVNOST`. Sinhronizacija teče v
ozadju ob zagonu, po novem lokalnem dogodku in periodično. Ne blokira zaključka
servisa in nima produkcijskega obvoda.

Produkcijski Servis ne uporablja skupnega razvojnega žetona. Delovna postaja
mora biti centralno registrirana z oznako, ki jo program sestavi iz poslovnega
prostora in elektronske naprave. Enkrat izdana skrivnost postaje in pot do
zasebnega Ed25519 ključa se podata procesu, ne zapišeta pa se v nastavitve ali
Git:

```sh
export MIKROFIX_WORKSTATION_SECRET='<skrivnost registrirane postaje>'
export MIKROFIX_SERVICE_SIGNING_KEY='/zaščitena/pot/service-workstation-signing.key'
```

Če pot ni podana, je privzeta datoteka
`<data_dir>/service/service-workstation-signing.key`. Imeti mora dovoljenja
`0600`; program je ne ustvari sam, ker mora biti njen javni ključ že vezan na
točno registrirano postajo.

Centralni dostopni žeton obstaja samo v pomnilniku. Ob ročni odjavi ali izhodu
se sinhronizacija najprej ustavi, seja se centralno prekliče in lokalna
identiteta pozabi tudi, če je strežnik nedosegljiv. Za delo brez povezave se
lokalno hrani samo zadnja potrjena centralna identiteta uporabnika in različica
dovoljenj. Prva prijava uporabnika zato zahteva povezavo. Izrecno zavrnjena
prijava, napačen odgovor strežnika ali obvezna še neopravljena menjava PIN-a se
nikoli ne obravnavajo kot izpad omrežja. Offline servisni dogodki se podpišejo z
zasebnim ključem postaje in ob sinhronizaciji ponovno preverijo na strežniku.
Ker PIN ni shranjen za poznejše samodejno prijavljanje, gumb `PRIJAVA / SYNC`
po povratku omrežja zahteva PIN istega uporabnika, zamenja morebitno pretečeno
sejo in takoj zahteva sinhronizacijo.

Servisni čas ni izračunan iz strežniške ure. Lokalni interval shrani stenski
začetek za revizijo ter monotoni začetek in identiteto zagona Linux sistema za
trajanje. Monotoni podatek je uporaben tudi po nenadzorovanem ponovnem zagonu
aplikacije, dokler računalnik ni bil ponovno zagnan. Ob drugačni identiteti
zagona se uporabi stenski zapis; premik ure, ki bi dal negativen čas, je
blokiran in prikazan kot napaka. Centralni ukaz vedno pošlje že izračunane
sekunde, strežnik pa jih ne preračuna. Tehnični zaključek uporabi monotoni
marker trenutka klika tudi takrat, ko mora pred transakcijo dokončati varnostno
kopijo. Samodejno strežniško opozorilo za zelo
veliko odstopanje med stenskima časoma in izmerjenim trajanjem je še odprto.

## Razvojni priklop Blagajne

Skupno odjemalsko jedro `mikrofix_central_identity.py` podpira pravo spletno
prijavo za samostojna Office in Blagajno. Vrnjeno sejo sprejme samo, če se
ujemajo uporabnik, program in registrirana postaja ter seja vsebuje zahtevano
modulsko dovoljenje. Žeton ostane samo v pomnilniku; zavrnjena neujemajoča seja
se poskusi takoj preklicati.

Produkcijska zagona uporabljata `mikrofix_central_desktop.py` in se brez
centralnega URL-ja ali popolne identitete postaje varno ustavita. Tudi kadar oba
programa tečeta na istem fizičnem računalniku, imata ločeni aplikacijski
identiteti:

```sh
export MIKROFIX_OFFICE_WORKSTATION_CODE='MFD-PIS-OFFICE'
export MIKROFIX_OFFICE_WORKSTATION_SECRET='<skrivnost Office postaje>'
export MIKROFIX_CASHIER_WORKSTATION_CODE='MFD-PIS-CASHIER'
export MIKROFIX_CASHIER_WORKSTATION_SECRET='<skrivnost blagajniške postaje>'
```

Vrednosti morajo ustrezati vnaprej registriranima centralnima postajama in se
ne zapisujejo v nastavitve ali Git. Vsak zagon pokaže novo prijavo, zahteva
prisilno menjavo prvega PIN-a, če tako določi strežnik, ob zaprtju pa najprej
pozabi žeton in centralno sejo poskusi preklicati. HTTP 401 pomeni poteklo ali
preklicano sejo: odjemalec ga uporabniku jasno razlikuje od omrežne napake,
enkrat sproži zapiranje aplikacije in zahteva nov zagon ter prijavo. Že trajno
pripravljen ukaz pri tem ostane nedokončan in se po novi prijavi ponovi z isto
identiteto. HTTP 403 ne povzroči samodejne odjave, saj pomeni tudi običajno
zavrnitev posameznega dovoljenja. Odprt ostaja ročni end-to-end preizkus.

Glavni namizni program odpre centralni tok Blagajne, kadar je nastavljen
`database.server_url`. Uporabi isto razvojno okoljsko skrivnost
`MIKROFIX_SYNC_TOKEN`; skrivnost se ne zapisuje v nastavitve ali lokalno bazo.

Prazen URL ohrani izrecni stari lokalni način. Nastavljen URL brez skrivnosti,
nedosegljiv centralni strežnik ali napaka centralnega ukaza ne smejo povzročiti
samodejnega lokalnega knjiženja. Produkcijski način razvojno skrivnost zavrne,
dokler ni priklopljena centralna uporabniška seja in identiteta blagajniške
postaje.

Lokalni in centralni obnovitveni osnutki imajo oznako izvora. Stari osnutki brez
oznake veljajo za lokalne. Če način osnutka ne ustreza trenutno izbranemu načinu
Blagajne, se zagon ustavi brez spremembe osnutka. Enako centralnega toka ni
dovoljeno odpreti, dokler lokalno izdani račun še čaka na varno dokončanje
prvotnega izpisa.
