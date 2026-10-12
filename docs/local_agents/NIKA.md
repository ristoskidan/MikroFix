# Nika — lokalni coder profili in zaganjalniki MikroFIX v30

Stanje preverjeno: 12. 10. 2026. Skrbnica tega zapisa: **Nika** (Codex) —
zaganjalniki, Codex/Ollama nastavitve in uskladitev navodil. Moj operativni
zapis je `NIKA.md`; ločene Nikine `.codex/AGENTS.md` ni.
To je operativni popis, ne poslovni kanon in ne samodejno naložen prompt.
Ob vsaki naslednji dogovorjeni spremembi coder profilov posodobi ta zapis po
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

## Glavni profili in preverjene posebnosti

| Agent | Osnovni Ollama model | Aktivni lokalni tag | Zaganjalnik / profil | Kontekst / samodejna kompakcija | Parametri Ollama |
| --- | --- | --- | --- | --- | --- |
| Gemma 12B | `gemma4:12b` (arhitektura `gemma4`, 11,9B, Q4_K_M; enaki modelni blobi) | `mikrofix-gemma4-12b-focused-65k` | `gemma4-12b-coder`; `CODEX_HOME=/home/mikrofix/.gemma12-codex` | 65.536 / 55.000 tokenov | temperature 0,35; top_p 0,9; top_k 40; repeat_penalty 1,05; draft_num_predict 3 |
| Mistral 7B | `mistral:7b` (arhitektura `llama`, 7,2B, Q4_K_M; enak modelni blob) | `mikrofix/mistral:7b-coder-focused-65k` | `mistral-7b-coder`; `CODEX_HOME=/home/mikrofix/.mistral-codex` | 65.536 / 55.000 tokenov | temperature 0,2; top_p 0,9; top_k 40; repeat_penalty 1,05 |
| Ministral 3 8B | `ministral-3:8b` (arhitektura `mistral3`, 8,9B, Q4_K_M; enak modelni blob) | `mikrofix/ministral-3:8b-coder-focused-65k` | `ministral-3-8b-coder`; `CODEX_HOME=/home/mikrofix/.ministral-codex` | 65.536 / 55.000 tokenov | temperature 0,2; top_p 0,9; top_k 40; repeat_penalty 1,05 |

Vsi trije zaganjalniki uporabljajo lokalni Ollama Responses endpoint,
`model_reasoning_effort=none`, `model_verbosity=low`, `features.goals=true`,
`workspace-write`, brez interaktivne odobritve (`-a never`). Pred zagonom
preverijo berljivost projektnih `AGENTS.md` in `CODER.md` ter odsotnost
napačno postavljenega projektnega `.codex/AGENTS.md`. Profilnih
`CODEX_HOME/AGENTS.md` in njihovih oznak politike ne zahtevajo več.
Nastavijo `VISUAL`/`EDITOR` na `nano`, če nista določena.
Začetni poziv zahteva le kratko navedbo vloge in pripravljenosti; ne dodeli
izvedbenega sklopa. Nobenega modela med tem popisom nisem zagnala.

Gemma 12B in Mistral imata še dodatna, daljša `developer_instructions` v
svojem `config.toml`. Preostalih šest profilov tega polja nima. Vseh osem
modelov uporablja isti projektni `AGENTS.md` in kratki `CODER.md`; Codex
samodejno naloži prvega, drugega pa mora agent prebrati ob konkretnem cilju.
Sistemski prompt samega Codexa ni zapisan v teh lokalnih datotekah.
Mistralov profil je simbolna povezava na HDD; ta pot je bila pri popisu berljiva.
Mistralov nekdanji raziskovalni zaganjalnik je arhiviran; ni več ukaz v `PATH`.

## Terminalski zaganjalniki — samo coder

Odločitev lastnika, 12. 10. 2026: v aktivni terminalski mapi je natanko en
zaganjalnik za vsakega od osmih coder modelov. Ukaz nosi ime družine in
velikost modela ter pripono `-coder`.
Spodaj so dejanski cilji; nobenega modela zaradi preimenovanja nismo zagnali.

