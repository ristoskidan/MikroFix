# MikroFIX R9 – varovalke blagajne P2

Datum izvedbe: 3. september 2026

## Namen

Ta sklop dopolnjuje že potrjena globalna pravila nespremenljivih dokumentov,
storna, nadomestnega računa in zaščite odprtega dela pred prekinitvijo programa
ali izpadom elektrike. FURS produkcija ostaja izključena.

## Izvedeno

- Storno razdeljenega plačila ustvari ločeno povratno plačilno vrstico za vsak
  prvotni način plačila. Gotovina in bančno nakazilo zato ne izgubita ločene
  revizijske sledi.
- Novi račun je mogoče povezati samo z izdanim računom, ki je bil v celoti
  storniran in ima knjižen storno dokument.
- En stornirani račun je lahko neposredni predhodnik samo enega nadomestnega
  računa. Če je tudi nadomestni račun pozneje storniran, se naslednji račun
  poveže z njim in tako nadaljuje verigo.
- Povezava je zapisana v bazi, revizijskem dnevniku blagajne, A4 računu in
  termičnem računu. Vidna je tudi v predogledu pred izdajo.
- V blagajni je dodano dejanje `Veži na storno` ter bližnjica `Shift+F5`.
- Neizdani račun se trajno samodejno shranjuje po spremembah postavk, kupca,
  plačila, poslovnega prostora, servisa in povezave na storno.
- Osnutek ima kontrolni povzetek SHA-256. Poškodovan ali ročno spremenjen zapis
  se ne obnovi tiho.
- Ob naslednjem odprtju blagajne program ponudi nadaljevanje zadnjega osnutka.
  Zavrnjen osnutek ostane v sledi kot opuščen in se ne izbriše.
- Izdaja računa in označitev pripadajočega osnutka kot izdanega se izvedeta v
  isti SQLite transakciji.

## Preverjanje

- sintaktični pregled vseh Python datotek je uspešen;
- celoten samodejni nabor: 111 uspešnih testov;
- dodani so regresijski testi za razdeljeni storno, verigo nadomestnega računa,
  preverjanje pogojev povezave, trajno shranjevanje, poškodovan osnutek ter
  atomski prehod osnutka v izdani račun.

## Še ni produkcijsko omogočeno

- pošiljanje računov na FURS;
- produkcijsko davčno potrjevanje in uskladitev neznanega stanja;
- strežniška sinhronizacija; lokalni osnutek je pripravljena začetna varovalka,
  ne nadomestilo za prihodnjo oddaljeno bazo in varnostno kopijo.
