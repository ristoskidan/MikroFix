# MikroFIX v30 R6 – pregled Blagajne kot enotnega zaključnega sklopa

Datum pregleda: 1. 9. 2026

## Izvedena sprememba

- Neposredno dodana tarifa T1 v Blagajni samodejno dobi opis `Delo v delavnici`.
- Neposredno dodana tarifa T2 v Blagajni samodejno dobi opis `Delo na terenu`.
- Opisa storitve pri teh dveh gumbih ni več treba ročno vnašati.
- Postavke, prenesene iz konkretnega servisa, ohranijo dejanski opis, vnesen v servisu.

## Potrjeno delujoče jedro

- Blagajna bere samo aktivno, sproščeno in razpoložljivo glavno zalogo.
- Prejeti dokumenti se prek LOT-a povežejo z zalogo, prodaja pa prek FIFO-razporeditve s konkretnim izvorom.
- Zaključen servis se lahko izbere v Blagajni; prenesejo se kupec, servisne postavke in porabljeni deli.
- Del, ki je bil že porabljen pri servisu, se ob računu ne odšteje še enkrat; dodatna količina istega artikla se odšteje.
- Izdaja računa, odpis zaloge, prodaja obnovljene naprave ter zaključek povezanega servisa so v eni transakciji SQLite.
- Ponovljen isti žeton izdaje ne ustvari drugega računa ali zalogovnega premika.
- Rabat storitve se tiho omeji z L0, rabat artikla pa z nabavno vrednostjo + 5 %.
- Gotovina, TRR in razdeljeno plačilo morajo skupaj ustrezati računu do centa; razdeljen TRR mora biti potrjeno prejet.
- Račun ohrani posnetek prostora, naprave, operaterja in davčne številke operaterja.
- A4 in termični PDF vsebujeta slovenske oznake plačil ter stolpec rabata.
- Nezaključen prvi tisk se po ponovnem odprtju Blagajne obnovi in blokira izdajo novega računa.

## Blokade pred produkcijo

### 1. Izvor in zaklep posamezne vrstice

Račun ima povezavo na servis samo v glavi. Posamezna vrstica ne hrani dovolj natančnega izvora, na primer `servisna postavka`, `servisni del` ali `dodatna prodaja v blagajni`. Prenesene servisne vrstice je trenutno mogoče odstraniti, spreminjati količino ali jim spremeniti rabat enako kot dodatni prodaji. To onemogoča zanesljiv končni servisni list in ločitev garancijskih pravil.

Potrebno: nespremenljiv vir in ID izvorne servisne vrstice, jasna skupina `SERVIS – ZAKLENJENO` ter ločena skupina `DODATNA PRODAJA`.

### 2. Končni servisni list nastane prezgodaj

Program po pripravi servisa za račun že ustvari PDF servisnega lista, čeprav račun še nima številke. Končni list mora nastati in se zakleniti šele skupaj z izdanim računom; prej je dovoljen samo jasno označen predogled/osnutek.

### 3. Obnovljena naprava in rabljeni deli

Pri obnovljeni napravi Blagajna trenutno preveri samo notranji kljukec o 12-mesečnem dogovoru. Ne preveri obstoja konkretnega pripravljenega in podpisanega A4 garancijskega/prodajnega dokumenta ter ga ne poveže atomarno z računom. Termični račun tudi nima zahtevanih zadnjih štirih znakov IMEI in sklica na garancijski list.

Donorski oziroma `pulled` rabljeni del se obravnava kot navaden artikel, zato manjkajo ločen enoletni dogovor in ustrezno javno besedilo za samostojno prodan rabljeni del.

### 4. Končni pregled je mogoče obiti s spremembo plačila

Po potrjenem končnem pregledu sprememba zneskov gotovine/TRR in prejetega bankovca ne razveljavi pregleda. Tako se lahko izdani način oziroma sestava plačila razlikuje od pregledane, prejeti bankovec pa se lahko po pregledu zmanjša pod zahtevani znesek.

