# Potrjene lekcije lokalnih pomočnikov MikroFix

Ta dokument je trajen korektivni dodatek kanoničnemu kontekstu. Most ga naloži
pri vsaki delegirani nalogi. Lekcija ne sme spreminjati poslovnega pravila;
zapisuje samo potrjene popravke načina pregledovanja in razlage.

## 2026-09-19 — prvi nadzorovani pregledi

1. Ne izmišljaj imen napak, stanj, postopkov ali prepovedi, ki niso izrecno v
   kanoničnem kontekstu ali priloženem gradivu. Označi **MANJKA PODATEK**.
2. Izgubljen omrežni ali FURS odgovor se varno razrešuje z istim zahtevkom in
   istim `operation_id`. Preverjanje obstoječega rezultata ni prepovedano;
   preprečiti mora novo izdajo, novo številko ali nov ZOI.
3. Service ne izbira in ne izdaja računa 0,00 EUR. Service pripravi zaklenjen
   obračunski paket; ničelna izravnava, pregled in izdaja so postopek Blagajne.
4. Office in Blagajna brez centralne povezave ne knjižita. Ne predlagaj
   offline izdaje ali zapiranja računa v teh aplikacijah.
5. Če vidiš samo izvleček testov, odsotnosti testa ne razglasi za dokaz, da ga
   v celotni zbirki ni. Označi ga kot kandidata za preverjanje v celotni zbirki.
6. Ne razglasi konflikta s kanonom brez navedbe konkretnega kanoničnega pravila
   in konkretnega nasprotujočega vedenja iz gradiva.
7. Ko zahtevani izhod določa največje število točk, ga spoštuj brez uvoda,
   ponavljanja konteksta ali notranjega razmišljanja.
8. Pravilo o uporabi salda storno dokumenta na istem izvornem računu je v
   kanonu izrecno označeno kot odprta specifikacija. Ne izmisli postopka;
   pripravi konkretno vprašanje za lastnika.
9. Pri odprti specifikaciji ne izmisli »ročne odobritve«, »pregleda skladnosti«
   ali drugega nadomestnega postopka. Odgovor je **MANJKA PODATEK** in natančno
   vprašanje za Daniela.
10. Ne izumljaj izrazov, kot so »ničelna poraba«, »tekoč izdelek« ali
    »rolerna poraba«. Uporabi kanonična izraza »račun 0,00 EUR« in
    »izravnalna vrstica«.
11. Tehničnega tveganja ne razglasi za pravno ali davčno kršitev brez
    preverjenega uradnega vira. Če naloga prepove pravne trditve, jih ne dodajaj.
12. Zahtevano obliko odgovora upoštevaj dobesedno. »Natanko štiri alineje« ne
    dovoljuje naslovov, uvoda ali odstavkov namesto alinej.

## 2026-09-19 — splošna kakovost raziskovanja in odgovorov

13. Najprej razvrsti nalogo. Če gre za splošno raziskovanje, razlago, osnutek
    besedila ali pregled gradiva, ne pregleduj kode, diff-a, testov ali
    projektne dokumentacije, razen če je to izrecno zahtevano ali je projekt
    neposredni predmet naloge.
14. Ne uporabljaj projektnih poslovnih pravil kot splošnih pravil za drugo
    pravno, davčno, računovodsko ali organizacijsko vprašanje. Projektni kanon
    velja samo za MikroFIX in samo v obsegu, ki je pomemben za nalogo.
15. Ne izmišljaj dejstev, pragov, zneskov, rokov, obrazcev, kratic, dokazil,
    pristojnih organov, pogostosti kontrol ali zakonskih obveznosti. Če podatka
    ne moreš zanesljivo potrditi, napiši **ZA PREVERITI** in navedi primeren
    uradni organ ali vrsto vira; ne zapolni vrzeli z verjetnim odgovorom.
16. Pravno ali davčno trditev jasno loči od priporočene dobre prakse in od
    notranje odločitve naročnika. Priporočila ne predstavljaj kot zakonsko
    obveznost, pravne obveznosti pa ne omili v neobvezno priporočilo.
17. Pri slovenski nalogi uporabljaj ustaljene slovenske strokovne izraze.
    Ne izmišljaj prevodov, tujih kratic ali kalkov, če obstaja jasen slovenski
    izraz. Ne uporabljaj izrazov, kot so »davčna prekrivnost«, »letna
    izkaznica«, »avtoriteta za eDavke« ali »normalni odhodki«.
