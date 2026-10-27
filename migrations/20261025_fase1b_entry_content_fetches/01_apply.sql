-- =============================================================================
-- AsimovWatch - Fase 1B, pas 0
-- 01_apply.sql: taula public.entry_content_fetches + public.entries.discard_reason
--
-- ESTAT: PREPARAT, NO EXECUTAT. Revisar abans d'aplicar (vegeu README.md).
-- Objectiu: PostgreSQL 12 (esquema revisat: 12.22). Sense extensions ni enums.
--
-- Que fa:
--   1. Comprova (sense IF NOT EXISTS) que no hi ha cap element preexistent amb
--      els mateixos noms. Si n'hi ha, avorta i llista el conflicte.
--   2. Crea public.entry_content_fetches (un registre per intent d'obtencio).
--   3. Afegeix public.entries.discard_reason (nullable, amb CHECK).
--   4. Afegeix public.entries.analyzed_fetch_id (nullable, FK a la taula nova
--      ON DELETE RESTRICT, amb index). Es crea DESPRES de la taula nova.
--
-- Que NO fa:
--   - No modifica cap fila existent: les files d'entries queden amb
--     discard_reason = NULL (= motiu historic no registrat) i
--     analyzed_fetch_id = NULL (= text del feed o historia anterior).
--   - NO OMPLE analyzed_fetch_id ni discard_reason: les dues columnes queden a
--     NULL a totes les files. Ho omplira, mes endavant i en un pas separat, el
--     codi consumidor.
--   - No toca raw_content, raw_snippet, processing_status, ready_for_primary,
--     processing_retries ni cap altra columna existent.
--   - No dona permisos, no canvia propietaris, no crea extensions.
--
-- REQUISIT ABANS D'ACTIVAR EL PIPELINE (no forma part d'aquest script):
--   les dues FK son ON DELETE RESTRICT. L'endpoint DELETE /entries/{id} ha de
--   gestionar la violacio de FK amb un error controlat (no un 500) abans que
--   el pipeline comenci a escriure a la taula nova. Vegeu README.md.
--
-- Execucio: pgAdmin Query Tool, tot l'script d'una vegada, amb el rol
-- propietari de public.entries (a l'esquema revisat: asimovw_admin).
-- Si falla alguna sentencia, la transaccio queda abortada: executeu ROLLBACK;
-- i reviseu l'error. No s'ha aplicat res fins que no s'executa el COMMIT final.
-- =============================================================================

BEGIN;

-- Si no s'aconsegueix el bloqueig de entries en 5 s, falla en lloc d'esperar
-- indefinidament mentre el pipeline o l'API tenen la taula oberta.
SET LOCAL lock_timeout = '5s';

-- -----------------------------------------------------------------------------
-- 1. Comprovacions previes (no modifiquen res)
-- -----------------------------------------------------------------------------
DO $preflight$
DECLARE
    conflicts text[] := ARRAY[]::text[];
    r record;
BEGIN
    RAISE NOTICE 'Rol: %, base de dades: %, versio: %',
        current_user, current_database(), current_setting('server_version');

    IF current_setting('server_version_num')::int < 120000 THEN
        RAISE EXCEPTION 'Aquest script esta preparat per a PostgreSQL 12 o superior (versio actual: %).',
            current_setting('server_version');
    END IF;

    -- public.entries ha d'existir amb id integer i clau primaria sobre id
    IF to_regclass('public.entries') IS NULL THEN
        RAISE EXCEPTION 'No existeix public.entries.';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_attribute a
        WHERE a.attrelid = 'public.entries'::regclass
          AND a.attname = 'id'
          AND NOT a.attisdropped
          AND a.atttypid = 'integer'::regtype
    ) THEN
        RAISE EXCEPTION 'public.entries.id no es de tipus integer: la FK entry_id (integer) no encaixaria.';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.conrelid = 'public.entries'::regclass
          AND c.contype = 'p'
          AND c.conkey = ARRAY[(SELECT attnum FROM pg_attribute
                                WHERE attrelid = 'public.entries'::regclass AND attname = 'id')]
    ) THEN
        RAISE EXCEPTION 'public.entries.id no es la clau primaria (o la PK es composta).';
    END IF;

    -- Columna i CHECK de discard_reason
    IF EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = 'public.entries'::regclass
          AND attname = 'discard_reason' AND NOT attisdropped
    ) THEN
        conflicts := conflicts || 'columna public.entries.discard_reason ja existeix';
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.entries'::regclass
          AND conname = 'chk_entries_discard_reason'
    ) THEN
        conflicts := conflicts || 'constraint chk_entries_discard_reason ja existeix a public.entries';
    END IF;

    -- Columna, FK i index d'analyzed_fetch_id
    IF EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = 'public.entries'::regclass
          AND attname = 'analyzed_fetch_id' AND NOT attisdropped
    ) THEN
        conflicts := conflicts || 'columna public.entries.analyzed_fetch_id ja existeix';
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.entries'::regclass
          AND conname = 'entries_analyzed_fetch_id_fkey'
    ) THEN
        conflicts := conflicts || 'constraint entries_analyzed_fetch_id_fkey ja existeix a public.entries';
    END IF;

    -- Taula, sequencia i indexs (tots comparteixen espai de noms a pg_class)
    FOR r IN
        SELECT c.relname, c.relkind
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname IN (
              'entry_content_fetches',
              'entry_content_fetches_id_seq',
              'entry_content_fetches_pkey',
              'idx_entry_content_fetches_entry_fetched',
              'idx_entry_content_fetches_normalized_url_md5',
              'idx_entry_content_fetches_final_url_md5',
              'idx_entry_content_fetches_content_hash',
              'idx_entry_content_fetches_reused_from',
              'idx_entries_analyzed_fetch_id'
          )
    LOOP
        conflicts := conflicts || format('public.%s ja existeix (relkind=%s)', r.relname, r.relkind);
    END LOOP;

    IF array_length(conflicts, 1) IS NOT NULL THEN
        RAISE EXCEPTION E'Conflictes amb l''estructura preexistent; no s''aplica res:\n - %',
            array_to_string(conflicts, E'\n - ');
    END IF;
