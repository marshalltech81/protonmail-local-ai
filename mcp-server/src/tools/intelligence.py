"""
Intelligence tools — Group 3 (our differentiator).
Q&A/RAG, summarization, and structured extraction over email threads.
"""

import json
import logging

import httpx
from mcp.types import TextContent

log = logging.getLogger("mcp.tools.intelligence")

SUMMARIZE_SYSTEM = """You are an email assistant. You will be given indexed
thread context from an email thread. The context may be a snippet or truncated
thread text rather than the full raw mailbox thread. Summarize it clearly and
concisely according to the requested style. Be factual. Do not invent
information not present in the provided context."""

ASK_SYSTEM = """You are an email assistant with access to a person's email
archive. You will be given relevant email thread excerpts retrieved from their
mailbox. Answer the user's question based only on the provided email content.
If the answer is not in the provided threads, say so clearly. Be concise and
factual. Cite which thread(s) your answer comes from."""

EXTRACT_SYSTEM = """You are a data extraction assistant. You will be given
indexed email thread context, which may be a snippet or truncated thread text
rather than the full raw mailbox thread. Extract structured data matching the
requested schema. Return ONLY valid JSON matching the schema — no preamble, no
explanation."""


def register_intelligence_tools(server, db, ollama, llm_mode: str, anthropic_key: str):
    async def llm_complete(system: str, user: str) -> str:
        """Route to local Ollama or Claude API based on llm_mode."""
        if llm_mode == "cloud" and anthropic_key:
            return await _claude_complete(system, user, anthropic_key)
        return await ollama.complete(system, user)

    @server.tool()
    async def ask_mailbox(
        question: str,
        from_addr: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        folders: list[str] | None = None,
        max_threads: int = 5,
    ) -> list[TextContent]:
        """
        Ask a natural language question about your email.
        Retrieves relevant threads and generates an answer using an LLM.

        Args:
            question: Your question e.g. "What did my landlord say about the deposit?"
            from_addr: Optionally scope to a specific sender
            date_from: Optionally scope to emails after this date (ISO 8601)
            date_to: Optionally scope to emails before this date (ISO 8601)
            folders: Optionally scope to specific folders
            max_threads: Maximum threads to use as context (default: 5)

        Returns:
            A synthesized answer with source thread references.
        """
        try:
            # Retrieve relevant threads via hybrid search
            embedding = await ollama.embed(question)
            results = db.hybrid_search(
                query_text=question,
                query_embedding=embedding,
                folders=folders,
                from_addr=from_addr,
                date_from=date_from,
                date_to=date_to,
                limit=max_threads,
            )

            if not results:
                return [
                    TextContent(
                        type="text", text="No relevant emails found to answer your question."
                    )
                ]

            # Build context from retrieved threads
            context_parts = []
            for i, thread in enumerate(results, 1):
                context_parts.append(
                    f"[Thread {i}: {thread.subject}]\n"
                    f"Participants: {', '.join(thread.participants[:3])}\n"
                    f"Date: {thread.date_last.strftime('%Y-%m-%d')}\n"
                    f"{thread.snippet}\n"
                )

            context = "\n---\n".join(context_parts)
            user_prompt = f"Email threads:\n\n{context}\n\nQuestion: {question}"

            answer = await llm_complete(ASK_SYSTEM, user_prompt)

            sources = "\n".join(
                f"  - {r.subject} ({r.date_last.strftime('%Y-%m-%d')})" for r in results
            )

            return [TextContent(type="text", text=f"{answer}\n\nSources searched:\n{sources}")]

        except Exception as e:
            log.error(f"ask_mailbox error: {e}")
            return [TextContent(type="text", text=f"Error: {e}")]

    @server.tool()
    async def summarize_thread(
        thread_id: str,
        style: str = "brief",
    ) -> list[TextContent]:
        """
        Summarize indexed context for an email thread.

        Args:
            thread_id: The thread ID to summarize
            style: "brief" (2-3 sentences), "detailed" (full summary),
                   "action-items" (bullet list of actions), or
                   "timeline" (chronological sequence of events)

        Returns:
            A summary of the available indexed thread context in the requested style.
        """
        try:
            thread = db.get_thread(thread_id)
            if not thread:
                return [TextContent(type="text", text=f"Thread not found: {thread_id}")]

            style_instructions = {
                "brief": "Summarize in 2-3 sentences.",
                "detailed": "Provide a comprehensive summary covering all key points, decisions, and outcomes.",
                "action-items": "Extract all action items and next steps as a bullet list. Each item should name who is responsible if known.",
                "timeline": "Present the key events in this thread as a chronological timeline with dates.",
            }

            instruction = style_instructions.get(style, style_instructions["brief"])

            user_prompt = (
                f"Email thread: {thread.subject}\n"
                f"Participants: {', '.join(thread.participants)}\n"
                f"Date range: {thread.date_first.strftime('%Y-%m-%d')} "
                f"to {thread.date_last.strftime('%Y-%m-%d')}\n\n"
                f"Content:\n{thread.snippet}\n\n"
                f"Task: {instruction}"
            )

            summary = await llm_complete(SUMMARIZE_SYSTEM, user_prompt)

            return [
                TextContent(type="text", text=f"Summary ({style}) — {thread.subject}:\n\n{summary}")
            ]

        except Exception as e:
            log.error(f"summarize_thread error: {e}")
            return [TextContent(type="text", text=f"Error: {e}")]

    @server.tool()
    async def extract_from_emails(
        query: str,
        schema: dict,
        folders: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 20,
    ) -> list[TextContent]:
        """
        Extract structured data from indexed emails matching a query.
        Example: extract all invoices with vendor name and amount.

        Args:
            query: What to search for e.g. "invoices", "meeting confirmations"
            schema: JSON schema describing what to extract e.g.
                    {"vendor": "string", "amount": "number", "date": "string"}
            folders: Optionally scope to specific folders
            date_from: Optional date lower bound (ISO 8601)
            date_to: Optional date upper bound (ISO 8601)
            limit: Max threads to search through (default: 20)

        Returns:
            A JSON array of extracted records found in the available indexed thread context.
        """
        try:
            embedding = await ollama.embed(query)
            results = db.hybrid_search(
                query_text=query,
                query_embedding=embedding,
                folders=folders,
                date_from=date_from,
                date_to=date_to,
                limit=limit,
            )

            if not results:
                return [TextContent(type="text", text="No matching emails found.")]

            schema_str = json.dumps(schema, indent=2)
            extracted_records = []

            for thread in results:
                user_prompt = (
                    f"Extract data matching this schema:\n{schema_str}\n\n"
                    f"From this email thread:\n"
                    f"Subject: {thread.subject}\n"
                    f"Date: {thread.date_last.strftime('%Y-%m-%d')}\n"
                    f"Content: {thread.snippet}\n\n"
                    f"Return a JSON object matching the schema, "
                    f"or null if no relevant data found."
                )

                result_str = await llm_complete(EXTRACT_SYSTEM, user_prompt)

                try:
                    record = json.loads(result_str.strip())
                    if record:
                        record["_source_thread"] = thread.subject
                        record["_date"] = thread.date_last.strftime("%Y-%m-%d")
                        extracted_records.append(record)
                except json.JSONDecodeError:
                    pass  # LLM returned null or invalid JSON — skip

            if not extracted_records:
                return [
                    TextContent(
                        type="text",
                        text=f"No structured data matching the schema found in {len(results)} threads.",
                    )
                ]

            return [TextContent(type="text", text=json.dumps(extracted_records, indent=2))]

        except Exception as e:
            log.error(f"extract_from_emails error: {e}")
            return [TextContent(type="text", text=f"Error: {e}")]


async def _claude_complete(system: str, user: str, api_key: str) -> str:
    """Call the Claude API for higher-quality reasoning."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 1024,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"]
