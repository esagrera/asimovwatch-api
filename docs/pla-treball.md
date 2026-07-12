# AsimovWatch — Pla de treball
*Actualitzat: 4 de juliol de 2026*

## Objectiu
Convertir AsimovWatch en un observatori digital d’IA responsable amb un MVP funcional centrat en detectar, classificar i contextualitzar novetats, no en dictaminar l’ètica final de cada cas.

## Estat actual
L’arquitectura base ja està desplegada: frontend públic a Dinahosting, API pública a Render, i base de dades PostgreSQL a Dinahosting [file:789]. El backend ja té `app/main.py`, `requirements.txt`, `README.md`, `.gitignore` i entorn virtual, i ara toca ordenar la documentació i preparar la següent fase funcional [cite:846][file:789].

## Fase 1 — Infraestructura
Aquesta fase ja està completada. Inclou VPS, Python, virtualenv, repositori clonat, `.env` configurat, PostgreSQL operatiu i taules creades amb migració aplicada [file:789]. També queda documentat que el frontend i el backoffice funcionen, i que l’API pública passa per Render amb SSL automàtic [file:789].

## Fase 2 — API
També està completada. `app/main.py` ja incorpora `load_dotenv()`, endpoints CRUD per `entries`, `stats` i `config`, i compatibilitat amb Python 3.9 mitjançant `Optional[str]` [file:789]. El desplegament a GitHub i Render ja està integrat, amb redeploy automàtic en cada push [file:789].

## Fase 3 — Fonts i prompts
El proper pas és crear `feeds.json` i `prompts.py` [file:789]. `feeds.json` ha de reunir les fonts MVP prioritàries, i `prompts.py` ha de contenir plantilles per resum factual, classificació temàtica i generació de preguntes de debat amb sortida JSON [file:789].

### Evolució de la fase 3
La fase 3 no només inclou la creació de feeds.json i prompts.py, sinó també la preparació del primer sistema de classificació de Protecció humana. Aquest sistema ha de permetre assignar a cada entitat o notícia un estat inicial en tres àmbits: protecció declarada, protecció verificable i profunditat de la protecció.

### Primer criteri de classificació
La classificació inicial es farà amb semàfors simples per facilitar la lectura i la validació humana. El verd indicarà una protecció clara i ben definida; el groc, una protecció parcial, ambigua o incompletament verifica

## Fase 4 — Crawler
Després cal construir `crawler.py` [file:789]. Aquest script ha de llegir fonts, fer parsing, deduplicar continguts i enviar entrades a l’API via `POST /entries` [file:789].

## Fase 5 — Automatització
La següent peça és `crawler.yml` per GitHub Actions [file:789]. L’objectiu és programar execucions amb cron, usar secrets des de `.env` i fer que el crawler corri dins del virtualenv del projecte [file:789].

## Fase 6 — Panell admin
Quan la ingesta bàsica funcioni, cal afegir al panell d’administració la configuració de freqüència i llindar de rellevància [file:789]. Això permetrà ajustar el volum de novetats sense tocar el codi [file:789].

## Fase 7 — Traducció
L’últim bloc del pla inicial és el botó de traducció [file:789]. La llengua per defecte serà català, amb traduccions ES/EN sota demanda i emmagatzemades a la base de dades per evitar crides repetides [file:789].

## Decisions i criteris
El projecte manté el català com a llengua principal, usa traduccions sota demanda, i treballa amb arquitectura multimotor: Perplexity per buscar novetats, Claude per estructurar i classificar, i Mistral OCR per PDFs [file:789]. La supervisió humana continua sent obligatòria abans de publicar qualsevol entrada [file:789].

## Riscos i cauteles
No s’ha d’exposar l’API sense autenticació bàsica si hi ha dades sensibles [file:789]. També convé començar amb poques fonts i no confondre anàlisi amb fet; per això el sistema ha de mantenir validació humana i creixement gradual [file:789].
