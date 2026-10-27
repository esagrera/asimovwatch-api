-- =============================================================================
-- AsimovWatch - Fase 1B, pas 2
-- 05_validacio_integracio.sql: validacio de la SQL de la integracio de l'obtencio de contingut
--
-- ESTAT: PREPARAT, NO EXECUTAT. Fet per executar-lo a una COPIA de la BD (o, com a minim,
-- amb l'estructura del pas 0 ja aplicada).
--
-- COM S'EXECUTA: cada apartat (a, b, c, d) es INDEPENDENT. Executeu-los UN PER UN a pgAdmin
-- (seleccioneu-ne el bloc i premeu F5). Els apartats b i c acaben EXPRES amb RAISE EXCEPTION:
-- aixo desfa tot el que han fet (mateixa tecnica que el smoke). Si es veu l'error
--     VAL_OK ...      la validacio ha anat be (i no s'ha canviat res);
--     VAL_FAIL ...    una comprovacio ha fallat (tampoc no s'ha canviat res);
-- qualsevol altre error es un problema real de la SQL. Si executeu tot el fitxer d'una vegada,
-- pgAdmin s'aturara a la primera excepcio: no ho feu.
--
-- Efectes que NO es desfan: els valors de les sequencies (entry_content_fetches_id_seq).
-- Els blocs b i c fan UPDATE temporals a UNA entrada existent i els desfan amb l'excepcio.
--
-- Cada bloc usa target_id := NULL = "la primera entrada (id mes petit)". Canvieu-ho per un id
-- concret si voleu. Mantingueu el text de la consulta de l'apartat (a) sincronitzat amb
-- app/enrichment_queue.py (_load_candidate_entries, variant amb CONTENT_FETCH_ENABLED).
-- =============================================================================


-- =============================================================================
-- (a) Consulta de seleccio de la cua amb l'interruptor encès, amb literals
--     (retry_max = 3, 3600 s). NOMES SELECT, amb LIMIT 5.
--     Esperat: les entrades RAW (o ERROR amb menys de 3 reintents) SENSE un intent d'obtencio
--     TEMPORAL fallat de fa menys d'1 hora.
-- =============================================================================
SELECT
    id,
    source_domain,
    detected_at,
    processing_status,
    processing_error,
    processing_retries,
    raw_content,
    raw_snippet
FROM public.entries
WHERE (
        processing_status = 'RAW'
     OR (
            processing_status = 'ERROR'
        AND COALESCE(processing_retries, 0) < 3
     )
      )
  AND NOT EXISTS (
        SELECT 1
        FROM public.entry_content_fetches f
        WHERE f.entry_id = public.entries.id
          AND f.fetched_at > NOW() - (3600 * INTERVAL '1 second')
          AND (
                f.fetch_status IN ('timeout', 'robots_unreachable')
             OR (
                    f.fetch_status = 'http_error'
                AND (f.http_status = 429 OR f.http_status BETWEEN 500 AND 599)
                )
              )
      )
ORDER BY detected_at ASC NULLS LAST, id ASC
LIMIT 5;


-- =============================================================================
-- (b) Una fila temporal d'entry_content_fetches exclou (o no) l'entrada de la consulta de (a).
--     Per a una entrada existent: es posa temporalment RAW, es comprova que SURT a la consulta,
--     i per a cada escenari s'insereix una fila i es comprova si surt o no. Acaba amb
--     RAISE EXCEPTION 'VAL_OK ...' perque es desfaci tot.
--
--     Escenaris (estat, HTTP, antiguitat en minuts -> ha de sortir a la consulta?):
--       timeout 0 min -> NO (exclosa)        timeout 59 -> NO        timeout 60 -> SI (limit exacte)
--       timeout 61 -> SI                     timeout 120 (fa 2 hores) -> SI
--       robots_unreachable 0 -> NO
--       http_error 503 / 429 / 599, 0 min -> NO
--       http_error 404 / 499, 0 min -> SI (no es temporal)
--       access_denied 403, ok 200, text_too_short 200, error, pdf_unprocessed -> SI (no son temporals)
-- =============================================================================
DO $val_b$
DECLARE
    target_id integer := NULL;           -- NULL = la primera entrada (id mes petit)
    q text := $q$
        SELECT id, source_domain, detected_at, processing_status, processing_error, processing_retries,
               raw_content, raw_snippet
        FROM public.entries
        WHERE (
                processing_status = 'RAW'
             OR (
                    processing_status = 'ERROR'
                AND COALESCE(processing_retries, 0) < 3
             )
              )
          AND NOT EXISTS (
                SELECT 1
                FROM public.entry_content_fetches f
                WHERE f.entry_id = public.entries.id
                  AND f.fetched_at > NOW() - (3600 * INTERVAL '1 second')
                  AND (
                        f.fetch_status IN ('timeout', 'robots_unreachable')
                     OR (
                            f.fetch_status = 'http_error'
                        AND (f.http_status = 429 OR f.http_status BETWEEN 500 AND 599)
                        )
                      )
              )
    $q$;
    sc record;
    n integer;
    verified integer := 0;
BEGIN
    IF target_id IS NULL THEN
        SELECT id INTO target_id FROM public.entries ORDER BY id LIMIT 1;
    END IF;
    IF target_id IS NULL THEN
        RAISE EXCEPTION 'VAL_FAIL b: no hi ha cap entrada a public.entries';
    END IF;

    -- Temporal (es desfa amb l'excepcio final): l'entrada passa a ser candidata de la cua
    UPDATE public.entries
       SET processing_status = 'RAW', processing_error = NULL, processing_retries = 0
     WHERE id = target_id;

    EXECUTE format('SELECT count(*) FROM (%s) c WHERE c.id = %s', q, target_id) INTO n;
    IF n <> 1 THEN
        RAISE EXCEPTION 'VAL_FAIL b: l''entrada % no surt a la consulta abans de cap intent (n=%)', target_id, n;
    END IF;

    FOR sc IN
        SELECT * FROM (VALUES
            ('timeout',            NULL::integer,   0, false),
            ('timeout',            NULL::integer,  59, false),
            ('timeout',            NULL::integer,  60, true),
            ('timeout',            NULL::integer,  61, true),
            ('timeout',            NULL::integer, 120, true),
            ('robots_unreachable', NULL::integer,   0, false),
            ('http_error',         503,             0, false),
            ('http_error',         429,             0, false),
            ('http_error',         599,             0, false),
            ('http_error',         404,             0, true),
            ('http_error',         499,             0, true),
            ('access_denied',      403,             0, true),
            ('ok',                 200,             0, true),
            ('text_too_short',     200,             0, true),
            ('error',              NULL::integer,   0, true),
            ('pdf_unprocessed',    200,             0, true)
        ) AS t(st, http, age_min, expect_in_query)
    LOOP
        INSERT INTO public.entry_content_fetches
            (entry_id, requested_url, normalized_url, fetch_status, http_status, fetched_at, user_agent)
        VALUES
            (target_id, 'https://val.invalid/b', 'https://val.invalid/b', sc.st, sc.http,
             now() - (sc.age_min * INTERVAL '1 minute'), 'VAL/1.0');

        EXECUTE format('SELECT count(*) FROM (%s) c WHERE c.id = %s', q, target_id) INTO n;
        IF (n = 1) IS DISTINCT FROM sc.expect_in_query THEN
            RAISE EXCEPTION 'VAL_FAIL b: estat=% http=% antiguitat=% min: esperat surt=% i surt=% (n=%)',
                sc.st, sc.http, sc.age_min, sc.expect_in_query, (n = 1), n;
        END IF;

        DELETE FROM public.entry_content_fetches
         WHERE entry_id = target_id AND requested_url = 'https://val.invalid/b';
        verified := verified + 1;
    END LOOP;

    -- Una fila antiga (2 h) i una de recent (0 min) a la vegada: l'entrada continua exclosa
    INSERT INTO public.entry_content_fetches
        (entry_id, requested_url, normalized_url, fetch_status, http_status, fetched_at, user_agent)
    VALUES
        (target_id, 'https://val.invalid/b', 'https://val.invalid/b', 'timeout', NULL, now() - INTERVAL '2 hours', 'VAL/1.0'),
        (target_id, 'https://val.invalid/b', 'https://val.invalid/b', 'timeout', NULL, now(), 'VAL/1.0');
    EXECUTE format('SELECT count(*) FROM (%s) c WHERE c.id = %s', q, target_id) INTO n;
    IF n <> 0 THEN
        RAISE EXCEPTION 'VAL_FAIL b: amb un intent recent (a mes d''un d''antic) l''entrada hauria d''estar exclosa (n=%)', n;
    END IF;
    verified := verified + 1;

    RAISE EXCEPTION 'VAL_OK b: entrada %, % escenaris verificats; res no s''ha canviat (rollback per excepcio)', target_id, verified;
END
$val_b$;


-- =============================================================================
-- (c) Les sentencies d'escriptura del pipeline: entries.analyzed_fetch_id i entries.discard_reason
--     ('out_of_scope' / 'fetch_failed' / 'no_content'), amb els valors reals i les restriccions.
--     Es fan sobre UNA entrada existent (temporalment) i acaba amb RAISE EXCEPTION 'VAL_OK ...'.
--
--     Les sentencies son les de app/crawler.py amb literals en lloc de %s:
--       - enllaç:            UPDATE public.entries SET analyzed_fetch_id = <id> WHERE id = <entry>
--       - 'out_of_scope':    descart d'Input 'no' (UPDATE d'Input + discard_reason)
--       - 'fetch_failed':    descart per fallada d'obtencio (NO toca ready_for_primary ni camps d'Input)
--       - 'no_content':      descart per contingut buit sense intent d'obtencio (ready_for_primary = 'no', com avui)
--     Tambe es comprova que la BD REBUTJA un analyzed_fetch_id inexistent (FK) i un discard_reason invalid (CHECK).
-- =============================================================================
DO $val_c$
DECLARE
    target_id integer := NULL;           -- NULL = la primera entrada (id mes petit)
    fid bigint;
    v_status text;
    v_reason text;
    v_ready text;
    v_relevance text;
    v_link bigint;
    rejected boolean;
BEGIN
    IF target_id IS NULL THEN
        SELECT id INTO target_id FROM public.entries ORDER BY id LIMIT 1;
    END IF;
    IF target_id IS NULL THEN
        RAISE EXCEPTION 'VAL_FAIL c: no hi ha cap entrada a public.entries';
    END IF;

    -- Estat inicial temporal: RAW, sense camps d'Input ni motiu (es desfa amb l'excepcio final)
    UPDATE public.entries
       SET processing_status = 'RAW', processing_error = NULL, ready_for_primary = NULL,
           input_relevance = NULL, input_quality = NULL, discard_reason = NULL, analyzed_fetch_id = NULL
     WHERE id = target_id;

    -- Una fila d'obtencio 'ok' a la qual enllaçar
    INSERT INTO public.entry_content_fetches
        (entry_id, requested_url, normalized_url, fetch_status, http_status, fetched_at, text_content,
         text_length, content_hash, text_source, user_agent)
    VALUES
        (target_id, 'https://val.invalid/c', 'https://val.invalid/c', 'ok', 200, now(),
         'Text de validacio', 17, repeat('a', 64), 'html_main', 'VAL/1.0')
    RETURNING id INTO fid;

    -- 1) analyzed_fetch_id (l'UPDATE minim del pipeline)
    UPDATE public.entries SET analyzed_fetch_id = fid WHERE id = target_id;
    SELECT analyzed_fetch_id INTO v_link FROM public.entries WHERE id = target_id;
    IF v_link IS DISTINCT FROM fid THEN
        RAISE EXCEPTION 'VAL_FAIL c: analyzed_fetch_id no s''ha desat (esperat %, llegit %)', fid, v_link;
    END IF;

    -- 1b) la FK rebutja un id inexistent
    rejected := false;
    BEGIN
        UPDATE public.entries SET analyzed_fetch_id = -1 WHERE id = target_id;
    EXCEPTION WHEN foreign_key_violation THEN
        rejected := true;
    END;
    IF NOT rejected THEN
        RAISE EXCEPTION 'VAL_FAIL c: la FK d''analyzed_fetch_id hauria de rebutjar un id inexistent';
    END IF;

    -- 2) 'out_of_scope': descart d'Input 'no' (UPDATE d'Input amb discard_reason)
    UPDATE public.entries SET
        processing_status = 'DISCARDED',
        processing_error = NULL,
        input_relevance = 'low',
        input_relevance_reason = 'VAL',
        ready_for_primary = 'no',
        raw_snippet_original = COALESCE(raw_snippet_original, 'VAL'),
        input_quality = 'low',
        input_quality_notes = 'VAL',
        discard_reason = 'out_of_scope',
        updated_at = NOW()
    WHERE id = target_id;
    SELECT processing_status, discard_reason, ready_for_primary INTO v_status, v_reason, v_ready
      FROM public.entries WHERE id = target_id;
    IF v_status <> 'DISCARDED' OR v_reason IS DISTINCT FROM 'out_of_scope' OR v_ready IS DISTINCT FROM 'no' THEN
        RAISE EXCEPTION 'VAL_FAIL c: out_of_scope (estat=%, motiu=%, ready=%)', v_status, v_reason, v_ready;
    END IF;

    -- 3) 'fetch_failed': no ha d'escriure ready_for_primary='no' ni camps d'Input
    UPDATE public.entries
       SET processing_status = 'RAW', discard_reason = NULL, ready_for_primary = NULL, input_relevance = NULL
     WHERE id = target_id;
    UPDATE public.entries
    SET
        processing_status = 'DISCARDED',
        processing_error = NULL,
        discard_reason = 'fetch_failed',
        updated_at = NOW()
    WHERE id = target_id;
    SELECT processing_status, discard_reason, ready_for_primary, input_relevance
      INTO v_status, v_reason, v_ready, v_relevance
      FROM public.entries WHERE id = target_id;
    IF v_status <> 'DISCARDED' OR v_reason IS DISTINCT FROM 'fetch_failed'
       OR v_ready IS NOT NULL OR v_relevance IS NOT NULL THEN
        RAISE EXCEPTION 'VAL_FAIL c: fetch_failed (estat=%, motiu=%, ready=%, input_relevance=%)',
            v_status, v_reason, v_ready, v_relevance;
    END IF;

    -- 4) 'no_content': contingut buit sense intent d'obtencio (com avui, amb discard_reason)
    UPDATE public.entries
       SET processing_status = 'RAW', discard_reason = NULL, ready_for_primary = NULL, input_relevance = NULL
     WHERE id = target_id;
    UPDATE public.entries
    SET
        processing_status = 'DISCARDED',
        processing_error = NULL,
        input_relevance = 'unknown',
        input_relevance_reason = 'No hi ha contingut RSS analitzable.',
        ready_for_primary = 'no',
        input_quality = 'low',
        input_quality_notes = 'El feed no ha aportat summary, description ni content.',
        discard_reason = 'no_content',
        updated_at = NOW()
    WHERE id = target_id;
    SELECT processing_status, discard_reason, ready_for_primary INTO v_status, v_reason, v_ready
      FROM public.entries WHERE id = target_id;
    IF v_status <> 'DISCARDED' OR v_reason IS DISTINCT FROM 'no_content' OR v_ready IS DISTINCT FROM 'no' THEN
        RAISE EXCEPTION 'VAL_FAIL c: no_content (estat=%, motiu=%, ready=%)', v_status, v_reason, v_ready;
    END IF;

    -- 5) el CHECK de discard_reason rebutja un valor fora de la llista
    rejected := false;
    BEGIN
        UPDATE public.entries SET discard_reason = 'invalid' WHERE id = target_id;
    EXCEPTION WHEN check_violation THEN
        rejected := true;
    END;
    IF NOT rejected THEN
        RAISE EXCEPTION 'VAL_FAIL c: el CHECK de discard_reason hauria de rebutjar ''invalid''';
    END IF;

    RAISE EXCEPTION 'VAL_OK c: entrada %, fila d''obtencio %; enllaç, FK, out_of_scope, fetch_failed, no_content i CHECK verificats; res no s''ha canviat (rollback per excepcio)',
        target_id, fid;
END
$val_c$;


-- =============================================================================
-- (d) Quantes files hi ha per a cada fetch_status (nomes lectura). Els 11 estats del CHECK
--     surten sempre, tambe amb 0 files.
-- =============================================================================
SELECT
    s.fetch_status,
    count(f.id)        AS files,
    min(f.fetched_at)  AS mes_antiga,
    max(f.fetched_at)  AS mes_recent
FROM (VALUES
        ('ok'), ('robots_blocked'), ('robots_unreachable'), ('access_denied'), ('anti_bot_challenge'),
        ('text_too_short'), ('timeout'), ('http_error'), ('error'), ('pdf_unprocessed'), ('duplicate')
     ) AS s(fetch_status)
LEFT JOIN public.entry_content_fetches f ON f.fetch_status = s.fetch_status
GROUP BY s.fetch_status
ORDER BY files DESC, s.fetch_status;
