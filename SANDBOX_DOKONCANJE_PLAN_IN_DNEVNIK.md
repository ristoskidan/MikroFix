# MikroFix sandbox – načrt dokončanja in sprotni dnevnik

Začetek: 5. 9. 2026  
Cilj: dokončati vse varno izvedljive spremembe v sandboxu; Stable ostane
blokiran do ločene zaključne potrditve.

## Pravila izvedbe

- Spremembe se izvajajo v razvojni kodi in preverjajo samo nad podatkovno mapo
  `/home/mikrofix/MikroFixData_V30_SANDBOX`.
- `production-release.json` ostane preklican.
- Price Report formula `MF-ODKUP-1.0.1` se ne spreminja.
- Brez izrecnega naročila se nič ne pošilja po e-pošti, SMS-u ali drugemu
  zunanjemu prejemniku.
- Po prejšnji zahtevi se ne zaganja celotnih dragih testnih paketov. Med
  implementacijo se uporabljajo statični pregledi in kratka ciljana preverjanja;
  končni realni GUI/FURS/tisk preizkus zahteva uporabnikovo sodelovanje.

## Seznam sprememb, vpliv in vrstni red

| ID | Sprememba | Vpliva na | Tveganje | Stanje |
|---|---|---|---|---|
| S01 | Enoten časovnik `Začni servis` iz Service in Office | service_orders, navigacija, Office Servisi | srednje | NAREJENO V KODI |
| S02 | Samodejna postavka `Delo` iz dejanskega časa; brez ročnega vnosa storitve | Service, invoice bridge, POS, PDF | visoko | NAREJENO V KODI |
| S03 | Rezervacija delov ob pripravi servisa; dejanski odpis samo ob računu ali drugem potrjenem zaključku | zaloga, LOT/FIFO, storno, servis, POS | zelo visoko | NAREJENO V KODI |
| S04 | Testi brez privzetega `OK`; za druge naprave brez iPhone PRED/PO | Service GUI, servisni PDF, podatki testov | srednje | NAREJENO V KODI |
| S05 | Odprt blagajniški dnevnik za vsak račun | POS, dnevnik, vsi načini plačila | visoko | NAREJENO V KODI |
| S06 | Price Report aktiviran v sandboxu, formula in viri nespremenjeni | Office poročila, sandbox config | nizko | NAREJENO V KODI |
| S07 | Odstranitev zastarelih `/5`, ročnih tarif in T3 iz aktivnih poti | cene, Office, Service, dokumenti | srednje | NAREJENO V AKTIVNIH POTEH |
| S08 | AJPES partner po davčni številki z varnim ročnim fallbackom | Office partnerji, račun | srednje/zunanja storitev | KODA NAREJENA – ČAKA POVERILNICE |
| S09 | Dokaz odobritve dodatnega dela (podpis/e-pošta/SMS) | servisni nalog, arhiv prilog, plačljive postavke | visoko | NAREJENO V KODI |
| S10 | Neprevzeta naprava: 30 dni brezplačno, 10 EUR/30 dni, končni poziv po 90 dneh; brez samodejnega prenosa lastništva | Service, obvestila, terjatve | visoko/pravno | NAREJENO V KODI |
| S11 | Pregled in uskladitev javnih A4/80-mm PDF-jev ter oznake MikroFix | PDF generatorji, tisk | srednje | RENDER POTRJEN – ČAKA FIZIČNI TISK |
| S12 | Produkcijska in sandbox izolacija ter release-gate | launcherji, konfiguracija | visoko | NAREJENO, STABLE BLOKIRAN |
| S13 | Zaključni seznam za FURS online/offline, ZOI/EOR queue in tisk | POS, FURS, dokumenti | zelo visoko | KODA OBSTAJA – ČAKA KONČNI PREIZKUS |
| S14 | Backup/obnova in zunanji nosilec | vse baze in arhiv | zelo visoko | ČAKA UPORABNIŠKI VNOS GESLA/NOSILCA |
| S15 | Uskladitev glavne specifikacije in revizije z zadnjo kodo | dokumentacija | nizko | NAREJENO ZA OBRAVNAVANE ODLOČITVE |
| S16 | Samostojni F9 za kupca tudi na praznem računu | POS osnutek, pogodba za rabljeno napravo | srednje | NAREJENO IN CILJNO TESTIRANO |
| S17 | Ročni T1/T2 v blagajni z vnosom dejanskih minut | POS postavke dela, tarifni snapshot, PDF | srednje | NAREJENO IN CILJNO TESTIRANO |
| S18 | Razširjen Price Report na mini/Pro/Pro Max | Office poročila, javni viri | nizko | 16/16 VRSTIC OSVEŽENIH IN VELJAVNIH |
| S19 | Jasen prehod sprejem → odprti servis → zaključek in račun | Service GUI, servisni nalog, POS most | srednje | NAREJENO IN CILJNO TESTIRANO |
| S20 | Modulno razstavljanje, iFixit cene, unikatni kosi, popolni odpis in evidenca odpadkov | odkup, zaloga, LOT, POS, odpadki | visoko | JAVNI IFIXIT EU VIR + JEDRO + GUI NAREJENI; PRO DOSTOP ČAKA URADEN NAČIN |

