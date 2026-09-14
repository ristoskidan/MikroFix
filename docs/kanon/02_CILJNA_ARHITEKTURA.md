# MikroFIX — ciljna arhitektura

Datum konsolidacije: 14. 9. 2026

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
- Strežniški ukaz izdaje zaklene osnutek in atomsko zagotovi največ en veljaven
  račun ter eno porabljeno številko.
- Pred prvim FURS klicem se trajno shranijo natančni podpisani zahtevek,
  `MessageID`, ZOI in kontrolni odtis.
- Nejasen rezultat nadaljuje isto operacijo; ne ustvari novega računa ali ZOI.
- Servisna poraba zaloge se ob računu ne ponovi.

## 8. Priloge in varnostne kopije

Centralno potrjeni dokumenti in priloge imajo centralno avtoritativno hrambo.
Offline Servis lahko hrani lokalne delovne kopije do potrjene sinhronizacije.

Varnostne kopije so ločene na:

1. PostgreSQL;
2. centralne priloge;
3. strežniško konfiguracijo in zaščitene ključe;
4. lokalne Service baze z dogodki, ki še niso potrjeno sinhronizirani.

Obnova se šteje za preverjeno šele po dejanski testni obnovi združljive celote.

## 9. Polna inventura

Pred inventurnim zaklepom strežnik preveri:

- da ni odprtih ali neknjiženih dokumentov;
- da so vse registrirane Service postaje dosegljive in sinhronizirane;
- da ni čakajočih ali konfliktnih zalogovnih dogodkov;
- da so Blagajna, prejemi in drugi zapisovalni postopki zaprti.

Šele nato izda inventurni zaklep. Med njim noben program ne sme ustvariti
poslovnega dogodka, ki bi vplival na zamrznjeno stanje.

## 10. Postopna migracija

- Stari monolit ostane primerjalna osnova in se ne prepiše naenkrat.
- Preverjene poslovne funkcije se izločajo za centralni API po posameznih
  vertikalah.
- Vsaka vertikala ima eno avtoritativno zapisovalno pot.
- Združljivostni adapter je začasen in ne postane trajna druga poslovna baza.
- Nov odjemalec se priključi šele, ko so njegovi pogodbeni testi z API-jem
  uspešni.

