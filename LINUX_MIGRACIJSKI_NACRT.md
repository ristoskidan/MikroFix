# MikroFIX – varen prehod na Linux

Datum načrta: 4. september 2026  
Ciljna izdaja: LMDE 7 »Gigi«, Cinnamon, 64-bit (Debian 13 Trixie)

## Odločitev o distribuciji

LMDE 7 združi namizje Linux Mint z bazo Debian 13 in sistemskim Pythonom 3.13.
Debian 13 je stabilna produkcijska izdaja s podporo do leta 2030, LMDE 7 pa je
izdaja LTS. To je bolj predvidljiva osnova za blagajno kot tekoče posodabljani
Arch Linux.

Uradni viri:

- https://www.linuxmint.com/download_lmde.php
- https://www.linuxmint.com/rel_gigi.php
- https://www.debian.org/releases/trixie/

## Kaj je v tej pretvorbi že pripravljeno

- Windows `pywin32` ni več obvezna Linux odvisnost.
- Neposredni termični tisk uporablja CUPS in spremlja tiskalniško opravilo.
- PDF se na Linuxu odpira z `xdg-open`.
- MicroDetect uporablja `usbmuxd`; iTunes na Linuxu ni potreben.
- produkcijske datoteke dobijo dovoljenja 0600, imeniki 0700;
- obnovitveno geslo se hrani v namiznem Secret Service/keyringu, ne v navadni
  datoteki;
- vsi Linux zaganjalniki izrecno odstranijo dovoljenje za produkcijski FURS;
- testna konfiguracija nima poti do nobenega zasebnega `.p12`/`.pfx`.

## Priporočena strojna osnova

Uradni minimum Linux Mint je 2 GB RAM in 20 GB diska. Za MikroFIX je razumen
minimum 8 GB RAM, dvojedrni 64-bit procesor in 256 GB SSD. Priporočilo je 16 GB
RAM in 512 GB SSD zaradi PDF-jev, fotografij, lokalnih kopij ter prihodnje
povezave s strežnikom. UEFI, TPM in možnost šifriranja diska so zaželeni.

## Vrstni red prehoda

1. Windows izdajo ohrani kot nespremenjen povratni sistem.
2. Pred selitvijo naredi preverjeno šifrirano kopijo vseh baz in jo obnovi v
   ločeno TEST mapo. SQLite baze se ne kopirajo, ko je program odprt.
3. Prenesi uradni LMDE 7 ISO in preveri objavljeni SHA256 ter podpis ISO.
4. Namesti LMDE z ločenim uporabnikom za MikroFIX, brez samodejne prijave in s
   šifriranjem sistemskega diska.
5. Vključi varnostne posodobitve, zaklep zaslona po 10 minutah in požarni zid
   z zavrnjenimi dohodnimi povezavami. Aplikacija se nikoli ne zaganja kot root.
6. Kopiraj to mapo na notranji SSD, nato zaženi:

       bash namesti_mikrofix_linux.sh
       ./preveri_mikrofix_linux.sh
       ./zazeni_mikrofix_linux_E2E_TEST.sh

7. Šele po celotni testni matriki se ločeno načrtuje prenos živih baz in
   produkcijskega FURS potrdila.

## Epson TM-T20II in ostali tiskalniki

TM-T20II je uradno podprt z Epson TM/BA Series Thermal Printer Driverjem za
GNU/Linux/CUPS. Ko je tiskalnik fizično pri računalniku:

1. namesti uradni Epson TM/BA paket za Debian/amd64;
2. prek CUPS dodaj USB vrsto in jo poimenuj npr. `EPSON_TM_T20II`;
3. nastavi 80-mm papir, 203 dpi in samodejni rez;
4. preveri `lpstat -p -d` in poskusi testno stran;
5. ime vrste vpiši kot `printer.printer_name` v `mikrofix_settings.json`;
6. iz MikroFIX natisni dolg testni račun, šumnike, QR in vsaj eno kopijo.

Dokument Epson potrjuje CUPS podporo:
https://files.support.epson.com/pdf/pos/bulk/tm-t20ii_trg_en_revc.pdf

A4 tiskalnik naj ima svojo ločeno CUPS vrsto. MikroFIX A4 pogodbo odpre v
pregledovalniku, da uporabnik zavestno izbere A4 tiskalnik. Čitalnik Zebra
DS2208 praviloma deluje kot USB tipkovnica; preveriti je treba slovensko
razporeditev, barkode in končni Enter.

## MicroDetect na Linuxu

Paketa `usbmuxd` in `libimobiledevice-utils` sta v Debian 13. Po priklopu se
iPhone odklene in na njem potrdi Trust. Osnovna preverba:

    idevice_id -l
    idevicepair validate
    ideviceinfo

Windows pairing zapisov se ne kopira. Vsaka naprava se na Linuxu ponovno
avtorizira. MikroFIX še vedno bere samo servisne identifikacijske podatke.

Uradna projekta:

- https://github.com/libimobiledevice/usbmuxd
- https://packages.debian.org/trixie/libimobiledevice-utils

## Certifikati

Zasebni certifikati niso del programske mape. Na Linuxu naj bodo šifrirani
`.p12/.pfx` v zasebnem imeniku z dovoljenjem 0600. TEST in PRODUKCIJA morata
imeti ločeni konfiguraciji. Osebni certifikat Daniel Ristoski ima neizvozljiv
zasebni ključ, zato potrebuje Linux gonilnik podpisne USB naprave in PKCS#11.

## Zunanje kopije

USB za varnostne kopije se v produkciji priklopi na stalno točko, npr.
`/mnt/mikrofix-backup`, določeno po UUID. Če nosilec ni resnično priklopljen,
Linux izdaja kopijo zavrne. Geslo je v Secret Service keyringu, šifrirane kopije
pa se še naprej ustvarijo največ enkrat na 24 ur in hranijo 30 dni.

Ta Kingston USB trenutno služi samo prenosu programa. Produkcijska aplikacija
in aktivne SQLite baze ne smejo delovati neposredno z njega.

## Meja trenutne potrditve

Na Windows je uspešnih 127 avtomatskih testov, vključno s štirimi novimi testi
Linux plasti. Dejansko LMDE, Epson USB, iPhone USB, keyring in obnovitev kopije
morajo biti še fizično preverjeni. Do takrat je ta izdaja samo LINUX DEV/TEST.