## Konkreten načrt

### Faza A – servisni tok

1. Dodati skupno funkcijo za izbiro in zagon odprtega servisnega naročila.
2. Office mora prikazati odprta naročila in omogočiti `ZAČNI / NADALJUJ SERVIS`.
3. Oba vstopa morata klicati isti `start_service_work_timer` in nikoli ponastaviti
   začetka.
4. Zaključek mora uporabiti isti čas konca za zapis dejanskih minut in tarifni
   izračun.
5. Postavka `Delo` se shrani enkrat, prenese v Blagajno in je tam zaklenjena.

### Faza B – zaloga servisa

1. Ob pripravi servisa rezervirati konkretne količine/LOT-e, ne zmanjšati fizične
   zaloge.
2. Račun iz servisa v isti transakciji porabi rezervacijo in zmanjša zalogo.
3. Storno računa obnovi točno isti LOT in servis ponovno odpre v skladno stanje.
4. Brezplačni oziroma drug dovoljeni zaključek potrebuje ločeno, sledljivo
   odločitev; dokler ta pot ni potrjena, se je ne izmisli.

### Faza C – pravilnost vnosa

1. Testna polja začnejo prazna/nepreverjena.
2. Pri drugih napravah se iPhone-specifični testi skrijejo in ne shranjujejo.
3. Odstraniti aktivne ročne storitvene vrstice in zastarele tarife.
4. Ohraniti Price Report formulo in vključiti modul v sandboxu.

### Faza D – preostali poslovni tokovi

1. Pregledati AJPES povezavo, dokaze odobritve in neprevzete naprave.
2. Pregledati vse javne PDF-je in tiskalne nastavitve.
3. Pregledati FURS online/offline in varnostno kopiranje brez aktivacije Stable.

### Faza E – sprejem

1. Statični pregled/migracije sandbox baze.
2. Kratka ciljna preverjanja samo novih kritičnih funkcij.
3. Ročni scenarij z uporabnikom: Office/Service → čas → deli → dnevnik → račun →
   FURS TEST/offline → A4 in 80 mm.
4. Šele po uspešnem scenariju pripraviti kandidat za Stable; aktivacija produkcije
   je ločena odločitev.

## Sprotni dnevnik

### 6. 9. 2026 – potrjena prenova Service, Blagajne in dnevnika

- Potrjeno je globalno maksimiranje glavnih/delovnih oken; majhna modalna okna
  ostanejo vsebinsko velika in sredinsko poravnana.
- Sprejem servisnega naročila ne sme več odpreti delovnega servisa ali zagnati
  časa. Čas se vodi po več intervalih `začni/pavza/nadaljuj`, delovni pogled pa
  mora stalno kazati uro, začetek in seštevek.
- Servisni sprejem dobi skupni izbor partnerja (obstoječi/AJPES/ročni), po
  uspešnem sprejemu pa se delovni podatki naprave in stranke počistijo.
- POS osnutek nima številke računa; ta nastane šele ob atomski izdaji in FURS
  dogodku. Prazen delovni račun se ne hrani kot trajni osnutek.
- Brisanje postavk ter F11/F12/DEL se opustijo. Korekcija se zapiše kot ista
  postavka z negativno celo količino; ničelna vrstica ni dovoljena.
- Po izdaji se delovni obrazec počisti, ločeni povzetek zadnjega računa pa ohrani
  dostop do PDF-ja, kopije, storna in reklamacije.
- A4 in 80-mm račun morata prikazovati istega kupca. Zgodovina računov se doda v
  Blagajno in uporablja en skupni števec kopij na račun za oba formata.
- Blagajniški dnevnik dobi tri jasne vstope in ločena izpisa: enostranski dnevni
  zaključek ter podrobno arhivsko prilogo za velike dneve.

### 5. 9. 2026 – začetno stanje

