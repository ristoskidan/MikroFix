<!-- GENERIRANO iz docs/MIKROFIX_GPT_OSS_20B_KONTEKST.md; ne urejaj ročno. -->

## 4. Ciljna razdelitev aplikacij

Sistem sestavljajo tri samostojne aplikacije s skupnim poslovnim jedrom in centralnim API-jem.

### MikroFix Office

Odgovoren je za:

- partnerje, stranke, dobavitelje in dokumente;
- vhod blaga: prevzem, pregled, vrednotenje, določitev cene in sprostitev v zalogo;
- nabavo, cenike, poročila, administracijo, uporabnike in dovoljenja;
- inventuro;
- ustvarjanje reklamacijskega primera po pravilnem sprejemu v Blagajni.

Office je glavni za vhod zaloge. Ne izdaja računov.

### MikroFix Service

Odgovoren je za:

- servisne naloge, dejanski čas, delo, ugotovitve, teste in neobvezne fotografije;
- uporabljene dele in tehnični zaključek;
- pripravo nespremenljivega obračunskega paketa za Blagajno;
- odkup naprav, obnovo naprav in razgradnjo donorskih naprav;
- pridobitev uporabnih delov iz dokumentiranega donorskega postopka.

Service ne izdaja računov in ob tehničnem zaključku ne odpira Blagajne.

### MikroFix Blagajna

Odgovorna je za:

- neposredno prodajo artiklov;
- prevzem pripravljenega servisnega obračunskega paketa;
- način plačila, izdajo in davčno potrditev računa;
- računovodsko zaprtje servisa;
- sprejem storna in sprejem reklamacije;
- povezavo računa s servisom, reklamacijo in morebitnim garancijskim listom.

Samo Blagajna izda račun. Reklamacijo sprejme Blagajna, reklamacijski primer pa ustvari Office.

Več aplikacij sme teči na istem računalniku. V prvi fazi sedanji pisarniški računalnik nosi vse funkcije. Prihodnji terenski računalnik bo nosil Service in Blagajno. Pozneje je predviden ločen strežnik.

---

---

## 5. Osnovna arhitektura in zaupanje

- PostgreSQL je centralni avtoritativni podatkovni vir.
- Centralni API je edini dovoljeni pisec skupnih poslovnih podatkov.
- Namizne aplikacije ne smejo neposredno spreminjati PostgreSQL.
- Service ima lokalni SQLite in trajni outbox za delo brez povezave.
- Office in Blagajna sta v prvi fazi samo spletno povezana; brez centralne povezave ne knjižita.
- Trajno dvojno avtoritativno zapisovanje je prepovedano.
- Strežnik določa centralne časovne žige, zaporedja dokumentov in številke računov.

Vsak občutljiv ukaz vsebuje najmanj:

- `operation_id` oziroma idempotency ključ;
- identiteto uporabnika;
- identiteto delovne postaje in namestitve;
- tip odjemalca;
- poslovni prostor, kadar je pomemben;
- lokalno zaporedje dogodka pri delu brez povezave;
- različico pravil oziroma podatkov, na katerih je odločitev temeljila.

### Idempotentnost

- Enak `operation_id` in enak zahtevek morata vrniti popoln prvotni rezultat.
- Enak `operation_id` z drugačnim zahtevkom se zavrne kot konflikt.
- Izgubljen odgovor ali ponovljen klik ne smeta ustvariti novega računa, nove številke, nove porabe zaloge ali novega servisa.
- Dva različna `operation_id`, ki tekmujeta za isti osnutek, lahko ustvarita največ en račun.
- En kupec sme imeti poljubno število različnih računov.
- Vsaka izdana številka računa je enolična.

Nespremenljiv osnutek ne pomeni verige blokov ali kriptovalute. Pomeni, da se potrjeni izvorni paket ne prepisuje; popravki se dodajajo kot novi sledljivi dogodki ali vrstice.

---

---

## 6. Prijava, seja in dovoljenja

- Ob vsakem zagonu vsake aplikacije je potrebna nova prijava.
- Seja se med zagoni ne ohranja.
- Izhod iz aplikacije pomeni samodejno odjavo in izbris žetona ter dovoljenj iz pomnilnika.
- Skupni računi so prepovedani; vsak uporabnik ima svojo identiteto in PIN.
- Po petih napačnih PIN-ih sledi petminutna blokada; neuspešna obnovitev ima daljšo blokado.
- Po desetih minutah nedejavnosti se aplikacija zaklene, vendar delo ostane shranjeno.
- Ročna odjava ali nadzorovan izhod iz Service sta blokirana, če ima uporabnik aktiven, nezaustavljen časovni interval. Uporabnik dobi jasno opozorilo.

### Offline prijava v Service

- Offline posnetek uporabnika velja največ sedem dni od zadnje uspešne centralne prijave.
- Vsaka offline prijava jasno opozori, da gre za offline način, in pokaže čas do poteka.
- Po poteku prijava ni dovoljena.
- Premik sistemske ure nazaj ne sme podaljšati veljavnosti.

---
