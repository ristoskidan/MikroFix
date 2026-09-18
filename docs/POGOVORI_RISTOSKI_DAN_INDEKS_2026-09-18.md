# MikroFIX — indeks pregledanih pogovorov računa ristoski.dan

Datum pregleda: 18. 9. 2026

## Namen in obseg

Pregledanih je bilo vseh 25 pogovorov, ki jih je uporabnik povezal s projektom:
skupaj 742 pogovornih obratov na 84 straneh zgodovine. Ta dokument je trajni
iskalni indeks ugotovitev; ne nadomešča novejših kanonskih pravil v
`docs/kanon/` ali potrjenih pravil v `MIKROFIX_v30_MASTER_SPECIFIKACIJA.md`.
Kadar se zgodovinska odločitev razlikuje od novejše izrecne odločitve, velja
novejša odločitev.

## Najdba glede odkupa in pooblaščenih agentov

### Dokument, ki je bil dejansko izdelan

V pogovoru `66aebced-b4dc-8328-a2b2-90dda7646799` je bil izdelan dokončan
osnutek dokumenta **MikroFIX – ODKUP NAPRAVE** (pogovorni obrat
`161bd20c-d8d6-41fc-9a58-809dddd2d1ad`, writing block `73142`). To je pogodba
med prodajalcem naprave in MikroFIX. Vsebuje najmanj:

- oznako odkupa in povezavo s servisnim nalogom;
- podatke prodajalca;
- model, IMEI in serijsko številko naprave;
- namen odkupa: obnova/prodaja, deli ali nadaljnja diagnostika;
- stanje naprave in znane napake;
- preverjanje Activation Lock/Find My;
- dogovorjeno ceno in način izplačila;
- izjavo prodajalca o lastništvu in odsotnosti pravic tretjih oseb;
- dovoljenje za pregled, razstavljanje, popravilo, obnovo, uporabo delov,
  recikliranje, nadaljnjo prodajo in izbris podatkov;
- prehod lastništva po plačilu in izročitvi;
- podpisa prodajalca in MikroFIX.

### Dokument, ki ga v pregledanih pogovorih ni

V nobenem od 25 pregledanih pogovorov ni bil najden dokončan ločen dokument z
naslovom ali vsebino **Pooblastilo za odkup naprav v imenu MikroFIX**, niti
ločena pogodba o sodelovanju z odkupnim agentom. Pogovori vsebujejo agentov
cenik, procesna pravila in odkupno pogodbo s prodajalcem, ne pa dokončnega
pooblastila zastopniku.

Zato je varna ugotovitev:

1. odkupna pogodba s prodajalcem je bila izdelana;
2. pisno pooblastilo agentu še ni bilo najdeno oziroma dokončano;
3. tedenski PDF za agente ni pooblastilo in ne dokazuje izvora posamezne
   naprave;
4. pooblastilo je treba izdelati kot ločen, različicen dokument in ga povezati
   z vsakim agentskim odkupom.

Poznejša kanonska odločitev je že zapisana v pravilih 221–222 master
specifikacije: kupec je neposredno MikroFIX, agent nastopa kot predstavnik;
program preveri identiteto, vsebinski in časovni obseg ter veljavnost pisnega
pooblastila na čas odkupa. V trenutni različici ni programskega finančnega
limita agenta.

## Časovnica internega akta

- 18.–19. 8. 2026: izdelan je bil prvi akt z oznakama `MFD` in `MFT`, napravo
  `1`, operaterjem `DR` ter številčenjem `MFD-1-x` in `MFT-1-x`.
- 20.–21. 8. 2026: stara logika tablice/POS-tiskalnika je bila popravljena v
  računalnik, spletno MiniBlagajno in A4/PDF. Bluetooth in model tiskalnika ne
  sodita v interni akt.
- Fizična kopija dokazuje, da je bila poznejša **različica 1.2 podpisana
  31. 8. 2026**. Na njej je firma zapisana kot `MikroFix`, uporabnik pa je
  pozneje opozoril, da to ni pravilen zapis firme.
- Sedanja zasnova aplikacije in matrika naprav/prostorov se razlikujeta od
  podpisane različice. Pred produkcijskim FURS tokom je zato predvidena nova
  prijava/uskladitev in nov interni akt; stare podpisane različice se ne
  prepisuje za nazaj.

## Cenovni motor — najdene zgodovinske formule

### Minimalna cena rezervnega dela