- Produkcijski release preklican do konca revizije.
- Popravljeno ločevanje produkcijskih in testnih okoljskih spremenljivk.
- Dodana zahteva odprtega dnevnika za vsak nov račun.
- Dodan osnovni skupni časovnik in avtomatski tarifni izračun ob zaključku.
- Ročni zavihek storitvenih postavk skrit.
- Revizija Price Reporta popravljena: formula in modul ostaneta.
- S01: Office zdaj pokaže odprte servisne naloge in gumb `ZAČNI / NADALJUJ
  SERVIS`; izbrani nalog preda istemu servisnemu zaslonu kot Servis. Oba vstopa
  uporabljata isti časovnik, ki ob ponovnem odpiranju ne ponastavi začetka.
- S02: zaključek uporablja izmerjene minute in samodejno ustvari eno postavko
  `Delo`; ročni storitveni zavihek ostaja skrit.
- S04: testi iPhone se začnejo kot `NI TESTIRANO`, ne kot `OK`. Za druge naprave
  sta testa PRED/PO skrita, vrednosti so `N/A` in se ne zapisujejo v servis.
- S06: tedensko poročilo je vključeno v sandbox konfiguraciji. Formula
  `MF-ODKUP-1.0.1` in obstoječi javni evropski viri niso bili spremenjeni.
- S03: servisni deli po pripravi ne zmanjšajo več `stock_qty`. Vsak kos rezervira
  konkreten FIFO/LOT in poveča `reserved_qty`; povezani račun v isti transakciji
  rezervacijo spremeni v prodajni odpis. Storno računa ponovno odpre servis in
  isti LOT vrne v rezervacijo istega servisnega dela.
- Združljivost: stari servisi, pri katerih je bil del že odpisan ob shranitvi,
  ostanejo berljivi in se pri računu ne odpišejo drugič.
- S07: aktivni Office izračun dokumentne postavke ne uporablja več pribitka in
  zaokroževanja `/5`, ampak pravo maržo ter naslednji cel EUR. Oznake `/5` so iz
  aktivnega Office vmesnika in PDF glave odstranjene. T3 ni aktivna izbira.
- Zgodovinske tabele in bralniki starih ročnih servisnih vrstic ostajajo zaradi
  odpiranja že ustvarjenih zapisov; novi servisni tok jih uporabniku ne pokaže.
- Takratna odločitev o odstranitvi ročnih T1/T2 iz Blagajne je bila po ročnem
  testiranju preklicana: uporabnik je izrecno zahteval samostojno dodajanje dela.
  Trenutno stanje je opisano pri S17; avtomatsko delo iz servisa ostaja ločeno.
- S15: glavna specifikacija in poslovna pravila so pri T1/T2 posodobljena na
  zadnjo odločitev: avtomatski čas in ena vrstica `Delo`.
- S08: v urejevalnik partnerja je dodan uradni AJPES `restPrsInfo` lookup po
  davčni številki. Sprejme samo enoličen rezultat in podatkov ne ugiba. Geslo se
  ne zapisuje v kodo/bazo; produkcijski klic zato čaka na AJPES uporabniško ime,
  geslo in naročeno shemo (`MIKROFIX_AJPES_USERNAME`,
  `MIKROFIX_AJPES_PASSWORD`, `MIKROFIX_AJPES_SCHEMA`). Brez njih ostane ročni
  vnos.
- S09: če deli + avtomatsko delo presežejo prvotno ponudbo, zaključek zahteva
  odobreni limit, kanal `PODPIS`/`E-POŠTA`/`SMS` in pripeto slikovno dokazilo.
  Brez vseh treh račun ni pripravljen. Dokazilo se arhivira s servisom, dogodek
  in uporabljeni kanal pa se zapišeta v revizijsko sled.
- S10: izdaja povezanega računa začne lokalno evidenco `Pri MikroFix`. Prvih 30
  dni je 0 EUR, nato se prikaže 10 EUR na vsak začeti dodatni 30-dnevni blok.
  Po 90 dneh program zahteva ročno evidenco dejansko poslanega/izročenega
  končnega poziva. Dodana sta ločena gumba za potrditev prevzema naprave in
  evidence poziva. Program ničesar ne pošilja in nikoli ne prenese lastništva.
- S11: ustvarjena sta sveža ciljna A4 in 80-mm predogleda s šumniki, dolgim
  nazivom artikla, dolgim registriranim nazivom in poudarjenim `MikroFix`.
  Vizualni render ima belo ozadje, pravilne č/š/ž, berljive prelome in večjo
  termično pisavo. Črna/invertirana podlaga ni v PDF-ju. Ostane fizični izpis na
  HP in Epsonu; če Epson invertira ta beli PDF, je treba popraviti nastavitev
  gonilnika/tiskalnika.
