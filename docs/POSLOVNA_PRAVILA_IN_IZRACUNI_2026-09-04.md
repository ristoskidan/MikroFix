# MikroFIX — inventar poslovnih pravil in načinov računanja

Datum pregleda: 4. 9. 2026

Ta dokument združuje pravila, ki vplivajo na denar, cene, zalogo, statuse,
dokumente, roke in dovoljenja. Vir resnice za vse potrjene podrobnosti ostaja
`MIKROFIX_v30_MASTER_SPECIFIKACIJA.md`. Oznake:

- **IZVEDENO** — pravilo je razvidno iz aktivne kode in pokrito s testi;
- **DOGOVORJENO** — potrjeno v glavni specifikaciji, vendar ni nujno v celoti izvedeno;
- **NESKLADJE** — aktivna koda računa drugače od potrjene specifikacije;
- **ZAMRZNJENO** — dogovorjeno za pozneje in ni pogoj za prvo produkcijo;
- **ODPRTO PRED PRODUKCIJO** — zahteva zunanjo pravno, davčno ali računovodsko potrditev.

## 1. Denarna natančnost in davki

1. **DOGOVORJENO/DELNO IZVEDENO:** finančni izračuni uporabljajo decimalno aritmetiko.
2. Vhodne nabavne/prodajne cene na enoto, delež prevoza in dejanska nabavna
   cena se pri prejemu vodijo na 4 decimalna mesta.
3. Vrstični zneski, plačila, skupni zneski in javni prikaz se zaključijo na 2
   decimalni mesti; uporabljeno je komercialno zaokroževanje `ROUND_HALF_UP`.
4. Razlika pri finančnem ujemanju mora biti natančno `0,00 EUR`; tolerance 0,01 EUR ni.
5. **IZVEDENO:** prejemni modul trenutno podpira samo EUR.
6. **IZVEDENO:** trenutni davčni način je `NON_VAT`, privzeti DDV 0 %, javna
   opomba: »Oproščeno po prvem odstavku 94. člena ZDDV-1.«
7. Dokler ni pravice do odbitka vstopnega DDV, je neodbitni DDV del dejanske
   nabavne vrednosti.
8. **ODPRTO PRED PRODUKCIJO:** davčni status in datum veljavnosti mora potrditi računovodja.

## 2. Prejem blaga in izračun nabavne vrednosti

1. Neto osnova vrstice:
   `količina × neto cena na enoto × (1 − dobaviteljev rabat / 100)`.
2. Končni izračun vrstice:
   `neto osnova × (1 + DDV / 100)`, zaokroženo na cent.
3. Izračun vrstice se mora natančno ujemati z ročno prepisanim dobaviteljevim
   končnim zneskom iste vrstice. Razlike drugih vrstic se ne smejo izničevati.
4. Dokumentna kontrola:
   `vsota dobaviteljevih končnih zneskov vrstic + prevoz = znesek dokumenta`.
5. Prevoz se deli sorazmerno po številu fizičnih kosov:
   `prevoz × količina vrstice / skupna količina vseh vrstic`.
6. Delež se vodi na 4 decimalke; zadnja vrstica dobi deterministični ostanek,
   zato je vsota razdeljenega prevoza natančno enaka vnesenemu prevozu.
7. Dejanska nabavna vrednost na kos:
   `(dobaviteljev končni znesek vrstice + delež prevoza) / količina`.
8. Potrjena prodajna cena ob prejemu mora biti najmanj:
   `dejanska nabavna vrednost na kos × 1,05`.
9. Nova skupna cena glavnega artikla mora to mejo spoštovati pri najdražjem še
   razpoložljivem LOT-u.
10. Dobaviteljev nabavni rabat znižuje nabavno vrednost; nikoli se samodejno ne
    prenese kupcu kot prodajni rabat.
11. Dobavnica brez računa ustvari blokirano količino. Poznejši račun jo samo
    sprosti in je nikoli ne poveča drugič.
