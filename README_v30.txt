MikroFix v30.0
================

To je locena razvojna osnova novega enotnega programa MikroFIX.
Mapa in podatki MikroFIX v29 ostanejo nespremenjeni.

Trenutno:
- program uporablja loceno mapo MikroFixData_V30_DEV;
- FURS produkcija je izklopljena;
- SI Price Report je zamrznjen in se ne zaganja samodejno;
- prevzem in vracilo uporabljata potrjeno tarifo 30,00 EUR;
- PIN vsebuje 6 do 14 stevilk in ne sme biti preprost;
- novi uporabnik dobi zacasni PIN in ga ob prvi prijavi zamenja;
- dovoljenja se vodijo po modulih in obcutljivih dejanjih.
- odkup privzeto uporablja bancno nakazilo; gotovina je dovoljena samo do
  vkljucno 420,00 EUR in istega odkupa ni dovoljeno razdeliti;
- gotovinski odkup samodejno pripravi enolicen blagajniski izdatek
  BI-LETO-ZAPOREDJE, vendar priprava dokumentov se ne ustvari zaloge;
- naprava preide v karanteno sele po gumbu POTRDI PODPIS IN IZPLACILO;
- zunanji komercialni IMEI/GSMA pregled je v zacetnem toku preskocen;
- novi odkup zahteva slovensko davcno stevilko prodajalca, datum rojstva pa se
  ne zbira;
- pogodba se pripravi in zaklene kot A4 dokument, nato jo stranka in MikroFIX
  takoj lastnorocno podpiseta; podpisna USB naprava ni del trenutnega nacrta;

FURS TEST:
- produkcijski naslov in produkcijsko potrdilo sta konstrukcijsko blokirana;
- vrstni red je: izberi Tax CA Test.pfx, ECHO, prijavi TESTMFD/PIS, nato poslji
  anonimizirani testni racun 1,00 EUR;
- nadzorovani preizkus 1. 9. 2026 je uspel: ECHO, prijava TESTMFD/PIS in
  anonimizirani racun 1,00 EUR s testnim EOR so bili potrjeni;
- testni racun je blokiran, dokler testni prostor za davcno stevilko iz
  izbranega testnega potrdila ni uspesno prijavljen;
- paket vsebuje uradno SI-TRUST CA verigo in potrdilo DavPotRacTEST za strogo
  preverjanje TLS povezave ter podpisa FURS odgovora;
- minimalne servisne fotografije imajo potrjen petletni rok hrambe;
- Office vsebuje novi enotni prejem blaga in prejetih racunov;
- dobavnica brez racuna vodi blago loceno in ga ne sprosti v prodajno zalogo;
- racun zahteva natancno ujemanje 0,00 EUR in obvezno arhivirano prilogo;
- neposredni rocni popravek zaloge in neposredno knjizenje inventure sta blokirana;
- servis ostane v obdelavi do izdaje povezanega racuna;
- gumb Blagajna odpre namenski POS z iskanjem in skeniranjem zaloge,
  kosarico, T1/T2, placili, koncnim pregledom ter izdajo in tiskom s F10;
- neposredna prodaja odsteje zalogo samo enkrat, del iz povezanega servisa
  pa se na racunu ne odpise ponovno;
- isti servis ne more dobiti dveh racunov; izdaja racuna hkrati zakljuci
  povezano servisno narocilo;
- prejeme, servisno porabo, prodajo, odpis in storno povezuje konkretna
  LOT/serijska sled; stornirati ni mogoce prejema, katerega LOT je ze porabljen;
- previsok rabat se samodejno omeji na nabavno ceno + 5 % pri artiklu
  oziroma na L0 pri T1/T2, brez razkritja notranje dovoljene meje;
- prvi F10 izdela A4 in 80-mm predogled brez stevilke, zalogovnega ucinka ali
  FURS prenosa; drugi F10 izda nespremenjeni racun in sprozi neposredni tisk;
- Windows tiskalniska vrsta mora potrditi zakljucek izpisa; ob napaki racun
  ostane izdan, PDF ostane na voljo, ponovitev pa tiska isti original;
- poznejsi uspesni izpisi so ostevilcene kopije; neuspel poskus ne porabi
  stevilke kopije;
- blagajna pred izdajo zahteva izbiro MFD ali MFT in shrani nespremenljiv
  posnetek prostora, naprave PIS, operaterja DR ter davcne stevilke operaterja;
