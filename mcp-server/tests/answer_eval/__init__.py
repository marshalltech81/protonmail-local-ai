"""Answer-quality evaluation for ``ask_mailbox`` (#604).

An offline development tool, not part of the server: it runs the
registered ``ask_mailbox`` handler against the synthetic baseline index,
captures the evidence actually sent to inference, grades the answer with
deterministic checks and (optionally) a separately configured AI judge,
and compares runs. See ``tests/eval/README.md``, "Answer-quality
evaluation".
"""
