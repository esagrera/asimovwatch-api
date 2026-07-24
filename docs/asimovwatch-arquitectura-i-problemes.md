# AsimovWatch — Arquitectura, Problemes Coneguts i Solucions

## Descripció general de l'arquitectura

AsimovWatch és un observatori digital sobre ètica i governança de la IA. La infraestructura és híbrida: el frontend viu a Dinahosting, l'API pública es desplega a Render, i la base de dades PostgreSQL viu al servidor de Dinahosting.

### Diagrama de l'arquitectura

```
Usuari (navegador)
        │
        ▼
┌───────────────────────────────────┐
│  Dinahosting (Apache)             │
│  asimovwatch.com/index.html       │  ← Frontend públic
│  asimovwatch.com/admin.html       │  ← Backoffice
└───────────────┬───────────────────┘
                │ peticions API (fetch)
                ▼
┌───────────────────────────────────┐
│  api.asimovwatch.com              │
│  CNAME → asimovwatch-api.onrender.com │
│  Render (FastAPI + Uvicorn)       │  ← API pública (HTTPS, SSL automàtic)
└───────────────┬───────────────────┘
                │ connexió PostgreSQL
                ▼
┌───────────────────────────────────┐
│  REDACTED_HOST:5432      │
│  BD: asimovwatch                  │  ← Base de dades PostgreSQL
│  Usuari: REDACTED_USER            │
└───────────────────────────────────┘
```

### Taula de components

| Component | Tecnologia | URL | Allotjament |
|---|---|---|---|
| Frontend | HTML/JS estàtic | `https://asimovwatch.com` | Dinahosting (`~/www/index.html`) |
| Backoffice | HTML/JS estàtic | `https://asimovwatch.com/admin.html` | Dinahosting (`~/www/admin.html`) |
| API pública | FastAPI + Uvicorn | `https://api.asimovwatch.com` | Render (`asimovwatch-api`) |
| Swagger | FastAPI docs | `https://api.asimovwatch.com/docs` | Render |
| Base de dades | PostgreSQL 12 | `REDACTED_HOST:5432` | Dinahosting |
| Repositori codi | GitHub | `github.com/esagrera/asimovwatch-api` | GitHub |
| Servidor SSH | Python 3.9 / Uvicorn | `hl1613.dinaserver.com` port 8000 (intern) | Dinahosting VPS |

---

## Variables d'entorn necessàries

### A Render (asimovwatch-api → Environment)

Aquestes 5 variables han d'existir a Render per connectar amb la BD de Dinahosting:

| Variable | Valor |
|---|---|
| `DB_HOST` |
| `DB_PORT` |
| `DB_NAME` |
| `DB_USER` |
| `DB_PASSWORD` |

> ⚠️ La variable `DATABASE_URL` és opcional i no és suficient per si sola si el `main.py` usa `os.environ["DB_HOST"]`. Cal que hi siguin les 5 variables individuals.

### Claus LLM necessàries a Render

A més de les variables de connexió a la base de dades, el servei de Render ha de tenir definides les claus dels proveïdors LLM que es facin servir en runtime.

| Variable | Ús actual |
|---|---|
| `GEMINI_API_KEY` | Necessària per al flux actual de discovery de `source_candidates` |
| `OPENAI_API_KEY` | Reservada per a evolució futura |
| `CLAUDE_API_KEY` | Reservada per a evolució futura |

> En l’estat actual del sistema, el discovery de fonts candidates depèn operativament de Gemini. Si `GEMINI_API_KEY` no està definida a Render, el crawler manual i la descoberta assistida per LLM fallaran.

### Al servidor Dinahosting (~/asimovwatch-api/.env)

El fitxer `.env` al servidor ha de contenir les mateixes variables per arrencar Uvicorn localment:

```
DB_HOST
DB_PORT
DB_NAME
DB_USER
DB_PASSWORD
PERPLEXITY_API_KEY
```

El `main.py` ha de tenir al principi:
```python
from dotenv import load_dotenv
load_dotenv()
```

---

## Limitacions conegudes del servidor Dinahosting

### 1. No hi ha accés sudo
L'usuari `asimovwatch` no té permisos `sudo`. No es poden fer canvis a la configuració del sistema, Apache, ni Nginx del servidor.

### 2. Apache intercepta tot el trànsit HTTP/HTTPS exterior
El servidor web Apache de Dinahosting serveix el contingut de `~/www/` i intercepta totes les peticions als ports 80 i 443. Uvicorn corre al port 8000 però **no és accessible des de l'exterior** directament via el domini.

### 3. No es pot configurar un reverse proxy sense suport de Dinahosting
Dinahosting ofereix un proxy Nginx des del panell de control, però **activa'l redirigeix TOT el trànsit HTTP/HTTPS al port configurat**, cosa que fa que el frontend (`asimovwatch.com`) deixi de funcionar. Per tant, no es pot usar per servir l'API i el frontend alhora des del mateix domini.