Končni pregled tudi ne prikaže vseh zahtevanih podatkov: kupca, dejansko uporabljenega rabata vsake vrstice in celotne sestave plačila.

### 5. Storno izdanega računa ni izdelan

Potrjen POS račun je nespremenljiv, vendar ni zaključene poti za celotni sledljivi storno, vrnitev konkretnih LOT-ov, razveljavitev plačilnega dogodka, razveljavitev povezave s servisom/obnovljeno napravo in nov pravilen dokument z referenco.

### 6. Tisk ni zanesljivo potrjen

- Neposredni tisk še uporablja Windows ukaz PDF-pregledovalnika `printto`, ki je na tem računalniku že vrnil napako 31.
- Funkcija za branje privzetega Windows tiskalnika ima nedosegljiv del kode in zato trenutno vedno vrne prazen rezultat.
- Program ne spremlja dejanskega Windows tiskalnega opravila.
- Številka kopije se porabi še pred uspešno izdelavo oziroma potrditvijo izpisa; neuspešen poskus je lahko napačno evidentiran kot izdelana kopija.
- Poskusi in rezultati tiskanja niso v celoti zapisani v revizijski sledi.

### 7. Zadnji dogovor F10 in razpored Blagajne še nista izvedena

Trenutni prvi F10 odpre besedilni pregled, naslednji račun izda in natisne, tretji korak pa ročno potrdi tisk. Zadnji dogovor zahteva:

1. prvi F10 preveri podatke ter izdela jasno označena A4 in 80-mm predogleda brez številke, zaloge in FURS;
2. drugi F10 izda nespremenljivi račun in ga natisne;
3. ob neuspehu ostane isti račun na voljo za ponovni tisk/PDF brez nove številke.

Manjkajo tudi dogovorjeni levi navpični ukazi, F5 za novi račun in F8 za UPN QR.

### 8. Plačilni tok še ni cel

- Pri razdeljenem plačilu se zadnji delež ne izračuna samodejno kot natančen preostanek.
- UPN QR ni izdelan.
- Neplačanega TRR računa ni mogoče pozneje pravilno označiti kot plačanega z datumom in revizijsko sledjo.
- Ni razveljavitve napačnega plačilnega dogodka.
- Rok plačila in opozorila za zapadle TRR račune niso vključeni.

### 9. Cenovna varovalka mora ob izdaji ponovno prebrati LOT

Blagajna pri izračunu uporabi nabavno vrednost, ki jo je uporabniški vmesnik prebral ob dodajanju artikla. Ob sami izdaji iz LOT-a prebere dejanski strošek, vendar šele po izračunu končne cene. Pred produkcijo mora isti zaklenjeni transakcijski korak ponovno preveriti aktualno ceno iz Office in dejansko nabavno vrednost prodanega LOT-a + 5 %.

### 10. Polna inventura trenutno ne more zakleniti Blagajne

Specifikacija zahteva popoln zaklep prodaje med polno inventuro, vendar celoten inventurni postopek še ni implementiran. Blagajna zato še nima aktivne sistemske blokade za inventurni dan.

## Predlagani vrstni red popravkov

1. Izvor/zaklep vrstic in končni servisni list.
2. Garancijski dokument obnovljene naprave in pravilo rabljenih delov.
3. Neobhodljiv končni pregled ter zadnji dogovor F10.
4. Tiskalni podsistem in pravilno evidentiranje originala/kopij.
5. Razdeljeno plačilo, UPN QR in življenjski cikel TRR plačila.
6. Celotni storno izdanega računa.
7. Ponovni transakcijski pregled cene in konkretnega LOT-a.
8. Inventurni zaklep Blagajne.
9. Produkcijska povezava FURS kot zadnji zaključni sklop.

## Preverjanje

- Vseh 77 avtomatskih testov je uspešnih.
- Preverjanje Windows/Tk okolja je uspešno.
- FURS produkcija je ostala izklopljena.
- Obstoječe DEV in E2E baze ter že izdani testni računi pri tem pregledu niso bili spreminjani.
