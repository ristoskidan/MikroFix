# MikroFIX central API — razvojni zagon

To je prvi, namenoma ozek sinhronizacijski rez. Sprejema `CreateServiceOrder`,
`CompleteServiceTechnically`, `CreatePartner`, `IssueServiceInvoice`,
`ConfirmPurchaseReceipt` in `AttachPurchaseInvoice`. Razvojni
in testni bazi imata centralni model uporabnikov, dovoljenj, registriranih
delovnih postaj, sej in revizijske sledi; produkcijski preklop ni izveden.

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

Service prejete inventurne spremembe hrani ločeno od starih lokalnih zalogovnih
tabel. `mikrofix_service_inventory.py` iz njih izračuna razpoložljivost in do
prihoda ustreznega centralnega dogodka odšteva lokalno že zaključene porabe.
Potrdilo HTTP samo še ni dokaz osveženega posnetka. Pot
`/v1/service/inventory-snapshot` zagotovi začetni celotni popis iz ene
`REPEATABLE READ` transakcije, skupaj z zaporedno številko in markerjem
`complete`. UI preklopi na centralni iskalnik šele po atomskem lokalnem zapisu
tega markerja. Sprotni dogodek z isto različico in starejšo zaporedno številko
posnetka ne more prepisati.

Office ima bralne poti za partnerje, artikle in njihove dobaviteljeve identitete,
prejeme ter uporabniške vrste dokumentov. `mikrofix_office_client.py` pred vsakim
poslovnim pošiljanjem trajno shrani nespremenljiv ukaz. Digitalne priloge so
neobvezne; pogodba sprejme njihov kontrolni opis, dejanski prenos datotek v
centralni upravljani arhiv pa še ni produkcijsko priklopljen.

Centralni P7 model je že prisoten: odpiranje osnutka ne ustvari `CLAIMED`, ukaz
izdaje pa sprejme samo plačilno izbiro in identitete blagajne. Vsebina računa se
izračuna iz strežniškega posnetka. Enolične omejitve in zaklep vrstice preprečijo
dvojno izdajo in dvojno številko. Gotovinski račun se ne šteje za dokončno
izdanega in servis se ne zapre, dokler isti fiskalni dogodek nima potrjenega EOR.
Dejanski centralni omrežni izvajalnik s produkcijskim potrdilom ostaja izključen.

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

## Razvojni priklop Servisa

Servis zažene sinhronizacijo samo, če sta hkrati nastavljena URL in skrivnost.
Skrivnost se ne zapisuje v JSON:

```sh
export MIKROFIX_SYNC_TOKEN="$MIKROFIX_DEV_SYNC_TOKEN"
export MIKROFIX_SYNC_INTERVAL_SECONDS=15
```

V aktivni datoteki nastavitev mora biti `database.server_url` na primer
`http://127.0.0.1:8765`. Prazen URL pomeni `SYNC: NI NASTAVLJEN`; nastavljen URL
brez okoljske skrivnosti pomeni `SYNC: MANJKA SKRIVNOST`. Sinhronizacija teče v
ozadju ob zagonu, po novem lokalnem dogodku in periodično. Ne blokira zaključka
servisa in nima produkcijskega obvoda.
