# MikroFIX R9 – varnostni popravki P1

Datum: 3. 9. 2026

## Izvedeno

- neposredni zagon Office zahteva dovoljenje `MODULE_OFFICE`;
- neposredni zagon blagajne zahteva dovoljenje `MODULE_CASHIER`;
- zavrnjen dostop se zabeleži v revizijsko sled;
- servisne fotografije sprejemajo samo JPG, PNG, WEBP ali BMP do 25 MB in 80 milijonov slikovnih točk;
- fotografije se dekodirajo, ponovno kodirajo v programsko določen JPG in s tem odstranijo aktivna vsebina ter metapodatki;
- fotografije se odpirajo v internem pregledovalniku, ne prek Windows dejanja po končnici;
- poti fotografij so omejene na `service/photos` in pravilno delujejo tudi, ko je program na USB-ju, podatki pa na lokalnem disku;
- ob neuspešni transakciji se že ustvarjene fotografije odstranijo;
- priloge prejetih dokumentov dobijo končnico iz preverjene vsebine, ne iz uporabnikovega imena datoteke;
- FURS TEST odjemalec je lažje izolirano preizkusljiv, pri običajnem delovanju pa še vedno zahteva omrežno knjižnico, uradni testni naslov, TLS in namensko testno potrdilo.

## Preverjanje

- 105 avtomatskih testov: uspešno;
- Python preverjanje sintakse spremenjenih modulov: uspešno;
- Codex Security Standard scan `7d2be79c-ed67-4f6b-a55d-5283db922eaf`: 13 pregledanih varnostnih površin, 0 novih potrjenih ugotovitev na zamrznjenem posnetku;
- dodatna utrditev prilog po zamrznitvi posnetka je bila preverjena z namenskim testom in nato s celotnim paketom 105 testov.

## Obseg

Produkcijska povezava FURS in SI Price ostajata namenoma izven tega popravka. Žive baze, osebni podatki, računi, fotografije, zasebna potrdila in lokalne nastavitve niso vključeni v Git.
