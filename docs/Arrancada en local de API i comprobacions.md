1) Arrenca l’API
Des del directori del projecte:

bash
uvicorn main:app --reload --host 127.0.0.1 --port 8000
Uvicorn servirà l’app localment a http://127.0.0.1:8000, i --reload farà que recarregui quan canviïs el codi.

2) Comprova que respón
Obre una altra terminal i prova:

bash
curl http://127.0.0.1:8000/health
Si tot va bé, hauria de tornar alguna cosa com {"status":"ok"}; aquest endpoint és públic i no necessita API key.

També pots provar:

bash
curl http://127.0.0.1:8000/
Aquest root endpoint també és públic i et dirà que l’API és viva.

3) Prova la connexió amb la base de dades
Si vols validar que el .env i PostgreSQL estan bé, prova:

bash
curl http://127.0.0.1:8000/db-check
Aquest endpoint intenta connectar amb la base de dades i et retornarà connected o l’error exacte.

4) Fes el POST des del terminal
Com que el router està protegit, has d’enviar l’header X-API-Key.

Exemple:

bash
curl -X POST "http://127.0.0.1:8000/entries" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "source_url": "https://example.org/test-entry-human-protection",
    "source_domain": "example.org",
    "source_title": "Test d'entrada Protecció humana",
    "source_type": "other",
    "source_language": "ca",
    "ingest_method": "manual",
    "country_region": "Catalunya",
    "institution_type": "test",
    "raw_snippet": "Entrada de prova per validar el model i la persistència.",
    "raw_content": "Això és una prova tècnica per comprovar que el POST /entries desa correctament els camps.",
    "summary_factual": "Entrada de prova per validar la integració del model.",
    "why_it_matters": "Permet comprovar que l'API accepta i desa els camps nous.",
    "theme_tags": ["test", "validation", "human-protection"],
    "affected_principles": ["transparency", "human_oversight"],
    "risk_level": "low",
    "debate_questions": [
      "El model desa els camps correctament?",
      "El GET retorna la mateixa informació?"
    ],
    "confidence_notes": "Prova manual local.",
    "human_protection_declared": "green",
    "human_protection_verifiable": "yellow",
    "human_protection_depth": "yellow",
    "human_protection_notes": "Prova local dels semàfors de protecció humana.",
    "review_status": "NEW"
  }'
5) Verifica la resposta
Si el POST funciona, t’hauria de tornar 201 amb un id nou.

Després comprova:

bash
curl -H "X-API-Key: $API_KEY" http://127.0.0.1:8000/entries/ID_NOU
Així veuràs si els camps human_protection_* ja es guarden i surten al detall.

Si alguna cosa falla
Si db-check falla, el problema és .env o la connexió a Postgres.

Si /health funciona però POST /entries falla, el problema és l’SQL de create_entry().