- S13: statični pregled potrjuje obstoječo offline pot `PENDING_RECONCILIATION`,
  lokalni ZOI, besedilo `EOR BO PRIDOBLJEN NAKNADNO` in funkcijo za poznejšo
  uskladitev. Tega se ne označi kot sprejeto, dokler ni izveden nadzorovan FURS
  TEST online in simulacija izpada/ponovne povezave.
- Končna statična kontrola vseh spremenjenih Python datotek in JSON sandbox
  nastavitev je uspešna. Sandbox se naloži kot `TEST` nad
  `/home/mikrofix/MikroFixData_V30_SANDBOX`; pravila hrambe so 30 dni / 10 EUR
  na dodatni 30-dnevni blok / končni poziv po 90 dneh; Price Report je vključen.
- Produkcijski manifest ostaja `REVOKED_PENDING_REQUIREMENTS_AUDIT`; nobena od
  teh sprememb ne odklene Stable.
- Ciljni sandbox smoke-zagon je najprej odkril dejansko zagonsko napako Office:
  barva okoljskega opozorila se je izračunala pred definicijo barvnih konstant.
  Vrstni red je popravljen; ponovljeni zagon glavnega menija in vgrajenega Office
  se je zaključil brez napake.
- Smoke-način ob notranji napaki Office zdaj izpiše celoten traceback v terminal,
  zato pravi vzrok ne ostane skrit za grafičnim opozorilom.
- Migracijska kontrola nad izključno sandbox podatki potrjuje časovne stolpce v
  `service_orders`, pravila neprevzetih naprav v `service` ter LOT rezervacijsko
  shemo in `reserved_qty` v inventurni bazi.
- Opravljenih je bilo osem ozkih testov, ne celoten paket: pravila 30/10/90,
  neponastavljiv časovnik, rezerviran LOT skozi račun in storno, življenjski
  cikel servisa do računa, blokada dvojnega računa, atomsko ponovno odprtje
  servisa po stornu, ponovitev istega FURS zahtevka iz čakalne vrste ter zaklep
  produkcijskega odjemalca na uradni FURS naslov. Vseh osem je uspešnih.
- Lokalni bližnjici kažeta na isto lokalno programsko mapo, vendar na ločeni
  podatkovni mapi `MikroFixData_V30_PRODUCTION` in
  `MikroFixData_V30_SANDBOX`. Navadni Sandbox launcher nič več ne nastavlja
  posebne oznake `MIKROFIX_E2E_TEST`; ta ostaja samo za namenski E2E komplet.

### 5. 9. 2026 – vrzeli odkrite pri ročnem testiranju

- S16: `F9 STRANKA` je ločen od `F10 PLAČILO / IZDAJA`. Odpre samostojni vnos
  kupca in ga shrani v trajni, neizdani osnutek tudi kadar račun nima nobene
  postavke. Priprava 12-mesečnega dogovora ob manjkajočem imenu ali naslovu
  odpre ta dialog in ne poskuša izdati računa.
- S17: v Blagajni sta ponovno aktivna `F3 T1 DELO` in `F4 T2 DELO`, ker ju je
  uporabnik izrecno zahteval za samostojno prodajo dela. Ob kliku program vpraša
  po dejanskih minutah, prvo uro zaračuna po izbranem razredu in vsako začeto
  nadaljnjo poluro po zaklenjeni tarifi. Avtomatsko delo iz zaključenega servisa
  ostaja ločena, zaklenjena vrstica in se ne podvaja.
- S18: začetni katalog poročila je razširjen za iPhone 12 mini, 12 Pro Max,
  13 Pro, 13 Pro Max, 14 Pro, 14 Pro Max, 15 Pro in 15 Pro Max. Združevanje
  kataloga nikoli ne prepiše obstoječih ročno preverjenih/osveženih cen.
  Osvežitev delovne sandbox baze je izvedla 48/48 uspešnih zajemov; vseh 16
  modelov je veljavnih. Formula ostaja `MF-ODKUP-1.0.1`, izpis pa še vedno kaže
  samo dve dogovorjeni ceni: priporočeno in največjo.