Referència: Resposta oficial de Dinahosting (5 de juny de 2026):
> *"al activar esta funcionalidad todo el tráfico http y https se redirigirá al puerto que se active, por lo que si tenéis alguna web en asimovwatch.com esta dejará de funcionar"*

### 4. Python 3.9 al servidor
El servidor Dinahosting té **Python 3.9**. La sintaxi de tipus moderna de Python 3.10+ (`str | None`, `int | None`) **no funciona** i provoca un `TypeError` en importar el mòdul, impedint que Uvicorn arrenqui.

### 5. Port 8000 accessible per IP directa però no per domini
El port 8000 del servidor (`82.98.164.62:8000`) **sí és accessible des de l'exterior per IP directa**, però no per domini perquè Apache intercepta els ports 80/443 abans d'arribar a Uvicorn.

---

## Problemes coneguts i solucions

### Problema 1 — Uvicorn no arrenca (TypeError Python 3.9)

**Símptoma:** Tot el backend falla amb 500. `tail ~/uvicorn.log` mostra:
```
TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'
```
`curl http://localhost:8000/` retorna `Connection refused`.

**Causa:** El `main.py` usa sintaxi de tipus `str | None` o `int | None` compatible només amb Python 3.10+. El servidor Dinahosting té Python 3.9.

**Solució:** Substituir totes les anotacions `X | None` per `Optional[X]` i importar `Optional` de `typing`:
```python
from typing import Optional

# MAL (Python 3.10+)
def list_entries(status: str | None = None):

# BÉ (Python 3.9 compatible)
def list_entries(status: Optional[str] = None):
```

Afecta especialment els paràmetres de les funcions d'endpoint com `list_entries()`.

---

### Problema 2 — API pública retorna 500 però localhost funciona

**Símptoma:** `curl http://localhost:8000/entries` retorna 200, però `https://api.asimovwatch.com/entries` retorna 500. El log local no registra les peticions externes.

**Causa més freqüent A — DNS apunta a Render amb variables d'entorn incorrectes:**
El CNAME `api.asimovwatch.com → asimovwatch-api.onrender.com` és correcte, però Render no té les variables `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` configurades a l'Environment.

**Solució A:** Anar a Render → `asimovwatch-api` → Environment i afegir les 5 variables de la BD. Render redesplegarà automàticament.

**Causa més freqüent B — DNS apunta al servidor Dinahosting sense proxy:**
El registre DNS `api` és un registre `A` apuntant a `82.98.164.62`, però Apache intercepta el trànsit i no hi ha proxy cap al port 8000.

**Solució B:** Tornar al CNAME original:
```
api  CNAME  asimovwatch-api.onrender.com
```

---

### Problema 3 — KeyError: 'DB_HOST' a Render

**Símptoma:** Els logs de Render mostren:
```
KeyError: 'DB_HOST'
File ".../app/main.py", line 39, in get_connection
    host=os.environ["DB_HOST"],
```

**Causa:** El `main.py` actual usa `os.environ["DB_HOST"]` però Render només té `DATABASE_URL` configurada.

**Solució:** Afegir a Render → Environment les 5 variables individuals (`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`).

---

### Problema 4 — Variables d'entorn no carreguen al servidor

**Símptoma:** `curl http://localhost:8000/stats` retorna 500. El log mostra `KeyError: 'DB_HOST'` o similar.

**Causa:** El `.env` existeix però `load_dotenv()` no és al `main.py`, o el `.env` té valors incorrectes (p.ex. `DB_HOST=localhost`).

**Solució:**
1. Verificar que les primeres línies del `main.py` siguin:
```python
from dotenv import load_dotenv
load_dotenv()
```
2. Verificar el contingut del `.env`:
```bash
cat ~/asimovwatch-api/.env
```
3. Reiniciar Uvicorn:
```bash
pkill -f "uvicorn app.main" 2>/dev/null; sleep 1
cd ~/asimovwatch-api
nohup python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000 > ~/uvicorn.log 2>&1 &
sleep 3 && curl http://localhost:8000/stats
```

---

### Problema 5 — Conflicte d'ordre de rutes GET: `/entries/pending/enrichment` vs `/entries/{entry_id}`

**Símptoma:** `GET /entries/pending/enrichment` retornava `422 Unprocessable Entity` en lloc de la llista d'entrades pendents d'enriquiment.

**Causa:** FastAPI (via Starlette) avalua les rutes en l'ordre en què es registren, separades per verb HTTP. El bloc `list_pending_enrichment` (ruta `/entries/pending/enrichment`) estava registrat *després* de `get_entry` (ruta `/entries/{entry_id}`) dins dels endpoints `GET`. En arribar una petició a `/entries/pending/enrichment`, FastAPI la comparava primer contra el patró `/entries/{entry_id}` i intentava convertir el segment `"pending"` al tipus `int` esperat per `entry_id`, provocant l'error de validació.

