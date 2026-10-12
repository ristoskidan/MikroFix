# MikroFIX — kanonska meja lokalnih agentov

Status: **izpeljana procesna meja, ne vir poslovnih pravil**

Aktivna razdelitev strokovnih vlog Codex je v ločenem repozitoriju
`/home/mikrofix/Codex_Agenti/README.md`, tam so tudi njihove operativne
beležke. Lokalni coderji in njihova projektna orodja ostajajo v
`docs/local_agents/` in `CODER.md`. Operativni zapisi strokovnih vlog ne
morejo sami ustvariti ali spremeniti poslovnega pravila.

- Lastnik določa poslovna pravila, obseg dela in sprejem sprememb.
- Sidro usklajuje kanon, Git stanje in aktivne naloge.
- Nika vzdržuje profile lokalnih modelov, zaganjalnike in njihova navodila.
- Piksi vzdržuje sistem vizualnih identitet in oblikovalska pravila. Potrjeno
  identitetno pravilo se najprej zapiše v poslovni kanon; njegov operativni zapis
  nato vodi izvedbene podrobnosti in odprte oblikovalske odločitve.
- Svit vzdržuje javno spletno prisotnost `MikroFix`, gostovanje in nadzorovani
  postopek objave. Produkcijske objave, spremembe domen, varnostni posegi in
  vključevanje zunanjih storitev zahtevajo izrecno lastnikovo odobritev.
- Prizma neodvisno preverja pravno-tehnično skladnost, KERN pa lokalne jezikovne
  modele za coding naloge; njuni operativni beležki sta v agentskem repozitoriju.
- Coder izvede samo dodeljeni sklop. Strokovni skrbnik pregleda vpliv na svoje
  področje, Sidro pa pred commitom preveri skladnost kanona, naloge in diffa.
- Commit in push opravi Git-agent po lastnikovi odobritvi; coder tega ne opravi
  sam.
- Vloge ne spreminjajo profilnih ali operativnih datotek drugega skrbnika brez
  njegovega dogovora. Potrebno spremembo mu predajo z razlogom in predlogom.
- Noben agent ne sme poslovnega pravila izpeljati samo iz kode, starega pogovora
  ali zgodovinskega pravilnika.
- Model dobi relevantne module veljavnega kanona, potrebne za svojo vlogo in
  trenutno nalogo. Celotnega kanona ne prebira brez stvarne potrebe.
- Pred testi in daljšimi opravili s čakanjem agent lastniku napove namen ter
  okvir čakanja in po koncu sporoči dejanski izid. Orodja Codex in navodila
  strokovnih agentov se ne nalagajo lokalnim coderjem.

`docs/local_agents/PRAVILNIK_VERIGE.md` in pripadajoči artefakti prejšnje
nadzorovane verige so zgodovinsko oziroma tehnično gradivo, ne vzporedni aktivni
postopek.