12. Osnutek nima zalogovnega učinka. Zaključek poveča zalogo natanko enkrat.
13. Zaključen dokument je nespremenljiv; napaka zahteva celotni storno in nov dokument.
14. Storno prejema je dovoljen samo, če je celotna konkretna zaloga tega prejema
    še na voljo; delnega vračila dobavitelju trenutna pravila ne dovoljujejo.
15. Kombinacija dobavitelj + vrsta dokumenta + dobaviteljeva številka je enolična.

## 3. Dogovorjeni cenovni profili fizičnih artiklov

### Skupna dogovorjena formula

1. Stroškovna osnova:
   `dejanska nabavna vrednost × (1 + garancijska rezerva)`.
2. Potrošni material ni pavšalni del profila; tesnilo, lepilo in drugi materiali
   se evidentirajo samo ob dejanski uporabi.
3. Prava prodajna marža ni pribitek. Priporočilo pred zaokroževanjem:
   `stroškovna osnova / (1 − ciljna marža)`.
4. Rezultat se zaokroži **navzgor na naslednji cel evro**.
5. Fiksnega minimalnega prispevka v EUR ni.
6. Ločenega »trdega minimuma marže« ni. Edina prodajna spodnja meja fizičnega
   artikla je dejanska nabavna vrednost konkretnega LOT-a + 5 %.
7. Vsak profil ima tri stroškovne razrede. Sprememba profila ustvari novo
   različico in ne preračuna zgodovine.
8. Uporabnik lahko potrdi drugačno ceno nad spodnjo mejo. Shranijo se
   priporočena cena, potrjena cena, dejanska marža, uporabnik in čas.
9. Ročno izbrana marža mora biti `0 % ≤ marža < 80 %`; nad 60 % zahteva drugo potrditev.

### Potrjena tabela profilov v1

| Profil | Nizki razred | Srednji razred | Visoki razred | Garancijska rezerva |
|---|---:|---:|---:|---:|
| Baterija FixPremium | do 15 € → 35 % | nad 15 do 30 € → 30 % | nad 30 € → 20 % | 8 % |
| Baterija Diagnostic | do 25 € → 30 % | nad 25 do 50 € → 25 % | nad 50 € → 20 % | 8 % |
| Zaslon LCD/In-Cell | do 30 € → 35 % | nad 30 do 60 € → 30 % | nad 60 € → 25 % | 5 % |
| Zaslon Soft OLED | do 80 € → 30 % | nad 80 do 140 € → 25 % | nad 140 € → 20 % | 5 % |
| Obnovljen originalni zaslon | do 80 € → 30 % | nad 80 do 160 € → 25 % | nad 160 € → 20 % | 5 % |
| Kamera/večji modul | do 20 € → 40 % | nad 20 do 60 € → 30 % | nad 60 € → 20 % | 5 % |
| Priključek/manjši modul | do 10 € → 45 % | nad 10 do 30 € → 35 % | nad 30 € → 25 % | 5 % |
| Računalniški/prenosniški/konzolni del | do 50 € → 30 % | nad 50 do 150 € → 20 % | nad 150 € → 12 % | 3 % |
| Splošni rezervni del | do 20 € → 35 % | nad 20 do 75 € → 25 % | nad 75 € → 15 % | 5 % |

### Kritično ugotovljeno neskladje

**NESKLADJE PRED PRODUKCIJO:** aktivna koda še uporablja starejšo formulo:

`landed = nabavna cena + pavšalni material + nabavna cena × rezerva`

`priporočilo = max(landed / (1 − target_margin), landed + minimalni prispevek)`

ter ločen `hard_margin` in zaokroževanje navzgor na 5 EUR. Aktivne nastavitve
vsebujejo samo pet starih profilov. To je v neposrednem nasprotju s potrjenimi
pravili zgoraj in mora biti usklajeno pred produkcijo.

## 4. Servisno delo, čas in tarife

1. **IZVEDENO:** L0 je `22,00 EUR` in je samo spodnja varovalka po rabatu za T1/T2.
2. T1: prvih začetih 60 minut `30,00 EUR`; vsaka nadaljnja začeta 30-minutna
   enota `15,00 EUR`.
