# Com afegir un nou provider LLM a AsimovWatch

Aquest document explica exactament què s'ha de tocar quan es vol afegir un
nou provider LLM al sistema. Recull la versió actual de l'arquitectura,
amb `llm_config.py` com a font principal de veritat.

## Idea clau

Quan afegeixes un provider nou, **no n'hi ha prou amb tocar un sol fitxer**.
Perquè tot funcioni de cap a cap, cal actualitzar:

1. la configuració base (`llm_config.py`),
2. el client del provider,
3. el dispatcher (`app/llm_clients/__init__.py`),
4. la validació de l'admin a `main.py`,
5. el constraint SQL de la base de dades,
6. i, si cal, la UI i les variables d'entorn.

---

## Arquitectura actual

### Font de veritat

Ara mateix, `llm_config.py` concentra la informació principal:

- `SUPPORTED_PROVIDERS`: quins providers existeixen.
- `PROVIDER_ENV_MAP`: quina variable d'entorn usa cada provider.
- `PROVIDER_CLIENT_MAP`: quin mòdul i quina funció s'han d'importar per cridar-lo.

Això fa que afegir un provider nou sigui molt més segur, perquè ja no cal
mantenir llista i importacions repartides per diversos llocs.

---

## On s'ha de tocar

### 1. `llm_config.py`

Aquest fitxer s'ha d'actualitzar amb el nou provider en tres llocs:

```python
SUPPORTED_PROVIDERS = {"gemini", "claude", "openai", "perplexity", "<nou_provider>"}

PROVIDER_ENV_MAP = {
    "gemini": "GEMINI_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "<nou_provider>": "<NOU_PROVIDER>_API_KEY",
}

PROVIDER_CLIENT_MAP = {
    "gemini": ("app.llm_clients.gemini_client", "call_gemini_client"),
    "claude": ("app.llm_clients.claude_client", "call_claude_client"),
    "openai": ("app.llm_clients.openai_client", "call_openai_client"),
    "perplexity": ("app.llm_clients.perplexity_client", "call_perplexity_client"),
    "<nou_provider>": ("app.llm_clients.<provider>_client", "call_<provider>_client"),
}
```

Si oblides `PROVIDER_ENV_MAP`, el sistema no sabrà quina API key ha de
llegir. Si oblides `PROVIDER_CLIENT_MAP`, el provider existirà a la
configuració però no es podrà cridar.

### 2. Client del provider

Cal crear un fitxer nou:

```text
app/llm_clients/<provider>_client.py
```

Amb una funció semblant a aquesta:

```python
def call_<provider>_client(model: str, prompt: str, **kwargs) -> str:
    ...
    return resposta_text
```

Aquest client és el que realment fa la crida a l'API del provider.

### 3. `app/llm_clients/__init__.py`

Aquest fitxer ha de quedar com a dispatcher genèric, carregant els clients
segons `PROVIDER_CLIENT_MAP`.

La versió correcta és aquesta:

```python
import importlib
import logging
from typing import Any

from app.llm_config import SUPPORTED_PROVIDERS, PROVIDER_CLIENT_MAP

logger = logging.getLogger(__name__)


def get_supported_providers() -> list[str]:
    return sorted(SUPPORTED_PROVIDERS)


def _resolve_provider_callable(provider: str):
    provider = (provider or "").strip().lower()

    if provider not in PROVIDER_CLIENT_MAP:
        raise ValueError(
            f"provider no suportat: {provider}. "
            f"Disponibles: {', '.join(get_supported_providers())}"
        )

    module_path, func_name = PROVIDER_CLIENT_MAP[provider]
    module = importlib.import_module(module_path)
    return getattr(module, func_name)
```

Així evites tenir un bloc `if provider == ...` per cada provider nou.

### 4. `main.py` → `update_prompt()`

Quan l'admin desa un prompt, `main.py` valida el provider. Aquesta validació
descarta valors buits i també providers no suportats.

Ha de quedar així:

