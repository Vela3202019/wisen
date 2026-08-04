"""
Manual test for chat history search.
Usage: python3 -m tests.test_search "your query" [source]
"""
import asyncio
import sys

# Load env so connector paths resolve
from dotenv import load_dotenv

load_dotenv()

from wisen.chat.search import search_chat_history


async def main():
    query = sys.argv[1] if len(sys.argv) > 1 else "alignment"
    sources = [sys.argv[2]] if len(sys.argv) > 2 else None

    print(f"Searching for: {query!r}")
    if sources:
        print(f"Sources: {sources}")
    print()

    results = await search_chat_history(query, sources=sources, max_results=10)

    if not results:
        print("No matches found.")
        return

    for i, r in enumerate(results, 1):
        print(f"[{i}] {r['source']} · {r['project']} · {r['date']} · {r['role']}")
        print(f"    {r['excerpt'][:120].replace(chr(10), ' ')}")
        print(f"    context turns: {len(r['context'])}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
