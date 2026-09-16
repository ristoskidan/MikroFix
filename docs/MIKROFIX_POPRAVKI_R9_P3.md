# MikroFIX R9 – blagajniški izdatki odkupa P3

Datum izvedbe: 3. september 2026

## Izvedeno

- Podpisan in dokončno potrjen gotovinski odkup pripravi izdatek v stanju
  `CONFIRMED`; to še ni knjiženje v blagajniški dnevnik.
- V odprtem blagajniškem dnevniku je dodano dejanje
  `PRENESI ODKUPNI IZDATEK`.
- Uporabnik izbere konkreten čakajoči izdatek ter ga pred knjiženjem še enkrat
  fizično potrdi.
- Knjiženje zahteva odprto blagajno izbranega poslovnega prostora in naprave
  ter ne dovoli negativnega gotovinskega stanja.
- Vpis v blagajniški dnevnik in sprememba izdatka v stanje `BOOKED` sta ena
  nedeljiva transakcija čez pisarniško in servisno SQLite bazo.
- Isti izdatek je mogoče knjižiti samo enkrat. Ponovitev vrne obstoječo povezavo
  in ne ustvari novega gotovinskega dogodka.
- Izdatek hrani povezavo na blagajniško sejo in konkretno vrstico dnevnika.
- Čakajoče listine se ob zaprtju pokažejo kot opozorilo, vendar skladno s
  potrjeno odločitvijo ne blokirajo zaprtja blagajne.

## Preverjanje

- potrjen izdatek se knjiži z negativnim zneskom in pravilno referenco;
- dvojni klik oziroma ponovitev ne podvoji knjiženja;
- nepotrjen odkup se ne more knjižiti;
- izdatek, ki bi povzročil negativno gotovino, se v nobeni bazi ne spremeni;
- celoten samodejni nabor je po spremembi ponovno izveden.
