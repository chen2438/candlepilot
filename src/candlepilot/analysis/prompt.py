from __future__ import annotations

import json
from typing import Any


PROMPT_VERSION = "market-analysis-v1"


def build_analysis_prompt(data_pack: dict[str, Any]) -> str:
    payload = json.dumps(data_pack, separators=(",", ":"), ensure_ascii=False)
    return f"""You are producing an independent market study, never an order or permission to trade.
Use only DATA_PACK below. Do not use tools, files, network access, remembered prices, or unsupported facts.

Analysis method:
1. Use 1h for trend context, 15m for structure and the primary anchor, and 5m only for timing.
2. Read price structure first. EMA(9/21/55), MACD(12/26/9), UTC-session VWAP, relative volume and flow may confirm or weaken structure; none is a standalone trade signal.
3. Name 2-4 mutually useful scenarios. Their probabilities should total about 100%.
4. Choose long, short, or neutral. Neutral must include a numeric range containing the anchor and no entry plan.
5. A directional plan must provide explicit entry, structure-based stop, T1 and T2. Never invent fallback percentage targets. T1 reward/risk must be at least 1; disclose weak quality in the summary when it is below 2.
6. The stop must sit beyond a named price structure, not at a convenient percentage or exactly on a crowded round number.
7. Management should state: only activate the plan after the entry trigger; at T1 reduce roughly half and move the remainder stop toward breakeven when market structure permits; treat roughly six 15m anchor bars without progress as a reason to reassess. This is a plan, not automatic execution.
8. Missing inputs are unknown, not benign. Explain their impact in missing_data_impact. In particular, unavailable news, event and options inputs must not be described as quiet or absent risk.
9. The anchor time must be timezone-aware and point to a supplied bar. Keep evidence factual and tied to supplied fields.

Return only the required JSON object.

DATA_PACK:
{payload}
"""
