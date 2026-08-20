#!/usr/bin/env python
"""Report the running configuration and whether each provider is reachable.

Run before an extraction session. A missing key or a stopped Ollama daemon is
reported here rather than halfway through a run.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.config import get_settings  # noqa: E402
from app.db import database  # noqa: E402
from app.extraction.llm.gemini import GeminiProvider  # noqa: E402
from app.extraction.llm.ollama import OllamaProvider  # noqa: E402
from app.extraction.llm.cache import ResponseCache  # noqa: E402
from app.extraction.prompts import list_prompts  # noqa: E402

GREEN, RED, DIM, BOLD, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def main() -> int:
    s = get_settings()
    tick = lambda ok: f"{GREEN}ok{OFF}" if ok else f"{RED}no{OFF}"

    print(f"\n{BOLD}configuration{OFF}")
    print(f"  database        {s.db_path}  {DIM}(schema v{database.schema_version(s) or '-'}){OFF}")
    print(f"  active provider {s.llm_provider}")
    print(f"  retrieval mode  {s.retrieval_mode}")
    print(f"  adapters        tracker={s.tracker_provider} store={s.store_provider} notifier={s.notifier_provider}")
    print(f"  chunking        {s.chunk_max_tokens} tokens, {s.chunk_overlap_tokens} overlap")
    print(f"  approval expiry {s.pending_expiry_hours} h")

    print(f"\n{BOLD}providers{OFF}  {DIM}(all implement one interface; LLM_PROVIDER selects){OFF}")
    for provider in (
        GeminiProvider(s.gemini_api_key, s.gemini_model, s.llm_timeout_seconds),
        OllamaProvider(s.ollama_base_url, s.ollama_model, s.llm_timeout_seconds),
    ):
        usable, reason = provider.available()
        active = f"{BOLD} <- active{OFF}" if provider.name == s.llm_provider else ""
        print(f"  {tick(usable)}  {provider.describe():<34}{DIM}{reason}{OFF}{active}")

    print(f"\n{BOLD}prompts{OFF}")
    for prompt in list_prompts():
        print(f"  {prompt.capability:<24} v{prompt.version_tag}  {DIM}{prompt.changed or ''}{OFF}")

    cache = ResponseCache(s.llm_cache_dir, s.llm_cache_enabled)
    print(f"\n{BOLD}cache{OFF}  {'enabled' if s.llm_cache_enabled else 'disabled'}, "
          f"{cache.size()} entry(ies) in {s.llm_cache_dir}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
