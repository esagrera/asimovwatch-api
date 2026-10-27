-- =============================================================================
-- AsimovWatch - Fase 1B, pas 0
-- 03_rollback.sql: desfa 01_apply.sql
--
-- ESTAT: PREPARAT, NO EXECUTAT.
--
-- ATENCIO: eliminar public.entry_content_fetches comporta PERDRE L'HISTORIAL
-- D'INTENTS I ELS TEXTOS OBTINGUTS. Per aixo l'script AVORTA si:
--   - la taula public.entry_content_fetches conte alguna fila, o
--   - public.entries.discard_reason conte algun valor, o
--   - public.entries.analyzed_fetch_id conte algun valor.
-- Si ha d'avortar perque ja hi ha dades, no s'ha de forcar: cal una decisio
-- explicita (exportar-ho primer i acordar-ne la politica).
--
-- Ordre: primer es treu entries.analyzed_fetch_id (la seva FK depen de la taula
-- nova; en eliminar la columna desapareix tambe idx_entries_analyzed_fetch_id) i
-- despres la taula nova.
--
-- DROP TABLE elimina tambe els seus indexs (inclosos els d'expressio
-- md5(normalized_url) i md5(final_url), i l'index sobre reused_from_fetch_id),
-- les restriccions i la sequencia propia: no calen DROP INDEX separats.
--
-- No fa servir DROP ... CASCADE: si algun altre objecte depen de la taula o de
-- la columna, la sentencia falla i la transaccio es desfa.
-- Si falla alguna sentencia, executeu ROLLBACK; abans de res mes.
-- =============================================================================

BEGIN;

SET LOCAL lock_timeout = '5s';

-- Bloquejos exclusius: ningu pot inserir intents ni escriure motius entre la
-- comprovacio i l'eliminacio. (La taula nova es bloqueja nomes si existeix.)
LOCK TABLE public.entries IN ACCESS EXCLUSIVE MODE;

DO $lock$
BEGIN
    IF to_regclass('public.entry_content_fetches') IS NOT NULL THEN
        LOCK TABLE public.entry_content_fetches IN ACCESS EXCLUSIVE MODE;
    END IF;
END
$lock$;

-- Salvaguarda: avorta si hi ha dades que es perdrien
DO $guard$
DECLARE
    n_fetches bigint := 0;
    n_reasons bigint := 0;
    n_analyzed bigint := 0;
    has_table boolean := to_regclass('public.entry_content_fetches') IS NOT NULL;
    has_column boolean := EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = 'public.entries'::regclass
          AND attname = 'discard_reason' AND NOT attisdropped
    );
    has_analyzed boolean := EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = 'public.entries'::regclass
          AND attname = 'analyzed_fetch_id' AND NOT attisdropped
    );
BEGIN
    IF NOT has_table AND NOT has_column AND NOT has_analyzed THEN
        RAISE EXCEPTION 'No hi ha res a desfer: no existeix ni la taula ni les columnes discard_reason i analyzed_fetch_id.';
    END IF;

    IF has_table THEN
        EXECUTE 'SELECT count(*) FROM public.entry_content_fetches' INTO n_fetches;
    END IF;
    IF has_column THEN
        EXECUTE 'SELECT count(*) FROM public.entries WHERE discard_reason IS NOT NULL' INTO n_reasons;
    END IF;
    IF has_analyzed THEN
        EXECUTE 'SELECT count(*) FROM public.entries WHERE analyzed_fetch_id IS NOT NULL' INTO n_analyzed;
    END IF;

    IF n_fetches > 0 THEN
        RAISE EXCEPTION 'ROLLBACK AVORTAT: public.entry_content_fetches conte % fila(es). Eliminar-la perdria l''historial i els textos obtinguts.', n_fetches;
    END IF;
    IF n_reasons > 0 THEN
        RAISE EXCEPTION 'ROLLBACK AVORTAT: public.entries.discard_reason conte % valor(s). Eliminar-la perdria motius de descart.', n_reasons;
    END IF;
    IF n_analyzed > 0 THEN
        RAISE EXCEPTION 'ROLLBACK AVORTAT: public.entries.analyzed_fetch_id conte % valor(s). Eliminar-la perdria l''enllac entre entrades i obtencions.', n_analyzed;
    END IF;

    RAISE NOTICE 'Salvaguarda superada: taula (existeix=%) buida; discard_reason (existeix=%) i analyzed_fetch_id (existeix=%) sense valors.', has_table, has_column, has_analyzed;
END
$guard$;

-- Ordre invers a l'aplicacio. Sense CASCADE (RESTRICT es el comportament per defecte,
-- indicat explicitament).
-- 1) analyzed_fetch_id ha de desapareixer ABANS que la taula nova (la seva FK la referencia).
ALTER TABLE public.entries DROP CONSTRAINT IF EXISTS entries_analyzed_fetch_id_fkey RESTRICT;
-- Eliminar la columna elimina tambe idx_entries_analyzed_fetch_id.
ALTER TABLE public.entries DROP COLUMN IF EXISTS analyzed_fetch_id RESTRICT;

-- 2) discard_reason
ALTER TABLE public.entries DROP CONSTRAINT IF EXISTS chk_entries_discard_reason RESTRICT;
ALTER TABLE public.entries DROP COLUMN IF EXISTS discard_reason RESTRICT;

-- 3) Taula nova. Elimina tambe els 5 indexs i la PK, les restriccions i la sequencia propia
-- (entry_content_fetches_id_seq). Cap altre objecte ha de dependre'n; si en depen, falla.
DROP TABLE IF EXISTS public.entry_content_fetches RESTRICT;

COMMIT;