- javna zaporedja racunov so locena po prostoru in napravi (npr. MFD-PIS-1 in
  MFT-PIS-1), notranja tehnicna stevilka pa ostane locena od izpisa;
- MFT je evidentiran kot premicni objekt tipa A; izbira MFT zato v blagajni
  opozori, da mora biti racun dejansko izdan v premicnem objektu;
- razdeljeno placilo s TRR delom je blokirano, dokler uporabnik ne potrdi, da
  je nakazilo dejansko prejeto; vsota vseh delov mora biti tocna do centa;
- ce izdelava PDF ali tisk po izdaji odpove, racun ostane izdan in se ob
  naslednjem zagonu nadaljuje isti original; novega racuna ni mogoce odpreti;
- F8 izdela standardni UPN QR z IBAN SI56040000283154872 in zneskom, ki se
  mora ujemati do centa; izpis ni racun;
- obnovljena naprava zahteva v Blagajni pripravljen A4 dogovor/garancijski
  list z IMEI, nespremenljivim kontrolnim odtisom in potrditvijo podpisa;
- celotni storno ohrani prvotni racun, ustvari nov zaporedni storno dokument,
  povrne konkretne LOT-e in sprosti povezan servis za pravilno novo izdajo;
- gotovinski in razdeljeni racun zahtevata odprt blagajniski dnevnik za izbrani
  prostor/napravo; cisti TRR ne zahteva odprte gotovinske blagajne;
- odprtje shrani cas, operaterja in zacetno stanje; gotovinski racuni, storni,
  blagajniski prejemki, izdatki in potrjeni gotovinski odkupi se vodijo kronolosko;
- zaprtje zahteva dejansko presteto gotovino ter shrani pricakovano stanje,
  razliko, cas in operaterja; zakljuceni dnevnik je nespremenljiv in ima A4 PDF;
- ob zaprtju blagajne program prikaze cakajoce nepotrjene izdatke; izbrani se
  knjizijo samo enkrat, neizbrani ostanejo jasno oznaceni kot cakajoci;
- PyMuPDF je potreben za neposredni tisk prek Windows gonilnika, segno pa za
  standardni UPN QR; oba namesti namesti_v30.bat;
- jedro sifriranih dnevnih kopij je pripravljeno, vendar ostane izklopljeno,
  dokler nista dolocena zunanji nosilec in loceno obnovitveno geslo.

Namestitev:
1. zazeni namesti_v30.bat;
2. zazeni preveri_v30.bat;
3. nato zazeni zazeni_mikrofix_v30.bat.

Razvojna omejitev:
Te izdaje se ne uporablja za produkcijsko izdajo racunov ali prave poslovne
podatke, dokler celotni sprejemni preizkus v30 ni uspesen in FURS produkcijski
postopek ni posebej pravno ter tehnicno potrjen.
IZOLIRANI E2E TEST
------------------
- Za celovit ročni preizkus zaženi zazeni_mikrofix_v30_E2E_TEST.bat.
- Uporablja izključno MikroFixData_V30_E2E_TEST; produkcijska baza ni dostopna.
- FURS produkcija in produkcijsko potrdilo sta izključena. Lokalni testni računi
  ostanejo TEST_DISABLED; ločena konzola FURS TEST se ne sproži samodejno.
- Podroben vrstni red je v NAVODILA_E2E_TEST_V30.txt.

R9 DEV VAROVALKE (3. 9. 2026)
--------------------------------
- Zunanji nosilec je nastavljen na KINGSTON E:\MikroFix_Backup.
- Na zacetnem zaslonu klikni NASTAVI in doloci loceno obnovitveno geslo
  z najmanj 12 znaki. Geslo v nastavitvah ni shranjeno kot navadno besedilo.
- Ob vsakem zagonu se preveri, ali je zadnja preverjena sifrirana kopija
  mlajsa od 24 ur. Hrani se zadnjih 30 dnevnih kopij.
- Ce kljucek ni prikljucen, program dela naprej, vendar prikaze opozorilo.
- Obnovitveni center prikaze prekinjene ali neuspele poslovne operacije.
- Zakljucek odkupa hkrati potrdi pogodbo, morebitni gotovinski izdatek in
  napravo v karanteni; delni zakljucek ni dovoljen.
- V odprtem servisu je na voljo PONUDBA ZA ODKUP. Zavrnjena ponudba vrne
  servis v delo, potrjena pa ga brez servisnega racuna poveze z odkupom.
