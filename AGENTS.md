# MikroFIX — globalne varnostne invariants lokalne izvedbe

Celoten tok in vloge določa `docs/local_agents/PRAVILNIK_VERIGE.md`. Ta datoteka
se dodaja bridge sejam in zato vsebuje samo kratke, splošno veljavne varovalke.

## Meja avtoritete

- Poslovne, pravne, davčne in varnostno pomembne odločitve sprejme lastnik.
- Model ne spreminja cilja, testnih ukazov, dovoljenj ali state machine.
- Vloga in model sta ločeni identiteti. Uporabi prompt in dovoljenja vloge ter
  model/tag/digest iz immutable snapshota.
- Brez izrecne zahteve lastnika ni plačljivih Codex podagentov ali zunanjih
  sporočil.

## Obvezno delo z dokazi

1. Preberi samo kanon, kodo, teste in diff, ki so potrebni za trenutni atom.
   Tujih sprememb ne prepisuj.
2. Preveri shemo orodja in uporabi veljaven strukturiran klic. Besedilo z imenom
   orodja ni dokaz njegove uporabe.
3. Ne izmišljaj poti, simbolov, testov, rezultatov, endpointov ali poslovnih
   pravil. Nepotrjeno trditev tako tudi označi.
4. Pred `READY` preglej dejanski `project_diff` in izvedi managerjev imenovani
   testni profil. Navedi dejanski ukaz, število testov in rezultat.
5. Če ni potrebna zunanja odločitev, prilagodi naslednji korak in nadaljuj.
   `BLOCKED` uporabi samo ob konkretnem manjkajočem podatku, odločitvi ali
   zmožnosti.
6. Lokalni pregledovalec zaključi z natanko eno samostojno vrstico
   `VERDICT: APPROVE` ali `VERDICT: REJECT`.

## Prepovedi

- Ne vračaj, briši, čisti ali prepisuj sprememb zunaj dovoljenega obsega.
- Ne uporabljaj `git checkout`, `restore`, `reset`, `clean`, `merge`, `push`
  ali deploya.
- Ne dostopaj do skrivnosti ali produkcijskih podatkov in ne obidi uporabnikove
  potrditve.
- Ne razglasi lokalnega rezultata za merge, produkcijo ali Codexovo odobritev.

## Natančen `BLOCKED`

```text
BLOCKED
Naloga: <ID in prikazno ime>
Opravljeno: <konkretno opravljeno delo>
Natančen vzrok: <točen blokator>
Dokaz: <pot ali preverljiv izpis>
Poskušeni koraki: <konkretni koraki brez ponavljanja iste napake>
Potrebujem: <najmanjša konkretna odločitev, podatek ali zmožnost>
Naslednji korak: <kaj se izvede takoj po razrešitvi>
```

## Zaključek

Končni odgovor navede dejanske spremenjene datoteke, diff, izvedene teste in
preostala tveganja. Lokalni rezultat ostane kandidat do Codexove odločitve.
