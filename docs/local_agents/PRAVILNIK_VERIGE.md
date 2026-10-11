# MikroFIX — zgodovinski pravilnik lokalne agentne verige

Status: **zgodovinski procesni vir; nadomeščen 11. 10. 2026**
Veljal od: 25. 9. 2026 do 11. 10. 2026

Ta pravilnik ne določa več aktivnega izvedbenega toka. Trenutna razdelitev vlog
in potek sta zapisana v `docs/local_agents/README.md`, operativne odločitve pa v
vlogovnih zapisih skrbnikov. Spodnja vsebina ostaja nespremenjena kot sled
prejšnje nadzorovane verige in tehnični kontekst za njene artefakte.

## 1. Namen in avtoriteta

Ta dokument določa pot zahteve od pogovora z lastnikom do Codexove odločitve.
Ne določa novih poslovnih pravil. Poslovni kanon ostaja v `docs/kanon/`, strojne
pogodbe pa v JSON shemah pod `docs/local_agents/supervisor/`.

Ob neskladju velja:

1. zadnja izrecna odločitev lastnika, trajno vključena v trenutno nalogo;
2. `docs/kanon/01_POSLOVNA_PRAVILA.md`;
3. `docs/kanon/02_CILJNA_ARHITEKTURA.md`;
4. `docs/kanon/03_SPREJEMNI_TESTI.md`;
5. ta pravilnik za proces lokalnih agentov;
6. stanje izvedbe in zgodovinski dokumenti samo kot dokaz, ne kot nova zahteva.

## 2. Vloge so ločene od modelov

- **Lastnik** poda cilj, dopolnitve in poslovne odločitve ter odobri izvedbo.
- **`conversation_lead`** je orkestrator brez običajnih projektnih orodij. Iz
  obveznih vprašanj pripravi eno do tri omejene podnaloge, zaporedno preveri
  worker rezultate in izdela samo sintezo, vezano na managerjeve evidence ID-je.
- **`research_worker`** v sveži read-only seji pregleda eno podnalogo. Lastnik
  njegov model izbere ločeno; po vsakem poskusu se model sprosti.
- **`plan_preparer`** je ločena seja brez skritega spomina raziskovalca. Ob
  `/prepare-plan` iz shranjene zahteve, Codexovega prompta, poročila in manifesta
  pripravi prikazno revizijo načrta.
- **`plan_validator`** po `/plan-dispatch` neodvisno preveri prikazano revizijo
  in pripravi omejen kandidat načrta ali natančen `BLOCKED`.
- **Deterministični manager** preveri izvor zahtev, poti, shemo, Git checkpoint
  in izdela immutable izvedbeni načrt. Model ne določa testnih ukazov ali stanj.
- **`executor`** po lastnikovem `/approve-plan` v izoliranem worktreeju izvede
  en omejen atom in sme pisati samo v dovoljene datoteke.
- **Deterministični gatekeeper** preveri dejanski diff, obseg in imenovane teste.
- **`local_reviewer`** v novi seji pregleda samo zahtevo, kandidatni diff,
  rezultate testov, sprejemna merila in ciljno izbrane kanonične module.
- **Codex** opravi končni pregled kandidata. Šele njegova odobritev dovoli
  prenos v zaščiteni original; objava ali produkcija zahtevata svoj ukaz.

Model, tag, digest in runtime parametri so lastnosti immutable snapshota, ne
imena vloge ali prompta. Zamenjava modela ne preimenuje vloge ali dokumentov.

## 3. Uradni tok

```text
lastnik -> Codexov omejeni raziskovalni prompt
  -> /research (ločeno zamrznjena lead in worker model)
  -> conversation_lead razdeli 1..3 podnaloge
  -> zaporedni research_worker poskusi (1 začetni + največ 2 popravka)
  -> deterministične evidence kartice in evidence gate
  -> nespremenljiv, verzioniran DOSSIER_READY
  -> AWAITING_CODEX_RESEARCH_REVIEW
  -> /research-approved <dossier_sha256>
  -> /prepare-plan samo iz trenutno potrjenega dosjeja
  -> plan_preparer (ločena seja)
  -> pogovor in popravki prikazane revizije
  -> /prepare-plan (nova dokončna revizija)
  -> /plan-dispatch
  -> plan_validator
  -> deterministični manager
  -> READY_FOR_OWNER_LEASE
  -> /approve-plan
  -> executor
  -> diff/test gate
  -> local_reviewer
  -> READY_FOR_CODEX_REVIEW ali BLOCKED
  -> Codex APPROVE / REVISE / REJECT
  -> Codex prenese isti preverjeni kandidat in ustvari commit
  -> /codex-approved <SHA> preveri trenutni čisti HEAD, poti in končne blob hashe
  -> APPLIED samo po potrjenem prenosu
```

