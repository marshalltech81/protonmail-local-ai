"""Deterministic entity resolution.

Every participant address becomes a **person** entity keyed by its
canonical address (``threader.canonical_addr``); each display name seen
for that address is recorded as an alias of that one entity. Two
different addresses are never merged, however similar their names: a
display name is sender-controlled, so name similarity is not identity.

Each person's exact address domain becomes an **organization** entity,
except for the common free-mail providers below, whose users share a
domain but not an organization. The domain is taken as written, with
no public-suffix lookup: ``mail.northwind.example`` and
``northwind.example`` are two organizations rather than a guessed
merge.

Entity IDs are deterministic (``person:<address>``,
``org:<domain>``), so reprocessing a message rewrites the same rows.
"""

PERSON_PREFIX = "person:"
ORG_PREFIX = "org:"

# Common free-mail domains: an address here names a person, not an
# organization. Kept deliberately small; anything missing just gets an
# organization entity of its own, which merges nobody.
FREE_MAIL_DOMAINS = frozenset(
    {
        "aol.com",
        "fastmail.com",
        "gmail.com",
        "gmx.com",
        "gmx.de",
        "googlemail.com",
        "hey.com",
        "hotmail.com",
        "icloud.com",
        "live.com",
        "mac.com",
        "mail.com",
        "me.com",
        "msn.com",
        "outlook.com",
        "pm.me",
        "proton.me",
        "protonmail.com",
        "qq.com",
        "tutanota.com",
        "yahoo.com",
        "yandex.ru",
        "zoho.com",
    }
)


def address_domain(address: str) -> str:
    """The domain part of a canonical address, or ``""``."""
    _, at, domain = address.rpartition("@")
    return domain if at else ""


def organization_domain(address: str) -> str | None:
    """The organization domain for a canonical address, or ``None`` for
    a free-mail provider or an address without a domain."""
    domain = address_domain(address)
    if not domain or domain in FREE_MAIL_DOMAINS:
        return None
    return domain


def person_entity_id(address: str) -> str:
    return PERSON_PREFIX + address


def org_entity_id(domain: str) -> str:
    return ORG_PREFIX + domain
