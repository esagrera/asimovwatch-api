# AsimovWatch — Decisions de disseny
Actualitzat: 9 d'agost de 2026

# Principis bàsics
AsimovWatch no pretén dictaminar l’ètica final de cada cas, sinó detectar, classificar i contextualitzar allò que passa al món sobre IA responsable.

# Marc de referència
El marc editorial i conceptual s’ha de basar en estàndards reals i verificables, especialment la Recomanació de la UNESCO sobre l’ètica de la IA, els principis de l’OCDE i l’EU AI Act.

## Marc conceptual: Protecció humana
AsimovWatch incorpora Protecció humana com a capa editorial i marc conceptual propi per analitzar fins a quin punt sistemes, empreses i institucions orienten el desenvolupament de la IA cap a la protecció efectiva de les persones. Aquest concepte no substitueix els marcs verificables existents, sinó que es construeix sobre estàndards reals i contrastables com la Recomanació de la UNESCO sobre l’ètica de la IA, els principis de l’OCDE i l’EU AI Act.

La Protecció humana s’entén com una manera de comparar el que els actors declaren, el que realment es pot verificar i a quin nivell tècnic s’apliquen les mesures de salvaguarda. El projecte no pretén decidir de manera definitiva què és ètic, sinó obrir un espai de lectura crítica que ajudi a detectar si la IA es desplega amb una orientació real al bé de la humanitat o només amb una narrativa declarativa.

### Criteri editorial
El relat d’AsimovWatch ha de distingir clarament entre tres nivells: allò que un actor diu que fa, allò que es pot comprovar amb evidència i allò que efectivament està integrat en el producte, el model, la infraestructura o el xip. Aquesta separació és necessària per evitar confondre màrqueting, política pública i implementació tècnica.

Directrius de protecció humana incorporada (Directrius BIHP)
Per evitar que «BIHP» designi alhora el mètode d’avaluació i l’objecte avaluat, AsimovWatch distingeix dos conceptes complementaris:

- **Marc BIHP** (Built-in Human Protection): la metodologia pròpia d’avaluació, amb els tres eixos —protecció declarada, protecció verificable i profunditat d’implementació— i les quatre etiquetes green, yellow, red i unknown descrites més amunt.

- **Directrius BIHP** (Directrius de protecció humana incorporada): l’objecte concret que el Marc BIHP avalua. Són les instruccions, polítiques, llindars operatius i mecanismes tècnics que un sistema d’intel·ligència artificial té —o hauria de tenir— configurats per limitar el risc que suposa per a les persones. No és un concepte abstracte: es materialitza en artefactes verificables com marcs de seguretat de frontera (p. ex. una Responsible Scaling Policy, un Preparedness Framework o un Frontier Safety Framework), llindars de capacitat que activen mesures addicionals, protocols de revisió humana abans de desplegar canvis sensibles, compromisos de red-teaming extern, canals de notificació d’incidents, o restriccions configurades directament al model o al producte (instruccions de sistema orientades a seguretat, filtres, mecanismes de refús).

Perquè una Directriu BIHP concreta es pugui considerar identificada —i no merament suposada—, ha de complir tres condicions mínimes:

1. Estar documentada per l’actor o per un tercer independent, amb una font consultable i datada.

2. Ser específica: un compromís genèric ("treballem per una IA segura") no compta com a Directriu BIHP si no es concreta en cap política, llindar o mecanisme identificable.

3. Tenir vigència identificable (versió, data de publicació o d’actualització), perquè el marc regulatori i les pràctiques del sector canvien ràpidament.

Cada Directriu BIHP identificada s’avalua amb el Marc BIHP existent: es documenta si l’actor la declara, si hi ha evidència independent que la verifiqui, i a quin nivell tècnic està efectivament implementada. Si l’evidència és insuficient per a algun d’aquests eixos, es marca unknown en lloc d’inferir-hi una conclusió no demostrada — el mateix criteri de prudència que ja regeix la resta de la classificació.

Aquest inventari de Directrius BIHP, aplicat sistema per sistema i proveïdor per proveïdor, és el material de base que ha d’alimentar tant la classificació de novetats individuals com la base pública progressiva de sistemes d’IA prevista a llarg termini al index.html. La comparativa de 6 proveïdors LLM (docs/bihp-comparativa-6-proveidors-2026-08-05.md, 5 d’agost de 2026) n’és el primer precedent aplicat dins del projecte.

# Llengua i contingut
La llengua per defecte del projecte és el català. Les traduccions a ES i EN s’han de fer sota demanda i guardar-se a la base de dades per evitar crides repetides.

# Gestió de LLM i prompts

AsimovWatch utilitza una arquitectura multimodal i multi-proveïdor, sense quedar vinculat a models o plataformes concretes. Els models es poden combinar i substituir segons les necessitats de cada tasca, prompt i modalitat de contingut.

La selecció dels proveïdors i models correspon a la supervisió humana, amb el suport de l’informe de recomanació de models i de la informació disponible sobre la classificació BIHP. Aquesta classificació serveix com a criteri de referència, però no substitueix la decisió editorial ni tècnica.

