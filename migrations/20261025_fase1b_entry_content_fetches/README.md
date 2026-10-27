# Migració Fase 1B, pas 0: `entry_content_fetches` i `entries.discard_reason`

**Estat: preparada, no executada.** Cap canvi a la BD ni al pipeline.

| Fitxer | Funció |
|---|---|
| `01_apply.sql` | Comprovacions prèvies, taula `public.entry_content_fetches` i columnes `public.entries.discard_reason` i `public.entries.analyzed_fetch_id`. Transaccional. |
| `02_verify.sql` | Consultes només de lectura. |
| `03_rollback.sql` | Desfà la migració. Avorta si ja hi ha dades. |

## Abast dels esquemes consultats

Els fitxers `backup/schema_251026_full.sql` i `backup/schema_only.sql` (PostgreSQL 12.22) **només cobreixen `public.entries`**. No acrediten una còpia completa de la BD: no hi apareix cap altra taula (`prompts`, `config`, `sources`, `batch_jobs`, etc.) ni cap `GRANT`. Aquests scripts s'han escrit només contra `entries` i no s'ha pogut comprovar res de la resta de l'esquema (vistes, funcions, disparadors, privilegis). L'exportació completa que es fa abans d'executar-los és la referència real.

## Què crea

- **Taula `entry_content_fetches`**: un registre per intent d'obtenció, amb `entry_id` (FK a `entries.id`), URL sol·licitada i normalitzada, estat, HTTP, text obtingut (separat de `raw_content` i `raw_snippet`, que no es toquen), hash SHA-256 complet, User-Agent i `reused_from_fetch_id` per reutilitzar un text sense duplicar-lo.
- **Estats**: `ok`, `robots_blocked`, `robots_unreachable`, `access_denied`, `anti_bot_challenge`, `text_too_short`, `timeout`, `http_error`, `error`, `pdf_unprocessed`, `duplicate`.
- **`entries.discard_reason`**: `out_of_scope`, `no_content`, `insufficient_text` o `fetch_failed`. Les files existents queden amb `NULL`, que vol dir «motiu històric no registrat». No s'infereix de la longitud del text i no es reompl cap motiu històric.
- **`entries.analyzed_fetch_id`** (`bigint`, nullable, FK a `entry_content_fetches(id)` amb `ON DELETE RESTRICT` i un índex, `idx_entries_analyzed_fetch_id`): obtenció de la qual va sortir el text analitzat. `NULL` vol dir text del feed o història anterior a la Fase 1B. Es crea després de la taula nova. **No s'omple en aquest pas**: queda a `NULL` a totes les files fins que el codi consumidor l'usi. Si el registre referenciat és `duplicate`, el text es resol via `reused_from_fetch_id`.
- Els intents d'obtenció són independents de `processing_retries`, que continua sent dels errors del pipeline.

### `text_source`

Té un `CHECK`: `NULL` o un de `html_article`, `html_main`, `html_body` (contenidor HTML d'on s'ha extret el text). Amb `NULL` no hi ha text desat. Ampliar el vocabulari (per exemple per a PDF) exigirà una migració que substitueixi el `CHECK`.

### `normalized_url`

És `NOT NULL`. Si una URL no es pot normalitzar, `normalized_url` desa la URL sol·licitada (`requested_url`) tal com es va demanar, perquè l'intent es pugui registrar igualment.

### Camps `NOT NULL` i nullable

- **`NOT NULL`**: `id`, `entry_id`, `requested_url`, `normalized_url`, `fetch_status`, `fetched_at`, `truncated` (per defecte `false`) i `user_agent`.
- **Nullable**: `final_url`, `http_status`, `content_type`, `text_content`, `text_length`, `content_hash`, `text_source`, `error_detail` i `reused_from_fetch_id`.
- Cap `CHECK` exigeix un valor no nul als nullables: tots són vàlids amb `NULL`. Un intent fallit (`robots_blocked`, `robots_unreachable`, `timeout`, `error`, `access_denied`…) es pot registrar sense HTTP, sense URL final, sense tipus de contingut, sense text i sense hash.
- Una fila `duplicate` pot tenir `text_content` i `text_length` a `NULL` i apuntar amb `reused_from_fetch_id` al registre amb el contingut: no cal copiar el text. El text es resol seguint aquesta referència. Cap restricció obliga que `duplicate` tingui `reused_from_fetch_id` ni que el registre referenciat sigui `ok`: ho ha de garantir el codi consumidor.

