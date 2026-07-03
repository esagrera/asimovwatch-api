# AsimovWatch — DB host i Render
*Actualitzat: 4 de juliol de 2026*

## Problema
Render ha de connectar-se a la base de dades PostgreSQL de Dinahosting amb les variables d'entorn correctes. Si només hi ha `DATABASE_URL`, l'aplicació pot fallar perquè el codi espera variables individuals com `DB_HOST`.

## Variables necessàries a Render
A Render → asimovwatch-api → Environment han d'existir aquestes 5 variables:
- `DB_HOST=REDACTED_HOST`
- `DB_PORT=5432`
- `DB_NAME=asimovwatch`
- `DB_USER=REDACTED_USER`
- `DB_PASSWORD=<contrasenya real>`

## Per què no n'hi ha prou amb `DATABASE_URL`
El backend està preparat per llegir les variables individuals i no només una URL compacta. Per això, si a Render hi falta `DB_HOST` o alguna de les altres, poden aparèixer errors del tipus `KeyError: 'DB_HOST'`.

## Com comprovar-ho
1. Entra a Render.
2. Obre el servei `asimovwatch-api`.
3. Revisa la secció `Environment`.
4. Verifica que les cinc variables hi siguin i que els valors siguin correctes.

## Símptomes si falla
- `curl http://localhost:8000/entries` funciona, però `https://api.asimovwatch.com/entries` retorna 500.
- Als logs de Render apareix `KeyError: 'DB_HOST'`.
- El servei públic respon amb errors tot i que el backend local sembla correcte.

## Solució
Afegir o corregir les 5 variables d’entorn a Render i desar els canvis. Després, fer redeploy del servei i tornar a provar els endpoints públics.

## Notes
A Dinahosting el fitxer `.env` del servidor també ha de contenir aquestes dades per a l’execució local, però aquest fitxer no s’ha de pujar a GitHub.
