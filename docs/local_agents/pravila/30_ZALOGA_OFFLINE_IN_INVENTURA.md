<!-- GENERIRANO iz docs/MIKROFIX_GPT_OSS_20B_KONTEKST.md; ne urejaj ročno. -->

## 10. Zaloga

Pozitivna zaloga lahko nastane samo iz dveh dokumentiranih virov:

1. potrjena dobavnica oziroma vhodna nabava, ki je bila prevzeta, pregledana in sproščena v zalogo;
2. potrjen uporaben del, pridobljen iz naprave z dokumentiranim odkupom in donorskim postopkom.

Prepovedani so:

- `STOCK_EXCEPTION` kot splošni mehanizem;
- negativna zaloga;
- ročni pozitivni popravek brez dovoljenega izvora;
- inventurni višek kot tretji izvor zaloge;
- samodejno knjiženje domnevnega viška.

Inventurni višek ostane nerazpoložljiv, dokler ni vezan na dovoljen dokumentiran izvor.

Črtna koda je osnovna identiteta zaloge. Več enakih kosov ima lahko skupen sledljiv LOT; serijsko vodeni artikel ima enolično identiteto.

Service pošlje dejansko porabo dela natanko enkrat ob tehničnem zaključku. Izdaja računa porabe ne ponovi. Blagajna lahko istočasno neodvisno prodaja fizično razpoložljive artikle. Rezervacija dela je dovoljena možnost, ni pa obvezna za sprostitev ali porabo zaloge.

Če ob sinhronizaciji zaloge ni dovolj:

- transakcija ne sme delno knjižiti ničesar;
- centralni osnutek ne nastane;
- sporni artikel oziroma črtna koda se jasno pokaže;
- nalog dobi stanje **SYNC_CONFLICT** in čaka na razrešitev;
- sistem ne sme sam izmisliti dodatnega kosa.

---

---

## 11. Delo brez povezave

- Service lahko brez povezave odpre in tehnično zaključi poljubno število servisov.
- Uporablja zadnji veljavni posnetek zaloge; nima ločene fizične zaloge.
- Office in Blagajna lahko medtem centralno uporabljata isto fizično zalogo.
- Končni razplet določi centralna transakcija ob sinhronizaciji. Konflikt je viden in se ne rešuje s skrito negativno zalogo.

Offline oznaka ima obliko:

`MF-OFF-<POSTAJA>-<LETO>-<ZAPOREDJE>`

Ta oznaka ostane trajna sled. Strežnik po uspešni sinhronizaciji doda centralno oznako `MF-<LETO>-<ZAPOREDJE>`. Vsak dogodek vsebuje UUID, izvorno namestitev in lokalno zaporedje, zato se vrstni red ne zameša in ponovitev ne ustvari dvojnika.

---

---

## 14. Inventura

Na dan polne inventure:

- je prodaja zaprta;
- vsi odprti dokumenti so zaključeni in knjiženi;
- vse registrirane Service postaje so sinhronizirane;
- noben outbox ali konflikt ne sme ostati odprt;
- manjkajoča postaja blokira začetek polne inventure;
- inventurna blokada prepreči vse operacije, ki spreminjajo zalogo ali dokumente.

Delna inventura je lahko samo pregledna, brez knjiženja, dokler niso izpolnjeni varni pogoji.

---