**Solució:** Moure el bloc complet de:
```python
@protected_router.get("/entries/pending/enrichment")
def list_pending_enrichment(limit: int = 50):
    ...
```
a una posició **anterior** a:
```python
@protected_router.get("/entries/{entry_id}")
def get_entry(entry_id: int):
    ...
```
dins de `app/main.py`.

**Regla general per a futurs endpoints:** Qualsevol ruta amb segments fixos (p. ex. `/entries/pending/enrichment`, `/entries/stats`) ha de registrar-se **sempre abans** que rutes amb paràmetres dinàmics del mateix nivell i del mateix verb HTTP (p. ex. `/entries/{entry_id}`). Aquesta regla és rellevant especialment durant la fase d'ingesta, quan s'afegiran nous endpoints sota `/entries/`.

**Verificat:** 4 de juliol de 2026. Provades ambdues rutes després del reordenament — `GET /entries/pending/enrichment` i `GET /entries/{entry_id}` amb ID real — totes dues retornen `200 OK` correctament.

---

### Problema 6 — El crawler mostra “items trobats” però no apareixen candidates noves a l’admin

**Símptoma:** Després d’executar `Run now`, el resum operatiu del crawler mostra valors com `Fonts revisades: 1`, `Items trobats: 3` o `Items rellevants: 3`, però a la secció de candidates no apareix cap nova fila.

**Causa més freqüent:** L’execució s’ha fet amb `dry_run=True`. En aquest mode, el backend executa tota la descoberta, calcula comptadors i retorna els items detectats, però **no escriu res** a `public.source_candidates`.

**Solució:** Si només es vol validar si el brief troba resultats útils, `dry_run=True` és correcte. Si es vol revisar i gestionar els resultats des del panell admin, cal repetir l’execució amb `dry_run=False` perquè els candidats quedin persistits amb estat `PENDING`.

**Nota operativa:** Aquesta és una decisió funcional deliberada. El `dry_run=True` s’interpreta com una simulació de descoberta, no com una alta real de candidates.

---

## Procediment de diagnòstic estàndard

Quan alguna cosa falla, executar sempre aquests comandos **en ordre** al servidor SSH:

```bash
# 1. Comprova si Uvicorn arrenca
tail -n 100 ~/uvicorn.log

# 2. Comprova si el backend local respon
curl -i http://localhost:8000/
curl -i http://localhost:8000/entries

# 3. Comprova si el domini públic respon
curl -i https://api.asimovwatch.com/
curl -i https://api.asimovwatch.com/entries

# 4. Comprova a on apunta el DNS
dig api.asimovwatch.com +short
```

**Interpretació:**
- Si el pas 1 mostra `TypeError` → Problema Python 3.9 (Problema 1)
- Si el pas 1 mostra `KeyError: 'DB_HOST'` → Falta `.env` o `load_dotenv()` (Problema 4)
- Si el pas 2 falla amb `Connection refused` → Uvicorn no corre
- Si el pas 2 funciona però el pas 3 falla → Problema de DNS o Render (Problemes 2 i 3)
- Si el pas 4 mostra `asimovwatch-api.onrender.com` → DNS correcte, mirar Render
- Si el pas 4 mostra `82.98.164.62` → DNS apunta al servidor, cal tornar al CNAME de Render

---

## Procediment de desplegament de canvis

Quan es fa un canvi al codi:

1. **Fer commit i push a GitHub** (des de local o directament a GitHub.com)
2. **Al servidor Dinahosting** (SSH):
```bash
cd ~/asimovwatch-api
git pull origin main
pkill -f "uvicorn app.main" 2>/dev/null; sleep 1
nohup python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000 > ~/uvicorn.log 2>&1 &
sleep 3 && tail -n 20 ~/uvicorn.log
curl http://localhost:8000/
```
3. **Render redesplega automàticament** quan detecta el push a GitHub (branca `main`)
4. **Verificar a Render** → Logs que aparegui `Your service is live`

---

## Arxius i directoris importants

| Ruta | Contingut |
|---|---|
| `~/asimovwatch-api/` | Repositori de l'API |
| `~/asimovwatch-api/app/main.py` | Codi principal de l'API |
| `~/asimovwatch-api/.env` | Variables d'entorn del servidor (no pujar a GitHub) |
| `~/uvicorn.log` | Log de Uvicorn (diagnòstic principal) |
| `~/www/index.html` | Frontend públic |
| `~/www/admin.html` | Backoffice/panell d'administració |
| `~/venv/` | Entorn virtual Python del servidor |

