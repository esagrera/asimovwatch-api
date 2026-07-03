# AsimovWatch — Render deploy i Swagger
*Actualitzat: 4 de juliol de 2026*

## Objectiu
Aquest document recull el flux de desplegament a Render i la verificació del Swagger quan l’API local funciona però el domini públic no respon com toca.

## Arquitectura de servei
La web pública es serveix des de Dinahosting, però l’API pública ha de passar per Render. El domini `api.asimovwatch.com` apunta a `asimovwatch-api.onrender.com` i no directament al servidor Dinahosting.

## Desplegament correcte
1. Fer `git push` a la branca `main` del repositori de GitHub.
2. Verificar que Render detecta el canvi i llança el redeploy automàtic.
3. Repassar els logs de Render fins que aparegui un missatge equivalent a “Your service is live”.
4. Provar els endpoints públics després del redeploy.

## Swagger
El Swagger de FastAPI s’ha de comprovar a `https://api.asimovwatch.com/docs`. Si la URL pública falla però el servei local respon, el problema sol ser de variables d’entorn, DNS o desplegament a Render.

## Símptomes habituals
- `curl http://localhost:8000/entries` funciona, però `https://api.asimovwatch.com/entries` retorna 500.
- Swagger accessible localment però no públicament.
- Render mostra errors de variables d’entorn o reinicis repetits.

## Causes més freqüents
### Variables d’entorn a Render
Si falta alguna variable `DB_*`, el servei pot arrencar però fallar en executar consultes.

### DNS incorrecte
Si `api.asimovwatch.com` apunta al servidor Dinahosting en lloc de Render, l’API pública no funcionarà bé.

### Codi no sincronitzat
Si el commit nou no ha arribat a GitHub o Render encara no ha redeplegat, el servei públic continuarà usant una versió anterior.

## Verificació recomanada
Després de cada desplegament, comprovar:
- `https://api.asimovwatch.com/`
- `https://api.asimovwatch.com/entries`
- `https://api.asimovwatch.com/docs`

## Solució ràpida
Si el Swagger públic falla, revisar en aquest ordre:
1. Logs de Render.
2. Variables d’entorn.
3. DNS del subdomini `api`.
4. Commits recents al repositori.

## Nota operativa
No cal tocar Apache ni el backend local de Dinahosting per solucionar problemes de Swagger públic; la peça crítica és Render.
