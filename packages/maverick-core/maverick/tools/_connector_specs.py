"""Exact read-only connector specifications retained by the law firm."""
from __future__ import annotations

# These are deliberately already named ``*_read``. The builder exposes GET
# only and therefore cannot submit, sign, update, or delete third-party data.
_SPECS: list[dict] = [
    {
        "name": "carta_read",
        "base_url_env": "CARTA_BASE_URL",
        "token_env": "CARTA_TOKEN",
        "description": (
            "Carta equity and capitalization records, READ-ONLY (GET). "
            "Auth: CARTA_BASE_URL + CARTA_TOKEN."
        ),
    },
    {
        "name": "clio_read",
        "base_url_env": "CLIO_BASE_URL",
        "token_env": "CLIO_TOKEN",
        "default_base_url": "https://app.clio.com",
        "description": (
            "Clio legal practice-management records, READ-ONLY (GET). "
            "Auth: CLIO_BASE_URL + CLIO_TOKEN."
        ),
    },
    {
        "name": "contractbook_read",
        "base_url_env": "CONTRACTBOOK_BASE_URL",
        "token_env": "CONTRACTBOOK_TOKEN",
        "default_base_url": "https://api.contractbook.com",
        "description": (
            "Contractbook contract records, READ-ONLY (GET). "
            "Auth: CONTRACTBOOK_BASE_URL + CONTRACTBOOK_TOKEN."
        ),
    },
    {
        "name": "docusign_read",
        "base_url_env": "DOCUSIGN_BASE_URL",
        "token_env": "DOCUSIGN_TOKEN",
        "description": (
            "DocuSign envelope and signature status, READ-ONLY (GET). "
            "Auth: DOCUSIGN_BASE_URL + DOCUSIGN_TOKEN."
        ),
    },
    {
        "name": "ironclad_read",
        "base_url_env": "IRONCLAD_BASE_URL",
        "token_env": "IRONCLAD_TOKEN",
        "default_base_url": "https://ironcladapp.com",
        "description": (
            "Ironclad contract workflow records, READ-ONLY (GET). "
            "Auth: IRONCLAD_BASE_URL + IRONCLAD_TOKEN."
        ),
    },
]

# Compatibility export: the firm ships no GraphQL connector.
_GRAPHQL_SPECS: list[dict] = []

__all__ = ["_GRAPHQL_SPECS", "_SPECS"]