3. T2: prvih začetih 60 minut `90,00 EUR`; vsaka nadaljnja začeta 30-minutna
   enota `15,00 EUR`.
4. Formula T1: `30 + 15 × število dodatnih začetih polur`.
5. Formula T2: `90 + 15 × število dodatnih začetih polur`.
6. Obračunske minute: `60 + 30 × število dodatnih enot`.
7. T1/T2 sta na računu vsaka ena združena vrstica; dodatna polura ni ločena vrstica.
8. Uporabnik v servisnem toku dodaja fizične artikle in zapiše resničen opis
   dela. T1 oziroma T2 določi pogodbeni kontekst naloga; tarife, trajanja ali
   poljubne cene uporabnik ne vnaša ročno.
9. Čas se meri od prvega ukaza `Začni servis` do `Zaključi servis`. Ponovno
   odpiranje istega naloga začetka ne ponastavi. Začetni prevzem pred začetkom
   časovnika se ne šteje v obračunljivo delo.
10. Na PDF je merska količina dela izražena v minutah, npr. `90 min`.
11. Prihodnja sprememba tarife velja samo za nove postavke; dokument shrani
    uporabljeno različico, prvo tarifo, dodatno polurno tarifo in minute.
12. **DOGOVORJENO/NEPREVERJENO V CELOTNEM TOKU:** diagnostika `30,00 EUR`; če se
    popravilo izvede, se diagnostika všteje oziroma prizna.
13. Preseganje dogovorjene ponudbe je dovoljeno samo z izrecno odobritvijo stranke.
14. Servis ostane `V obdelavi` do izdaje povezanega računa; isti servis ne sme
    biti zaračunan dvakrat.

## 5. Logistika in paketne ponudbe

1. Konfigurirana organizirana prevzem-vračilo storitev: `30,00 EUR`.
2. Organizirana trasa velja do največ `12 km` skupnega dodatnega odklona.
3. Namenski prevzem/vračilo ne sme biti cenejši od organiziranega; konfigurirana
   začetna vrednost je 30 EUR, vendar ni potrjena kot vedno javno fiksna cena.
4. Konfiguriran paketni popust zaslon + baterija: `5,00 EUR`.
5. Stara dobaviteljska ponudbena formula:
   - organizirani servis: `cena dela + T1 + prevzem/vračilo`;
   - terenski servis: `cena dela + prva ura T2`;
   - paket: `oba dela + ena storitev − 5 EUR`.
6. **NESKLADJE/NI JEDRO PRODUKCIJE:** ta ponudbeni kalkulator uporablja isto
   zastarelo profilno formulo iz prejšnjega poglavja in ni vir cene na POS računu.

## 6. Prodajni rabat in račun

1. Zahtevani rabat se omeji na območje 0–100 %.
2. Zahtevana cena po rabatu:
   `osnovna prodajna cena × (1 − zahtevani rabat / 100)`.
3. Fizični artikel po rabatu ne sme pasti pod:
   `najvišja zaščitena dejanska nabavna vrednost razpoložljivih LOT-ov × 1,05`.
4. Storitev T1/T2 po rabatu ne sme pasti pod L0 = 22 EUR.
5. Če je zahtevani rabat previsok, ga program brez opozorila zmanjša na največji
   dovoljen rabat. Kupec vidi samo dejansko uporabljeni rabat.
6. Notranja revizija mora ohraniti zahtevani in uporabljeni rabat ter razlog
   `AUTO_DISCOUNT_FLOOR`.
7. Končna vrednost vrstice: `končna cena po varovalki × količina`.
8. Končni račun: vsota vseh vrstic, zaokrožena na cent.
9. Vsota pozitivnih plačil CASH + CARD + BANK_TRANSFER mora biti natančno enaka
   skupnemu znesku računa.
10. Delno ali previsoko plačilo ni dovoljeno. Razdeljeno plačilo je dovoljeno
    samo ob popolnem ujemanju; TRR del mora biti potrjeno prejet.
11. Gotovinski del zahteva odprto blagajno za izbrani prostor in napravo.
12. Prvi F10 pripravi jasno označen predogled brez številke, zalogovnega ali
    FURS učinka; naslednja izrecna potrditev izda nespremenjeni račun.
