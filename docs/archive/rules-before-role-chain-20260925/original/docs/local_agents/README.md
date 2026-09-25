# Lokalni pomočniki MikroFix

Ta mapa vsebuje sledljive naloge, ki jih glavni pomočnik pošlje lokalnima
modeloma prek Ollama. Lokalna modela sta svetovalca in ne smeta samostojno
spreminjati kode, poslovnih pravil ali podatkov.

## Vlogi

- `qwen` (`qwen3.5:9b`): tehnični pregled kode, testov in manjših izvedbenih
  vprašanj;
- `reviewer` (`gpt-oss:20b`): skeptični drugi pregled arhitekture, konfliktov,
  sočasnosti, idempotentnosti in revizijske sledi.

Oba vedno prejmeta kanonični kontekst iz
`docs/local_agents/pravila/` in potrjene korektivne lekcije iz
`docs/local_agents/LEKCIJE.md`. Pravila so mehansko izdelani tematski moduli iz
kanoničnega dokumenta `docs/MIKROFIX_GPT_OSS_20B_KONTEKST.md`.

Most vedno naloži `00_JEDRO.md`, trajne lekcije in module, ki ustrezajo nalogi.
Obseg je mogoče določiti tudi izrecno, na primer `--scope invoices --scope
stock_offline`. Če naloge ni mogoče varno razvrstiti, most naloži vse module.
Model zato nikoli ne ostane brez pravil samo zaradi neuspešnega iskanja.

Vsak dnevnik vsebuje seznam in SHA-256 vseh naloženih modulov. Če se kanonični
dokument spremeni, most zavrne delo, dokler se moduli ne osvežijo z:

```bash
python3 tools/build_local_agent_rules.py
```

## Uporaba

Iz korena projekta:

```bash
python3 tools/mikrofix_local_agent.py \
  --agent qwen \
  --scope invoices \
  --task "Preveri predlagano spremembo." \
  --material-file /pot/do/gradiva.txt \
  --output "Naštej konflikte in pet manjkajočih testov."
```

Za skeptični drugi pregled se uporabi `--agent reviewer`. Odgovor se med
nastajanjem izpisuje v terminalu in v skupnem toku
`~/.local/state/mikrofix/agent-console.log`, celoten poziv in nespremenjen
odgovor pa se atomsko shranita v
`docs/local_agents/dnevnik/LOCAL-....md`.

Na namizju je bližnjica **MikroFix – lokalni agenti**. Odpre eno stalno
terminalsko okno, ki spremlja skupni tok. Most vanj za vsako nalogo zapiše
agenta, model, uporabljene module pravil, zahtevo, zahtevani izhod, sprotni
odgovor in pot do trajnega dnevnika. Bližnjice ni treba ponovno odpirati za
vsako nalogo. Če je spremljevalnik že odprt, drugi primerek ne ostane odprt.

Ker lokalna modela uporabljata isti grafični procesor, skupni tok hkrati služi
kot čakalna vrsta: naslednji klic počaka, dokler se prejšnji odgovor ne konča.
Tako se odgovori ne prepletajo in ena naloga ne jemlje pomnilnika drugi.

Most ne daje modeloma dostopa do aplikacij ali baze in njun odgovor ni
samodejno odobren. V vsakem dnevniku ostane razdelek za pregled glavnega
pomočnika.

Za preizkus zapisa brez zagona modela dodaj `--dry-run`.

Most privzeto uporablja isti Ollama strežnik kot Open WebUI na
`172.17.0.1:11434`. Drugačen naslov se lahko izrecno poda z `--ollama-url`.
Delegirani klic privzeto uporablja kontekstno okno 32768 in zahteva neposreden
končni odgovor brez ločenega prikaza notranjega razmišljanja.
Odgovor je privzeto omejen na 1600 tokenov; drugačna meja se poda z
`--max-tokens`, ne da bi se zaradi tega zmanjšal vhodni kontekst.
