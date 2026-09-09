# MikroFix Sandbox – enotni ročni sprejemni scenarij

Ta scenarij se izvede samo v **MikroFix SANDBOX**. Ne odklepa produkcije in ne
spreminja `production-release.json`.

## 1. Okolje in prijava

1. Odpri namizno bližnjico **MikroFix SANDBOX**.
2. Potrdi, da glavno okno in Office jasno kažeta `SANDBOX / NI PRODUKCIJA`.
3. Prijavi se z obstoječim lokalnim uporabnikom; prehod Main → Office ne sme
   ponovno zahtevati PIN-a.

Uspeh: podatkovna mapa je `MikroFixData_V30_SANDBOX`; nikjer ni trditve, da je
to Stable ali produkcija.

## 2. Začetek servisa iz Office

1. V Office izberi odprto servisno naročilo.
2. Klikni **ZAČNI / NADALJUJ SERVIS**.
3. Preveri, da se odpre isti servisni delovni prostor kot iz glavnega gumba
   Service in da je prikazan čas prvega začetka.
4. Vrni se v Office in isti nalog znova odpri.

Uspeh: drugi vstop ne ponastavi časa začetka.

## 3. Deli, testi in zaključek

1. Dodaj en fizični artikel z zaloge.
2. Pri iPhonu preveri, da testi PRED/PO začnejo kot `NI TESTIRANO`.
3. Pri testu »Druga naprava« preveri, da iPhone testov ni in se ne shranijo.
4. Zaključi servis.

Uspeh: program sam izračuna dejanske minute ter ustvari natanko eno zaklenjeno
postavko **Delo**. MFD uporabi T1; MFT teren uporabi T2. Artikel je do računa
rezerviran, ne dvakrat odpisan. Če skupna cena presega ponudbo, zaključek brez
odobrenega limita, kanala in dokazila ne sme uspeti.

## 4. Dnevnik, račun in storno

1. Poskusi izdati račun brez odprtega dnevnika.
2. Odpri dnevnik za pravilni par MFD/PIS ali MFT/PIS.
3. Prenesi zaključeni servis v Blagajno; ne spreminjaj zaklenjene postavke Delo.
4. Izdaj sandbox račun in potrdi originalni tisk/PDF.
5. V testnem scenariju račun storniraj.

Uspeh: prvi poskus je blokiran; po odprtju dnevnika račun vsebuje vse dele in
eno postavko Delo. Račun fizično odpiše rezervirani LOT. Storno isti servis
ponovno odpre in isti LOT vrne v njegovo rezervacijo.

## 5. FURS online/offline

Ta korak se izvede šele, ko je v sandboxu nameščeno veljavno FURS testno
potrdilo.

1. Pošlji en račun v FURS TEST in preveri prejeti EOR.
2. V nadzorovanem poskusu onemogoči omrežje ter izdaj nov testni račun.
3. Preveri lokalni ZOI, besedilo `EOR BO PRIDOBLJEN NAKNADNO` in en zapis
   `PENDING_RECONCILIATION`.
4. Obnovi omrežje in sproži uskladitev.

Uspeh: blagajna med izpadom deluje, isti nespremenjeni zahtevek pa pozneje dobi
EOR in izgine iz čakalne vrste.

## 6. Fizični tisk

1. Natisni isti račun na HP A4.
2. Natisni 80-mm različico na Epson TM-T20II.
3. Preveri `MikroFix`, celotni registrirani naziv, č/š/ž, belo ozadje, berljivo
   večjo termično pisavo ter plačilni UPN QR v bančni aplikaciji.

Uspeh: oba odtisa sta fizično pravilna; termični gonilnik ne invertira slike.

## 7. Backup in meja za Stable

1. Izberi zunanji ext4 nosilec in vnesi backup geslo v sistemski poziv.
2. Ustvari šifriran backup in ga obnovi v ločeno testno mapo.
3. Rezultat zapiši v dnevnik dokončanja.

Šele ko so vsi zgornji koraki potrjeni, se lahko pripravi nov kandidat Stable.
Produkcijski manifest mora do takrat ostati `REVOKED_PENDING_REQUIREMENTS_AUDIT`.
