# MikroFIX — kanonski kontekst lokalnih agentov

Status: **izpeljan povzetek, ne samostojen vir poslovnih pravil**

Ta dokument se lokalnemu pregledovalcu pošlje skupaj s celotnim urejenim
kanonskim sklopom 01–05. Ob neskladju vedno prevladajo izrecna zadnja odločitev
uporabnika, `01_POSLOVNA_PRAVILA.md` in nato `02_CILJNA_ARHITEKTURA.md`.

## Stalna dejstva

- MikroFIX ima tri ločene programe: Office vodi vhod blaga in administracijo,
  Servis vodi naloge, čas, porabo delov, odkup in obnovo, Blagajna pa izdaja
  račune, neposredno prodaja in obračunsko zapira servise.
- Skupne podatke spreminja samo centralni API nad PostgreSQL. Servis ima lokalni
  SQLite in urejen idempotentni outbox za offline delo.
- Pozitivna zaloga nastane samo iz potrjenega prejema ali potrjenega dela iz
  dokumentirano odkupljene donorske naprave. `STOCK_EXCEPTION`, negativna
  zaloga in izmišljena količina so prepovedani.
- Servis del porabi enkrat ob tehničnem zaključku. Servisni račun zaloge ne
  spremeni ponovno. Konflikt zaloge ne sme povzročiti delnega knjiženja.
- Servis pripravi nespremenljiv osnutek s pozitivnimi vrsticami. Samo Blagajna
  sme izdati ničelni servisni račun in deterministično dodati enake negativne
  izravnalne vrstice; te ne vračajo zaloge.
- Občutljiv ukaz uporablja `operation_id`. Ista vsebina z istim ID vrne celoten
  prvotni rezultat, druga vsebina z istim ID se zavrne. Za isti osnutek sme
  nastati največ en račun in ena številka tudi ob sočasnosti ali izgubi odgovora.
- Vsak zagon vsakega programa zahteva novo prijavo. Lokalni offline uporabniški
  posnetek velja največ sedem dni. Polna inventura zahteva zaprto prodajo,
  zaključene dokumente in vse Service postaje brez čakajoče sinhronizacije.

## Pogodba pregledovalca

Lokalni agent je samo bralni tehnični pregledovalec. Ne spreminja datotek, ne
izvaja ukazov, ne upravlja aplikacij in ne sprejema odločitev. Analizira samo
poslani izsek. Ne izmišlja pravnih, davčnih, FURS, zalogovnih ali procesnih
pravil. Odgovor loči na potrjena dejstva, konflikte, tveganja, manjkajoče teste
in negotovosti. Če kontekst ne zadošča, pove natančno, kaj manjka. Codex vsako
ugotovitev sam preveri in ostane edini nadzornik sprememb ter testov.

## Ločeni nadzorovani izvajalec

Na izrecno odločitev lastnika z dne 19. 9. 2026 obstaja še tretja vloga
`MikroFIX Qwen – izvajalec`. Ta vloga ni eden od zgornjih pregledovalcev in ne
spreminja njune pogodbe. Izvajalec sme uporabljati samo namenski lokalni most,
ki vodi revizijsko sled. Pisanje, testi, namizna dejanja in izjemni terminal so
na voljo le med kratkim dovoljenjem, ki ga lastnik ročno vključi. Poljubni
terminalski ukaz zahteva še posamično vidno potrditev. Skrbniški ukaz sme
sprožiti sistemsko preverjanje, vendar geslo vedno vnese lastnik in ga agent ne
prejme ali hrani. Izvajalec ne sme sam potrjevati poslovnih pravil, produkcijskih
podatkov, FURS postopkov, certifikatov ali bančnih operacij. Njegova stalna
navodila so v `docs/local_agents/izvajalec/QWEN_IZVAJALEC_SYSTEM.md`.

Dogovorjeni delovni tok je: lastnik določi cilj in potrebne poslovne odločitve,
Codex nalogo usmeri in določi merila sprejema, Qwen izvede raziskavo, spremembe
in teste, GPT-OSS po potrebi opravi neodvisni skeptični pregled, Codex pa
spremembo potrdi ali zavrne. Običajnega izvedbenega dela Codex ne podvaja, razen
če je lokalni izvajalec dokazano blokiran ali bi bila opustitev neposrednega
posega nevarna za podatke oziroma projekt. Pravne, davčne, finančne in nove
poslovne odločitve kljub temu ostanejo lastnikove.
