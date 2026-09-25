# Ločeno delovno območje lokalnih agentov

`MikroFIX_Agent_Workspace` je pripravljalno območje, ne kanonični original.
Ustvari se iz zadnjega commita zaščitenega projekta:

```bash
python3 tools/mikrofix_agent_workspace.py init
```

Ukaz namerno izloči vse necommitane spremembe, ignorirane datoteke, baze,
skrivnosti in virtualna okolja. Njihove poti zapiše v poročilo, vseeno pa jih ne
kopira. Projektni kontekst kopira v `knowledge/`, ga opremi s SHA-256 manifestom
in nastavi samo za branje. Lokalni agenti zapisujejo nova spoznanja samo v
`proposals/`.

## Meja zaupanja

- `repository/` je agentna osnovna kopija kode.
- `worktrees/<run-id>/` je izolirana izvedba posamezne naloge.
- `artifacts/<run-id>/` vsebuje kandidatni patch in manifest.
- Zaupanja vredni supervisor, bridge, izvajalec in reviewer se vedno zaženejo iz
  zaščitenega projekta zunaj agentnega območja.
- Zaključek privzeto ustvari dogodek za lastnika in ne zbudi Codexa.
- Codex se kliče ročno in lahko paketno pregleda več kandidatov.
- Lokalna oznaka READY nikoli ne pomeni merge-a, backupa ali produkcijske
  odobritve.

Celovitost območja se preveri z:

```bash
python3 tools/mikrofix_agent_workspace.py verify
```

Po Codexovi odločitvi `REVISE` ali `REJECT` se pripombe vrnejo v izvorni job.
Splošno uporabna ugotovitev se najprej zapiše v `proposals/`; šele Codexova
potrditev jo sme prenesti v kanonična pravila oziroma naslednji `knowledge`
checkpoint. Samo `APPROVE` se sme prenesti v zaščiten original in varnostne
kopije USB A, USB B ter šifriran oblačni arhiv.