```python
from app.llm_clients import get_supported_providers

...

clean_category = (body.category or "").strip() or None
clean_provider = (body.provider or "").strip().lower()
clean_model = (body.model or "").strip()

if not clean_provider:
    raise HTTPException(status_code=400, detail="Provider és obligatori")

if not clean_model:
    raise HTTPException(status_code=400, detail="Model és obligatori")

if clean_provider not in get_supported_providers():
    raise HTTPException(status_code=400, detail="Provider no suportat")
```

Important: **no s'ha de tornar a hardcodejar** una llista com
`{"gemini", "claude", "openai"}` en aquest fitxer.

### 5. Constraint SQL de la base de dades

La taula `public.llm_runtime_config` té una restricció de tipus `CHECK` que
valida el camp `provider`. Si no l'actualitzes, el formulari pot passar la
validació de Python però fallarà en fer l'`INSERT` o `UPDATE`.

La migració necessària és aquesta:

```sql
ALTER TABLE public.llm_runtime_config
    DROP CONSTRAINT chk_llm_runtime_provider;

ALTER TABLE public.llm_runtime_config
    ADD CONSTRAINT chk_llm_runtime_provider
    CHECK (provider::text = ANY (ARRAY[
        'gemini', 'claude', 'openai', 'perplexity', '<nou_provider>'
    ]::text[]));
```

Després, actualitza també `schema.sql` perquè el repositori i la base de
producció no quedin desincronitzats.

---

## Altres llocs a revisar

### `admin.html`

Si el selector de provider està escrit a mà en HTML, afegeix-hi la nova
`<option>`. Si el frontend omple les opcions des de l'API, potser no cal
canviar res.

### `llm_admin.py`

Revisa si hi ha validacions pròpies o llistes de providers hardcodejades.
En el flux actual, aquest fitxer ja fa servir `get_supported_providers()`
per validar providers, cosa que és correcta.

### Variables d'entorn

Afegeix la nova API key al desplegament:

- `GEMINI_API_KEY`
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`
- `PERPLEXITY_API_KEY`
- `NOVA_API_KEY` o el nom que correspongui al nou provider

---

## Ordre recomanat per afegir un provider

1. Afegir el provider a `llm_config.py`.
2. Crear `app/llm_clients/<provider>_client.py`.
3. Registrar el provider a `PROVIDER_CLIENT_MAP`.
4. Confirmar que `app/llm_clients/__init__.py` el resol correctament.
5. Actualitzar `main.py` si cal tocar la validació o el valor per defecte.
6. Actualitzar el constraint SQL de `llm_runtime_config`.
7. Actualitzar `schema.sql`.
8. Revisar la UI i les variables d'entorn.
9. Provar el flux complet: desar prompt, carregar configuració i executar el provider.

---

## Checklist ràpida

- [ ] 1. Afegir el provider a `SUPPORTED_PROVIDERS`.
- [ ] 2. Afegir la variable d'entorn corresponent a `PROVIDER_ENV_MAP`.
- [ ] 3. Afegir el client a `PROVIDER_CLIENT_MAP`.
- [ ] 4. Crear `app/llm_clients/<provider>_client.py`.
- [ ] 5. Confirmar que `app/llm_clients/__init__.py` usa el mapa centralitzat.
- [ ] 6. Confirmar que `main.py` valida amb `get_supported_providers()`.
- [ ] 7. Actualitzar el constraint `chk_llm_runtime_provider` a la BD.
- [ ] 8. Actualitzar `schema.sql`.
- [ ] 9. Revisar `admin.html` i `llm_admin.py` si hi ha llistes o selectors fixos.
- [ ] 10. Afegir la nova API key al desplegament.
- [ ] 11. Provar el flux complet.

---

## Historial del canvi

Aquest document es va crear arran de la incorporació de `perplexity`, que va
requerir actualitzar la validació de l'admin, el dispatcher LLM i el
constraint SQL. La lliçó principal és que el provider s'ha d'afegir a tots
els nivells on el sistema el valida o el resol.
