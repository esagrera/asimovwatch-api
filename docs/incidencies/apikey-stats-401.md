# AsimovWatch — API key i errors 401
*Actualitzat: 4 de juliol de 2026*

## Objectiu
Documentar els errors d’autenticació relacionats amb una API key, especialment quan l’endpoint `stats` o altres rutes protegides retornen 401.

## Què significa un 401
Un codi 401 indica que la petició no està autenticada o que la credencial enviada no és vàlida. En aquest projecte, això pot passar si falta la clau, si la capçalera és incorrecta o si el servidor espera una credencial diferent de la que s’està enviant.

## On pot aparèixer
- Crides des del frontend.
- Crides des del crawler o scripts interns.
- Crides a `stats` o a altres endpoints protegits.

## Causes habituals
### Clau absent
La variable d’entorn o el secret no està present al servidor o al desplegament.

### Clau errònia
La clau enviada no coincideix amb la que el servidor espera.

### Capçalera mal formada
La petició no envia el format correcte de `Authorization` o utilitza un header diferent del que toca.

### Endpoint protegit sense credencial
S’està intentant accedir a una ruta que requereix autenticació però no s’està enviant cap credencial.

## Què comprovar
1. Revisar les variables d’entorn del servidor o de Render.
2. Confirmar que la clau s’està enviant amb el format correcte.
3. Verificar si l’endpoint realment exigeix autenticació.
4. Comprovar els logs per veure si el problema és de credencial o de ruta.

## Relació amb `stats`
Si `stats` retorna 401, el problema normalment no és de base de dades sinó d’autenticació o configuració de permisos. Si retorna 500, llavors el problema és una altra capa, com ara codi, variables d’entorn o connexió a BD.

## Solució típica
- Posar la clau correcta al secret o a l’entorn.
- Repetir la crida amb la capçalera adequada.
- Revisar si el backend ha canviat la política d’accés.

## Cas resolt — Swagger perd la API Key després de recàrrega/inactivitat

**Data de resolució:** 4 de juliol de 2026

**Endpoint afectat:** Tots els endpoints protegits sota `protected_router` (per exemple `/stats`, `/entries`), accedits des de `/docs`.

**Símptoma concret:** Després d'un període d'inactivitat o de recarregar la pàgina de `/docs`, el valor introduït al diàleg "Authorize" (camp `X-API-Key`) quedava buit. Totes les crides fetes des del Swagger a partir d'aquell moment retornaven 401, tot i que la clau configurada al servidor (`API_KEY` a Render) era correcta i no havia canviat.

**Causa identificada:** No es tractava d'una clau absent, errònia, ni d'una capçalera mal formada (les causes habituals descrites més amunt). El problema era un comportament per defecte de Swagger UI: el paràmetre `persistAuthorization` és `false` per defecte, de manera que les dades d'autorització es guarden només en memòria del navegador i es perden en recarregar la pàgina o tancar la sessió del navegador. No hi havia cap problema al backend, ni a les variables d'entorn de Render, ni al codi de `verify_api_key`.

**Solució aplicada:** Afegir el paràmetre `swagger_ui_parameters` al constructor de `FastAPI()` a `app/main.py`:
```python
app = FastAPI(
    title="Asimovwatch API",
    version="2.0.0",
    swagger_ui_parameters={"persistAuthorization": True}
)
```
Amb aquest canvi, Swagger UI guarda la clau al `localStorage` del navegador i la recupera automàticament, evitant haver de reintroduir-la a cada recàrrega.

**Verificació:** Provat introduint la clau a `/docs`, recarregant la pàgina i cridant `/stats` — resposta 200 OK correcta sense tornar a introduir la clau.

**Advertència de seguretat:** La clau queda emmagatzemada de manera persistent al navegador (no només durant la sessió). Si `/docs` s'accedeix des d'un ordinador o navegador compartit, cal netejar el `localStorage` manualment per evitar exposició no desitjada de la credencial.

**Relació amb les causes habituals d'aquest document:** Aquest cas no encaixa en cap de les quatre causes llistades a la secció "Causes habituals" — cal afegir-hi una cinquena categoria: **"Pèrdua de credencial al client (Swagger UI) sense relació amb el servidor"**, per si es torna a repetir un patró similar en altres eines de testeig.

## Nota operativa
Aquest fitxer serveix com a marcador de diagnòstic. Quan tinguem el cas exacte d’AsimovWatch, es pot completar amb l’endpoint, el missatge d’error i la solució definitiva.
