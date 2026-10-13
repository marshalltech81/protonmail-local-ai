"""Provider policy for mailbox occurrences (PLAN.md decision 45, #1568,
#1569).

Folder names are account-relative, and what a folder means (its role
and its source category) is the provider's: the Trash folder that
mailbox-wide retrieval leaves out by default (#441), the Spam and
Recovered Messages folders whose mail never counts toward an
``authority_class`` (#463, #1317), and each occurrence's source category
are dispatched by the provider of the occurrence's account
(``accounts.provider``), so an account of another provider never
inherits Proton's names. Today the only provider is ``proton-bridge``
(``SOURCE_MODE``), so these are today's defaults.

The SQL these helpers build reads an occurrence ``o`` and binds only
constants from this module, inlined as quoted literals so the fragments
can sit in constant column lists; no caller value reaches them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderPolicy:
    """What a provider's folder names mean: ``roles`` maps a role
    (``trash``, ``spam``, ``recovered``) to the folders holding it, and
    ``categories`` a source category to its folders, both matched
    exactly; ``category_prefixes`` maps a category to folder-name
    prefixes whose child folders hold it (``Folders/`` for Proton's own
    folders)."""

    roles: dict[str, tuple[str, ...]]
    categories: dict[str, tuple[str, ...]]
    category_prefixes: dict[str, tuple[str, ...]]


# Source categories (#1317, #1569): ``mailbox`` is ordinary synced mail;
# ``unsent`` is Drafts and Scheduled, unsent evidence whose folder does
# not certify that it is still unsent; ``recovered`` is Bridge-local
# recovery mail, searchable but untrusted (its identity headers are the
# client's, never authoritative). A folder, or provider, the policy does
# not name is ``unknown``, never a default.
CATEGORY_UNKNOWN = "unknown"
_PROTON_RECOVERED = ("Recovered Messages",)

PROVIDER_POLICIES: dict[str, ProviderPolicy] = {
    "proton-bridge": ProviderPolicy(
        roles={"trash": ("Trash",), "spam": ("Spam",), "recovered": _PROTON_RECOVERED},
        categories={
            "mailbox": ("INBOX", "Sent", "Archive", "Spam", "Trash"),
            "unsent": ("Drafts", "Scheduled"),
            "recovered": _PROTON_RECOVERED,
        },
        category_prefixes={"mailbox": ("Folders/",)},
    ),
}


def category_of(provider: str, folder: str) -> str:
    """The source category of a copy in ``folder`` under an account of
    ``provider``, derived at read time (nothing is stored, so a policy
    change needs no migration); ``CATEGORY_UNKNOWN`` when the policy
    does not name the folder or the provider has no policy."""
    policy = PROVIDER_POLICIES.get(provider)
    if policy is None:
        return CATEGORY_UNKNOWN
    for category, names in policy.categories.items():
        if folder in names:
            return category
    for category, prefixes in policy.category_prefixes.items():
        if any(folder.startswith(p) and len(folder) > len(p) for p in prefixes):
            return category
    return CATEGORY_UNKNOWN


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def role_sql(role: str, alias: str = "o") -> str:
    """SQL that is 1 when occurrence ``alias`` is in a folder that holds
    ``role`` under its account's provider, else 0. Never NULL: an
    occurrence's account and folder are always stored."""
    terms = []
    for provider, policy in sorted(PROVIDER_POLICIES.items()):
        folders = policy.roles.get(role, ())
        if not folders:
            continue
        terms.append(
            f"({alias}.folder IN ({', '.join(_literal(f) for f in folders)}) "  # nosec B608 - module constants only
            f"AND EXISTS (SELECT 1 FROM accounts ac WHERE ac.id = {alias}.account_id "
            f"AND ac.provider = {_literal(provider)}))"
        )
    return "(" + " OR ".join(terms) + ")" if terms else "0"


def active_sql(alias: str = "o") -> str:
    """SQL that holds for an occurrence that counts as a current copy:
    folder filters, folder browsers, flag filters and the default scope
    match only these (#1568). A retired occurrence (its file vanished or
    was replaced) is history, not a copy."""
    return f"{alias}.state = 'active'"


def live_sql(alias: str = "o") -> str:
    """SQL that holds for a live occurrence: active and not ``T``-flagged
    (#1567)."""
    return f"{active_sql(alias)} AND {alias}.trashed = 0"


def default_scope_sql(alias: str = "o") -> str:
    """SQL that holds for an occurrence that puts its message in
    mailbox-wide retrieval's default scope (#441; owner decision on
    #1568, 2026-10-13): an active copy outside its provider's Trash role,
    counting the message's live (not ``T``-flagged) active copies when it
    has any, else all its active copies. So a message whose only active
    copies are ``T``-flagged outside Trash stays listed, as before
    occurrences; one whose only active copies are in Trash, or that has
    no active copy, does not. The ``T`` fallback looks at the claimant's
    whole active population, across accounts, whatever flag filters the
    query adds, while those filters stay conjoined on the same copy.

    The one definition every default-scoped reader uses: the predicates'
    ``default_scope`` leaf, the search folder membership and its
    ``DefaultFolderScope`` lookup, and the attachment search default."""
    return (
        f"{active_sql(alias)} AND NOT {role_sql('trash', alias)} "
        f"AND ({alias}.trashed = 0 OR NOT EXISTS (SELECT 1 FROM message_occurrences o2 "  # nosec B608 - module constants only
        f"WHERE o2.claimant_id = {alias}.claimant_id AND {live_sql('o2')}))"
    )


# The order in which a message's occurrences are listed, and the first of
# which is its representative (the single folder and flags shown for the
# message): active first, then not ``T``-flagged, then outside Trash,
# then the oldest row.
REPRESENTATIVE_ORDER = f"o.state = 'active' DESC, o.trashed, {role_sql('trash')}, o.id"

# ``REPRESENTATIVE_ORDER`` as one integer per occurrence, the lowest
# first, with the row id in its low 40 bits: a ``MIN`` over a message's
# occurrences names its representative without a sort.
REPRESENTATIVE_ID_BITS = 1 << 40
REPRESENTATIVE_KEY = (
    f"(((o.state <> 'active') * 4 + o.trashed * 2 + {role_sql('trash')}) "
    f"* {REPRESENTATIVE_ID_BITS} + o.id)"
)


def authority_excluded_sql(alias: str = "o") -> str:
    """SQL that is 1 when occurrence ``alias`` is in a folder whose mail
    never counts toward an ``authority_class``: Spam, where the provider
    files most spoofed mail (#463), and recovery mail, whose identity
    headers are the client's (#1317, #1569)."""
    return f"({role_sql('spam', alias)} OR {role_sql('recovered', alias)})"