END
$preflight$;

-- -----------------------------------------------------------------------------
-- 2. Taula lateral: un registre per intent d'obtencio de contingut
-- -----------------------------------------------------------------------------
CREATE TABLE public.entry_content_fetches (
    id                   bigserial    NOT NULL,
    entry_id             integer      NOT NULL,
    requested_url        text         NOT NULL,
    normalized_url       text         NOT NULL,
    final_url            text,
    fetch_status         text         NOT NULL,
    http_status          integer,
    content_type         text,
    fetched_at           timestamptz  NOT NULL DEFAULT now(),
    text_content         text,
    text_length          integer,
    truncated            boolean      NOT NULL DEFAULT false,
    content_hash         text,
    text_source          text,
    error_detail         text,
    user_agent           text         NOT NULL,
    reused_from_fetch_id bigint,

    CONSTRAINT entry_content_fetches_pkey
        PRIMARY KEY (id),

    -- RESTRICT: no es pot eliminar una entrada que te intents d'obtencio
    -- registrats. Evita perdre evidencia per cascada sense una politica acordada.
    CONSTRAINT entry_content_fetches_entry_id_fkey
        FOREIGN KEY (entry_id) REFERENCES public.entries (id)
        ON DELETE RESTRICT,

    -- RESTRICT: no es pot eliminar un registre del qual un altre en reutilitza el text.
    CONSTRAINT entry_content_fetches_reused_from_fetch_id_fkey
        FOREIGN KEY (reused_from_fetch_id) REFERENCES public.entry_content_fetches (id)
        ON DELETE RESTRICT,

    CONSTRAINT chk_entry_content_fetches_status CHECK (
        fetch_status IN (
            'ok',
            'robots_blocked',
            'robots_unreachable',
            'access_denied',
            'anti_bot_challenge',
            'text_too_short',
            'timeout',
            'http_error',
            'error',
            'pdf_unprocessed',
            'duplicate'
        )
    ),
    CONSTRAINT chk_entry_content_fetches_text_length
        CHECK (text_length IS NULL OR text_length >= 0),
    CONSTRAINT chk_entry_content_fetches_http_status
        CHECK (http_status IS NULL OR (http_status BETWEEN 100 AND 599)),
    CONSTRAINT chk_entry_content_fetches_not_self_reuse
        CHECK (reused_from_fetch_id IS NULL OR reused_from_fetch_id <> id),
    -- SHA-256 complet en hexadecimal minuscula (64 caracters)
    CONSTRAINT chk_entry_content_fetches_content_hash
        CHECK (content_hash IS NULL OR content_hash ~ '^[0-9a-f]{64}$'),
    -- Contenidor HTML d'on s'ha extret el text. Ampliar el vocabulari exigira
    -- una migracio (DROP + ADD d'aquest CHECK).
    CONSTRAINT chk_entry_content_fetches_text_source
        CHECK (text_source IS NULL OR text_source IN ('html_article', 'html_main', 'html_body'))
);

-- Cap UNIQUE sobre URL ni hash: pot haver-hi diversos intents legitims.
CREATE INDEX idx_entry_content_fetches_entry_fetched
    ON public.entry_content_fetches (entry_id, fetched_at DESC);

-- Indexs d'expressio sobre md5(url) en lloc de B-tree sobre la URL completa:
-- una URL molt llarga no pot fer fallar l'INSERT per mida de fila d'index
-- (limit d'uns 2.700 bytes en un B-tree). md5() es una funcio estandard
-- (immutable); no cal cap extensio.
-- ATENCIO, ho han de respectar les consultes futures: l'index nomes serveix
-- per localitzar candidats. md5 NO es una restriccio d'unicitat NI una prova
-- d'identitat (dues URL diferents podrien, teoricament, tenir el mateix md5).
-- Cal comparar SEMPRE les dues coses:
--     WHERE md5(normalized_url) = md5($1) AND normalized_url = $1
--     WHERE md5(final_url)      = md5($1) AND final_url      = $1
-- Sense la segona condicio, el resultat no es fiable. Sense la primera, no
-- s'utilitza l'index. md5(NULL) es NULL: les files amb final_url NULL no coincideixen mai.
CREATE INDEX idx_entry_content_fetches_normalized_url_md5
    ON public.entry_content_fetches (md5(normalized_url));
CREATE INDEX idx_entry_content_fetches_final_url_md5
    ON public.entry_content_fetches (md5(final_url));

CREATE INDEX idx_entry_content_fetches_content_hash
    ON public.entry_content_fetches (content_hash);

-- Suport a la FK de reutilitzacio: accelera la comprovacio ON DELETE RESTRICT
-- i la cerca dels registres que reutilitzen un contingut donat.
CREATE INDEX idx_entry_content_fetches_reused_from
    ON public.entry_content_fetches (reused_from_fetch_id);

COMMENT ON TABLE public.entry_content_fetches IS
    'Fase 1B: un registre per intent d''obtencio del contingut de l''article. El text obtingut es desa aqui i mai a entries.raw_content/raw_snippet. Si text_content es NULL i reused_from_fetch_id no, el text es resol a traves del registre referenciat. Els intents no depenen de entries.processing_retries.';
COMMENT ON COLUMN public.entry_content_fetches.reused_from_fetch_id IS
    'Registre del qual es reutilitza el text (mateix contingut), per no duplicar-lo i mantenir la tracabilitat. Una fila amb text_content NULL i aquest camp informat resol el text a traves del registre referenciat; no cal copiar text_content.';
COMMENT ON INDEX public.idx_entry_content_fetches_normalized_url_md5 IS
    'Index d''expressio md5(normalized_url). No es unicitat ni prova d''identitat: les consultes han de comparar md5(normalized_url) = md5($1) I normalized_url = $1.';
COMMENT ON INDEX public.idx_entry_content_fetches_final_url_md5 IS
    'Index d''expressio md5(final_url). No es unicitat ni prova d''identitat: les consultes han de comparar md5(final_url) = md5($1) I final_url = $1.';
COMMENT ON COLUMN public.entry_content_fetches.text_source IS
    'Contenidor HTML d''on s''ha extret el text: html_article, html_main o html_body (CHECK). NULL si no hi ha text desat.';
COMMENT ON COLUMN public.entry_content_fetches.normalized_url IS
    'URL normalitzada (sense parametres de seguiment, etc.). Si la URL no es pot normalitzar, desa la URL sollicitada (requested_url) tal com es va demanar.';

-- -----------------------------------------------------------------------------
-- 3. entries.discard_reason (nullable). Les files existents queden a NULL.
--    NULL = motiu historic no registrat; no s'infereix de la longitud del text.
--    ADD COLUMN sense DEFAULT no reescriu la taula a PostgreSQL 12.
-- -----------------------------------------------------------------------------
ALTER TABLE public.entries
    ADD COLUMN discard_reason text;

ALTER TABLE public.entries
    ADD CONSTRAINT chk_entries_discard_reason
    CHECK (
        discard_reason IS NULL
        OR discard_reason IN (
            'out_of_scope',
            'no_content',
            'insufficient_text',
            'fetch_failed'
        )
    );

COMMENT ON COLUMN public.entries.discard_reason IS
    'Motiu estructurat del descart (Fase 1B). NULL = motiu historic no registrat (no inferir-lo). Una fallada d''obtencio es registra com a fetch_failed/insufficient_text, mai com ready_for_primary=no.';

-- -----------------------------------------------------------------------------
-- 4. entries.analyzed_fetch_id: obtencio de la qual va sortir el text analitzat.
--    Es crea DESPRES de la taula nova (la FK la referencia).
--    NULL = text del feed o historia anterior a la Fase 1B.
--    NO S'OMPLE EN AQUEST PAS: queda a NULL a totes les files. Un cop el codi
--    consumidor la usi, apuntara a un registre d'entry_content_fetches (si
--    aquest es 'duplicate', el text es resol via reused_from_fetch_id).
--    Cap restriccio comprova que el registre referenciat sigui de la mateixa
--    entrada ni que tingui text: ho ha de garantir el codi.
--    ADD COLUMN sense DEFAULT no reescriu la taula a PostgreSQL 12.
-- -----------------------------------------------------------------------------
ALTER TABLE public.entries
    ADD COLUMN analyzed_fetch_id bigint;

-- RESTRICT: no es pot eliminar un registre d'obtencio que una entrada encara
-- indica com a text analitzat.
ALTER TABLE public.entries
    ADD CONSTRAINT entries_analyzed_fetch_id_fkey
    FOREIGN KEY (analyzed_fetch_id) REFERENCES public.entry_content_fetches (id)
    ON DELETE RESTRICT;

CREATE INDEX idx_entries_analyzed_fetch_id
    ON public.entries (analyzed_fetch_id);

COMMENT ON COLUMN public.entries.analyzed_fetch_id IS
    'Fase 1B: registre d''entry_content_fetches del qual va sortir el text que va analitzar Input/Primary. NULL = text del feed o historia anterior. No s''omple a la migracio; l''omple el codi consumidor.';

-- -----------------------------------------------------------------------------
-- 5. Comprovacio final: cap valor omplert. Si falla, es desfa tot.
-- -----------------------------------------------------------------------------
DO $postcheck$
DECLARE
    n bigint;
BEGIN
    SELECT count(*) INTO n FROM public.entries WHERE discard_reason IS NOT NULL;
    IF n <> 0 THEN
        RAISE EXCEPTION 'S''esperaven 0 files amb discard_reason i n''hi ha %.', n;
    END IF;
    SELECT count(*) INTO n FROM public.entries WHERE analyzed_fetch_id IS NOT NULL;
    IF n <> 0 THEN
        RAISE EXCEPTION 'S''esperaven 0 files amb analyzed_fetch_id i n''hi ha %.', n;
    END IF;
    SELECT count(*) INTO n FROM public.entry_content_fetches;
    IF n <> 0 THEN
        RAISE EXCEPTION 'La taula nova hauria d''estar buida i te % files.', n;
    END IF;
END
$postcheck$;

COMMIT;