Poznejša odločitev lastnika istega dne: vseh osem aktivnih zaganjalnikov zdaj
kliče skupni `/home/mikrofix/goal_guard_lab/codex-bridge-launch.sh`. Ta za
vsako sejo ustvari ločena lokalna socketa in dnevnik v profilnem
`CODEX_HOME/runtime/bridge.*`, zažene Codex app-server z istim modelom in
konfiguracijskimi preglasitvami kot zaganjalnik, nato `bridge.js`, nazadnje pa
interaktivni Codex poveže prek `--remote`. Ob koncu odjemalca ustavi oba
podrejena procesa. Imena modelov, profili, začetni pozivi in nastavitve
vzorčenja so ostali nespremenjeni.

**Trenutno delovno stanje bridgea, 12. 10. 2026:** lastnik je po dejanski
uporabi Gemme 12B potrdil, da bridge stabilno deluje in ustavi potrjeno
ponavljajočo se zanko. Dnevnik
`/home/mikrofix/.gemma12-codex/runtime/bridge.sciYKX/guard.log` vsebuje
`repeat_reply_4`, `armed_reply` in `interrupt` naslednjega obrata; terminal
je po prekinitvi ostal uporaben. Panelna ikona `M` uporablja Piksijevo
potrjeno paleto: siva `#929CAA`, modra `#4EA3E3`, turkizna `#35C9C1`,
rumena `#F2B84B` in rdeča `#FF6B67`. Vseh osem zaganjalnikov je povezanih
prek bridgea, vendar delovanje preostalih sedmih modelnih sej še ni ločeno
potrjeno. Bridge ni poslovna odločitev in ne spreminja modelnih nastavitev.

| Ukaz | Aktivni Ollama tag | Temp. / top_p / top_k | Privzeto reasoning |
| --- | --- | --- | --- |
| `gemma4-12b-coder` | `mikrofix-gemma4-12b-focused-65k` | 0,35 / 0,9 / 40 | none (potrjen profil) |
| `gemma4-26b-coder` | `mikrofix/gemma4:26b-coder-focused-65k` | 1,0 / 0,95 / 64 | none; možen vklop thinking |
| `mistral-7b-coder` | `mikrofix/mistral:7b-coder-focused-65k` | 0,2 / 0,9 / 40 | none; brez thinking |
| `ministral-3-8b-coder` | `mikrofix/ministral-3:8b-coder-focused-65k` | 0,2 / 0,9 / 40 | none; brez thinking |
| `qwen2.5-14b-coder` | `mikrofix/qwen2.5:14b-coder-focused-65k` | 0,7 / 0,8 / 20 | none; brez thinking |
| `qwen3.5-9b-coder` | `mikrofix/qwen3.5-coder:9b-focused-65k` | 0,6 / 0,95 / 20 | low; on/off, stopnje niso potrjene |
| `gpt-oss-20b-coder` | `mikrofix/gpt-oss-coder:20b-focused-65k` | 1,0 / 1,0 / 0 | low; low/medium/high |
| `deepseek-r1-14b-coder` | `mikrofix/deepseek-r1:14b-focused-65k` | 0,6 / 0,95 / ni določen | low; on/off |

### Uskladitev konteksta, 12. 10. 2026

Na lastnikovo zahtevo sem pregledala vseh osem aktivnih zaganjalnikov, njihove
`coder-model-catalog.json` in Ollamine metapodatke (`ollama show`), brez zagona
modela ali testne seje. **Vseh osem** profilov je zdaj v vseh treh slojih
nastavljenih na **65.536** tokenov. Pet jih je to že imelo. Ministral 3 8B,
Qwen 2.5 14B in Mistral 7B so dobili nove 65K tage; njihovi zaganjalniki in
katalogi uporabljajo ustrezne nove tage, `model_context_window=65536` in prag
samodejne kompakcije 55.000. Modelfili so v profilnih mapah pod imenom
`Modelfile.coder-65k`. Stari 16K, 32K in 40K tagi niso odstranjeni.

