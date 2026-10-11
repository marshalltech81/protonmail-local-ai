"""Answer-quality evaluation for the intelligence tools (#604).

An offline development tool, not part of the server: it runs the
registered handler of each case's tool (``ask_mailbox``,
``summarize_thread``, ``extract_from_emails`` and the experimental
``brief_issue`` and ``check_conclusion``) against the synthetic baseline index,
captures the evidence actually sent to inference, grades the answer with
deterministic checks and (optionally) a separately configured AI judge,
and compares runs. See ``tests/eval/README.md``, "Answer-quality
evaluation".
"""
