-- =============================================================================
-- AsimovWatch - Fase 1B, pas 2
-- proves_entrades_fetch.sql: entrades de prova per al primer desplegament controlat de l'obtencio
--
-- ESTAT: PREPARAT, NO EXECUTAT.
--
-- Tres blocs independents (executeu-los d'un en un, seleccionant el bloc i F5):
--   1) INSERT de 3 entrades de prova            2) consulta per localitzar-les
--   3) neteja segura (nomes les del prefix de titol)
--
-- ABANS DE L'INSERT (bloc 1), SUBSTITUIU ELS PLACEHOLDERS:
--   PLACEHOLDER_URL_1, PLACEHOLDER_URL_2, PLACEHOLDER_URL_3   -> les URL reals d'article (https://...)
-- Mentre un canonical_url continuï sent PLACEHOLDER_URL_n NO es un URL http(s): l'entrada NO es
-- elegible per a l'obtencio i el pipeline nomes usaria el text del feed.
--
-- ATENCIO: son entrades RAW REALS. La cua d'enriquiment (cada 5 min, si l'scheduler esta actiu) les
-- agafara i les enviara a Input/Primary (cost d'LLM). Per al primer desplegament:
--   * fixeu CONTENT_FETCH_ENTRY_IDS amb els ids que dona el bloc 2, o
--   * feu servir una copia de la BD.
--
-- Eligibilitat (app/content_fetch_pipeline.py): ingest_method = 'rss', canonical_url http(s) no buit,
-- text del feed (raw_content o, si es buit, raw_snippet) < 400 caracters, domini no omes
-- (per defecte openai.com i fra.europa.eu), i, si CONTENT_FETCH_ENTRY_IDS no es buida, id inclos.
-- Les tres entrades tenen text de feed curt: 77, 315 i 0 (buit) caracters.
-- =============================================================================


-- =============================================================================
-- 1) INSERT de 3 entrades de prova (RAW, ingest_method = 'rss'). Es pot repetir sense duplicar:
--    cada fila nomes s'insereix si no existeix ja una entrada amb el mateix dedup_key.
-- =============================================================================
INSERT INTO public.entries (
    source_url, source_domain, source_title, source_type, source_language, ingest_method,
    external_id, canonical_url, published_date, detected_at,
    raw_snippet, raw_content, raw_content_format, raw_payload,
    review_status, dedup_key, ingested_at, updated_at, ingest_status, processing_status
)
SELECT
    v.source_url, 'zz-prova-fetch.example', v.source_title, 'company', 'en', 'rss',
    v.external_id, v.canonical_url, now(), now(),
    v.feed_text, v.feed_text, 'text', '{"zz_prova_fetch": true}'::jsonb,
    'NEW', v.dedup_key, now(), now(), 'ingested', 'RAW'
FROM (VALUES
    ('PLACEHOLDER_SOURCE_URL_1',
     'ZZ-PROVA FETCH 1 (feed curt, 77 caracters)',
     'zz-prova-fetch-1',
     'PLACEHOLDER_URL_1',
     'Resum curt del feed per provar l''obtencio del text de l''article (ZZ-PROVA 1).',
     'zz-prova-fetch-dedup-1'),
    ('PLACEHOLDER_SOURCE_URL_2',
     'ZZ-PROVA FETCH 2 (feed 315 caracters)',
     'zz-prova-fetch-2',
     'PLACEHOLDER_URL_2',
     'Resum del feed una mica mes llarg, pero encara per sota del llindar de 400 caracters, per comprovar que '
     'tambe es considera text insuficient i es prova l''obtencio. ' || repeat('Text de farciment. ', 8),
     'zz-prova-fetch-dedup-2'),
    ('PLACEHOLDER_SOURCE_URL_3',
     'ZZ-PROVA FETCH 3 (feed buit)',
     'zz-prova-fetch-3',
     'PLACEHOLDER_URL_3',
     '',
     'zz-prova-fetch-dedup-3')
) AS v(source_url, source_title, external_id, canonical_url, feed_text, dedup_key)
WHERE NOT EXISTS (
    SELECT 1 FROM public.entries e WHERE e.dedup_key = v.dedup_key
)
RETURNING id, source_title, canonical_url, processing_status, ingest_method, length(raw_content) AS feed_chars;


-- =============================================================================
-- 2) Localitzar-les (nomes lectura): entrades, i els intents d'obtencio que tinguin
-- =============================================================================
SELECT
    e.id,
    e.source_title,
    e.canonical_url,
    e.ingest_method,
    e.processing_status,
    e.processing_retries,
    length(coalesce(e.raw_content, e.raw_snippet, ''))  AS feed_chars,
    e.analyzed_fetch_id,
    e.discard_reason,
    e.ready_for_primary,
    (SELECT count(*) FROM public.entry_content_fetches f WHERE f.entry_id = e.id) AS intents
