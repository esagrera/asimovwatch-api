import json
import os
from typing import Any, Dict, List

import requests

from prompts import ENTRY_ENRICHMENT_PROMPT, SOURCE_CANDIDATE_PROMPT


ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
FEEDS_PATH = os.path.join(ROOT_DIR, "feeds.json")
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")


def load_sources() -> List[Dict[str, Any]]:
    if not os.path.exists(FEEDS_PATH):
        return []
    with open(FEEDS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def enrich_source(source: Dict[str, Any]) -> Dict[str, Any]:
    domain = (source.get("domain") or "").lower()
    name = source.get("name", "")

    if "unesco" in domain:
        return {
            "source_url": source.get("url", ""),
            "source_domain": domain,
            "source_title": name,
            "raw_content": "",
            "summary_factual": "Font institucional de referència sobre ètica i governança de la IA.",
            "why_it_matters": "Pot aportar criteris i marcs de política pública verificables.",
            "theme_tags": ["ethics", "governance", "policy"],
            "affected_principles": ["transparency", "human_oversight"],
            "risk_level": "low",
            "debate_questions": [
                "Quin criteri de supervisió humana aplica?",
                "Com es tradueix aquest marc a pràctica?"
            ],
            "confidence_notes": "Prova de pipeline amb enriquiment dummy.",
            "review_status": "NEW",
            "human_protection_declared": "unknown",
            "human_protection_verifiable": "unknown",
            "human_protection_depth": "unknown",
            "human_protection_notes": ""
        }

    if "ec.europa" in domain:
        return {
            "source_url": source.get("url", ""),
            "source_domain": domain,
            "source_title": name,
            "raw_content": "",
            "summary_factual": "Font institucional de la UE relacionada amb governança de la IA.",
            "why_it_matters": "Pot indicar canvis reguladors i orientacions oficials.",
            "theme_tags": ["regulation", "governance", "eu"],
            "affected_principles": ["accountability", "transparency"],
            "risk_level": "low",
            "debate_questions": [
                "Quins requisits operatius imposa?",
                "Quin impacte té sobre supervisió i compliment?"
            ],
            "confidence_notes": "Prova de pipeline amb enriquiment dummy.",
            "review_status": "NEW",
            "human_protection_declared": "unknown",
            "human_protection_verifiable": "unknown",
            "human_protection_depth": "unknown",
            "human_protection_notes": ""
        }

    return {
        "source_url": source.get("url", ""),
        "source_domain": domain,
        "source_title": name,
        "raw_content": "",
        "summary_factual": "",
        "why_it_matters": "",
        "theme_tags": [],
        "affected_principles": [],
        "risk_level": "unknown",
        "debate_questions": [],
        "confidence_notes": "Sense enriquiment encara.",
        "review_status": "NEW",
        "human_protection_declared": "unknown",
        "human_protection_verifiable": "unknown",
        "human_protection_depth": "unknown",
        "human_protection_notes": ""
    }


def post_entry(payload: Dict[str, Any]) -> None:
    r = requests.post(f"{API_BASE_URL}/entries", json=payload, timeout=20)
    r.raise_for_status()
    print(f"POST /entries -> {r.status_code}")


def run():
    sources = load_sources()
    print(f"Sources loaded: {len(sources)}")

    for source in sources:
        payload = enrich_source(source)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        post_entry(payload)


if __name__ == "__main__":
    run()