# MikroFIX – varnostni in strukturni pregled R9

Datum: 3. 9. 2026  
Pregledana osnova: `MikroFIX_Service_v30_R9_DEV`  
Stanje kode: med pregledom ni bila popravljena ali funkcionalno spremenjena.

## Rezultat

- Celotni samodejni paket: **97/97 testov uspešnih**.
- Codex Security: **2 potrjeni ugotovitvi srednje stopnje**.
- Pregledane varnostne površine: **13**.
- Produkcijska FURS povezava in SI Price sta skladno z dogovorom ostala zunaj izvedbe ter prideta na vrsto pred produkcijskim zagonom.

## Obvezna popravka pred produkcijo

### P1 – dovoljenja morajo veljati tudi pri neposrednem zagonu

`mikrofix_office.py` po neposrednem zagonu preveri veljaven PIN, ne pa tudi pravice `OFFICE`. Pri argumentu `--new-invoice` lahko po isti poti odpre Blagajno brez ločenega preverjanja pravice `CASHIER`. Glavni meni ti pravici sicer pravilno preverja, zato je težava v alternativni vstopni točki.

Popravek mora:

1. zahtevati `OFFICE` ob vsakem zagonu Office;
2. zahtevati `CASHIER` ob vsakem odpiranju Blagajne;
3. preverjati pravico tudi v osrednjem poslovnem ukazu za izdajo, knjiženje, storno in spremembo zaloge;
4. dodati teste, ki module kličejo mimo glavnega menija.

### P1 – servisna fotografija ne sme biti poljubna izvršljiva datoteka

Izbirnik fotografij trenutno ponuja tudi »Vse datoteke«. Datoteka se kopira brez preverjanja vsebine, ohrani izvirno končnico, pozneje pa jo dvoklik odpre z Windows `os.startfile`. Tako bi se lahko namesto slike izvedla datoteka `.exe`, `.bat`, `.cmd`, `.lnk` ali druga aktivna vsebina.

Popravek mora:

1. dovoliti le dejansko dekodirane slike;
2. uvesti omejitev velikosti;
3. sliko prekodirati v nov, programsko določen JPG ali PNG;
4. uporabljati interni varni pregledovalnik namesto Windows dejanja po končnici;
5. enako centralno pravilo uporabiti za vse fotografije in priloge.

## Pomemben funkcionalni popravek

Pri zagonu programa z USB-ja so baze namenoma na lokalnem disku. Shranjevanje servisnih fotografij pa pot računa relativno glede na mapo programa. Če je program na USB-ju, podatki pa v uporabnikovi mapi Documents, `relative_to(APP_DIR)` odpove. Posledica je neuspešno shranjevanje servisa s fotografijo in osirotela kopirana datoteka.

Pot fotografije mora biti relativna glede na podatkovno mapo oziroma mora biti shranjena kot nadzorovana absolutna pot z migracijsko strategijo. Kopiranje datoteke in zapis v bazo morata imeti tudi kompenzacijsko čiščenje ob napaki.

## Potrjene dobre varovalke

- ponovni klik ne izda dveh računov in ne odvzame zaloge dvakrat;
- istega servisa ni mogoče zaračunati dvakrat;
- knjiženi prejemi, računi in blagajniški zapisi uporabljajo storno sled;
- prejem sprosti zalogo samo enkrat in zahteva natančno finančno ujemanje;
- odkupna transakcija je idempotentna in se pri napaki razveljavi čez vse vključene baze;
- operacijski dnevnik zazna prekinjene operacije;
- PIN uporablja `scrypt`, primerjavo v stalnem času in zaklep po napačnih poskusih;
- predaja seje je kratkotrajna, namenska in enkratna;
- FURS TEST je omejen na uradni testni naslov, zavrne produkcijsko potrdilo ter preverja TLS in podpis odgovora;
- zunanje varnostne kopije so šifrirane z AES-256-GCM, preverjene in testirane z obnovitvijo;
- v pregledani kodi ni `shell=True`, sestavljenega ukaza iz uporabniškega vnosa ali izklopljenega TLS preverjanja;
- trenutne dostopne SQLite baze uporabljajo `journal_mode=delete` in `synchronous=FULL`.

## Utrditve pred prvo produkcijsko izdajo

1. Program naj ob vsakem odpiranju vseh baz zahteva `journal_mode=delete`, `synchronous=FULL`, `foreign_keys=ON` in primeren `busy_timeout`; ob odstopanju naj produkcijo blokira.
2. Dovoljenja naj bodo del poslovnih ukazov, ne samo vidnosti gumbov in zavihkov.
3. Revizijska sled naj dobi kriptografsko povezovanje zapisov oziroma ločen, samo-dodajalni izvoz.
4. Žive baze, PDF-ji in fotografije naj bodo zaščiteni z namenskim Windows računom, NTFS pravicami in šifriranjem diska. Šifrirana kopija sama ne šifrira delovnih datotek.
5. Odvisnosti naj dobijo točen zaklep različic in kontrolnih vsot. GitHub naj uporablja Dependabot ter avtomatske teste.
6. Obnovitev naj se pred vsako izdajo izvede v prazno testno mapo in preveri s kontrolnimi scenariji.
7. Produkcijski FURS mora dobiti ločen pregled in dvostopenjsko aktivacijo šele po končanem TEST postopku.

## GitHub varna začetna osnova

Repozitorij mora biti zaseben in ne sme vsebovati baz, osebnih podatkov, računov, pogodb, fotografij, varnostnih kopij, `.p12/.pfx`, zasebnih ključev ali lokalnih nastavitev s potmi in certifikati.

Prvi uvoz se izvede v tem vrstnem redu:

1. inventar datotek in skrivnosti;
2. strogi `.gitignore`;
3. samo izvorna koda, umetni testni podatki, testi in dokumentacija;
4. lokalni zagon 97 testov;
5. zasebni začetni commit;
6. zaščitena veja `main` in spremembe samo prek ločenih vej oziroma pregledov;
7. avtomatski testi, pregled odvisnosti in iskanje skrivnosti;
8. Codex pregled in Codex Security pregled vsake pomembne spremembe.

Za nadaljevanje GitHub dela je potreben samo URL praznega zasebnega repozitorija na računu `ristoskidan`, do katerega ima GitHub povezava v Codexu dostop. Gesel, PIN-ov, certifikatov ali žetonov se ne pošilja.

## Opomba o obsegu

Varnostni pregled je bil narejen na prvotnem posnetku R9. Med pregledom so avtomatski testi ustvarili oziroma osvežili generirane datoteke, zato je Codex Security pravilno opozoril, da se je vsebina delovne mape po začetnem posnetku spremenila. Ugotovitve ostajajo vezane na zabeleženi začetni posnetek; pred izdajo se zato izvede še pregled končnega Git commita.