13. Izdaja je atomska in idempotentna: dvojni klik ne sme izdati drugega računa,
    ponovno odšteti zaloge ali porabiti nove številke.
14. Številčenje računov je ločeno po letu, poslovnem prostoru in napravi:
    `MFD-PIS-zaporedje` oziroma `MFT-PIS-zaporedje`.
15. Poznejši uspešni izpisi so `KOPIJA 1`, `KOPIJA 2` …; neuspešen tisk ne
    poveča števca in ne ustvari novega računa.
16. Celotni storno ustvari nov sledljiv storno dokument, obrne vsa plačila,
    povrne konkretne LOT-e in ponovno odpre povezan servis.
17. UPN QR mora vsebovati znesek računa natančno do centa in IBAN
    `SI56040000283154872`; UPN ni račun.

## 7. Blagajniški dnevnik

1. Za vsak prostor/napravo je lahko odprta samo ena blagajniška seja.
2. Začetno stanje in prešteto stanje ne smeta biti negativna.
3. Posamezni blagajniški dogodek ne sme biti 0,00 EUR.
4. Novi saldo:
   `začetno stanje + vsota vseh predhodnih podpisanih dogodkov + novi dogodek`.
5. Izdatek, ki bi povzročil negativen saldo, je blokiran.
6. Pričakovano stanje ob zaprtju:
   `začetno stanje + vsi prejemki − vsi izdatki`.
7. Razlika ob zaprtju:
   `dejansko prešteto stanje − pričakovano stanje`.
8. Zaključena seja in njeni dogodki so nespremenljivi; popravek se izvede z
   nasprotnim sledljivim dogodkom, ne s prepisom.

## 8. Odkup rabljene naprave

1. Odkupna cena mora biti večja od 0 EUR.
2. Privzeti način izplačila je bančno nakazilo; zahteva IBAN in potrditev
   dejansko izvedenega nakazila.
3. Gotovina je dovoljena samo do vključno `420,00 EUR` za celoten odkup.
4. Odkupa ni dovoljeno razdeliti na več gotovinskih izplačil.
5. Gotovinski odkup pripravi natanko en vezan blagajniški izdatek z enakim zneskom.
6. Sam osnutek ali PDF ne ustvarita zaloge. Naprava gre v karanteno šele po
   potrditvi podpisa in celotnega izplačila.
7. Zaključek je atomski in idempotenten; pogodbe, izplačila ali zaloge ni mogoče podvojiti.
8. Zaključek je dovoljen samo pri zeleni oceni tveganja in potrjeni identiteti,
   lastništvu, odstranitvi računov/aktivacijskega zaklepa ter odsotnosti MDM.
9. Komercialni zunanji IMEI/GSMA pregled v začetni različici ni obvezen.
10. Lastnikovo delo pri preusmeritvi servisa v odkup se računovodsko ne
    kapitalizira samodejno; porabljeni deli ostanejo stroškovno sledljivi.

## 9. Obnova in razstavljanje

1. Strošek obnovljene naprave v aktivni kodi:
   `odkupna vrednost + delo + deli + drugi neposredni stroški`.
2. Dogovorjena standardna osnova vključuje eno uro T1; časomer je samo referenčen.
3. Prodajna cena obnovljene naprave mora biti najmanj:
   `skupni strošek × 1,05`.
4. Prodajna zaloga nastane samo po uspešnem končnem testu in kot enoličen kos.
5. Pri razstavljanju smejo v zalogo samo dejansko potrjeni delujoči deli;
   vsak potrjeni modul ustvari natanko en `pulled part`.
6. Aktivna koda razdeli odkupno vrednost donorja enakomerno:
   `odkupna vrednost / število uporabnih delov`; zadnji del dobi centni ostanek.
7. **DOGOVORJENO ŠIRŠE PRAVILO:** razdeliti bi bilo treba celotni strošek donorja,
   vključno z eno uro T1 in drugimi neposrednimi stroški. Trenutna koda deli le
   zapisano nabavno vrednost naprave — to je treba pred produkcijsko uporabo
   razstavljanja uskladiti.
