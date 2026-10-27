-- =============================================================================
-- AsimovWatch - Fase 1B, pas 0
-- 02_verify.sql: comprovacions NOMES DE LECTURA (cap INSERT/UPDATE/DELETE/DDL)
--
-- Us: executeu les consultes A1 i A2 ABANS d'aplicar 01_apply.sql i guardeu el
-- resultat; despres d'aplicar-lo executeu-les de nou i compareu. La resta
-- (B..F) nomes te sentit despres d'aplicar.
--
-- pgAdmin nomes mostra el resultat de l'ULTIMA sentencia d'un script: executeu
-- cada consulta per separat (seleccioneu-la i premeu F5).
-- =============================================================================

-- -----------------------------------------------------------------------------
-- A1. Recompte i distribucio d'estats (abans i despres: han de ser identics)
-- -----------------------------------------------------------------------------
SELECT processing_status,
       review_status,
       count(*) AS n
FROM public.entries
GROUP BY ROLLUP (processing_status, review_status)
ORDER BY processing_status NULLS LAST, review_status NULLS LAST;

-- -----------------------------------------------------------------------------
-- A2. Empremta de les dades historiques (abans i despres: ha de ser identica).
--     No inclou discard_reason ni analyzed_fetch_id, que no existeixen abans d'aplicar.
-- -----------------------------------------------------------------------------
SELECT count(*)                                    AS entries_total,
       max(id)                                     AS max_id,
       max(updated_at)                             AS max_updated_at,
       md5(string_agg(concat_ws('|',
               id, processing_status, review_status, processing_retries,
               coalesce(ready_for_primary, '~'), coalesce(input_relevance, '~'),
               coalesce(canonical_url, '~'),
               md5(coalesce(raw_content, '')), md5(coalesce(raw_snippet, ''))
           ), E'\n' ORDER BY id))                  AS fingerprint
FROM public.entries;

-- -----------------------------------------------------------------------------
-- B. Taula nova: columnes, tipus, nullabilitat i valors per defecte
--    Esperat: 17 files. Han de ser NOT NULL nomes: id, entry_id, requested_url,
--    normalized_url, fetch_status, fetched_at, truncated i user_agent.
--    Han de ser nullable: final_url, http_status, content_type, text_content,
--    text_length, content_hash, text_source, error_detail i reused_from_fetch_id
--    (els intents fallits i els duplicate no tenen ni text ni, de vegades, HTTP).
-- -----------------------------------------------------------------------------
SELECT a.attnum                                    AS pos,
       a.attname                                   AS columna,
       format_type(a.atttypid, a.atttypmod)        AS tipus,
       CASE WHEN a.attnotnull THEN 'NOT NULL' ELSE 'nullable' END AS nullabilitat,
       pg_get_expr(d.adbin, d.adrelid)             AS valor_per_defecte
FROM pg_attribute a
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE a.attrelid = to_regclass('public.entry_content_fetches')
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;

-- -----------------------------------------------------------------------------
-- C. Restriccions (PK, FK, CHECK) de la taula nova i de les columnes noves d'entries
--    Esperat a la taula nova: 1 PK, 2 FK, 6 CHECK (status, text_length, http_status,
--    not_self_reuse, content_hash, text_source).
--    A entries: chk_entries_discard_reason (CHECK) i entries_analyzed_fetch_id_fkey (FK).
--    Total: 11 files.
-- -----------------------------------------------------------------------------
SELECT conrelid::regclass AS taula,
       conname,
       CASE contype WHEN 'p' THEN 'PRIMARY KEY' WHEN 'f' THEN 'FOREIGN KEY'
                    WHEN 'c' THEN 'CHECK' WHEN 'u' THEN 'UNIQUE' ELSE contype::text END AS tipus,
       pg_get_constraintdef(oid) AS definicio
FROM pg_constraint
WHERE conrelid = to_regclass('public.entry_content_fetches')
   OR (conrelid = to_regclass('public.entries')
       AND conname IN ('chk_entries_discard_reason', 'entries_analyzed_fetch_id_fkey'))
ORDER BY conrelid::regclass::text, contype, conname;

-- -----------------------------------------------------------------------------
-- D. Claus foranes: accio ON DELETE (confdeltype: 'r' = RESTRICT, 'a' = NO ACTION,
--    'c' = CASCADE). Esperat: 3 files, totes amb 'r':
--      entry_content_fetches.entry_id             -> entries(id)
--      entry_content_fetches.reused_from_fetch_id -> entry_content_fetches(id)
--      entries.analyzed_fetch_id                  -> entry_content_fetches(id)
-- -----------------------------------------------------------------------------
SELECT conname,
       conrelid::regclass  AS taula,
       confrelid::regclass AS referencia,
       confdeltype         AS on_delete,
       confupdtype         AS on_update
FROM pg_constraint
WHERE contype = 'f'
  AND (conrelid = to_regclass('public.entry_content_fetches')
       OR (conrelid = to_regclass('public.entries') AND conname = 'entries_analyzed_fetch_id_fkey'))
