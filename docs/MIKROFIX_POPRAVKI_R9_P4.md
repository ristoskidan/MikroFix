# MikroFIX R9 – varnostne in sledljivostne varovalke P4

Datum izvedbe: 4. september 2026

## Izvedeno

- Storno računa in vezava novega računa na storno zahtevata ločeno dovoljenje
  `STORNO_DOCUMENT` v uporabniškem vmesniku in v poslovnem jedru.
- Blagajna ločeno prikazuje in zapisuje prijavljenega uporabnika ter FURS-ovo
  oznako operaterja. Nova revizijska sled zato hrani oba podatka.
- POS-osnutek je vezan na dejansko prijavljenega uporabnika in elektronsko
  napravo. Stari osnutki brez dokazljivega lastnika ostanejo ohranjeni, vendar
  se nikomur ne obnovijo samodejno.
- Gotovinski odkup, ki ga produkcijski atomski postopek že zapiše v blagajniški
  dnevnik, se takoj označi kot `BOOKED` in poveže s konkretno sejo ter vrstico.
- Stari `CONFIRMED` izdatki se samodejno povežejo samo, če se ujemajo vrsta,
  identiteta odkupa, referenca in znesek. Če se podatki ne ujemajo, postopek
  odpove brez novega gotovinskega dogodka.
- Stanje `BOOKED` se ne sprejme brez preverjanja povezane vrstice blagajniškega
  dnevnika.
- Storno servisnega računa je atomski tudi glede servisne baze. Če baza ali
  povezani servis oziroma servisno naročilo ni dosegljivo in skladno, se ne
  spremeni račun, plačilo, zaloga ali servis.

## Pojasnilo glede P3

Za produkcijski tok je merodajen `complete_buyback_atomic`: ob dokončni
potrditvi gotovinskega odkupa v isti transakciji nastaneta blagajniški dogodek
in povezava izdatka. Ročni prenos ostaja samo za stare ali drugače pripravljene
potrjene izdatke, ki še nimajo nobenega gotovinskega dogodka.

## Preverjanje

- 123/123 avtomatskih testov uspešnih;
- uspešen izoliran E2E-preizkus na TEST-bazi;
- preverjeni so zavrnjen in dovoljen storno, atomski storno povezanega servisa,
  izolacija osnutkov, migracija starih zapisov ter preprečitev dvojnega
  gotovinskega izdatka;
- produkcijski FURS in produkcijsko potrdilo sta bila v E2E-preizkusu
  nedosegljiva.