- S19: gumb za sprejem je preimenovan v `USTVARI NOV SPREJEM IN PDF`, ob njem
  je `ODPRI / NADALJUJ ODPRTI SERVIS`. Če za stranko že obstaja odprti servis,
  sprejem ne ustvari tihega dvojnika, ampak odpre obstoječi nalog in pokaže
  navodilo. V delu z vgrajenimi deli je jasno viden gumb `ZAKLJUČI SERVIS IN
  PRIPRAVI RAČUN`. Ciljni testi potrjujejo neponastavljiv začetek, dejanski konec,
  izračun minut ter enkratni prenos dela in delov v blagajno.
- S19 (7. 9.): Service se odpre na dostopnem pregledu odprtih sprejemov tudi
  brez nove USB-identifikacije. Iz iste čakalne vrste je mogoče začeti oziroma
  nadaljevati servis ter ponovno pripraviti A4 sprejemni PDF in 80-mm oznako.
  Po novem sprejemu ostane odprto namensko okno za oba izpisa in prehod v
  čakalno vrsto; Office uporablja iste naloge iz iste baze.
- S20: `RAZSTAVI ZA DELE` ne izbira več poljubnih obstoječih artiklov. Program
  sam pripravi seznam telefonskih modulov, vsak modul pa je treba označiti kot
  `PULLED_OK`, `NOT_PULLED` ali `UNUSABLE`. Uporaben modul ustvari oziroma
  uporabi en skupni master artikel z javnim nazivom `Rabljeni izvlečeni del –
  [modul] za [model]`; vsak fizični kos dobi lastno `PP...` črtno kodo in LOT.
- S20: skeniranje fizične `PP...` kode v Blagajni najde master artikel in na
  prodajni vrstici zaklene prav skenirani LOT. Razporeditev celotnega odkupnega
  stroška, ene ure T1 in neposrednih stroškov ostaja sorazmerna glede na
  priporočene prodajne vrednosti.
- S20: razstavljanja brez uporabnega dela ni več mogoče tiho zaključiti. Zahteva
  dobesedno potrditev `POPOLN ODPIS`, celoten strošek knjiži kot izgubo in ustvari
  ločena zapisa baterijskega ter elektronskega odpadka. Pri delnem razstavljanju
  se neuporabni/neizvlečeni moduli in preostanek prav tako vodijo v evidenci s
  statusom `AWAITING_HANDOFF`; dokaz prevzemnika je možno dodati pozneje.
- S20: javni iFixit EU katalog je priklopljen na modelno stran. Iz nje program
  prebere natančne ponudbe modulov in izloči dodatke (lepila, zaščitna stekla,
  mrežice in pokrove leč). Pri natančnem zadetku avtomatsko shrani EUR ceno,
  neposredno iFixit povezavo in datum; brez zadetka pokaže `NI PRIMERJAVE` in
  cene ne ugiba. Zunanji vir vpliva na priporočilo in razdelitev donorjevega
  stroška, nikoli pa samodejno ne prepiše aktivne prodajne cene master artikla.
- S20: iskanje pozna konkretne sinonime `Charge/Charging Port`, `USB-C Port`,
  `USB-C Connector Assembly`, `Lightning Port`, `Lightning Connector Assembly`,
  `Dock Connector`, `Taptic Engine`, `Vibration Motor` in `Vibrator` ter
  različice nazivov kamer, slušalke, zvočnika, ohišja in brezžičnega polnjenja.
  Ujemanje je razvrščeno po specifičnosti naziva. Regresijski primeri izločijo
  `Battery Connector`, `Rear Camera Test Cable`, tesnilo zvočnika, lepila,
  mrežice, leče in pokrove, čeprav vsebujejo ime glavnega modula.
- S20: dodana sta `POVEŽI IFIXIT PRO` in `ODKLOPI IFIXIT PRO`. Povezava odpre
  namenski Firefox profil samo znotraj izbrane PRODUCTION/SANDBOX podatkovne
  mape. Uporabnik geslo vnese neposredno na iFixit; MikroFix ga ne bere in
  namerno onemogoči Firefoxovo shranjevanje gesel. Po zaprtju okna program
  preveri prijavljeno sejo, prebere prijavljene cene in jih označi `iFixit Pro`;
  ob potekli/neuspešni seji sam pade nazaj na javne iFixit EU cene. Odklop
  odstrani samo iFixit piškotke in iFixit spletno shrambo namenskega profila.
- S20 – ročni korak: uporabnik mora v Sandboxu enkrat klikniti povezavo, se
  prijaviti in potrditi, ali račun vrne drugačne Pro cene. Brez dejanske prijave
  ni mogoče dokazati, da iFixit HTML prijavljenemu EU Pro računu izpisuje
  popust v istem cenovnem elementu.