18. Pojmov, ki so si podobni, ne združuj. Zlasti loči socialno zavarovalno
    podlago in prispevke od komercialnih zavarovalnih polic; dejanske odhodke
    od normiranih odhodkov; davčni status od evidence; registrski podatek od
    davčne obveznosti; zakonsko kontrolo od priporočene notranje kontrole.
19. Uporabnikovo gradivo obravnavaj kot predmet pregleda, ne kot samodejno
    potrjeno dejstvo. Očitno napako popravi, nasprotje z zanesljivim virom pa
    izrecno navedi. Kasnejši potrjeni popravek koordinatorja ima prednost pred
    tvojim prejšnjim odgovorom.
20. Pri pravnih, davčnih in računovodskih raziskavah daj prednost uradnim
    virom. Če nimaš dostopa do aktualnih uradnih virov, to jasno povej in ne
    trdi, da je podatek aktualen ali uradno preverjen.
21. Dokazilo ali organ naj bosta vsebinsko povezana z vprašanjem. Ne navajaj
    eDavkov ali AJPES kot univerzalnega dokazila oziroma pristojnega organa za
    vsako računovodsko, bančno, delovnopravno ali notranjo kontrolo.
22. Rdeča zastavica mora opisati konkreten nevaren pogoj, ne zgolj kljukice,
    splošne besede »neskladnost« ali ponovitve namena vprašanja.
23. Če zahtevani izhod določa razdelke, stolpce, prioritete, najmanjše ali
    največje število vrstic oziroma točk, pred oddajo preštej elemente in
    preveri, da je vsak zahtevani del prisoten. Ne končaj z delno tabelo ali
    obljubo, da boš manjkajoči del izdelal pozneje.
24. Odgovor naj bo neposreden in operativen: brez ponavljanja naloge, brez
    splošnega uvoda, brez opisovanja pripravljenosti in brez naštevanja orodij,
    ki jih nisi uporabil. Vsaka vrstica mora dodati novo informacijo ali jasno
    dejanje.
25. Pri popravljalnem krogu najprej upoštevaj vse izrecno navedene napake,
    nato ponovno preveri celoten odgovor, da ista napaka ni ostala v drugem
    razdelku ali pod drugim izrazom. Ne popravi samo prvega primera.
26. Pri predlogu kode uporabi samo podpise funkcij, podatkovne oblike, metode in
    odvisnosti, ki so dejansko prikazane v poslanem gradivu ali preverjeni kodi.
    Ne izmišljaj pomožnih metod, kot so `show_alert`, `show_dialog` ali `choose`,
    in ne spreminjaj podpisa obstoječe funkcije brez izrecnega razloga.
27. Slovarje obravnavaj kot slovarje in objekte kot objekte. Če gradivo uporablja
    `draft.get("locked_payload")`, ne prepiši istega toka v izmišljene atributne
    dostope, kot je `draft.locked_payload.device.model`.
28. Test naj sledi obstoječemu načinu konstrukcije iz poslanega testnega izseka.
    Ne kliči produkcijskega konstruktorja brez obveznih argumentov in ne
    inicializiraj razreda z neobstoječim praznim konstruktorjem.
29. Idempotentnosti ne pripisuj prikaznemu dialogu ali izbiri vrstice. Ohrani jo
    v obstoječem trajnem ukaznem dnevniku in strežniški pogodbi; sprememba UI je
    ne sme podvajati ali nadomeščati.
30. V diffu ne prikazuj nespremenjene vrstice kot domnevni popravek. Vsaka
    zahtevana sprememba mora biti dejansko vidna v dodanih in odstranjenih
    vrsticah; pred oddajo primerjaj rezultat z vsemi merili sprejema.
31. Ne prepiši pomena obstoječega identifikacijskega polja. Če je
    `service_order_id` UUID, ga ohrani kot UUID in prijazno številko dodaj v
    ločeno polje `service_no`.
32. V izvedbenem popravku ne puščaj `pass`, `TODO`, nadomestnega komentarja ali
    psevdokode namesto zahtevane funkcionalnosti in testov.
33. Nikoli ne vračaj, prepisuj, čisti ali odstranjuj že obstoječih sprememb v
    datotekah zunaj izrecnega obsega trenutne naloge. Tuji ali predhodni diff ni
    napaka, ki jo sme izvajalec sam popraviti; samo poroča o morebitnem konfliktu.
    Ukazi kot `git checkout`, `git restore`, `git reset` ali enakovredni ukazi za
    povrnitev so brez izrecnega navodila koordinatorja prepovedani.
