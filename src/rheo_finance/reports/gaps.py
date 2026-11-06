"""One bill-document gap policy shared by owed and status readers."""

from rheo_finance.documents import issuers


def bill_issuer(issuer: str) -> bool:
    rule = issuers.ISSUER_DIRS.get(issuer.split("/", 1)[0])
    return rule is not None and rule.kind == "bill"