- Prevedene so vse spremenjene Python datoteke. Opravljeni so bili samo ozki
  testi: prazen osnutek s kupcem, T1/T2 minute, razširitev kataloga brez prepisa,
  servisni čas/zaključek/prenos, izrecni popolni odpis, evidenca obeh vrst
  odpadka, master + unikatni pulled kos + LOT ter iskanje kosa po črtni kodi.
  Vsi so uspešni. Migracija je bila preizkušena na kopiji realne sandbox
  inventurne baze; `PRAGMA integrity_check` je vrnil `ok`.
- Dodanih je pet ozkih testov iFixit povezave: EU modelni URL, razčlenjevanje
  cen brez dodatkov, izolacija profila po podatkovni mapi, sprejem samo iFixit
  piškotkov ter zavrnitev prijave, ki preusmeri nazaj na `/Login`. Vsi so
  uspešni. Ponovljeni smoke-zagon Sandboxa je odkril in odpravil še pozna Tk
  klica rezervne ter FixShop niti po zaprtju glavnega okna; končni smoke-zagon
  je čist in ima izhodno kodo 0.

### 5. 9. 2026 – popravki po ročnem testu zaključka servisa in razstavljanja

- Iz `/home/mikrofix/.xsession-errors` je potrjen neposredni vzrok, zakaj oba
  gumba za zaključek nista dala odziva: `save()` je klical nedefinirani
  `safe_float` in vsak klik končal z `NameError`. Pretvorba decimalnega vnosa je
  dodana, podvojeni gumb odstranjen, sekundarna opravila po potrditvi pa ne morejo
  več pustiti gumba v tiho zaklenjenem stanju. Zadnji ročni servis
  `MF-SN-2026-0003` je ostal odprt in brez delno ustvarjenega servisnega zapisa.
- Dodan je varen storno pomotnega `FULL_WRITEOFF`: izvirni harvest in odpadne
  vrstice se ne brišejo, temveč označijo `STORNIRAN` oziroma `CANCELLED`, naprava
  pa se samo ob odsotnosti delov in oddaje odpada vrne v karanteno. To neposredno
  odpravi blokado testne naprave `NAP-2026-0002` ob naslednjem kliku
  `RAZSTAVI ZA DELE`, vendar podatkov brez uporabnikove potrditve nismo spremenili.
- Cenovni tok je razširjen na vrstni red iFixit (Pro/javni) in nato FixShop
  (Pro/javni) samo za module brez iFixit zadetka. Vsaka cena hrani URL, datum in
  vidno oznako vira. Dodana je ločena FixShop Pro povezava z namenskim Firefox
  profilom, ki bere samo FixShop piškotke in ne shranjuje gesla v MikroFix.
- Javni modelni cenik FixShop za iPhone 11 Pro je v živem ozkem preverjanju našel
  devet natančnih modulov; izločeni so steklo kamere, lepila in polnilni čip kot
  napačne zamenjave za glavni modul. Izvedenih je deset ozkih testov za oba
  cenovna vira, izolacijo prijavnih profilov, popolni odpis/storno in sledljiv
  pulled kos; vseh deset je uspešnih. Celotni testni paket ni bil zagnan.

### 5. 9. 2026 – prikaz časa in F9 identiteta kupca

- Ročni račun je potrdil, da ima servis `MF-2026-0001` pravilno shranjen razred
  `T2`, 510 obračunskih minut, prvo stopnjo 90 EUR, nadaljnjo polurno stopnjo
  15 EUR in skupno delo 315 EUR. Napaka je bila samo v košarici, ki je kazala
  tehnično količino `1`; zdaj kaže `510 min / 8,5 h`. Tarifna matematika in
  nespremenljiva sled nista spremenjeni.
- F9 je razširjen v enoten izbor kupca: obstoječi partner, novi ročni vnos ali
  pravo AJPES iskanje po davčni številki. Partner se shrani oziroma poišče brez
  podvajanja, njegov ID pa se shrani v obnovljivi osnutek in nato v izdani račun.
- Dogovor za 12 mesecev se pripravi samo za kupca, potrjenega prek F9, in shrani
  isti `customer_partner_id`. Izdaja obnovljene naprave preveri, da podpisani
  dogovor, račun in partner uporabljajo isti ID; poznejša sprememba polja kupca
  povezavo ter stari pripravljeni dogovor razveljavi.
- Opravljeni so bili samo ciljni testi: prazen osnutek, tarifni posnetek,
  `partner_id` na računu in isti `partner_id` na podpisanem dogovoru ter računu.
  Celotni paket ni bil zagnan.