La configuració activa de proveïdors, models, prompts i paràmetres es gestiona des del panell `admin` i es persisteix a la base de dades. Els valors definits al codi funcionen com a configuració base o de reserva. Els canvis consolidats com a decisions estables s’han de reflectir també a la documentació del repositori.

# Supervisió humana
Abans de publicar qualsevol entrada al sistema, cal validació humana. El sistema pot suggerir, resumir i classificar, però no ha de publicar automàticament contingut sensible o ambigu.

# Fase d’ingesta de fonts
Fase 1 — MVP inicial
S’hi inclouen fonts institucionals i organismes oficials: UNESCO, OCDE, EU AI Office, Parlament Europeu, Council of Europe i NIST (EUA).

## Fase 1 — Fonts xineses prioritàries
També s’hi poden afegir fonts xineses dins la primera fase: DigiChina (Stanford), Xinhua Tech i CAICT.

## Fase 2 — Labs i empreses d’IA
En una segona fase s’incorporen fonts de laboratoris i empreses d’IA: OpenAI, Anthropic, DeepMind i Meta AI.

## Fase 2 — Xips i semiconductors
També s’hi afegeixen fonts del sector de xips i semiconductors: NVIDIA, Intel, TSMC i ASML.

## Fase 3 — Ampliació acadèmica i institucional
En una tercera fase s’amplien les fonts amb governs nacionals com gov.uk AI, Bundestag i Sénat France.

## Fase 3 — Acadèmia i recerca
També s’hi afegeixen arXiv cs.AI ethics, SSRN i altres fonts acadèmiques o de recerca aplicada.

## Fase 3 — Think tanks
Per a una fase més avançada s’incorporen AI Now Institute, Future of Life i AlgorithmWatch.

# Risc i prudència
No s’ha d’exposar l’API sense autenticació bàsica quan hi hagi dades sensibles. També convé començar amb poques fonts i ampliar-les amb validació progressiva.

# Criteri operatiu
Cada nova font ha de tenir una finalitat clara: cobertura geogràfica, cobertura institucional o diversitat de perspectives. Si una font no aporta valor diferencial, es deixa fora.

# Decisions operatives recents — juliol de 2026
## Descoberta separada de promoció
La descoberta automàtica de fonts no crea fonts actives directament. Tota troballa nova entra primer com a source_candidate i només pot esdevenir source després de revisió humana i promoció explícita.

**Motiu**: mantenir la coherència amb AsimovWatch com a observatori editorial i no com a sistema de publicació automàtica.

## Supervisió humana obligatòria per a noves fonts
Cap candidate es pot promocionar si no està en estat APPROVED. El sistema pot descobrir, classificar i proposar, però la decisió editorial final continua essent humana.

**Motiu**: la fiabilitat i la pertinència editorial d’una font no s’han d’automatitzar completament.

### dry_run=True no persisteix dades
S’ha decidit que dry_run=True serveixi només per validar si el crawler troba fonts potencialment útils. Aquest mode no escriu cap resultat a la base de dades.

**Motiu**: separar clarament simulació i persistència, i evitar contaminar la taula de candidates amb execucions provisionals.

### No es mostrarà detall operatiu complet del dry_run=True a l’admin
Per ara, no es construirà una vista específica per navegar els resultats detallats d’un dry_run=True dins del panell admin. El resum operatiu indica si hi ha hagut troballes, però per revisar-les realment cal fer un dry_run=False.

**Motiu**: simplificar la UX editorial i evitar una doble capa de gestió entre resultats temporals i persistits.

## El crawler manual passa a formar part del flux editorial normal
El botó “Run now” del panell admin no és només una eina tècnica de prova, sinó una part del flux editorial de descoberta. El seu resultat, quan s’executa amb dry_run=False, genera candidates reals que entren a la cua de revisió.

**Motiu**: donar al panell una funció operativa real sobre el radar de fonts.

# Decisions conceptuals recents — agost de 2026
## Introducció formal de les Directrius BIHP com a concepte propi
S’incorpora a la documentació conceptual el terme Directrius de protecció humana incorporada (Directrius BIHP), per distingir explícitament l’objecte que s’avalua (les instruccions, polítiques i mecanismes que un sistema d’IA té o hauria de tenir configurats per protegir les persones) del mètode d’avaluació ja existent (el Marc BIHP, amb els seus tres eixos i quatre etiquetes). Vegeu la secció «Directrius de protecció humana incorporada (Directrius BIHP)» dins de «Marc conceptual: Protecció humana».

**Motiu**: evitar que «BIHP» es faci servir de manera ambigua per referir-se alhora al mètode d’avaluació i al que s’avalua. La comparativa de 6 proveïdors LLM (docs/bihp-comparativa-6-proveidors-2026-08-05.md, 5 d’agost de 2026) ja aplicava aquesta distinció de manera implícita —RSP, Preparedness Framework o Frontier Safety Framework hi funcionen com a Directrius BIHP concretes avaluades amb el Marc BIHP— i calia fer-la explícita a la documentació de referència.