---

## Crontab per arrencada automàtica

Uvicorn arrenca automàticament quan el servidor reinicia gràcies al crontab:

```bash
crontab -e
# Línia afegida:
@reboot cd ~/asimovwatch-api && source ~/.env && nohup python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000 > ~/uvicorn.log 2>&1 &
```

Si load_dotenv() és al main.py, el `source ~/.env` no és necessari.

### Problema 5 — Després d’un redeploy de Render, Swagger demana reautenticació o cal tornar a validar l’API key

**Símptoma:** Després de fer `Manual Deploy → Deploy latest commit` a Render, Swagger pot tornar a demanar user/password i algun endpoint protegit pot respondre 401 fins que es torna a reautoritzar.

**Causa probable:** El redeploy no hauria d’esborrar variables d’entorn de Render, així que si passa habitualment cal revisar si la clau s’està carregant des del lloc correcte o si el client/front no està reenviant la capçalera `X-API-Key` després del reinici del servei.

**Solució pràctica:**
1. A Render, fer `Manual Deploy → Deploy latest commit`.
2. Revisar que les variables d’entorn del servei continuen presents.
3. Provar Swagger de nou i validar si l’API key es torna a demanar.
4. Si un endpoint com `/stats` retorna 401, comprovar que el client envia `X-API-Key` i que `APIKEY` segueix definida al servei.

**Nota operativa:** El redeploy de Render no hauria de perdre configuració persistent; si sembla que la key desapareix, el problema és de configuració o d’autenticació, no del deploy en si.

## Configuració LLM i prompts en runtime

AsimovWatch ja no depèn només dels valors definits al codi per configurar el comportament del sistema LLM. La configuració activa de providers, models i prompts es pot gestionar des del panell `admin.html`, i es desa a la base de dades per ser llegida en temps d’execució.

### Flux operatiu actual

El flux actual funciona així:

1. L’usuari edita providers, models o prompts des del panell d’administració.
2. El frontend envia els canvis a l’API.
3. L’API desa la configuració a `public.config` i els prompts a `public.prompts`.
4. `llm_router.py` carrega aquesta configuració en runtime i la fa prevaldre sobre els valors per defecte definits al codi.

### Implicació operativa

Això vol dir que fer `git commit` del repositori no és suficient per conservar tota la configuració activa del sistema. Els fitxers del codi poden contenir valors base o de reserva, però la configuració efectiva en producció pot estar vivint a la base de dades.

Per aquest motiu, cada vegada que es validin nous models o prompts des del panell admin convé deixar-ne constància també a la documentació del repositori, especialment si aquests valors passen a formar part del comportament estable del sistema.

### Estat validat

Durant les proves de juliol de 2026 s’ha validat el següent:

- El panell `admin.html` guarda correctament la configuració LLM.
- `main.py` exposa els endpoints necessaris perquè la configuració quedi persistida.
- `llm_router.py` resol correctament la configuració activa des de la base de dades.
- El model `gemini-3.1-flash-lite` ha quedat validat en execució real.
- El prompt `Primary` ha quedat validat i retorna una sortida JSON coherent amb l’esquema d’enriquiment d’AsimovWatch.

### Nota sobre els valors per defecte

Els valors definits dins `DEFAULT_CONFIG` a `llm_router.py` s’han d’entendre com a reserva tècnica. Si existeixen claus equivalents a `public.config`, aquestes tenen prioritat en temps d’execució.

De la mateixa manera, `prompts.py` continua sent útil com a font base de plantilles i com a referència de desenvolupament, però els prompts efectivament actius poden ser els editats i desats des del panell admin.

### Estat actual del discovery de fonts candidates

A juliol de 2026, el sistema ja disposa d’un flux específic de descoberta, revisió i promoció de fonts candidates.

Aquest flux es basa en un mòdul `source_candidates.py` que introdueix una capa editorial intermèdia entre la cerca automàtica i l’activació d’una font real dins del radar d’ingesta. La descoberta assistida per LLM no crea directament fonts actives a `public.sources`, sinó candidates a `public.source_candidates` que requereixen revisió humana posterior.

Els endpoints clau d’aquest flux són:

- `GET /source-candidates`
- `GET /source-candidates/{candidate_id}`
- `POST /source-candidates`
- `PATCH /source-candidates/{candidate_id}`
- `DELETE /source-candidates/{candidate_id}`
- `POST /source-candidates/discover`
- `POST /source-candidates/{candidate_id}/review`
- `POST /source-candidates/{candidate_id}/promote`
- `POST /source-candidates/{candidate_id}/demote`
- `GET /source-candidates/{candidate_id}/source`

Aquesta arquitectura reforça el criteri editorial d’AsimovWatch: el sistema pot proposar, però no pot aprovar ni activar autònomament noves fonts.
