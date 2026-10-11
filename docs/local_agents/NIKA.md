# Nika — lokalni coder profili in zaganjalniki MikroFIX v30

Stanje preverjeno: 11. 10. 2026. Skrbnica tega zapisa: **Nika** (Codex) —
zaganjalniki, profilni `AGENTS.md`, Codex/Ollama nastavitve in uskladitev navodil.
To je operativni popis, ne poslovni kanon in ne samodejno naložen prompt.
Ob vsaki naslednji dogovorjeni spremembi teh treh profilov posodobi ta zapis po
pregledu dejanskih datotek. Zapis ne pomeni spremljanja v ozadju med sejami.

## Namizje in dostop

Odločitev lastnika, 11. 10. 2026: lokalne modele zaganjamo iz terminala.
Namizne zaganjalnike in bližnjice do testnih ukazov smo premaknili v koš;
izvršljivi ukazi v `/home/mikrofix/.local/bin/` so ostali nedotaknjeni.
Na namizju je delovni dokument `/home/mikrofix/Desktop/aktivna naloga.md`.

Namizje upravlja Xfce (`xfdesktop`). V kanalu `xfce4-desktop` so vidne
sistemske ikone Home, File system in Trash, prikaz fiksnih, odstranljivih in
omrežnih naprav pa je izključen. To spreminja samo prikaz ikon; particij ne
odklaplja. Lastnik je potrdil, da naj ikona Codex odpira aplikacijo; zato je
`/home/mikrofix/Desktop/Codex.desktop` edina izjema od pravila brez namiznih
zaganjalnikov. Bližnjica uporablja nameščeno aplikacijo `/usr/bin/chatgpt`,
ne terminalskega ukaza `codex`. Lokalni modeli ostajajo dostopni iz terminala.

## Privzeta vloga coderja

Odločitev lastnika, 11. 10. 2026: **trenutni profil Gemme 12B je privzeto
izhodišče za vlogo lokalnega coderja**. Pri nadaljnjem urejanju drugih coder
profilov najprej primerjaj njihovo vlogo, razporeditev navodil in način dela z
Gemminim profilom. To ne pomeni samodejnega kopiranja njenih modelnih parametrov
(npr. konteksta ali temperature) na druge modele. Posamezne prilagoditve in
izjeme ostanejo predmet izrecnega dogovora. Ta zapis sam po sebi ne spremeni
zaganjalnikov, `config.toml` ali `AGENTS.md` drugih agentov.

## Trenutno dejansko stanje

| Agent | Osnovni Ollama model | Aktivni lokalni tag | Zaganjalnik / profil | Kontekst / samodejna kompakcija | Parametri Ollama |
| --- | --- | --- | --- | --- | --- |
| Gemma 12B | `gemma4:12b` (arhitektura `gemma4`, 11,9B, Q4_K_M; enaki modelni blobi) | `mikrofix-gemma4-12b-focused-65k` | `gemma12-coder` → `mikrofix-gemma12-coder`; `CODEX_HOME=/home/mikrofix/.gemma12-codex` | 65.536 / 55.000 tokenov | temperature 0,35; top_p 0,9; top_k 40; repeat_penalty 1,05; draft_num_predict 3 |
| Mistral 7B | `mistral:7b` (arhitektura `llama`, 7,2B, Q4_K_M; enak modelni blob) | `mikrofix/mistral:7b-40k` | `mistral-coder` → `mikrofix-mistral-coder`; `CODEX_HOME=/home/mikrofix/.mistral-codex` | 40.960 / 34.000 tokenov | temperature 0,2; top_p 0,9; top_k 40; repeat_penalty 1,05 |
| Ministral 3 8B | `ministral-3:8b` (arhitektura `mistral3`, 8,9B, Q4_K_M; enak modelni blob) | `mikrofix/ministral-3:8b-coder-16k` | `mikrofix-ministral-coder`; `CODEX_HOME=/home/mikrofix/.ministral-codex` | 16.384 / 13.000 tokenov | temperature 1,2; top_p 0,9; top_k 40; repeat_penalty 1,05 |

