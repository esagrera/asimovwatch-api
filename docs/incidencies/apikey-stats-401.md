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

## Nota operativa
Aquest fitxer serveix com a marcador de diagnòstic. Quan tinguem el cas exacte d’AsimovWatch, es pot completar amb l’endpoint, el missatge d’error i la solució definitiva.
