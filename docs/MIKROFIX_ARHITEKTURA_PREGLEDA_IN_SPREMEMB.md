# MikroFIX – arhitektura pregleda in varnega poteka sprememb

Datum priprave: 3. 9. 2026  
Izhodišče: `MikroFIX_Service_v30_R9_DEV`

## 1. Namen

Vzpostaviti eno sledljivo razvojno pot za programe Service, Office in Blagajna, pri kateri nobena sprememba ne pride v potrjeno različico brez tehničnega pregleda, kontrolnih testov in po potrebi računovodske, pravne ali FURS potrditve.

## 2. Ciljna zgradba programa

```text
Uporabnik
  |
  +-- Service uporabniški vmesnik
  +-- Office uporabniški vmesnik
  +-- Blagajna uporabniški vmesnik
          |
          v
  Aplikacijske funkcije oziroma ukazi
  (nov dokument, knjiži, storno, izdaj račun, odkup, inventura ...)
          |
          v
  Skupna poslovna pravila in stanja
  (cene, rabati, L0/T1/T2, dovoljenja, prehodi stanj, nespremenljivost)
          |
          v
  Enotna transakcijska plast
  (idempotentnost, atomski zapis, revizijska sled, povrnitev po prekinitvi)
          |
          v
  Podatkovna plast
  (Office + zaloga + servis; za zdaj lokalni SQLite)

Zunanji priključki ob strani:
  MicroDetect | FURS | Epson tisk | UPN QR | varnostne kopije | dobavitelji
```

Uporabniška okna ne smejo neposredno spreminjati knjiženih poslovnih podatkov. Pokličejo samo skupno aplikacijsko funkcijo. Vsako pomembno pravilo mora obstajati na enem mestu in imeti samodejni test.

## 3. Okolja

| Okolje | Podatki | FURS | Namen |
|---|---|---|---|
| DEV | samo umetni podatki | izključen ali TEST | razvoj in samodejni testi |
| TEST | posebna testna baza | samo FURS TEST | Danielov celotni ročni preizkus |
| PRODUKCIJA | dejanski poslovni podatki | samo produkcija | dejansko poslovanje |

Med okolji ni dovoljeno prenašati baz, certifikatov ali osebnih podatkov. Testno in produkcijsko okolje morata biti vidno različna ter uporabljati različne podatkovne mape in nastavitve.

## 4. GitHub kot sled sprememb

Repozitorij mora biti zaseben. Veja `main` predstavlja zadnjo potrjeno razvojno osnovo in se ne spreminja neposredno.

Pred prvim uvozom se izločijo:

- `MikroFixData*/` in vse podatkovne baze;
- `*.db`, `*.sqlite*`, `*.p12`, `*.pfx`, zasebni ključi in gesla;
- ustvarjeni računi, pogodbe, fotografije, servisni dokumenti in varnostne kopije;
- `__pycache__/`, `*.pyc`, lokalni dnevniki in začasne datoteke;
- lokalne nastavitve, če vsebujejo poti, certifikate ali produkcijske podatke.

V repozitoriju ostanejo izvorna koda, prazne oziroma umetne testne predloge, testi, dokumentacija, migracije in javni FURS korenski certifikati, ko se preveri njihov izvor.

## 5. Pot posamezne spremembe

1. **Zahteva:** zapiše se cilj, razlog, področje in pričakovani rezultat.
2. **Ocena vpliva:** označi se vpliv na podatke, zalogo, cene, račune, FURS, osebne podatke in tisk.
3. **Veja spremembe:** sprememba nastane na ločeni Git veji.
4. **Izvedba:** koda in migracija baze se pripravita skupaj; že knjiženih podatkov se ne prepisuje.
5. **Samodejni testi:** normalni primer, podvojeni klik, napačno stanje, prekinitev ter storno.
6. **Codex pregled:** pregled logike, povezav in skladnosti z glavno specifikacijo.
7. **Codex Security diff pregled:** pregleda se samo dejanska sprememba in njen vpliv.
8. **Ročni TEST:** Daniel izvede opisani resnični scenarij na umetni bazi.
9. **Posebna potrditev:** računovodja, pravni pregled ali FURS TEST samo, kadar sprememba posega na to področje.
10. **Združitev:** šele po uspešnih kontrolah se sprememba združi v `main`.
11. **Izdaja:** nastanejo oznaka različice, kontrolna vsota, ZIP in kratka navodila za posodobitev ter povrnitev.
12. **Namestitev:** najprej varnostna kopija, nato preverjena posodobitev; na USB gre samo potrjena izdaja.

