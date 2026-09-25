<!-- GENERIRANO iz docs/MIKROFIX_GPT_OSS_20B_KONTEKST.md; ne urejaj ročno. -->

## 7. Servisni čas

- Aktivni čas je trajno stanje v bazi, ne stanje zavihka, okna ali globalne spremenljivke uporabniškega vmesnika.
- Menjava zavihka, okna ali servisnega naloga časa ne ustavi.
- En serviser sme imeti več servisov, katerih čas teče istočasno.
- Pred menjavo naloga se trenutni osnutek shrani.
- Čas ustavi izrecno uporabnik ali tehnični zaključek izbranega servisa.
- Aktivni interval mora preživeti ponovni zagon aplikacije.
- V istem zagonu se trajanje meri z monotono uro. Po ponovnem zagonu se uporabi varno nadaljevanje iz shranjenih podatkov; negativno ali nelogično trajanje blokira zaključek in zahteva pregled.

---

---

## 8. Identifikacija naprav

### Naprave, ki jih podpira MicroDetect

- Servis se sme odpreti še pred uspešnim branjem naprave.
- Dobi notranji UUID in fizično oznako.
- Ročni vnos modela, IMEI ali serijske številke se ne sme prikazati kot preverjen podatek.
- Neposredno branje iz naprave je potrebno pred običajnim tehničnim zaključkom, lahko pred ali po posegu.
- Napravo po potrebi odklene stranka. PIN-i in gesla se ne zapisujejo ali hranijo.

### Naprave, ki jih MicroDetect ne podpira

Obstoječi poenostavljeni fizični sprejem je dovolj. Zabeleži se:

- status **FIZIČNO PREVERJENA IDENTITETA**;
- kategorija, proizvajalec in model;
- stabilna serijska oznaka, če obstaja;
- uporabnik, datum in čas preverbe;
- sicer samo notranji UUID in fizična nalepka.

Prosta trditev stranke sama po sebi ni preverjena identiteta naprave.

---

---

## 9. Tehnični zaključek servisa

Gumb **ZAKLJUČI SERVIS IN PRIPRAVI RAČUN** mora lokalno in atomsko:

1. ustaviti tekoči interval izbranega servisa;
2. shraniti delo, ugotovitve, teste in neobvezne fotografije;
3. shraniti uporabljene dele;
4. tehnično zaključiti servis;
5. ustvariti nespremenljiv lokalni obračunski paket;
6. dogodek zapisati v trajni outbox za centralno sinhronizacijo.

Ne sme odpreti ali ponuditi Blagajne. Prikaže samo enega od jasnih rezultatov: paket je pripravljen, čaka na sinhronizacijo ali obstaja konflikt sinhronizacije.

Na strežniku ena transakcija preveri identiteto naloga, zalogo, idempotentnost in dovoljenja ter nato ustvari zalogovne dogodke in zaklenjen centralni servisni osnutek.

Servis je tehnično zaprt ob tem koraku, obračunsko pa šele po uspešno izdanem računu. **Ni servisa brez računa.** Tudi račun z vrednostjo 0,00 EUR se mora izdati.

---