8. Če ni uporabnega dela, nastane popolni odpis; navidezni artikli se ne ustvarijo.

## 10. Garancije, neskladnost in hramba

1. Tržna garancija na vgrajene dele: 3 koledarski meseci od zaključka servisa.
2. Zamenjani del začne novo lastno 3-mesečno obdobje.
3. Obnovljena naprava: dogovorjena 12-mesečna komercialna garancija.
4. Rabljeno blago: 12-mesečni rok odgovornosti za neskladnost samo z ločenim
   podpisanim dogovorom; zakonske pravice ostanejo nespremenjene.
5. Normalna obraba kapacitete baterije ni napaka; napake obnovljene baterije so
   vključene v dogovorjeno 12-mesečno obdobje.
6. Za del, ki ga dobavi stranka, MikroFIX ne daje garancije na sam del; to ne
   izključuje odgovornosti za lastno opravljeno storitev.
7. Brez tlačnega testa se vodoodpornost ne zagotavlja.
8. Minimalne servisne fotografije se hranijo 5 let po zaključku, razen ob
   dokumentiranem odprtem sporu.
9. Neprevzeta naprava ima 30 brezplačnih dni, nato je dogovorjena hramba
   `10,00 EUR` za vsak začeti nadaljnji 30-dnevni blok. Po 90 dneh program
   zahteva ročno evidenco dejansko vročenega končnega poziva; lastništvo nikoli
   ne preide samodejno na MikroFix.
10. Koledarski mesečni roki ohranijo dan; če ciljnega dne ni, uporabijo zadnji
    dan ciljnega meseca.
11. **ODPRTO PRED PRODUKCIJO:** končna pravna besedila in računovodska obravnava
    odkupa, inventurnih razlik ter rabljenega blaga zahtevajo strokovno potrditev.

## 11. Zaloga, LOT-i, inventura in odpisi

1. Različne dobaviteljeve barkode ostanejo ločene zalogovne identitete.
2. Enaka barkoda in isti LOT sta količinska zaloga; serijski kos je enolična enota.
3. Poraba količinske zaloge uporablja FIFO po konkretnih LOT-ih.
4. Del, porabljen v servisu, se na računu ne odšteje drugič.
5. Ročni pozitivni popravek zaloge in neposredno knjiženje inventure sta blokirana.
6. Zmanjšanje je dovoljeno samo z dokumentiranim odpisom ali interno porabo.
7. Potrjen odpis je nespremenljiv; napaka zahteva celotni storno in nov dokument.
8. Delna inventura je slepi informativni popis in ne spremeni zaloge.
9. Polna inventura zahteva zaprte vse poslovne tokove in med izvedbo zaklene delo/prodajo.
10. Inventurna razlika: `dejansko stanje − knjigovodsko stanje`; manjko je
    negativen, presežek pozitiven.
11. Inventura sama ne spremeni zaloge. Spremembo atomsko izvede šele potrjeni
    sklep o obravnavi inventurnih razlik.

## 12. Dokumenti, statusi in dovoljenja

1. Osnutek mora biti jasno označen in ga ni mogoče zamenjati za končni dokument.
2. Javni PDF ne vsebuje nabavnih cen, internih marž, priporočil ali zasebnih opomb.
3. Potrjeni dokumenti, dogodki in povezave se ne prepisujejo ali brišejo;
   popravek je nov povezan dogodek oziroma storno.
4. Uradne številčne serije se vodijo po koledarskem letu; prvi dokument leta je `0001`.
5. Uporabnik vidi samo dovoljene module; pravice se ponovno preverijo ob izvedbi dejanja.
6. Daniel je začetni skrbnik. Uporabljenega uporabnika ni mogoče izbrisati, le deaktivirati.
7. Vsak uporabnik ima svoj PIN dolžine 6–14 številk; deljenje računov ni dovoljeno.
8. Pet napačnih PIN-ov zaklene prijavo za 5 minut; pet napačnih obnovitvenih
   gesel zaklene obnovitev za 30 minut.
