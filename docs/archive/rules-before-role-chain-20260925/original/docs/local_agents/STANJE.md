# Začetno stanje lokalnih pomočnikov

**Datum:** 19. 9. 2026

## Qwen 3.5 9B

Pripravljen za začetne tehnične preglede pod nadzorom glavnega pomočnika.
Uspešno je ponovil pravila o idempotentnem retryju, računu 0,00 EUR, online
Blagajni in odprti specifikaciji storna. Pri širšem pregledu je prej izumljal
kode napak in pravne posledice, zato njegov seznam »manjkajočih testov« vedno
zahteva preverjanje v celotni kodi.

## GPT OSS 20B

Pripravljen za začetni skeptični drugi pregled pod nadzorom glavnega
pomočnika. Pravilno razume retry, vlogo Blagajne in zahtevo po centralni
povezavi. Pri odprti specifikaciji storna je enkrat izumil ročno odobritev;
ta napaka je zdaj izrecno zajeta v trajnih lekcijah. Uporablja nizko raven
razmišljanja in omejen končni odgovor.

## Pravilo uporabe

Noben lokalni odgovor ni odobritev. Glavni pomočnik preveri navedbe proti
kanoničnemu kontekstu, celotni kodi in testom ter v dnevniku zapiše, kaj je
sprejeto, zavrnjeno ali zahteva Danielovo odločitev.
