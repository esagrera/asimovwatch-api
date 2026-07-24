# AsimovWatch — Decisions de disseny
*Actualitzat: 4 de juliol de 2026*

## Principis bàsics
AsimovWatch no pretén dictaminar l’ètica final de cada cas, sinó detectar, classificar i contextualitzar allò que passa al món sobre IA responsable.

## Marc de referència
El marc editorial i conceptual s’ha de basar en estàndards reals i verificables, especialment la Recomanació de la UNESCO sobre l’ètica de la IA, els principis de l’OCDE i l’EU AI Act.

## Marc conceptual: Protecció humana
AsimovWatch incorpora Protecció humana com a capa editorial i marc conceptual propi per analitzar fins a quin punt sistemes, empreses i institucions orienten el desenvolupament de la IA cap a la protecció efectiva de les persones. Aquest concepte no substitueix els marcs verificables existents, sinó que es construeix sobre estàndards reals i contrastables com la Recomanació de la UNESCO sobre l’ètica de la IA, els principis de l’OCDE i l’EU AI Act.

La Protecció humana s’entén com una manera de comparar el que els actors declaren, el que realment es pot verificar i a quin nivell tècnic s’apliquen les mesures de salvaguarda. El projecte no pretén decidir de manera definitiva què és ètic, sinó obrir un espai de lectura crítica que ajudi a detectar si la IA es desplega amb una orientació real al bé de la humanitat o només amb una narrativa declarativa.

### Criteri editorial
El relat d’AsimovWatch ha de distingir clarament entre tres nivells: allò que un actor diu que fa, allò que es pot comprovar amb evidència i allò que efectivament està integrat en el producte, el model, la infraestructura o el xip. Aquesta separació és necessària per evitar confondre màrqueting, política pública i implementació tècnica.

## Llengua i contingut
La llengua per defecte del projecte és el català. Les traduccions a ES i EN s’han de fer sota demanda i guardar-se a la base de dades per evitar crides repetides.

## Arquitectura de lectura i classificació
La classificació i l’enriquiment d’informació han de seguir una arquitectura multimotor: Perplexity per buscar novetats, Claude per estructurar i classificar, i Mistral OCR per a PDFs i documents escanejats.

## Supervisió humana
Abans de publicar qualsevol entrada al sistema, cal validació humana. El sistema pot suggerir, resumir i classificar, però no ha de publicar automàticament contingut sensible o ambigu.

## Fase d’ingesta de fonts
### Fase 1 — MVP inicial
S’hi inclouen fonts institucionals i organismes oficials: UNESCO, OCDE, EU AI Office, Parlament Europeu, Council of Europe i NIST (EUA).

### Fase 1 — Fonts xineses prioritàries
També s’hi poden afegir fonts xineses dins la primera fase: DigiChina (Stanford), Xinhua Tech i CAICT.

### Fase 2 — Labs i empreses d’IA
En una segona fase s’incorporen fonts de laboratoris i empreses d’IA: OpenAI, Anthropic, DeepMind i Meta AI.

### Fase 2 — Xips i semiconductors
També s’hi afegeixen fonts del sector de xips i semiconductors: NVIDIA, Intel, TSMC i ASML.

### Fase 3 — Ampliació acadèmica i institucional
En una tercera fase s’amplien les fonts amb governs nacionals com gov.uk AI, Bundestag i Sénat France.

### Fase 3 — Acadèmia i recerca
També s’hi afegeixen arXiv cs.AI ethics, SSRN i altres fonts acadèmiques o de recerca aplicada.

### Fase 3 — Think tanks
Per a una fase més avançada s’incorporen AI Now Institute, Future of Life i AlgorithmWatch.

## Risc i prudència
No s’ha d’exposar l’API sense autenticació bàsica quan hi hagi dades sensibles. També convé començar amb poques fonts i ampliar-les amb validació progressiva.

## Criteri operatiu
Cada nova font ha de tenir una finalitat clara: cobertura geogràfica, cobertura institucional o diversitat de perspectives. Si una font no aporta valor diferencial, es deixa fora.

## Configuració runtime de models i prompts

La configuració activa dels models i prompts del sistema es gestiona des del panell admin i es persisteix a la base de dades. Els valors definits al codi s’han de considerar valors base o de reserva, però no necessàriament la configuració efectiva en producció.

Això permet ajustar providers, models i prompts sense redeploy immediat del backend i facilita iterar sobre el comportament del sistema des del backoffice.

Com a criteri operatiu, qualsevol canvi que es consolidi com a configuració estable s’hauria de reflectir també a la documentació del repositori, per evitar que el coneixement del sistema quedi només dins la base de dades.

## Decisions operatives recents — juliol de 2026

### Descoberta separada de promoció
La descoberta automàtica de fonts no crea fonts actives directament. Tota troballa nova entra primer com a `source_candidate` i només pot esdevenir `source` després de revisió humana i promoció explícita.

**Motiu:** mantenir la coherència amb AsimovWatch com a observatori editorial i no com a sistema de publicació automàtica.

### Supervisió humana obligatòria per a noves fonts
Cap candidate es pot promocionar si no està en estat `APPROVED`. El sistema pot descobrir, classificar i proposar, però la decisió editorial final continua essent humana.

**Motiu:** la fiabilitat i la pertinència editorial d’una font no s’han d’automatitzar completament.

### `dry_run=True` no persisteix dades
S’ha decidit que `dry_run=True` serveixi només per validar si el crawler troba fonts potencialment útils. Aquest mode no escriu cap resultat a la base de dades.

**Motiu:** separar clarament simulació i persistència, i evitar contaminar la taula de candidates amb execucions provisionals.

### No es mostrarà detall operatiu complet del `dry_run=True` a l’admin
Per ara, no es construirà una vista específica per navegar els resultats detallats d’un `dry_run=True` dins del panell admin. El resum operatiu indica si hi ha hagut troballes, però per revisar-les realment cal fer un `dry_run=False`.

**Motiu:** simplificar la UX editorial i evitar una doble capa de gestió entre resultats temporals i persistits.

### El crawler manual passa a formar part del flux editorial normal
El botó “Run now” del panell admin no és només una eina tècnica de prova, sinó una part del flux editorial de descoberta. El seu resultat, quan s’executa amb `dry_run=False`, genera candidates reals que entren a la cua de revisió.

**Motiu:** donar al panell una funció operativa real sobre el radar de fonts.

### Gemini és el provider LLM operatiu actual del discovery
Tot i que el sistema ja incorpora configuració runtime de models i prompts, la descoberta de `source_candidates` funciona avui efectivament amb Gemini com a provider real.

**Motiu:** prioritzar un MVP estable i funcional abans del refactor complet a una arquitectura modular de `llm_clients_xxxx` amb múltiples proveïdors.