# MikroFIX Linux – obvezna preizkusna matrika

## Sistem

- [ ] uradni LMDE 7 ISO in SHA256 sta preverjena;
- [ ] sistemski disk je šifriran;
- [ ] ni samodejne prijave, zaslon se zaklene po 10 minutah;
- [ ] varnostne posodobitve in UFW so vključeni;
- [ ] MikroFIX teče kot navaden uporabnik, nikoli kot root;
- [ ] `preveri_mikrofix_linux.sh` nima napak.

## Epson TM-T20II

- [ ] CUPS vidi samo pravo USB napravo in izbrano vrsto;
- [ ] testna stran, 80-mm račun, dolg naziv, šumniki in QR so čitljivi;
- [ ] samodejni rez deluje;
- [ ] F10 prvič izdela PDF, drugič izda in natisne;
- [ ] ob odklopljenem tiskalniku račun ostane izdan, PDF pa dosegljiv;
- [ ] ponovitev tiska ne ustvari novega računa.

## MicroDetect

- [ ] `idevice_id -l`, `idevicepair validate` in `ideviceinfo` uspejo;
- [ ] brez Trust se osebni podatki ne odprejo;
- [ ] po Trust se prenesejo model, serijska, IMEI in pomnilnik;
- [ ] dve napravi hkrati sta zavrnjeni;
- [ ] odklop med branjem ne ustvari delnega servisnega naročila.

## Poslovni tokovi

- [ ] novi prejem, osnutek, uskladitev, knjiženje in celotni storno;
- [ ] zaloga, LOT, rezervacija, servis in prenos v blagajno;
- [ ] odkup, dvakratni podpis, bančno oziroma gotovinsko izplačilo;
- [ ] blagajniški dnevnik in enkratno knjiženje odkupnega izdatka;
- [ ] obnovljena naprava, paket, 12-mesečni podpisani dogovor in prodaja;
- [ ] deljeno plačilo je do centa natančno;
- [ ] storno in nadomestni račun zahtevata pravo dovoljenje.

## Obnovitev in izpad

- [ ] izpad med vsakim kritičnim korakom obnovi samo isti osnutek;
- [ ] drugi uporabnik ne more prevzeti tujega osnutka;
- [ ] šifrirana kopija nastane na ločenem nosilcu;
- [ ] napačno geslo in spremenjena kopija sta zavrnjena;
- [ ] obnova na praznem TEST profilu vrne vse baze in dokumente;
- [ ] odklopljen USB ne preusmeri kopije na notranji disk.

## FURS

- [ ] TEST uporablja samo testni `.p12` in testni naslov;
- [ ] produkcijski certifikat je v TEST konzoli zavrnjen;
- [ ] brez okoljske potrditve je produkcijski FURS vedno zaklenjen;
- [ ] produkcija se obravnava šele po vseh zgornjih potrditvah.