Pri Mistralu je prvi poskus `ollama create` odpovedal zaradi `chtimes` nad
root-owned blobi na HDD. Z `pkexec` sem po lastnikovi ponovni potrditvi
lastništvo štirih natančno določenih blobov tega modela prenesla na
`ollama:ollama`; nato je `ollama create` za
`mikrofix/mistral:7b-coder-focused-65k` uspel. Mistralov zaganjalnik in katalog
zdaj kažeta na ustvarjeni tag. Mistralovi in Qwenovi modelni metapodatki še
vedno kažejo izvorno dolžino 32.768; nastavitev 65K je razširitev nad izvorno
mejo, ne potrditev kakovosti celotnega konteksta.

Med tem pregledom niso bili zagnani modeli, testi ali Codexove seje. Lastnik
poroča, da je razširjeni kontekst že preizkusil in da delno deluje; iz opažanja
ni razvidno, kateri model in kolikšen del 65K konteksta je bil preverjen.

Presoja vzorčenja, 12. 10. 2026 (efektivne vrednosti prebrane z
`ollama show --modelfile`, brez pogovora z modeli ali testov):

- Gemma 12B: obdržana lokalno potrjena kombinacija 0,35 / 0,9 / 40 s
  `repeat_penalty=1,05`; privzeti coder ostane primerjalna osnova.
- Gemma 26B: samostojen še nepreizkušen profil 1,0 / 0,95 / 64 po priporočilu
  avtorja Gemma 4; Gemme 12B to ne spreminja.
- Mistral 7B in Ministral 3 8B: obdržana konservativna kombinacija 0,2 / 0,9
  / 40 ter `repeat_penalty=1,05`; avtorjevi konfiguraciji ne določata posebnih
  vrednosti vzorčenja za programiranje.
- Qwen 2.5 14B: 0,7 / 0,8 / 20 ter `repeat_penalty=1,05` po avtorjevi
  `generation_config.json`. Tag še vedno uporablja splošne uteži `qwen2.5:14b`,
  ne ločenih uteži Qwen2.5-Coder. Kontekst je 12. 10. 2026 povečan z 8.192
  na 32.768 tokenov (izvorna meja po lokalnih Ollaminih metapodatkih), prag
  kompakcije pa s 6.500 na 26.000. Pozneje je na lastnikovo zahtevo nastal
  še 65K tag; to presega izvornih 32K in lahko vpliva na kakovost. Prejšnja
  8K in 32K taga ostajata za vrnitev; obnašanje novega 65K profila med
  dejansko sejo še ni potrjeno.
- Qwen 3.5 9B: 0,6 / 0,95 / 20, `min_p=0`, `presence_penalty=0` in
  `repeat_penalty=1,0` po avtorjevem profilu za natančne coding naloge.
  Lokalni tag je dodatno prilagojena različica, zato to ni dokaz enakega
  obnašanja kot pri uradnih utežeh.
- GPT-OSS 20B: ohranjena nevtralna izbira 1,0 / 1,0 / 0,
  `min_p=0`, `repeat_penalty=1,0`; dodatnega rezanja izbora tokenov ni.
- DeepSeek R1 14B: ohranjena avtorjeva kombinacija 0,6 / 0,95; `top_k`
  namenoma ni nastavljen, ker izvorno priporočilo zanj ne določa vrednosti.

Vzorčenje je hipoteza za nadaljnji lastnikov preizkus, ne jamstvo sledenja
navodilom. Fokusirani tagi dedujejo uteži in predloge iz izvornih modelov;
stari izvorni tagi so arhivirani za morebitno vrnitev. Pri Qwen 3.5 je lokalno potrjena
zmožnost thinking, ne pa samostojne stopnje medium/high, zato katalog ponuja
samo none/low. Efektivna skladnost `reasoning_effort` z Ollaminim thinking
pri delovanju ostaja za preverjanje ob lastnikovem naslednjem preizkusu.

