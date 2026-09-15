# MikroFIX — kanonski kontekst lokalnih agentov

Status: **izpeljan povzetek, ne samostojen vir poslovnih pravil**

Ta kratki dokument se pošlje lokalnemu pregledovalcu ob vsaki nalogi. Ob
neskladju vedno prevladajo `01_POSLOVNA_PRAVILA.md`, nato
`02_CILJNA_ARHITEKTURA.md` in izrecna zadnja odločitev uporabnika.

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
