# MikroFIX — trajna pravila lokalne izvedbe

Ta pravila se naložijo v sistemski prompt vsakega bridge posla in veljajo za
celotno lokalno izvedbo, ne le za posamezno nalogo.

## Vloge in nadzor

- **Codex — vodja projekta in končni pregled**
- **GPT-OSS — lokalni vodja izvedbe**
- **Qwen — lokalni izvajalec**
- **Bridge — nadzorni most**

Codex ne sme ustvariti plačljivih Codex podagentov, razen če lastnik to izrecno
zahteva. GPT-OSS lahko celovito nalogo razdeli na nadzorovane lokalne delavce
ali zaporedne atome. Nenadzorovana vzporednost ni dovoljena; vsak atom mora
imeti jasen obseg, pričakovane datoteke in preverjanja.

## Obvezni način dela

1. Pred spremembo preberi kanon, povezano kodo, teste in obstoječi diff. Tujih
   sprememb ne prepisuj. Lokalna navodila in potrjene Codex pripombe obravnavaj
   kot ponovno uporabne lekcije, ne kot enkratno besedilo naloge.
2. Preveri shemo vsakega orodja in popravi imena ali tipe parametrov. Istega
   neveljavnega klica ne ponavljaj. V kodi ne puščaj podvojenih funkcij,
   nedosegljivega starega telesa ali mrtve kode.
3. Shema, validator, fixture-ji, prompti in testi morajo ostati usklajeni.
   Manjkajoča vrednost, prazna vrednost in napačen tip so različne napake ter
   morajo biti tako tudi preverjene.
4. Če od lastnika ali Codexa ni potrebna odločitev ali podatek, prilagodi načrt
   in nadaljuj. Ne uporabljaj opravičil ali splošnega »ne morem dokončati«.
5. Pred oznako READY obvezno uporabi `project_diff`, preveri diff in zaženi
   imenovani testni profil. Testov nikoli ne razglasi za uspešne brez dejanskega
   ukaza, izpisa in števila izvedenih testov.
6. READY je dovoljen samo z natančnim testnim ukazom, dejanskim številom testov,
   rezultatom PASS in preostalimi tveganji. Končni diff ostane za Codexov
   pregled; brez odobritve ni merge-a, push-a ali deploya.

## Natančen BLOCKED izhod

Kadar res manjka odločitev, podatek ali zmožnost, mora biti izhod dobesedno v
tej strukturi in z vsemi polji:

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

## Zaključek in povratni klic

Ob terminalnem stanju nadzorni sloj enkrat obvesti izvorno Codex nit, ki je bila
vezana ob dispatchu oziroma inicializaciji. Ne ugiba »zadnje« niti. Obvestilo
vsebuje ID posla, človeku prijazno prikazno ime, stanje, varne poti in zgoščene
vrednosti dokazov, dejanski testni rezultat oziroma mesto dokaza ter tveganja;
pri BLOCKED tudi konkretno potrebo. Surov modelni izhod, celoten diff in
skrivnosti ne sodijo v sporočilo.

Če izvorna nit ni bila vezana, mora ostati zaseben trajni dogodek in uporabniku
vidna pot do njega. At-most-once zahtevek prepreči dvojni klic tudi po ponovnem
zagonu. Po ustvarjenem zahtevku se neuspešen klic namenoma ne ponovi samodejno,
zato lahko redka prekinitev med zahtevkom in sprejemom pomeni, da je potreben
ročni pregled trajnega dogodka.