Stari kratki vzdevki in dolgi `mikrofix-*-coder` zaganjalniki so odstranjeni
iz `/home/mikrofix/.local/bin/`. Prejšnji raziskovalni in testni zaganjalniki
ostajajo v obnovljivem arhivu
`/home/mikrofix/.local/share/mikrofix/retired-launchers/2026-10-11/`.
Neaktivne datoteke `Modelfile` so premaknjene v
`/home/mikrofix/.local/share/mikrofix/retired-profiles/2026-10-12/`.
Po dodatni odločitvi lastnika istega dne je 26 prejšnjih in osnovnih Ollaminih
tagov umaknjenih iz aktivnega kataloga: njihovi manifesti so v
`/usr/share/ollama/.ollama/archived-manifests/2026-10-12/`, ločeno po izvoru
`active` oziroma `parked`. Postopek in natančen seznam sta v
`/home/mikrofix/.local/share/mikrofix/retired-profiles/2026-10-12/archive-inactive-ollama-manifests.sh`.
Uteži oziroma blobov nismo premaknili ali izbrisali. `ollama list` je po
arhiviranju uspešno vrnil natanko osem zgoraj navedenih coder tagov; modelov
nismo zagnali. Če bo treba profil znova sestaviti iz starega osnovnega taga,
je treba najprej obnoviti njegov manifest na prvotno mesto. Poznejše čiščenje
blobov bi lahko zmanjšalo obnovljivost tega arhiva, zato ga ne izvajaj brez
ločene presoje. Lokalna zgodovina sej ostaja nedotaknjena.
Takratna ureditev profilnih `AGENTS.md` ni spreminjala; kasneje so bili
arhivirani v `/home/mikrofix/.local/share/mikrofix-coder-agents-archive/2026-10-12/`.
V `/home/mikrofix/Desktop/` ostaja samo splošna
bližnjica za aplikacijo Codex, ne zaganjalnik lokalnega modela.

## Izolirani poskus varovalke `/goal` za Gemmo 12B

Prejšnja odločitev lastnika: varovalka se je preizkušala samo z izoliranim
zaganjalnikom `gemma12-guard-trial`; varovalke
`/home/mikrofix/goal_guard_lab/bridge.js` in tedanjega običajnega zaganjalnika
v tem izoliranem preizkusu nismo spreminjali. Običajni zaganjalnik se zdaj
imenuje `/home/mikrofix/.local/bin/gemma4-12b-coder`. Lastnik preizkuse izvaja
sam in sporoči rezultat. Omejitev na izolirani zaganjalnik je nadomestila
zgornja izrecna odločitev o vključitvi bridgea v vseh osem coder zaganjalnikov.

Ob poenotenju zaganjalnikov 12. 10. 2026 je tudi izolirani zaganjalnik
umaknjen v `/home/mikrofix/.local/share/mikrofix/retired-launchers/2026-10-12/`.
Koda mostu in zgodovinski izpisi v `goal_guard_lab/` so ohranjeni; poskusni
zaganjalnik ni več aktiven terminalski ukaz.

Preverjeno stanje 11. 10. 2026: poskusni zaganjalnik zdaj uporablja Ollamin
lokalni tag `mikrofix-gemma4-12b-focused-65k` (ne surovega `gemma4:12b`) in
kopira Gemmin obstoječi modelni katalog v ločen `CODEX_HOME` poskusa. Isti tag
poda prednalaganju prek Ollaminega `/api/generate`, Codexovemu app-serverju in
odjemalcu; zahteva kontekst 65.536. Zaporedje v skripti je: prednalaganje in
preverjanje naloženega modela, zagon app-serverja, nato zagon varovalke in
nazadnje povezava odjemalca. Poskus uporablja projekt v načinu `read-only`.
To je opis konfiguracije in zaporedja skripte, ne potrditev uspešnega novega
zagona ali delovanja celotne verige.

Prejšnji dnevnik `goal_guard_lab/run.vDvK7c/guard.log` vsebuje dogodke
`goal_active`, `unmarked` in `goal_paused`, ne pa dokaza o štirih enakih
zaključkih `[WAITING_OWNER]`, oborožitvi in prekinitvi naslednjega samodejnega
obrata. Poznejši dnevnik `goal_guard_lab/run.2VQlzb/guard.log` tak niz za
`[WAITING_OWNER]` beleži; aktualni Gemmin dnevnik v profilnem `runtime/`
beleži tudi ustavitev ponavljajočega se navadnega odgovora. Lastnik je
delovanje bridgea potrdil v dejanski uporabi; ta stari dnevnik ni več ocena
trenutnega stanja.