# Decisions operatives — cerca temàtica i ingesta d’entries

## La cerca temàtica no replica el crawler de fonts

La cerca temàtica no farà una cerca separada sobre les fonts monitoritzades. Les fonts promogudes ja disposen d’un flux RSS/Atom i les seves entries ja són ingerides a `public.entries`.

**Motiu**: evitar duplicar funcionalitat, reduir costos i mantenir una separació clara entre:

- ingesta contínua de fonts monitoritzades;
- descoberta puntual de noves fonts o casos mitjançant cerca web.

La cerca temàtica pot trobar una font que ja està monitoritzada. En aquest cas, el sistema ha d’aplicar la deduplicació i informar de l’ID de l’entry ja existent.

## La cerca temàtica és una fase de descoberta

El prompt `Thematic entry discovery` serveix per localitzar candidates web verificables a partir d’un brief editorial.

No és un prompt d’enriquiment. No ha de:

- redactar un article final;
- classificar definitivament el risc;
- assignar una etiqueta BIHP definitiva;
- proposar Directrius BIHP;
- inferir protecció humana efectiva;
- substituir la revisió humana.

La seva sortida és una llista de candidates amb URL, títol i metadades bàsiques.

## Resultats de cerca com a entries crues

Les candidates vàlides de la cerca temàtica entren a `public.entries` amb:

- `review_status = 'NEW'`;
- `processing_status = 'RAW'`;
- `ingest_status = 'ingested'`;
- `ingest_method = 'web_search'`.

Les dades d’enriquiment no s’han d’omplir durant la descoberta. La cerca i l’enriquiment són fases separades del pipeline.

## Interpolació de prompts

Els prompts de cerca poden contenir placeholders:

```text
{{brief}}
{{date_range}}
{{source_scope}}
{{source_types}}
{{max_results}}
```

El backend els ha de substituir abans de la crida al model.

El model no ha de rebre aquests placeholders sense resoldre, ja que això pot provocar que interpreti literalment el text de plantilla i no el brief real.

## Capacitat de cerca obligatòria

La cerca temàtica web només es pot executar amb un provider/model que tingui capacitat real de cerca web.

Si el model configurat no té aquesta capacitat:

- no s’ha de simular una cerca web amb coneixement intern;
- no s’han d’inventar URLs ni dates;
- no s’han de crear entries basades en una resposta no verificable;
- l’API ha de retornar un error explícit i no executar la cerca.

La capacitat de cerca és una propietat operativa del provider/model configurat, no una propietat que el prompt pugui crear per si sol.

## Validació de resultats

Abans d’inserir una candidata cal validar com a mínim:

- que sigui un objecte JSON vàlid;
- que tingui `title`;
- que tingui `url`;
- que la URL sigui absoluta;
- que comenci per `http://` o `https://`;
- que no sigui una URL placeholder;
- que no sigui un duplicat.

Si la informació no és suficient, la candidata s’ha de descartar. No s’han d’inferir dades que no apareguin a la resposta o que no es puguin verificar.

## Duplicats i fonts monitoritzades

La resposta de la cerca temàtica ha d’informar dels IDs de les entries existents detectades pel mecanisme de deduplicació.

La resposta distingeix:

- `items_duplicates`: IDs de duplicats generals;
- `items_duplicates_monitored`: IDs de duplicats corresponents a fonts monitoritzades;
- `warnings`: informació contextual sobre les fonts i entries ja existents.

Aquesta informació serveix per mesurar la cobertura del radar i identificar quan una cerca temàtica no aporta una entry nova perquè el contingut ja havia estat ingerit.

## Un únic pipeline posterior

Després de la inserció, les entries RSS i les de cerca temàtica han de seguir el mateix pipeline de revisió i enriquiment.

La procedència es manté amb `ingest_method`, però no s’ha de duplicar la lògica d’enriquiment segons l’origen.

El flux de referència és:

```text
ingesta (RSS o cerca temàtica)
      │
      ▼
public.entries (RAW, review_status='NEW')
      │
      ▼
pipeline d'enriquiment (Input → Primary → Output)
      │
      ├─ summary_factual
      ├─ why_it_matters
      ├─ theme_tags
      ├─ affected_principles
      ├─ debate_questions
      ├─ risk_level
      ├─ human_protection_declared
      ├─ human_protection_verifiable
      ├─ human_protection_depth
      ├─ human_protection_notes
      └─ enriched_model (provider + model)
      │
      ▼
processing_status = 'ENRICHED' (o 'ERROR')
      │
      ▼
revisió humana (amb totes les dades anteriors)
      │
      ▼
review_status = 'APPROVED' o 'REJECTED'
      │
      └─ reviewer, reviewed_at, editor_notes, validation_notes
        
```

L’enriquiment ha de conservar el principi de prudència metodològica d’AsimovWatch: quan no hi hagi evidència suficient, els camps d’avaluació de Protecció humana han d’utilitzar `unknown` i no una inferència optimista o negativa no demostrada.