9. Po 10 minutah neaktivnosti se aplikacija zaklene, odprto delo pa ostane ohranjeno.

## 13. Varnostne kopije

1. Kopija mora biti šifrirana z AES-256-GCM in na drugem fizičnem nosilcu.
2. Največ ena uspešna redna kopija na koledarski dan; hrani se zadnjih 30.
3. Ob uspešni 31. kopiji se odstrani najstarejša.
4. Ob zagonu se preverita celovitost in starost; nad 24 ur se takoj poskusi nova kopija.
5. Neuspeh ali odsoten nosilec opozori in se zapiše, vendar ne blokira dela.
6. Obnovitev zahteva skrbnika, ločeno obnovitveno geslo, preverjanje kopije in
   izrecno potrditev.
7. Pred obnovitvijo nastane ločen šifriran reševalni paket, ki se hrani 30 dni.
8. **DOGOVORJENO, TRENUTNO IZKLOPLJENO:** samodejno varnostno kopiranje še ni konfigurirano.

## 14. SI Price Report — potrjeno, vendar zamrznjeno

Ta sklop ni pogoj za prvo produkcijo in trenutno ne sme spreminjati cen.

1. Sveža dosegljiva marža:
   `1 − ((sveža FixShop cena × (1 + garancijska rezerva)) / trenutna prodajna cena)`.
2. Opozorilo pri absolutnem odstopanju od ciljne marže nad 2,00 odstotni točki.
3. Tržni referenčni podatek je navadno aritmetično povprečje primerljivih rednih
   in razpoložljivih ponudb, ločeno po delu/storitvi ter kakovostni skupini.
4. Sprejemljiv razpon MikroFIX cene: od `povprečje × 0,95` do `povprečje`.
5. Tržno opozorilo zahteva najmanj tri primerljive ponudbe; ena ali dve sta informativni.
6. Podvojena objava iste ponudbe istega ponudnika se šteje samo enkrat.
7. Veljavnih odstopajočih cen se samo zaradi odstopanja ne odstrani iz povprečja.
8. Aktivna stara eksperimentalna formula za odkupno priporočilo uporablja
   mediano ponudb, 20 % varnostni prostor omejen na 40–150 EUR, stroške obnove,
   10 % pogajalsko rezervo omejeno na 10–50 EUR in korak 10 EUR. Ta formula ni
   enaka pozneje potrjenim pravilom 261–282 in je zamrznjena.

## 15. FURS in produkcijska meja

1. Lokalni TEST račun ne sme dobiti produkcijskega EOR.
2. Produkcijski FURS zahteva ločeno dovoljenje in potrjeno produkcijsko potrdilo.
3. **TRENUTNA BLOKADA:** aktivni odjemalec pozna samo FURS testne naslove;
   produkcijski odjemalec še ni implementiran.
4. Fiskalizacija, številka računa, zalogovni učinek in tisk morajo biti en
   idempotenten sledljiv proces; napaka tiskanja ne sme ustvariti novega računa.

## 16. Kratek seznam neskladij, ki jih je treba razrešiti pred produkcijo

1. Cenovni profili v kodi niso potrjeni tristopenjski profili iz specifikacije.
2. Koda še uporablja pavšalni material, minimalni prispevek, `hard_margin` in
   zaokroževanje na 5 EUR, čeprav je bilo dogovorjeno njihovo odstranjevanje.
3. Aktivnih je le pet starih kategorij namesto devetih potrjenih profilov.
4. Donorski deli trenutno delijo samo nabavno vrednost naprave, ne nujno celotnega
   dogovorjenega stroška z eno uro T1 in drugimi neposrednimi stroški.
5. Diagnostika 30 EUR, priznanje ob popravilu, ležarina in nekatera logistična
   pravila so konfigurirana oziroma dogovorjena, vendar nimajo enako močne
   avtomatske pokritosti kot POS, prejemi in blagajna.
6. Produkcijski FURS še ni izveden.
7. Samodejne varnostne kopije še niso vključene in konfigurirane.