## 6. Obvezne kontrolne zapore

- Brez neposrednega brisanja ali popravljanja knjiženih dokumentov.
- Storno vedno ohrani izvirnik in ustvari povezano sled.
- En uporabniški dogodek lahko povzroči samo en poslovni učinek.
- Sprememba več baz se potrdi v celoti ali se v celoti razveljavi.
- Zaloga se spremeni samo na podlagi dokumentiranega poslovnega dogodka.
- Izdani račun se ne sme podvojiti zaradi ponovnega klika, prekinitve ali ponovnega zagona.
- Produkcijski FURS se ne more vključiti iz TEST postopka.
- Certifikat in njegovo geslo se ne shranita v kodo, Git ali USB razvojno kopijo.
- Vsaka izdaja mora prestati obnovitev iz varnostne kopije na prazno testno lokacijo.
- Vsaka sprememba sheme ima enosmerno migracijo in predhodno varnostno kopijo.

## 7. Zaporedje strukturnega urejanja

1. Zavarovati in označiti trenutno R9 osnovo brez funkcionalnih sprememb.
2. Vzpostaviti zasebni GitHub repozitorij in varen začetni commit.
3. Uvesti eno mapno strukturo za jedro, uporabniške vmesnike, zunanje priključke in teste.
4. Preseliti neposredne poslovne zapise iz uporabniških oken v aplikacijske funkcije.
5. Centralizirati prehode stanj dokumentov in dovoljenja.
6. Centralizirati vse večbazne transakcije ter preverjanje SQLite nastavitev.
7. Dodati nespremenljivo oziroma kriptografsko povezano revizijsko sled.
8. Zakleniti odvisnosti in dodati avtomatske varnostne preglede.
9. Izvesti celoten E2E preizkus z izpadi in obnovitvijo.
10. Šele nato vključiti produkcijski FURS in pripraviti prvo produkcijsko izdajo.

## 7.1 Rezultat pregleda R9

Celotni paket 97 samodejnih testov je uspešen. Codex Security je potrdil dve ugotovitvi srednje stopnje, ki morata biti odpravljeni pred produkcijo:

1. neposredni zagon `mikrofix_office.py` preveri PIN, ne pa tudi pravice `OFFICE` oziroma `CASHIER`;
2. servisne fotografije sprejmejo poljubno datoteko, pregled pa jo odpre z Windows asociacijo in lahko povzroči izvajanje aktivne vsebine.

Poleg tega mora biti popravljena pot servisnih fotografij pri načinu, ko program teče z USB-ja, podatki pa so na lokalnem disku. Podrobnosti so v `MIKROFIX_VARNOSTNI_IN_STRUKTURNI_PREGLED_R9.md`.

Za zasebni repozitorij osebnega GitHub računa se ne predpostavlja plačljivi CodeQL. Obvezna osnova so lokalni Codex Security pregled, avtomatski testi, Dependabot opozorila, iskanje skrivnosti in po možnosti brezplačni CI pregledi, kot sta `pip-audit` in `ruff` oziroma `bandit`. CodeQL se vključi samo, če je za izbrano vrsto zasebnega repozitorija dejansko na voljo.

## 8. Podatki oziroma dejanja, potrebna od Daniela

1. Na GitHub računu `ristoskidan` ustvariti prazen **zasebni** repozitorij, priporočeno ime `mikrofix-private`.
2. Pri ustvarjanju ne dodati README, licence ali `.gitignore`, da se lahko varna lokalna osnova uvozi brez konflikta.
3. GitHub povezavi v Codexu dovoliti dostop samo do tega repozitorija.
4. Posredovati URL praznega repozitorija.
5. Pred produkcijskim zagonom določiti, kateri Windows uporabniški račun bo edini lastnik produkcijskih baz in certifikata.

Gesla, PIN-a, zasebnega ključa ali GitHub žetona ni treba in ni dovoljeno pošiljati v pogovor.
