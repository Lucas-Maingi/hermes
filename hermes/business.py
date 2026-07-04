"""The per-business (per-tenant) configuration a Hermes agent serves.

Each SME is a ``Business`` with its own catalog, info facts, and integration
config. Nothing about the agent is hard-coded to one shop -- this is the
multi-tenant seam that lets one deployment serve many businesses.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Product:
    name: str
    price: float             # in the business currency (KES)
    aliases: list[str] = field(default_factory=list)
    in_stock: bool = True
    unit: str = ""           # e.g. "2kg", "1L" -- display only

    def matches(self, text: str) -> bool:
        text = text.lower()
        candidates = [self.name.lower(), *(a.lower() for a in self.aliases)]
        return any(c in text or text in c for c in candidates)


@dataclass
class Business:
    id: str
    name: str
    products: list[Product] = field(default_factory=list)
    # Freeform facts the agent may answer from: hours, location, delivery, payment, returns...
    info: dict[str, str] = field(default_factory=dict)
    currency: str = "KES"

    # --- Integration config (used by later milestones; blank = use simulator) ---
    mpesa_shortcode: str = ""            # paybill/till (Daraja BusinessShortCode)
    whatsapp_phone_number_id: str = ""   # Meta Cloud API phone-number id
    staff_handoff_number: str = ""       # where escalations are routed

    def find_product(self, text: str) -> Product | None:
        """Best-effort resolve a customer's words to a catalog product.

        Exact/alias substring first, then a fuzzy fallback -- but only within
        the catalog, so a price is never invented for something not stocked.
        """
        text_l = text.lower().strip()
        for p in self.products:
            if p.matches(text_l):
                return p

        # Fuzzy fallback on the product/alias tokens.
        import difflib

        names = []
        by_name: dict[str, Product] = {}
        for p in self.products:
            for label in [p.name, *p.aliases]:
                names.append(label.lower())
                by_name[label.lower()] = p
        match = difflib.get_close_matches(text_l, names, n=1, cutoff=0.72)
        if match:
            return by_name[match[0]]
        # Also try matching any single word of the query against product labels.
        for word in text_l.split():
            m = difflib.get_close_matches(word, names, n=1, cutoff=0.85)
            if m:
                return by_name[m[0]]
        return None
