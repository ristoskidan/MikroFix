<!-- GENERIRANO iz docs/MIKROFIX_GPT_OSS_20B_KONTEKST.md; ne urejaj ročno. -->

## 17. Cenovne in časovne formule

Trenutne potrjene servisne tarife:

- `L0 = 22,00 EUR` je trdo dno za delo, kjer se pravilo uporablja;
- `T1`: prvih začetih 60 minut 30,00 EUR, vsaka nadaljnja začeta 30-minutna enota 15,00 EUR;
- `T2`: prvih začetih 60 minut 90,00 EUR, vsaka nadaljnja začeta 30-minutna enota 15,00 EUR;
- na terenu do vključno ene ure se obračuna cel T2; nad eno uro se dodajajo 30-minutne enote po logiki T1.

Diagnostika 30,00 EUR z morebitnim vštevanjem v izvedeno popravilo je zgodovinsko dogovorjena, vendar naj se pred novo implementacijo preveri proti trenutno objavljenemu ceniku.

### Zgodovinska formula odkupa

Ta formula je pomemben zgodovinski kontekst, ni pa dovoljenje za tiho samodejno uvedbo brez preverjanja aktualne politike:

- `R`: robustna sredina treh do petih primerljivih ponudb obnovljenih naprav v EU;
- `C`: trdi stroški baterije, najmanj ene ure T1, tesnila/materiala, splošnega materiala in paketa;
- `M`: 20 % od `R`, najmanj 40 EUR in največ 150 EUR;
- `MAX = R - C - M`, zaokroženo navzdol;
- `HAGGLE`: približno 10 % od `MAX`, najmanj 10 EUR in največ 50 EUR, zaokroženo na 10 EUR;
- priporočeni odkup = `MAX - HAGGLE`.

Če naloga posega v cenik ali odkupno formulo, najprej preveri, ali je ta zgodovinska formula še aktualna.

---