### 6. 9. 2026 – izvedba potrjenega prestrukturiranja servisa in blagajne

- Sprejem servisnega naročila ne zaganja več obračunljivega časa. Časovnik je
  intervalen: začetek/nadaljevanje odpre nov interval, ustavitev ali odhod iz
  servisnega delovnega prostora ga zapre, zaključek pa sešteje samo dejanske
  aktivne intervale. Delovni zaslon kaže uro, prvi začetek, tekoči seštevek in
  jasno stanje `ČAS TEČE` oziroma `ČAS JE USTAVLJEN`.
- V Servisu je dodan izbor obstoječe stranke ter ločeni poti AJPES in ročni
  vnos. Partner se shrani v skupno Office bazo, njegov ID pa tudi v servisno
  naročilo. Ročna sprememba posnetka prekine povezavo, dokler stranka ni znova
  potrjena.
- Ob sprejemu nastaneta A4 servisno naročilo in 80-mm Epson oznaka. QR vsebuje
  številko naloga, serijsko številko ter IMEI. Novi pregled odprtih nalogov zna
  prebrati celotno QR vsebino in pred zagonom preveri ujemanje vseh treh
  identifikatorjev; zato je napačna oznaka zavrnjena tudi brez odklepanja
  naprave.
- Blagajna stalno prikazuje izbranega kupca nad postavkami; 80-mm original in
  kopije zdaj izpišejo isto ime, naslov in po potrebi davčno številko kot A4.
  Prazen osnutek se ne shranjuje, najden stari prazen osnutek pa se varno opusti.
- Neposredni `F11`, `F12` in `Delete` so odstranjeni. Dvoklik/desni klik odpre
  majhno okno za rabat, dodatno pozitivno količino ali novo negativno
  korekcijsko vrstico. Posamezna vrstica nikoli ni 0; korekcija ne sme preseči
  dodane količine. Interni +/- dogodki ostanejo v osnutku, javni račun pa jih
  združi v eno pozitivno neto količino.
- Po izdelavi originalnih PDF-jev se košarica, kupec in plačilna polja vidno
  počistijo. Dejanja zadnjega računa ostanejo na gumbih, vsi računi pa so zdaj
  dosegljivi v novem maksimiranem pregledu z iskanjem, odpiranjem originala,
  ločeno A4/80-mm kopijo, popolnim stornom in reklamacijo. Kopiji obeh formatov
  uporabljata skupno zaporedno številčenje. Za prodajo brez servisnega naloga je
  dodana lastna nespremenljiva evidenca reklamacij `sales_claims_v31`.
- Blagajniški dnevnik ima tri glavne izbire `ODPRI`, `ZAPRI` in `PREGLED PO
  DNEVIH`; neveljavna izbira je siva glede na stanje. Pregled je iskalni in
  maksimiran. Zaključni PDF ima prvo stran z zaključnim povzetkom in podpisi,
  popoln seznam tudi približno 150 knjižb pa je arhivska priloga od druge strani.
- Večja delovna okna v Servisu, Office in Blagajni se odpirajo maksimirana;
  majhna količinska, potrditvena, tiskalniška in podobna modalna okna ostanejo
  vsebinske velikosti.
- Price Report in iFixit/FixShop logika nista bila spremenjena. Kandidatna
  izvorna koda je posodobljena samo za preverjanje skozi Sandbox; produkcijski
  izid in Stable zaganjalnik nista bila sproščena. Ponovni pregled potrjuje tri
  zaprta produkcijska vrata: manjka šifrirani zunanji backup, izdajni preizkus
  je preklican do ponovne revizije zahtev in datotečni sistem backupa ni potrjen.
- Izvedeni so bili samo ciljni testi, ne celotni paket: dva intervala časovnika,
  povezava partnerja s servisnim nalogom, A4 + 80-mm servisna oznaka, negativna
  korekcija in javna neto količina, iskanje računov in prodajna reklamacija,
  kupec na 80-mm računu ter dvostranski dnevniški povzetek/priloga. Vsi so
  uspešni; spremenjene datoteke se prevedejo brez sintaktične napake.

## Kaj še zahteva zunanji ali fizični korak

1. AJPES: vnesti veljavne poverilnice in naročeno shemo ter preveriti en testni
   in en produkcijski subjekt.
2. FURS: v sandboxu izvesti online testni račun, nato simulacijo nedosegljivega
   omrežja in poznejšo pridobitev EOR.