FROM public.entries e
WHERE e.source_title LIKE 'ZZ-PROVA FETCH%'
ORDER BY e.id;

-- Els intents d'obtencio d'aquestes entrades (mes recents primer):
SELECT
    f.id, f.entry_id, f.fetch_status, f.http_status, f.fetched_at, f.text_length, f.text_source,
    f.reused_from_fetch_id, f.error_detail
FROM public.entry_content_fetches f
JOIN public.entries e ON e.id = f.entry_id
WHERE e.source_title LIKE 'ZZ-PROVA FETCH%'
ORDER BY f.fetched_at DESC, f.id DESC;


-- =============================================================================
-- 3) Neteja SEGURA. Nomes afecta les entrades amb el prefix 'ZZ-PROVA FETCH' al titol i les seves
--    files d'entry_content_fetches. Ordre:
--      a) entries.analyzed_fetch_id -> NULL
--      b) esborrar PRIMER les files 'duplicate' i DESPRES la resta de files d'aquestes entrades
--      c) esborrar les entrades
--    Una transaccio: si una salvaguarda falla, no s'esborra res. Executeu el bloc sencer.
-- =============================================================================
BEGIN;

-- Salvaguarda 1: com a maxim 20 entrades amb el prefix (evita que un canvi de patro n'esborri moltes)
DO $guard_count$
DECLARE n integer;
BEGIN
    SELECT count(*) INTO n FROM public.entries WHERE source_title LIKE 'ZZ-PROVA FETCH%';
    IF n > 20 THEN
        RAISE EXCEPTION 'NETEJA ABORTADA: % entrades amb el prefix (maxim 20)', n;
    END IF;
END
$guard_count$;

-- Salvaguarda 2: cap fila d'una entrada que NO es de prova pot reutilitzar (reused_from_fetch_id) ni apuntar
-- (analyzed_fetch_id) a una fila d'una entrada de prova: l'esborrat fallaria per la FK RESTRICT.
DO $guard_refs$
DECLARE n integer;
BEGIN
    SELECT count(*) INTO n
    FROM public.entry_content_fetches child
    JOIN public.entry_content_fetches parent ON parent.id = child.reused_from_fetch_id
    JOIN public.entries pe ON pe.id = parent.entry_id AND pe.source_title LIKE 'ZZ-PROVA FETCH%'
    JOIN public.entries ce ON ce.id = child.entry_id AND ce.source_title NOT LIKE 'ZZ-PROVA FETCH%';
    IF n > 0 THEN
        RAISE EXCEPTION 'NETEJA ABORTADA: % fila(es) d''entrades que NO son de prova reutilitzen files de les entrades de prova', n;
    END IF;

    SELECT count(*) INTO n
    FROM public.entries other
    JOIN public.entry_content_fetches f ON f.id = other.analyzed_fetch_id
    JOIN public.entries owner ON owner.id = f.entry_id AND owner.source_title LIKE 'ZZ-PROVA FETCH%'
    WHERE other.source_title NOT LIKE 'ZZ-PROVA FETCH%';
    IF n > 0 THEN
        RAISE EXCEPTION 'NETEJA ABORTADA: % entrada(es) que NO son de prova apunten (analyzed_fetch_id) a files de prova', n;
    END IF;
END
$guard_refs$;

-- a) desenllaçar
UPDATE public.entries
   SET analyzed_fetch_id = NULL
 WHERE source_title LIKE 'ZZ-PROVA FETCH%'
   AND analyzed_fetch_id IS NOT NULL;

-- b1) primer les files 'duplicate' (apunten a files 'ok' que despres s'esborraran)
DELETE FROM public.entry_content_fetches
 WHERE fetch_status = 'duplicate'
   AND entry_id IN (SELECT id FROM public.entries WHERE source_title LIKE 'ZZ-PROVA FETCH%');

-- b2) la resta de files d'aquestes entrades
DELETE FROM public.entry_content_fetches
 WHERE entry_id IN (SELECT id FROM public.entries WHERE source_title LIKE 'ZZ-PROVA FETCH%');

-- c) les entrades de prova
DELETE FROM public.entries
 WHERE source_title LIKE 'ZZ-PROVA FETCH%'
RETURNING id, source_title;

COMMIT;
-- Si alguna sentencia falla, feu ROLLBACK; (no s'haura esborrat res).
