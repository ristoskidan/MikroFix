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
`docs/MIKROFIX_GPT_OSS_20B_KONTEKST.md`.

## Uporaba

Iz korena projekta:

```bash
python3 tools/mikrofix_local_agent.py \
  --agent qwen \
  --task "Preveri predlagano spremembo." \
  --material-file /pot/do/gradiva.txt \
  --output "Naštej konflikte in pet manjkajočih testov."
```

Za skeptični drugi pregled se uporabi `--agent reviewer`. Odgovor se med
nastajanjem izpisuje v terminalu, celoten poziv in nespremenjen odgovor pa se
atomsko shranita v `docs/local_agents/dnevnik/LOCAL-....md`.

Most ne daje modeloma dostopa do aplikacij ali baze in njun odgovor ni
samodejno odobren. V vsakem dnevniku ostane razdelek za pregled glavnega
pomočnika.

Za preizkus zapisa brez zagona modela dodaj `--dry-run`.

Most privzeto uporablja isti Ollama strežnik kot Open WebUI na
`172.17.0.1:11434`. Drugačen naslov se lahko izrecno poda z `--ollama-url`.