3. Tisk: sveža vzorca natisniti na HP A4 in Epson TM-T20II 80 mm; potrditi
   šumnike, belo ozadje, QR in fizično širino.
4. Backup: uporabnik mora vnesti geslo in izbrati zunanji nosilec; nato je treba
   dokazati obnovitev v ločeno testno mapo.
5. Po teh štirih korakih ponoviti en sam ročni sprejemni scenarij in šele nato
   ustvariti nov produkcijski kandidat. Do takrat Stable ni pripravljen.
- Natančen zaporedni postopek in pričakovani rezultat vsakega koraka sta zapisana
  v `SANDBOX_ROCNI_SPREJEMNI_SCENARIJ.md`.
- Statična kontrola: `mikrofix.py` in `mikrofix_office.py` se prevedeta brez
  sintaktične napake. Celoten testni paket ni bil ponovno zagnan.
- Naslednji korak: izvesti pripravljeni ročni sprejemni scenarij v Sandboxu.
  Programska statična kontrola in ciljni testi so zaključeni; Stable ostane
  blokiran do fizične potrditve FURS TEST, obeh tiskalnikov in obnovitve backupa.

### 8. 9. 2026 – trajni artikli odprtega servisa

- Odprti servisni nalog ima ločen trajni osnutek uporabljenih artiklov v tabeli
  `service_order_parts`. Dodajanje in odstranjevanje vrstice osnutek takoj
  sinhronizira z ID servisnega naročila.
- Vrnitev na zgodovino pred odhodom še enkrat preveri shranjevanje. Ponovno
  odprtje istega naloga naloži naziv, interno kodo, barkodo, artikel, LOT in
  posneto ceno v zavihek `Vgrajeni deli`.
- Osnutek sam po sebi še ne odpisuje in ne rezervira zaloge. Ob uspešnem
  zaključku se iste vrstice zapišejo v končni `service_parts`, izvede se že
  obstoječa rezervacija in šele nato se osnutek izbriše v isti transakciji.
  Neuspešen zaključek zato osnutka ne izgubi in ne povzroči dvojnega odpisa.
- Obstoječih artiklov, ki so pred tem popravkom obstajali samo v pomnilniku GUI
  in so že izginili ob odhodu, iz baze ni mogoče obnoviti. Štirje odprti nalogi
  v trenutni Sandbox bazi ob migraciji niso imeli nobene shranjene osnutkovne
  vrstice.
- Izvedena sta samo dva nova ciljna scenarija ter obstoječi test življenjskega
  cikla servisa; vsi trije so uspešni. Sintaksa spremenjenih modulov je veljavna.

### 8. 9. 2026 – samodejni 80-mm izpis ob sprejemu

- Po uspešno potrjenem zapisu servisnega naročila se pripravljena QR oznaka
  samodejno pošlje neposredno na nastavljeni termalni tiskalnik. Napaka tiskanja
  nikoli ne razveljavi že shranjenega sprejema; pokaže jasno opozorilo in ohrani
  gumb za ponovni izpis. Uspeh oziroma napaka se zapišeta v audit sled.
- 80-mm oznaka poleg poudarjenega `MikroFix`, številke naloga in naprave zdaj
  prikazuje datum sprejema ter vse razpoložljive podatke stranke: ime, naslov,
  telefon in e-pošto. QR ostane velik in namenjen priklicu pravilnega naloga.
- Vzorčni PDF je bil izrisan pri 203 dpi in vizualno pregledan: ena stran,
  pravilni šumniki, brez prekrivanja ali odreza. QR je bil uspešno dekodiran v
  prvotno vsebino naloga. Izveden je bil en ciljni PDF-test; celotni paket ni bil
  ponovno zagnan in testni papir ni bil poslan na Epson.

### 9. 9. 2026 – blokada izhoda ob aktivnem servisnem času

- Zapiranje programa zdaj v servisni bazi preveri vse intervale brez končnega
  časa, tudi kadar pripadajo servisu, ki trenutno ni odprt v uporabniškem
  vmesniku.
- Če obstaja tekoča seja, program prikaže servisno številko, napravo in stranko
  ter ostane odprt. Uporabnik mora servis najprej odpreti in izbrati `USTAVI ČAS`
  ali ga zaključiti; zapiranje časovnika ne ustavi več potiho.
- Dodan je regresijski test začetka, zaznave in ustavitve aktivne seje. Celotni
  paket ima po spremembi 179 uspešnih testov; začasne testne baze so bile zaradi
  ločene diagnostike diskovnega I/O izvedene v `/dev/shm`.