Najzgodnejša natančna formula v pregledanih pogovorih je:

```text
nabavna_cena_kos = vrednost dobaviteljeve postavke / količina
osnova = nabavna_cena_kos + vhodna logistika + 2,20 EUR stroška zaloge
minimalna_prodajna_cena = osnova * 1,05
```

Pojasnila iz pogovorov:

- vhodna logistika je dejanski strošek, da del prispe v MikroFIX;
- fiksnih `2,20 EUR` velja za dejanski založeni rezervni del, ne avtomatično za
  vsak vijak, tesnilo ali droben material;
- dodatnih 5 % je sklad za izboljšanje logistike oziroma obvezna varovalka;
- rabat, kupon, akcija ali ročni vnos cene ne sme prebiti minimuma;
- rabat lahko porablja samo maržo nad minimumom.

Novejša izrecna odločitev uporabnika v trenutnem projektu je nadrejena:
**minimalna cena artikla je dokumentirana nabavna cena z vsemi pripadajočimi
stroški + 5 %, nato zaokrožena navzgor na naslednjih 5 celih EUR.** To je
trenutno kanonsko pravilo za nadaljnjo izvedbo; zgodovinski `2,20 EUR` ostane
stroškovna sestavina samo tam, kjer je po veljavnem cenovnem profilu dejansko
uporabljena.

### L0 in delo

Zgodovinski pogovori uporabljajo več različic L0 in tarif, zato jih ni dovoljeno
združiti v eno navidezno sedanjo vrednost:

- L0 je bil vedno opredeljen kot interni hard floor, ki ga rabat ne prebije;
- zgodovinske vrednosti L0 so bile 11,08 EUR, 14,40 EUR in pozneje najmanj
  22 EUR;
- javna zgodovinska tarifa je bila T1 30 EUR/h oziroma 15 EUR na začetih
  30 minut in T2 90 EUR za prvo uro ter 15 EUR na nadaljnjih začetih 30 minut;
- T3 je bil najprej načrtovan, nato odstranjen iz ponudbe;
- hard floor ni javni cenik in se stranki ne razkriva kot interna kalkulacija.

Za produkcijo velja samo trenutno različicen cenovni profil, ne poljubna številka
iz starega pogovora.

### Odkup obnovljivih naprav — zaklenjena zgodovinska formula

```text
R = robustna sredina 3–5 primerljivih evropskih refurbished ponudb
C = baterija po hard costu + 1 h T1 hard floor + tesnilo/material
    + 1,25 EUR splošnega materiala + 30 EUR MikroFIX paketa
M = 20 % od R, najmanj 40 EUR in največ 150 EUR

MAX ODKUP = R - C - M
HAGGLE = približno 10 % MAX, najmanj 10 EUR in največ 50 EUR,
         zaokroženo na 10 EUR
PRIPOROČEN ODKUP = MAX ODKUP - HAGGLE
```

`MAX ODKUP` se zaokroži navzdol na okroglo vrednost in je fiksna meja. Agentov
končni prikaz vsebuje samo `Model | PRIPOROČEN ODKUP | MAX ODKUP`. Prejšnja
predloga 30 % prostora in 15 % haggle je bila pozneje zavrnjena in ne velja.

## Druge pomembne poslovne odločitve iz zgodovine

### Odkup, obnova in prodaja

- Odkup mora ustvariti sled `ODK -> kartica naprave -> obnova ali donor ->
  zaloga -> prodaja/račun`.
- Nova baterija je standard obnovljene naprave.
- Obnova vključuje 6-sklopni funkcionalni test, prosto besedilo zunanjega
  stanja, novo tesnilo ob odprtju in fotografije.
- Activation Lock/Find My mora biti odstranjen; naprava se preda na začetnem
  nastavitvenem zaslonu brez tujega računa.
- Prodajni paket vsebuje 30 W polnilec in kakovosten aftermarket kabel.
- Agentov cenovni PDF je samo operativni pripomoček; ne nadomesti individualne
  pogodbe, plačilne sledi ali identifikacije naprave.

### Servis in identifikacija

- Za podprte telefone ni ročnega prepisovanja IMEI/SN: servis se lahko začne
  brez USB zaupanja, neposredno branje pa mora uspeti pred tehničnim zaključkom.