## Ollamina shramba: odločitev in izvedba

Odločitev lastnika, 11. 10. 2026: privzeta Ollamina shramba ostane na SSD.
Modeli, katerih podatki so na HDD, se morajo v `ollama list` samodejno pojaviti
**samo, ko je HDD montiran**. Če HDD ni montiran, mora ukaz uspešno izpisati
dejanski seznam modelov na SSD, brez zapisov za nedostopne modele s HDD.
Devet obstoječih skupin manifestov za modele s HDD usklajuje sistemski
`ollama-hdd-manifests.timer` prek
`/usr/local/libexec/ollama-reconcile-hdd-manifests`. Preveri točen UUID
montirane particije. Ko HDD ni montiran, manifeste premakne iz aktivnega
kataloga v `/usr/share/ollama/.ollama/hdd-manifests-parked`; ob priklopu jih
vrne. Podatkovnih blobov in manifestov SSD-modelov ne premika. Uskladitev
teče približno vsaki dve sekundi, zato je neposredno po spremembi montaže
možen kratek zamik. Ob konfliktu aktivne in odložene kopije skripta ničesar
ne prepiše in napako zabeleži v sistemski dnevnik. Novi modeli, pozneje
premaknjeni na HDD, potrebujejo dopolnitev seznama v skripti.

Prejšnje manifestne povezave ostajajo kot varnostna kopija v
`/usr/share/ollama/.ollama/manifest-links-backup.UxnrNw`. Veliki blobi za
premaknjene modele ostajajo na HDD. Funkcionalno preverjanje odklopljenega
HDD je treba opraviti ločeno in s predhodnim lastnikovim dovoljenjem za test.

Pregled poti 12. 10. 2026: vseh osem aktivnih `*-coder` zaganjalnikov uporablja
tag, viden v `ollama list`. Servisni uporabnik `ollama` je ob montiranem HDD
lahko prebral manifest in vse v njem navedene blobe vsakega od osmih tagov.
Glavni blobi Gemme 12B, Gemme 26B, Ministrala 3 8B in Qwena 2.5 14B so
navadne datoteke v privzeti shrambi SSD. Glavni blobi DeepSeeka R1 14B,
GPT-OSS 20B, Mistrala 7B in Qwena 3.5 9B so simbolne povezave na HDD;
njihovi profilni `CODEX_HOME` so prav tako na HDD. To potrjuje poti in trenutno
berljivost, ne pa dejanskega nalaganja vseh modelov v GPU. Lastnik bo
odvisnost od HDD uredil pozneje; zaganjalnikov in shrambe ob tem pregledu
nismo spreminjali.

Odprta podrobnost: `/usr/local/libexec/ollama-reconcile-hdd-manifests` našteva
stara Mistralova taga `7b-32k` in `7b-40k`, ne pa aktivnega
`mikrofix/mistral:7b-coder-focused-65k`. Zato za novi tag še ni potrjeno,
da ob odklopljenem HDD izgine iz `ollama list`; glede na seznam v skripti
lahko ostane viden, čeprav njegov glavni blob ni dosegljiv. Odklopa nismo
preizkusili in popravka skripte nismo izvedli.

## Od kod pridejo navodila in kdo odloča

1. Codex ob novi seji naloži projektni
   `/home/mikrofix/MikroFIX_Service_v30_R9_LINUX_DEV/AGENTS.md`.
   `CODER.md` ni samodejno naložen: projektni dokument in začetni poziv
   naročata branje ob konkretnem cilju. Gemma 12B in Mistral imata še
   `developer_instructions` v profilu; ostalih šest teh dodatnih navodil nima.
   Sprememba teh vhodov velja šele za novo sejo.
2. Lastnik določi poslovna pravila, obseg posameznega `/goal` in odločitve med
   sklopi. `/goal` coderju poda *en* cilj z vrati; `/compact` ni odobritev novega
   cilja. Ob zagonu coder samo potrdi pripravljenost. Relevantni kanon se odpre
   ob cilju, ne celoten kanon preventivno ob začetku.
