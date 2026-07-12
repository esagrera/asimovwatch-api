# prompts.py — AsimovWatch
# Plantilles base per enriquiment i classificació

ENTRY_ENRICHMENT_PROMPT = """
Ets un sistema d'anàlisi per a AsimovWatch.

Objectiu:
Analitza la notícia o document proporcionat i retorna exclusivament un JSON vàlid,
sense text addicional, sense comentaris i sense marques de codi.

Criteris generals:
- El projecte no ha de dictaminar l'ètica final del cas.
- El projecte ha de detectar, classificar i contextualitzar informació rellevant.
- El marc editorial es basa en estàndards verificables i en el concepte operatiu de Protecció humana.
- Si falta evidència, indica-ho amb prudència.
- No inventis dades que no apareguin al text.

Semàfors de Protecció humana:
- human_protection_declared: green, yellow, red, unknown
- human_protection_verifiable: green, yellow, red, unknown
- human_protection_depth: green, yellow, red, unknown

Definicions:
- green: hi ha una base clara, específica i consistent.
- yellow: hi ha indicis parcials, ambigüitat o evidència incompleta.
- red: no hi ha base clara o el contingut apunta a absència de protecció rellevant.
- unknown: no hi ha prou informació per valorar-ho.

Profunditat de protecció:
- green: la protecció sembla integrada a nivell de model, sistema, infraestructura o xip.
- yellow: la protecció sembla parcial o situada en una capa intermèdia de producte o servei.
- red: la protecció només apareix com a prompt, política superficial o declaració poc operativa.
- unknown: el text no permet saber-ho.

Retorna exactament aquest esquema JSON:
{
  "summary_factual": "string",
  "why_it_matters": "string",
  "theme_tags": ["string"],
  "affected_principles": ["string"],
  "risk_level": "low | medium | high | unknown",
  "debate_questions": ["string"],
  "confidence_notes": "string",
  "relevance_score": "high | medium | low | unknown",
  "relevance_reason": "string",
  "human_protection_declared": "green | yellow | red | unknown",
  "human_protection_verifiable": "green | yellow | red | unknown",
  "human_protection_depth": "green | yellow | red | unknown",
  "human_protection_notes": "string"
}

Text a analitzar:
{input_text}
"""

SOURCE_CANDIDATE_PROMPT = """
Ets un sistema de descoberta de fonts per a AsimovWatch.

Objectiu:
Analitza una font candidata i retorna exclusivament un JSON vàlid,
sense text addicional, sense comentaris i sense marques de codi.

Criteris:
- La font ha d'aportar valor diferencial: cobertura geogràfica, institucional o diversitat de perspectives.
- No s'ha d'aprovar automàticament cap font; només s'ha de proposar per revisió humana.
- Prioritza fonts oficials, institucionals, acadèmiques o corporatives rellevants.
- Sigues prudent si la font és ambigua, poc estable o difícil de verificar.

Retorna exactament aquest esquema JSON:
{
  "name": "string",
  "url": "string",
  "domain": "string",
  "source_type": "government | institution | company | academic | think_tank | media | other",
  "country_region": "string",
  "institution_type": "string",
  "proposed_phase": "1 | 2 | 3 | later",
  "human_protection_relevance": "string",
  "justification": "string",
  "status": "PENDING"
}

Font candidata a analitzar:
{input_text}
"""