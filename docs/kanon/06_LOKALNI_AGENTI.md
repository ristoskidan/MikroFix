# MikroFIX — kanonska meja lokalnih agentov

Status: **izpeljana procesna meja, ne vir poslovnih pravil**

Aktivni proces je določen v `docs/local_agents/PRAVILNIK_VERIGE.md`. Ta dokument
zapisuje samo njegovo povezavo s poslovnim kanonom.

- Vloge `conversation_lead`, `plan_preparer`, `plan_validator`, `executor` in `local_reviewer`
  niso vezane na ime modela.
- Pogovorni raziskovalec sme projekt samo brati. `plan_preparer` je ločena seja
  brez njegovega skritega spomina. Izvedba se začne šele po prikazanem načrtu,
  `/plan-dispatch`, validaciji in lastnikovem `/approve-plan`.
- Izvajalec piše samo v dovoljene datoteke izoliranega worktreeja.
- Gatekeeper in reviewer preverita dejanski diff, teste ter sprejemna merila.
- Lokalni kandidat ni odobritev. Codex ostane končni tehnični pregledovalec in
  edini člen, ki lahko prenese preverjeni kandidat v zaščiteni original.
- Noben model ne sme spreminjati poslovnih pravil, produkcijskih podatkov,
  dovoljenj, testnih ukazov ali state machine.
- Model dobi samo tematske module veljavnega kanona, ki so potrebni za njegovo
  vlogo in trenutno nalogo. Zgodovinski master in eval-dnevnik se ne pošiljata.