- Poznejša kanonska izjema dovoljuje sledljiv ročni sprejem naprav, ki jih
  MicroDETECT ne podpira ali jih ni mogoče prebrati; razlog in razpoložljivi
  identifikatorji se zapišejo, podatki pa se pozneje dopolnijo kot nov dogodek.
- PIN oziroma geslo se ne zapisuje; stranka napravo po potrebi sama odklene.

### Garancija in dokumenti

- Polni IMEI je zahtevan na garancijskem listu; drugi dokumenti ga lahko
  maskirajo.
- Za obnovljene naprave je bila sprejeta 12-mesečna komercialna garancija,
  ločeno od zakonskih pravic zaradi neskladnosti.
- Reklamacija je vezana na račun; račun je pri servisu vezan na servis, pri
  prodaji naprave pa tudi na njen garancijski dokument in kartico naprave.
- Fotografije se po najnovejši odločitvi hranijo sorazmerno z garancijskim
  obdobjem; stara splošna 90-dnevna servisna vrednost ni več univerzalno
  pravilo.

### Neprevzete naprave

- 30 dni brezplačne hrambe po obvestilu o zaključku;
- nato 10 EUR za vsakih začetih nadaljnjih 30 dni;
- po 90 dneh končni poziv in samo zakonsko dovoljen nadaljnji postopek;
- naprava nikoli ne postane samodejno last MikroFIX.

### Metodološko pravilo projekta

Pri pravnih, davčnih in varnostno pomembnih vprašanjih se loči med zakonsko
obveznostjo, uradnim priporočilom, razlago in poslovno odločitvijo. Če ni vsaj
zelo visoke gotovosti in obstaja uradna pot preverjanja, se odločitev ne sme
zapolniti z ugibanjem.

## Indeks 25 pregledanih pogovorov

| Pogovor | Obrati / strani |
|---|---:|
| `b85384ad...` Branch · Branch · EU refurbished iPhone cene | 207 / 21 |
| `89d1c406...` EU refurbished iPhone cene | 99 / 10 |
| `66aebced...` Branch · EU refurbished iPhone cene | 144 / 15 |
| `219b8ed0...` Specifikacije domačega strežnika | 10 / 1 |
| `9e942b29...` SI price report ready email blocked | 32 / 4 |
| `5441fd9c...` ADR elektrika v kabini | 1 / 1 |
| `1e2c43ba...` Nakupovalni seznam avta | 84 / 9 |
| `46b9c472...` Zakleni cenik v1.0 | 8 / 1 |
| `65ad2f78...` Izdelava PDF cenika | 1 / 1 |
| `9d6f4e27...` Cena menjave stekla iPhone 14 | 2 / 1 |
| `5bde3acc...` Cena menjave zaslona in baterije | 10 / 1 |
| `14fa32cd...` Pojasnilo SEO optimizacije | 2 / 1 |
| `208ec61b...` Zahteve za delavnico | 29 / 3 |
| `614bc0b1...` Popravek preloma naslova | 13 / 2 |
| `178e9d36...` Logika servisnega lista | 2 / 1 |
| `f649b19f...` Odpiralni čas SPOT Ormož | 3 / 1 |
| `44783c6a...` Repair Assistant iPhone | 8 / 1 |
| `0f634dd1...` Dobavitelji delov za iPhone | 4 / 1 |
| `52542eb2...` MikroFix ideja in potencial | 9 / 1 |
| `24122f3f...` Cenik storitev in strategija | 7 / 1 |
| `c3c1dcf1...` Ustvarjanje spletne strani | 9 / 1 |
| `9528ef97...` Android popravila in zaslužek | 5 / 1 |
| `4a8c86b4...` JCID V1SE in iPhone | 12 / 2 |
| `f9d26694...` JCID V1S Legalnost | 16 / 2 |
| `174e9bdb...` MikroFix sivo modri dizajn | 3 / 1 |

## Naslednji dokumentni korak

Pred uporabo agentskega odkupa je treba pripraviti in pravno preveriti ločeno
predlogo **Pooblastilo za odkup naprav v imenu MikroFIX**. Vsebovati mora najmanj
identiteto pooblastitelja in pooblaščenca, izrecno zastopanje MikroFIX pri
odkupu naprav, vsebinski in časovni obseg, pravila podpisovanja odkupne pogodbe,
ravnanje z denarjem in napravami, obvezno uporabo MikroFIX dokumentacije,
prepoved osebnega vmesnega odkupa, varstvo podatkov, preklic in podpisa.