ORDER BY conrelid::regclass::text, conname;

-- -----------------------------------------------------------------------------
-- E. Indexos de la taula nova. Esperat: pkey + 5 indexs, cap UNIQUE llevat de la PK:
--      idx_entry_content_fetches_entry_fetched        (entry_id, fetched_at DESC)
--      idx_entry_content_fetches_normalized_url_md5   (md5(normalized_url))
--      idx_entry_content_fetches_final_url_md5        (md5(final_url))
--      idx_entry_content_fetches_content_hash         (content_hash)
--      idx_entry_content_fetches_reused_from          (reused_from_fetch_id)
-- -----------------------------------------------------------------------------
SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'public' AND tablename = 'entry_content_fetches'
ORDER BY indexname;

-- E2. Els dos indexs d'URL han de ser d'expressio md5(...) i NO B-tree sobre la URL
--     completa. Esperat: 2 files amb expressio_md5 = true i 0 indexs directes.
SELECT indexname,
       indexdef LIKE '%(md5(%' AS expressio_md5
FROM pg_indexes
WHERE schemaname = 'public' AND tablename = 'entry_content_fetches'
  AND indexname IN ('idx_entry_content_fetches_normalized_url_md5',
                    'idx_entry_content_fetches_final_url_md5');

SELECT indexname, indexdef AS index_directe_inesperat
FROM pg_indexes
WHERE schemaname = 'public' AND tablename = 'entry_content_fetches'
  AND (indexdef LIKE '%USING btree (normalized_url)%' OR indexdef LIKE '%USING btree (final_url)%');

-- E3. Index nou a entries. Esperat: 1 fila (idx_entries_analyzed_fetch_id, btree sobre
--     analyzed_fetch_id, no UNIQUE).
SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'public' AND tablename = 'entries'
  AND indexname = 'idx_entries_analyzed_fetch_id';

-- -----------------------------------------------------------------------------
-- F. discard_reason i analyzed_fetch_id: columnes, i absencia d'omplert
-- -----------------------------------------------------------------------------
-- F1. Definicio de les columnes. Esperat: 2 files, nullable, sense valor per defecte:
--     discard_reason (text) i analyzed_fetch_id (bigint).
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'entries'
  AND column_name IN ('discard_reason', 'analyzed_fetch_id')
ORDER BY column_name;

-- F2. Distribucio de discard_reason per estat. Esperat despres d'aplicar:
--     discard_reason NULL a TOTES les files (incloses les DISCARDED).
SELECT processing_status,
       discard_reason,
       count(*) AS n
FROM public.entries
GROUP BY processing_status, discard_reason
ORDER BY processing_status, discard_reason NULLS FIRST;

-- F3. Files amb discard_reason o analyzed_fetch_id informats. Esperat: 0 i 0 fins que
--     el codi nou no s'executi (la migracio no omple cap de les dues).
SELECT count(*) FILTER (WHERE discard_reason IS NOT NULL)    AS entries_amb_discard_reason,
       count(*) FILTER (WHERE analyzed_fetch_id IS NOT NULL) AS entries_amb_analyzed_fetch_id
FROM public.entries;

-- -----------------------------------------------------------------------------
-- G. Taula nova buida i sense text. Esperat: 0 files fins que el codi nou no s'executi.
-- -----------------------------------------------------------------------------
SELECT count(*) AS fetches_total,
       count(text_content) AS amb_text
FROM public.entry_content_fetches;

-- -----------------------------------------------------------------------------
-- H. Propietari i privilegis (informatiu). Si l'API es connecta amb un rol
--    diferent del propietari, caldra concedir-li privilegis de forma explicita
--    i revisada; aquest script no en concedeix cap.
-- -----------------------------------------------------------------------------
SELECT c.relname,
       pg_get_userbyid(c.relowner) AS propietari,
       c.relacl                    AS privilegis
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relname IN ('entries', 'entry_content_fetches', 'entry_content_fetches_id_seq')
ORDER BY c.relname;

-- -----------------------------------------------------------------------------
-- I. Informatiu previ a aplicar: hi ha ja cap objecte amb aquests noms?
--    Esperat ABANS d'aplicar: 0 files. (El preflight de 01_apply.sql ho torna a comprovar.)
-- -----------------------------------------------------------------------------
SELECT c.relname, c.relkind
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND (c.relname LIKE 'entry\_content\_fetches%'
       OR c.relname LIKE 'idx\_entry\_content\_fetches%'
       OR c.relname = 'idx_entries_analyzed_fetch_id');

SELECT column_name
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'entries'
  AND column_name IN ('discard_reason', 'analyzed_fetch_id');
--  (esperat ABANS d'aplicar: 0 files)

SELECT conname
FROM pg_constraint
WHERE conrelid = to_regclass('public.entries')
  AND conname IN ('chk_entries_discard_reason', 'entries_analyzed_fetch_id_fkey');
--  (esperat ABANS d'aplicar: 0 files)