3. Poslovni vir je `docs/kanon/00_KAZALO.md` z aktivnimi razdelki; aktivno
   razdelitev vlog in izvedbeni tok določa `docs/local_agents/README.md`.
   `PRAVILNIK_VERIGE.md` je zgodovinski opis prejšnje nadzorovane verige. Koda
   je dokaz trenutne izvedbe, ne samostojen vir novih poslovnih pravil.
4. Neposredni `*-coder` zaganjalniki so interaktivni Codex profili. Izvedejo
   samo cilj, ki jim ga dodeli lastnik; ne pomenijo poslovne odobritve, pregleda
   ali dovoljenja za commit in push. Vloge so ločene od imen modelov.
5. Coder izdela kandidatno spremembo in preveri vrata cilja. Codex opravi
   končni pregled, lastnik sprejme ali zavrne. Za odobren commit/push je po
   zadnjem dogovoru zadolžen poseben Git-agent, ne coder.

## Dogovorjene smernice, pomembne za nastavitve

- Kratki začetni pozivi brez starih ukazov `$task`/`$goal`; izvedbeni obseg je
  uporabnikov aktualni `/goal`, ne zagonski prompt. Model mora dobiti jasno
  vlogo coderja in počakati na prvi cilj. Vseh osem aktivnih zaganjalnikov ima
  zdaj usklajen kratek uvodni poziv; Gemma 12B ohranja svojo krajšo različico.
- Skupni projektni `AGENTS.md` je zemljevid in projektni kontekst, kratki
  `CODER.md` določa skupno izvedbeno vlogo, konkretno delo in vrata pa `/goal`.
  Branje kanona naj bo ciljno. Gemma 12B in Mistral imata še dodatna
  `developer_instructions`; oceniti je treba, ali katera od njih po nepotrebnem
  podvaja skupna navodila.
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

1. **Dodatna navodila Gemme 12B in Mistrala:** vsebujeta posebna izvedbena
   pravila poleg skupnega `CODER.md`. Pred dodatnim krajšanjem je treba
   primerjati njihovo dejansko obnašanje; drugih šest profilov dodatnega
   `developer_instructions` nima.
2. **Mistralov nekdanji research profil:** uporablja isti `CODEX_HOME/config.toml`
   kot coder. Njegov zaganjalnik je arhiviran in ni več aktivni terminalski ukaz;
   profilne datoteke ostanejo nedotaknjene za morebitno obnovitev.
3. **Razširjeni kontekst Mistrala in Qwena 2.5:** oba profila zdaj nastavita
   65.536 tokenov, Ollamini metapodatki osnovnih modelov pa navajajo izvorno
   dolžino 32.768. Lastnikovo opažanje je, da razširjeni kontekst delno deluje;
   kakovost in hitrost pri celotnih 65K ostajata nepotrjeni.

## Viri za ponovni pregled

- Zaganjalniki: `/home/mikrofix/.local/bin/gemma4-12b-coder`,
  `/home/mikrofix/.local/bin/mistral-7b-coder`,
  `/home/mikrofix/.local/bin/ministral-3-8b-coder`; vseh osem imen je v tabeli
  zgoraj.
- Profili: `/home/mikrofix/.gemma12-codex/`, `/home/mikrofix/.mistral-codex/`,
  `/home/mikrofix/.ministral-codex/` (`config.toml`; brez aktivnih profilnih
  `AGENTS.md`).
- Skupni projekt: `/home/mikrofix/MikroFIX_Service_v30_R9_LINUX_DEV/AGENTS.md`
  in `CODER.md`;
  aktivni potek: `docs/local_agents/README.md` v istem repozitoriju.
- Osnovni model, parametri in zmožnosti so preverjeni z `ollama show` in
  `ollama show --modelfile`; tagi z `ollama list`. Enaki osnovni modelni blobi
  potrjujejo zgoraj navedene pare osnovni model ↔ lokalni tag.

Ob spremembi nastavitev: preveri stanje in obstoječe vrednosti, spremeni le
dogovorjeni profil, preberi nazaj efektivni zaganjalnik/konfiguracijo/Modelfile,
zapiši datum in dejansko stanje sem. Ne zaganjaj modela ali testov zgolj zaradi
posodobitve popisa brez lastnikovega ločenega dogovora.