Vsi trije zaganjalniki uporabljajo lokalni Ollama Responses endpoint,
`model_reasoning_effort=none`, `model_verbosity=low`, `features.goals=true`,
`workspace-write`, brez interaktivne odobritve (`-a never`). Pred zagonom
preverijo berljivost profilnega in projektnega `AGENTS.md`, pravilno oznako
profilne politike in odsotnost napačno postavljenega projektnega
`.codex/AGENTS.md`. Nastavijo `VISUAL`/`EDITOR` na `nano`, če nista določena.
Začetni poziv zahteva le kratko navedbo vloge in pripravljenosti; ne dodeli
izvedbenega sklopa. Nobenega modela med tem popisom nisem zagnala.

Gemma 12B in Mistral imata izvedbeno vedenje v kratkem
`developer_instructions` v svojem `config.toml`; profilna `AGENTS.md` je
zemljevid projekta. Ministral je še na starejši ureditvi: nima tega
`developer_instructions`, njegov profilni `AGENTS.md` vsebuje obširna pravila.
Mistralov profil je simbolna povezava na HDD; ta pot je bila pri popisu berljiva.
Mistral ima tudi ločen raziskovalni zaganjalnik `mistral-research`, ki uporablja
isti profil in model, vendar `read-only` okolje in raziskovalni začetni poziv.

## Od kod pridejo navodila in kdo odloča

1. Codex ob novi seji naloži profilni `CODEX_HOME/AGENTS.md` in projektni
   `/home/mikrofix/MikroFIX_Service_v30_R9_LINUX_DEV/AGENTS.md`.
   `developer_instructions` v profilu dopolni izvedbeno vlogo. Sprememba teh
   datotek velja šele za novo sejo; zaganjalnik jih ne lepi ponovno v prompt.
2. Lastnik določi poslovna pravila, obseg posameznega `/goal` in odločitve med
   sklopi. `/goal` coderju poda *en* cilj z vrati; `/compact` ni odobritev novega
   cilja. Ob zagonu coder samo potrdi pripravljenost. Relevantni kanon se odpre
   ob cilju, ne celoten kanon preventivno ob začetku.
3. Poslovni vir je `docs/kanon/00_KAZALO.md` z aktivnimi razdelki; procesni vir
   je `docs/local_agents/PRAVILNIK_VERIGE.md`. Koda je dokaz trenutne izvedbe,
   ne samostojen vir novih poslovnih pravil.
4. Neposredni `*-coder` zaganjalniki so interaktivni Codex profili. Sami po
   sebi **ne dokazujejo**, da je bil izveden uradni tok `/research` → priprava
   in validacija načrta → lastnikova odobritev → izolirani `executor` →
   gatekeeper/reviewer → Codexov končni pregled. Vloge so ločene od imen modelov.
5. Coder izdela kandidatno spremembo in preveri vrata cilja. Codex opravi
   končni pregled, lastnik sprejme ali zavrne. Za odobren commit/push je po
   zadnjem dogovoru zadolžen poseben Git-agent, ne coder.

## Dogovorjene smernice, pomembne za nastavitve

- Kratki začetni pozivi brez starih ukazov `$task`/`$goal`; izvedbeni obseg je
  uporabnikov aktualni `/goal`, ne zagonski prompt. Model mora dobiti jasno
  vlogo coderja in počakati na prvi cilj. Gemma in Mistral sta bila temu že
  prilagojena; Ministral je ločen profil in še ni prestrukturiran po isti shemi.
- Profilni `AGENTS.md` naj bo predvsem kompakten zemljevid projekta; stalna
  izvedbena pravila spadajo v profilni `developer_instructions`, konkretno
  delo in vrata pa v `/goal`. Branje kanona naj bo ciljno. Ta delitev je trenutno
  uveljavljena pri Gemmi in Mistralu, ne še pri Ministralu.
- Pri implementaciji: pokaži resnično obstoječo kodo in namen pred posegom;
  pripravljeni patch preveri na trenutni podlagi in pred `apply_patch` preveri
  sintakso *celotne predvidene nastale izvorne datoteke* v začasni kopiji ali
  pomnilniku. Po posegu preveri dejanski diff in zahtevana vrata. Uporabi
  obstoječi `.venv` za Python, `.venv-server` za Alembic in pred migracijo
  potrdi izolirano ciljno bazo. To je zapisano v projektnem `AGENTS.md` in
  kratkih profilih Gemme/Mistrala.
