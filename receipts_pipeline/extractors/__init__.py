"""Receipt body extractors. Each module exports DOMAINS (set of sender_domain values)
and extract(body, subject) -> dict|None. Returned dict keys:
  amount_cents (required), merchant_norm (required), merchant_raw, txn_date,
  confidence (0.0-1.0), method (e.g. 'template:uber'), notes.
"""

from . import uber, lyft, capitalbikeshare, amazon_email, generic_regex, llm_fallback

# Domain -> list of template extractor modules to try in order.
TEMPLATE_REGISTRY = {}
for mod in (uber, lyft, capitalbikeshare, amazon_email):
    for dom in mod.DOMAINS:
        TEMPLATE_REGISTRY.setdefault(dom, []).append(mod)