`/prepare-plan` nikoli ne pošlje načrta naprej. `/plan-dispatch` pošlje samo
zadnjo nespremenjeno prikazano revizijo; če se je pogovor vmes spremenil, ukaz
fail-closed zahteva nov `/prepare-plan`.

`/replace-goal` razveljavi povezavo do vseh dotedanjih raziskovalnih dokazov.
Novi cilj mora začeti novo `/research` nalogo; starega poročila ali manifesta ni
dovoljeno ponovno uporabiti.

## 4. Kontekst po vlogah

- Pogovorni vodja dobi zahtevo, obvezna vprašanja, projektni zemljevid in kanon,
  nima pa običajnih projektnih orodij. Worker dobi samo eno podnalogo, izrecni
  obseg in bralna orodja; popravek vedno teče v novi omejeni seji.
- Pripravljavec načrta dobi samo eksplicitno shranjene in hashirane artefakte.
  Ne podeduje spomina ali seje raziskovalca in nima projektnih orodij.
- Validator dobi zamrznjeni PlanIntent, prikazano revizijo, osnovni poslovni
  kanon in konkretne poti trenutne naloge.
- Izvajalec dobi samo atom, dovoljene datoteke, sprejemna merila, relevantne
  poti in zadnjo konkretno povratno informacijo gatea.
- Reviewer dobi samo dokazni paket kandidata in tematske kanonične module,
  izbrane glede na nalogo.
- Celotni zgodovinski master, stare modelne ocene in interni eval-dnevnik se
  nobeni vlogi ne dodajajo v prompt.

Moduli pod `docs/local_agents/pravila/` so mehansko generirani iz trenutnega
`docs/kanon/`. Imajo hashe virov in niso nov neodvisen vir pravil.

## 5. Dokazi in fail-closed meje

- Vsaka naloga zaklene vlogo, model/tag/digest, prompt in njegove hashe,
  runtime parametre, kontekst, checkpoint ter vhodne in izhodne hashe.
- Neznano stanje, prehod, polje sheme, pot ali orodje se zavrne brez tihega
  nadaljevanja.
- Modelov opis ni dokaz. Dokazi so dejanski tool calli, prebrane datoteke,
  diff, izpis imenovanega testa in trajni audit dogodki.
- Bridge pred izvedbo preveri ime, dovoljeni seznam, argumente in osnovno shemo
  orodja. Ob napaki model dobi strukturirano kodo napake, opis pravilne rabe,
  seznam orodij, dovoljenih v konkretni seji, ter primer relevantnega klica.
- Pri `project_search` je `query` iskano besedilo, `file_glob` pa samo filter
  imen datotek. Prazno iskanje z očitno zamenjanima pomenoma ne šteje kot nov
  projektni dokaz; bridge najprej opozori, nato ponovitev iste napake blokira.
- Identičen `project_search` se izvede samo prvič. Drugi klic dobi opozorilo,
  tretji je blokiran, četrti pa zaključi nalogo z `LOOP_DETECTED`. Vsi dogodki
  in števci ostanejo v trajni bridge sledi.
- Sprememba zunaj `allowed_files`, odsoten pričakovani diff ali ponavljanje brez
  nove spremembe blokira tek.
- Nobena lokalna oznaka `READY`, `APPROVE` ali `VERDICT` ne pomeni mergea,
  produkcije ali objave.
- Obstoječih tujih ali uporabnikovih sprememb se ne vrača, čisti ali prepisuje.
- `git checkout`, `restore`, `reset`, `clean`, `merge`, `push` in deploy niso
  dovoljeni lokalnim modelom.

## 6. Lekcije in eval-dnevnik

`docs/local_agents/LEKCIJE.md` je interni zapis za lastnika in Codex: beleži,
kako so se modeli obnašali, kateri prompti so bili učinkoviti in katere napake
se ponavljajo. Modelom se ne pošilja. Potrjeno splošno varnostno pravilo se po
človeškem pregledu prenese v ustrezni prompt, shemo, test ali ta pravilnik;
modelu se ne kaže seznam njegovih preteklih napak.

## 7. Terminalna stanja in obvestilo

Celotni tek obvesti lastnika samo ob pomembnem checkpointu:
`READY_FOR_OWNER_LEASE`, `READY_FOR_CODEX_REVIEW`, `BLOCKED` ali `FAILED`.
Surov modelni izhod in skrivnosti ne sodijo v obvestilo. Audit in bridge sled
ostaneta avtoritativni dokaz pod konkretnim ID-jem naloge.