- Ne izmišljaj si poti, rezultata testa ali poslovnega pravila. Ohrani tuje
  spremembe. Pri izrecnem implementacijskem cilju coder samostojno izvede varna
  relevantna preverjanja do vrat, ne sprašuje znova za dovoljenje za vsak korak.
  Pregledni cilj ne dovoljuje implementacije. Posameznih testov ne ponavljaj
  brez spremenjenega vhoda ali potrebe novih vrat.
- Po zaključku sklopa zabeleži dejanske rezultate vrat ter `git status` in
  diff; po `/compact` obnovi kontekst iz checkpointa in primerjaj Git stanje.
  Loči spremembo *trenutnega* cilja od kumulativnega necommitanega kandidata.
  Celotna regresija sodi v zaključni sklop, ne avtomatsko v vsak nov sklop.
- Lastnik lahko med sklopoma določi poslovno odločitev; povzetek `/compact`
  mora ohraniti samo že sprejeto odločitev, ne je nadomestiti. Nepotrjene
  informacije ostanejo označene `NEPOTRJENO`.
- Ne nameščaj paketov, ne posegaj v okolje in ne izvajaj commit/push brez
  izrecne naloge. Konfiguracija mora uporabljati resnične zmožnosti lokalnega
  modela; privzeto `thinking/reasoning` je za te tri zaganjalnike `none`.

## Odprta nasprotja in še neizvedeni popravki

1. **Ministralov stari profil:** zahteva dodatno dovoljenje pred vsakim testom,
   celo `ast.parse`; projektni `AGENTS.md` za izrecno implementacijo dovoljuje
   varna potrebna preverjanja. Pred uporabo za nove sklope ga uskladi s
   potrjenim načinom Gemme/Mistrala. Tega v tem popisu nisem spreminjala.
2. **Projektni `AGENTS.md`:** izjema za samodejni commit navodil modela je v
   nasprotju z novo delitvijo (coder → Codex/lastnik → Git-agent). Enako je
   treba natančneje opredeliti preverjanje po `/compact`, da se ne ponavljajo
   nespremenjeni testi. Zapisana sta kot odprta predloga, ne kot že izveden
   popravek projektnega pravila.
3. **Mistralov research profil:** uporablja isti `CODEX_HOME/config.toml` kot
   coder, kjer `developer_instructions` Mistrala imenuje programer. To je
   možen konflikt vloge za raziskovalne seje; raziskovalnega profila v tem
   koraku nisem spreminjala.
4. **Mistralov kontekst:** profil nastavi 40.960 tokenov, Ollamina metapodatka
   osnovnega modela pa navajata izvorno dolžino 32.768. Dejanski vpliv na
   kakovost ali hitrost brez preizkusa ni potrjen.

## Viri za ponovni pregled

- Zaganjalniki: `/home/mikrofix/.local/bin/mikrofix-gemma12-coder`,
  `/home/mikrofix/.local/bin/mikrofix-mistral-coder`,
  `/home/mikrofix/.local/bin/mikrofix-ministral-coder`.
- Profili: `/home/mikrofix/.gemma12-codex/`, `/home/mikrofix/.mistral-codex/`,
  `/home/mikrofix/.ministral-codex/` (`config.toml`, `AGENTS.md`).
- Skupni projekt: `/home/mikrofix/MikroFIX_Service_v30_R9_LINUX_DEV/AGENTS.md`;
  proces: `docs/local_agents/PRAVILNIK_VERIGE.md` v istem repozitoriju.
- Osnovni model, parametri in zmožnosti so preverjeni z `ollama show` in
  `ollama show --modelfile`; tagi z `ollama list`. Enaki osnovni modelni blobi
  potrjujejo zgoraj navedene pare osnovni model ↔ lokalni tag.

Ob spremembi nastavitev: preveri stanje in obstoječe vrednosti, spremeni le
dogovorjeni profil, preberi nazaj efektivni zaganjalnik/konfiguracijo/Modelfile,
zapiši datum in dejansko stanje sem. Ne zaganjaj modela ali testov zgolj zaradi
posodobitve popisa brez lastnikovega ločenega dogovora.
