"""
Importa app.crawler i app.main sense llegir cap .env: ambdos (i app.db) fan `load_dotenv()` en
importar-se, aixi que se substitueix temporalment per una funcio que no fa res.
"""

import dotenv


def import_pipeline():
    original = dotenv.load_dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False
    try:
        import app.crawler as crawler
        import app.main as main
    finally:
        dotenv.load_dotenv = original
    return crawler, main