## Passos

1. **Exportació completa.** Feu una còpia completa de la BD (estructura i dades) abans de res i comproveu que es pot restaurar. Els volcats d'esquema de `backup/` no en fan de substitut (vegeu «Abast dels esquemes consultats»).
2. **Revisió.** Llegiu els tres scripts. Comproveu amb `02_verify.sql` (consulta I) que cap objecte té ja aquests noms. `01_apply.sql` també ho comprova i avorta si hi ha conflicte (no fa servir `IF NOT EXISTS`).
3. **Assaig en una còpia.** Restaureu l'exportació en una BD de proves (mateixa versió de PostgreSQL) i apliqueu-hi tot el procés: apply, verify i, si cal, rollback.
4. **Execució manual a pgAdmin.** Query Tool amb el rol propietari de `public.entries` (al volcat: `asimovw_admin`). Executeu abans les consultes A1 i A2 de `02_verify.sql` i deseu-ne el resultat. Després executeu `01_apply.sql` sencer. Si falla, feu `ROLLBACK;` i no continueu. El `COMMIT` és l'última sentència.
   - Agafa un bloqueig exclusiu breu sobre `entries` (5 s màxim, `lock_timeout`). Convé fer-ho sense pipeline ni batch en marxa.
5. **Verificació.** Executeu `02_verify.sql` consulta per consulta (pgAdmin només mostra l'última). Comproveu que A1 i A2 són idèntiques a les d'abans, que `discard_reason` i `analyzed_fetch_id` són `NULL` a totes les files i que la taula nova és buida. Els recomptes esperats són:
   - **B:** 17 columnes a la taula nova.
   - **C:** 11 restriccions: a la taula nova 1 PK, 2 FK i 6 `CHECK` (estat, longitud, HTTP, autoreutilització, hash i `text_source`); a `entries`, `chk_entries_discard_reason` i `entries_analyzed_fetch_id_fkey`.
   - **D:** 3 FK, totes amb `ON DELETE RESTRICT` (`entry_id`, `reused_from_fetch_id` i `entries.analyzed_fetch_id`).
   - **E:** 5 índexs més la PK a la taula nova. **E2:** els dos índexs d'URL són d'expressió `md5(...)` i no n'hi ha cap de directe. **E3:** 1 índex nou a `entries` (`idx_entries_analyzed_fetch_id`).
   - **F:** 2 columnes noves a `entries` i 0 valors a `discard_reason` i a `analyzed_fetch_id`.
6. **Rollback.** Només és segur mentre `entry_content_fetches` és buida i `discard_reason` i `analyzed_fetch_id` no tenen cap valor. Eliminar la taula perd l'historial d'intents i els textos obtinguts. Si ja hi ha dades, l'script avorta; no el forceu sense exportar-les i acordar-ho. Elimina primer `analyzed_fetch_id` (la seva FK depèn de la taula nova), després `discard_reason` i al final la taula. No fa `DROP ... CASCADE`.
7. **Codi consumidor.** Aplicar la migració no canvia el comportament: el pipeline, els prompts i el frontend no la fan servir. Desplegar el codi que escriu a la taula nova, a `discard_reason` i a `analyzed_fetch_id` és un pas posterior i separat.
8. **Abast.** Aquests scripts no modifiquen prompts ni dades històriques: no reclassifiquen ni reomplen cap descartada.

## Requisits abans d'activar el pipeline

Aquesta migració no els compleix; són condicions per al codi consumidor.

- **`DELETE /entries/{id}` ha de gestionar la violació de FK.** Amb `ON DELETE RESTRICT`, esborrar una entrada que tingui intents registrats fallarà. Amb `analyzed_fetch_id` hi ha una referència circular: `entry_content_fetches.entry_id` apunta a l'entrada i `entries.analyzed_fetch_id` pot apuntar a un dels seus intents. Per eliminar una entrada que tingui evidència caldria, en aquest ordre, posar `analyzed_fetch_id` a `NULL`, esborrar-ne els intents i esborrar l'entrada (o acordar una altra política). L'endpoint actual ([main.py:2213](../../app/main.py)) captura qualsevol excepció i respon 500 amb el text de l'error. Abans que el pipeline comenci a escriure a la taula nova, ha de capturar-la específicament (`psycopg2.errors.ForeignKeyViolation`) i retornar un error controlat (per exemple 409, amb un missatge clar). La política d'esborrat d'entrades amb evidència (esborrar-ne els intents, o `CASCADE`) s'ha d'acordar a part. L'endpoint no s'ha canviat.
- **Consultes per URL amb hash.** Vegeu la secció següent.
- **Integritat de la reutilització.** Que el registre referenciat tingui text (`ok`) i que no hi hagi cicles ni cadenes: ho ha de garantir el codi.

## Decisions i advertiments

- **FK `entry_id` → `entries(id)` amb `ON DELETE RESTRICT`.** No es perd evidència per cascada sense una política acordada. Vegeu el requisit anterior.
- **FK `reused_from_fetch_id` → mateixa taula amb `ON DELETE RESTRICT`.** No es pot eliminar un registre que algun altre reutilitza. El `CHECK` només impedeix l'autoreferència directa: cicles (A→B→A) i cadenes llargues no es poden impedir amb un `CHECK`.
- **FK `entries.analyzed_fetch_id` → `entry_content_fetches(id)` amb `ON DELETE RESTRICT`.** No es pot eliminar un registre d'obtenció que una entrada indica com a text analitzat. Cap restricció comprova que el registre referenciat sigui de la mateixa entrada ni que tingui text: ho ha de garantir el codi.
- **Sense unicitat** d'URL ni de hash.
- **`content_hash`.** El `CHECK` exigeix 64 caràcters hexadecimals en minúscula (SHA-256 complet).
- **Permisos.** No s'hi concedeix cap privilegi. Si l'API es connecta amb un rol diferent del propietari, haurà de rebre privilegis sobre la taula nova i la seva seqüència, de forma revisada. La consulta H de `02_verify.sql` ajuda a comprovar-ho.

### Índexs d'URL: `md5(normalized_url)` i `md5(final_url)`

Són índexs d'expressió, sense extensions. Un B-tree sobre la URL completa faria fallar l'`INSERT` amb URL de més d'uns 2.700 bytes; amb `md5()` la mida de l'índex és fixa.

El hash **no és una restricció d'unicitat ni una prova d'identitat**: dues URL diferents podrien, en teoria, tenir el mateix `md5`. Les consultes futures han de comparar sempre les dues coses:

```sql
WHERE md5(normalized_url) = md5($1) AND normalized_url = $1
WHERE md5(final_url)      = md5($1) AND final_url      = $1
```

- La primera condició és la que fa servir l'índex. Sense ella, la consulta fa un recorregut sencer.
- La segona és la que dona el resultat correcte. Sense ella, una col·lisió donaria una coincidència falsa.
- `md5(NULL)` és `NULL`: les files amb `final_url` nul no coincideixen mai.
- `md5` s'usa només per indexar, no com a mesura de seguretat.

`content_hash` (SHA-256) conserva un B-tree normal, perquè té mida fixa.
`reused_from_fetch_id` té un índex propi, que accelera la comprovació de la FK en esborrar.

## Punts pendents

No s'han imposat per no inventar-los:

- Si `duplicate` ha d'implicar `reused_from_fetch_id` i si aquest ha d'apuntar a una fila `ok`.
- Si `text_length` ha d'igualar la longitud de `text_content`.
- Que `entries.analyzed_fetch_id` apunti a un registre de la mateixa entrada (caldria una FK composta o una restricció addicional; no s'ha imposat).
- Política d'esborrat d'entrades amb intents registrats.
