# MikroFIX — odgovornosti in lokalna agentska veriga

## Odgovornosti

- **Lastnik** določa poslovna pravila, obseg dela in odloča o sprejemu sprememb.
- **Sidro** skrbi za kanon, stanje Gita in aktivne naloge; njegov delovni zapis
  je [`SIDRO.md`](SIDRO.md).
- **Nika** skrbi za profile lokalnih modelov, zaganjalnike in njihova navodila;
  njen delovni zapis je [`NIKA.md`](NIKA.md).
- **Piksi** skrbi za sistem vizualnih identitet, njihovo razmejitev in
  oblikovalska pravila; njegov delovni zapis je [`PIKSI.md`](PIKSI.md).

Vsak skrbnik spreminja svoje operativne zapise in vire, ki mu jih lastnik
izrecno dodeli. Potrebno spremembo na področju drugega skrbnika mu preda z
razlogom in predlogom, ne poseže pa sam v njegove profilne datoteke.

## Aktivni potek

Lastnik določi cilj in poslovne odločitve. Sidro uskladi cilj s kanonom in Git
stanjem, Nika oziroma Piksi pa se vključita, kadar cilj posega v njuno strokovno
področje. Coder izvede en smiselno zaključen sklop, rezultat se pregleda,
odobren commit in push pa opravi Git-agent. Aktivna naloga se izmenjuje v
`/home/mikrofix/Desktop/aktivna naloga.md`; ukaz za aktivacijo cilja ni del njene
vsebine.

## Tehnični viri

[`PRAVILNIK_VERIGE.md`](PRAVILNIK_VERIGE.md) ter mape `roles/`, `supervisor/` in
`dnevnik/` opisujejo prejšnjo nadzorovano verigo in ostajajo zgodovinsko oziroma
tehnično gradivo. Mapa `pravila/` vsebuje generirane izvlečke aktivnega kanona.
Datoteki `WORKSPACE.md` in `LEKCIJE.md` nista dodatni odločevalski odgovornosti.
