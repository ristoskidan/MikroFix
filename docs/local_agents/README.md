# MikroFIX — lokalna agentna veriga

Uradni proces je v [`PRAVILNIK_VERIGE.md`](PRAVILNIK_VERIGE.md). Vloge so
neodvisne od modelov; Open WebUI prikaže eno pogovorno vlogo, lastnik pa pred
prvim sporočilom izbere nameščeni model in njegove dovoljene runtime nastavitve.

## Aktivni deli

- `roles/` — kratki prompti posameznih vlog;
- `pravila/` — mehansko generirani, ciljno izbrani moduli iz `docs/kanon/`;
- `supervisor/` — strojne JSON pogodbe in operativni priročnik;
- `WORKSPACE.md` — meja izoliranega agentnega delovnega območja;
- `LEKCIJE.md` — interni eval-dnevnik za lastnika in Codex, ki se modelom ne
  pošilja;
- `dnevnik/` — zgodovinski izpisi prejšnjega lokalnega mosta.

Module po spremembi kanona osveži:

```bash
python3 tools/build_local_agent_rules.py
```

Generator shrani SHA-256 vsakega kanoničnega vira v `pravila/INDEX.json`.
Reviewer zavrne delo, če moduli in kanon niso usklajeni.

Stari model-specific pravilniki, prompti in registri pred konsolidacijo so
ohranjeni z izvirnimi potmi in hashi v
`docs/archive/rules-before-role-chain-20260925/`.
