<!-- GENERIRANO iz docs/MIKROFIX_GPT_OSS_20B_KONTEKST.md; ne urejaj ročno. -->

## 12. Servisni osnutek in blagajniške vrstice

Centralni servisni osnutek je zaklenjen izvorni paket. Blagajna ne sme prepisati ali izbrisati njegovih izvornih vrstic. Popravki se izvajajo samo z dodatnimi kronološkimi vrsticami, ki ohranijo celotno sled.

Neposredna prodaja ni dodatek servisnemu osnutku, ampak ločen račun. Tako se izvorni servisni paket ne meša s pozneje izbranimi prodajnimi artikli.

### Dodajanje artiklov v Blagajni

- Vsako skeniranje črtne kode ustvari novo vrstico, tudi če je isti artikel že na računu.
- Vsako dodajanje iz iskalnika prav tako ustvari novo vrstico.
- Količina `x2`, `x3` in podobno nastane samo, če uporabnik izrecno klikne količino vrstice in jo spremeni.
- Količina 0 je prepovedana.
- Dodana vrstica se nikoli ne izbriše.
- Napačna ali zmanjšana količina se popravi z novo negativno oziroma izravnalno vrstico v trenutku popravka.
- Izvorna vrstica ostane nespremenjena.
- Pri poznejšem ponovnem popravku se upošteva že obstoječa izravnava in doda samo potrebna razlika.
- Vrstice na izdanem dokumentu ostanejo v dejanskem kronološkem vrstnem redu dogodkov.

Primer napačnega artikla:

```text
Napačen zaslon    x 1     +XX EUR
Napačen zaslon    x-1     -XX EUR
Pravi zaslon      x 1     +XX EUR
```

Negativna vrstica je revizijska izravnava in sama ne vrne artikla na zalogo. Zalogovni dogodek mora imeti lastno izrecno poslovno podlago.

### Omejitve zneska

- Končni znesek računa ne sme biti negativen.
- Račun z 0,00 EUR je dovoljen in se mora izdati.
- Vsak račun se lahko pred izdajo izravna na nič z ustreznimi negativnimi vrsticami.
- Sistem sme predlagati ali sam ustvariti potrebne izravnalne vrstice, vendar jih mora blagajnik pregledati.
- Izdaja vedno zahteva postopek **F10 za predogled, nato še en F10 za izrecno izdajo**.

Točna ničelna vsebina za servisni primer:

```text
Zaslon OLED iPhone 11    x 1     +110,00 EUR
T2                       x 1      +30,00 EUR
Zaslon OLED iPhone 11    x-1     -110,00 EUR
T2                       x-1      -30,00 EUR
SKUPAJ                              0,00 EUR
```

Izdani račun je nespremenljiv, tudi če znaša 0,00 EUR. Poznejši popravek se izvede z vezanim storno oziroma novim popravnim dokumentom, nikoli s spremembo izdanega računa.

### Cena in rabat

- Uporabnik prodajne cene ne prepisuje poljubno. Spreminja jo samo prek dovoljenega rabata.
- Trdo dno dela `L0` je 22,00 EUR, kjer pravilo velja.
- Minimalna cena artikla je dokumentirana dejanska nabavna cena z vsemi pripadajočimi stroški, povečana za 5 %, nato zaokrožena **navzgor na naslednjih 5 celih EUR**.
- Noben rabat ne sme prebiti ustreznega dna.
- Starejše pravilo o zaokroževanju artikla samo na naslednji cel evro je razveljavljeno.

Odprta podrobnost, ki se ne sme izmišljati: natančen podatkovni in uporabniški potek pravila, da se saldo storno dokumenta uporabi na istem izvornem računu, mora biti pred implementacijo še zapisan kot preverljiva specifikacija.

---

---

## 13. Plačila, izdaja in FURS

- Vsota plačil mora biti natančno enaka vrednosti računa; delno plačilo ali preplačilo ni dovoljeno.
- Deljeno plačilo je dovoljeno samo, če se deli natančno seštejejo v celoten znesek.
- Del TRR mora biti potrjeno prejet.
- Gotovinsko plačilo zahteva odprto blagajniško sejo.
- Denarni izračuni uporabljajo decimalno aritmetiko in poslovno določeno zaokroževanje, ne binarnega `float`.

Računi se številčijo letno po poslovnem prostoru in elektronski napravi, zaporedje pa se vsako leto začne z 1. Dve sočasni izdaji ne smeta dobiti iste številke ali ustvariti luknje zaradi navadnega tekmovanja odjemalcev.

### Trenutna organizacija

- `MFD` pomeni delavnico oziroma pisarno.
- `MFT` pomeni terenski računalnik oziroma vozilo.
- Računi se bodo izdajali v vozilu, kjer poteka terenski servis.
- Sedanji pisarniški računalnik začasno nosi vse tri aplikacije.
- Prihodnji drugi računalnik bo nosil Service in Blagajno.
- Vsak računalnik oziroma elektronska naprava dobi svojo oznako.
- Trenutno je edini operater `DR`.
- Produkcijski računi še niso bili izdani ali potrjeni; opravljeni so bili samo testi v testnem okolju z ločenim razvijalskim potrdilom.

Podpisani interni akt različice 1.2 z dne 31. 8. 2026 je zgodovinska podlaga, vendar bo po dokončni matriki naprav in poslovnih prostorov pripravljen nov akt, podatki pa po potrebi ponovno prijavljeni FURS. Datum začetka veljavnosti novega akta ostane prazno polje do pripravljenosti okolja.

VKR se uporabi samo, ko ni nobenega elektronskega načina povezave ali izdaje. To je skrajna izjema, ne običajen offline način aplikacije.

Namensko potrdilo in zasebni ključi so centralno zaščiteni. Izgubljen odgovor FURS ali omrežja se ponovi z istim zahtevkom in istim `operation_id`; nikoli se ne ustvari nov račun, nova številka ali nov ZOI samo zato, ker odjemalec ni prejel odgovora.

---
