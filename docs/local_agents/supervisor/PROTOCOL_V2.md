# MikroFIX lokalni razvojni protokol v2

## Namen

Lokalna modela pripravljata kandidate brez neposrednega dostopa do zaščitenega
originala. Zaupanja vredna koda, ne jezikovni model, upravlja stanje, poti,
ukaze, dovoljenja, retryje, dokazila in obvestila.

## Vloge

- **Lastnik** določi cilj, potrdi časovno omejeno dovoljenje za pisanje in
  sprejema poslovne, pravne, davčne ter varnostne odločitve.
- **Deterministični manager** trajno zapiše zahtevo, preveri strukturiran predlog,
  iz dejanskega repozitorija določi kontekst in teste ter pripravi nespremenljiv
  načrt.
- **GPT-OSS lokalni vodja** z bralnimi orodji razišče zahtevo in predlaga samo
  cilj, dovoljene datoteke, atomske naloge, omejitve in sprejemne kriterije.
- **GPT-OSS izvajalec** deluje v ločeni, čisti izvedbeni seji in sme pisati samo
  v dovoljene datoteke izoliranega worktreeja.
- **Deterministični gatekeeper** preveri obseg diffa, pričakovane datoteke,
  teste, trajnost dokazil, retryje in terminalno stanje.
- **GPT-OSS reviewer** deluje v novi, čisti pregledovalni seji ter pregleda
  kandidat, testna dokazila in vsak sprejemni kriterij posebej. Ne upravlja
  stanja in ne odobrava prenosa v original. Ločitev sej prepreči deljenje
  pogovorne zgodovine, ne zagotavlja pa raznolikosti modelov.
- **Codex** opravi paketni končni pregled ter šele po odobritvi prenese kandidat
  v zaščiteni original in pripravi backup.

## Trajna glavna seja

Glavni GPT-OSS vodja enkrat prejme sestavljen projektni spomin iz kanoničnega
konteksta, master specifikacije, potrjenih lekcij in tega protokola. Potrditi
mora `CONTEXT_READY` skupaj z natančnim `parent_session_id` in SHA-256 spomina.
Dokler se hash virov ne spremeni, naslednje zahteve vsebujejo samo nalogo in
konkretne poti; jedrnih dokumentov ne bere ponovno. Vsak atom, ki ga vodja
določi, se odpre kot podizvajalska naloga z istim `parent_session_id`, istim
projektnim spominom in že podanimi shemami orodij. Ob spremembi kateregakoli
vira se stanje vrne v `AWAITING_CONTEXT_ACK` in celoten kontekst se potrdi znova.

## Stanja

```text
REQUESTED -> PLANNING -> READY_FOR_OWNER_LEASE -> EXECUTING
          -> VERIFYING -> REVIEWING -> CHECKPOINT -> READY_FOR_CODEX_REVIEW
                                      \-> BLOCKED
```

Notranji modelni odgovor ni terminalno stanje. Lastnik prejme zaključno
obvestilo samo za `READY_FOR_CODEX_REVIEW`, `BLOCKED` ali `FAILED` celotnega
teka.

## Nespremenljive garancije

1. Model ne izbira ukazov ali imen preverjevalnih profilov.
2. Modelne poti se normalizirajo in preverijo pred izdelavo worktreeja.
3. Kontekst vsebuje samo datoteke, katerih obstoj je manager preveril.
4. Načrt, manifest dovoljenih datotek in začetni checkpoint so nespremenljivi.
   Izvedbeni sistemski poziv se vedno naloži iz zaščitenega nadzornega repozitorija,
   nikoli iz kandidatovega worktreeja.
5. Retry uporablja isti omejeni kandidat; odsotnost novega diffa ne izbriše že
   veljavne spremembe.
6. Sprememba zunaj dovoljenega obsega takoj blokira tek.
7. Infrastrukturna napaka, modelna zavrnitev in neuspešen test so različni
   dogodki; ne sprožajo slepe enake ponovitve.
8. Lokalni `APPROVE` ali `READY` nikoli ne pomeni merge-a, produkcije ali backupa.
9. Vsak končni rezultat ima diff, izpis testov, reviewerjevo kontrolno listo in
   kriptografske hash-e dokazil.
10. Stara povezava ostane nedotaknjena, dokler v2 ne prestane uspešnega,
    zavrnjenega, blokiranega in prekinjenega simulacijskega teka.

## Poraba modelov

- Manager najprej opravi vse mehanske preglede brez modela.
- GPT-OSS dobi omejen kontekst in bralna orodja, ne celotnega repozitorija v
  pozivu.
- GPT-OSS izvajalec dobi samo en atom, dovoljene datoteke, sprejemne kriterije
  in zadnjo konkretno povratno informacijo.
- Reviewer dobi samo zahtevo, diff, rezultate testov in kontrolno listo.
- Infrastrukturne napake se ne pošiljajo modelu v popravljanje.

## Uvajanje

1. Enote za strukturiran predlog, preverjene poti in deterministične teste.
2. Simulacije napačnega JSON-a, izmišljene poti, praznega diffa, ponovnega
   zagona, timeouta in dvojnega obvestila.
3. Dokumentacijska pilotna naloga v izoliranem worktreeju.
4. Majhna kodna pilotna naloga z regresijskim testom.
5. Šele nato preklop bližnjice in samodejnega zagona na v2.
