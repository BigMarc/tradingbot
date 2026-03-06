from __future__ import annotations

import time
import orjson
import anthropic
from config.settings import settings, load_strategy
from core.signal_engine import SignalEvent
from storage.database import Database
from utils.logger import logger
from utils.helpers import format_usd, format_pct

SYSTEM_PROMPT = """Du bist ein professioneller Crypto-Trader mit Fokus auf Hyperliquid Perpetual Futures.
Du erhältst Marktdaten und ein Trading-Signal. Deine Aufgabe ist eine präzise Trade-Entscheidung.

Antworte AUSSCHLIESSLICH in diesem JSON-Format, keine Erklärung:
{
  "action": "LONG" | "SHORT" | "SKIP",
  "confidence": 0.0-1.0,
  "entry_type": "MARKET" | "LIMIT",
  "limit_price": null | number,
  "leverage": 1-5,
  "position_size_pct": 1.0-5.0,
  "stop_loss_pct": number,
  "take_profit_targets": [
    {"pct": number, "close_pct": number}
  ],
  "max_hold_minutes": number,
  "reasoning": "string (1 Satz)"
}

Regeln die du IMMER befolgst:
- Leverage NIEMALS über 5x
- Position Size NIEMALS über 5% der Bankroll pro Trade
- Stop-Loss ist PFLICHT bei jedem Trade
- Minimum Risk/Reward Ratio: 2:1
- Wenn unsicher -> action: "SKIP"
- Partial Take Profits: Immer mindestens 2 Targets
- In Seitwärtsmärkten (niedriger ADX): SKIP
- Bei extremer Funding Rate gegen dich: SKIP oder reduziere Size"""


def _build_user_message(
    signal: SignalEvent,
    bankroll: float,
    open_positions: list[dict],
    today_trades: list[dict],
    recent_trades: list[dict],
    btc_trend: str,
    market_sentiment: str,
    top_movers: list[dict],
) -> str:
    indicators = signal.indicators
    price = indicators.get("price", 0)

    # Recent trades summary
    recent_summary = "Keine"
    if recent_trades:
        parts = []
        for t in recent_trades[:3]:
            parts.append(f"{t['token']} {t['direction']} {format_pct(t.get('pnl_pct', 0))}")
        recent_summary = " | ".join(parts)

    # Today stats
    wins = sum(1 for t in today_trades if t.get("pnl_usd", 0) > 0)
    losses = sum(1 for t in today_trades if t.get("pnl_usd", 0) <= 0)
    today_pnl = sum(t.get("pnl_usd", 0) for t in today_trades)
    today_pnl_pct = (today_pnl / bankroll * 100) if bankroll > 0 else 0

    unrealized = sum(p.get("unrealizedPnl", 0) for p in open_positions) if open_positions else 0

    top_movers_str = "N/A"
    if top_movers:
        parts = [f"{m.get('token', '?')} {format_pct(m.get('change', 0))}" for m in top_movers[:3]]
        top_movers_str = ", ".join(parts)

    return f"""SIGNAL: {signal.direction} auf {signal.token}
Signal Score: {signal.score}/100

MARKTDATEN {signal.token} (letzte 4h):
- Aktueller Preis: ${price:.4f}
- 15min Change: {indicators.get('pct_15m', 0):.2f}%
- 1h Change: {indicators.get('pct_60m', 0):.2f}%
- 4h Change: {indicators.get('pct_240m', 0):.2f}%
- 24h Volume: {format_usd(indicators.get('volume_24h', 0))}
- Volume Ratio (5m vs avg): {indicators.get('volume_ratio', 1):.1f}x
- Funding Rate: {indicators.get('funding_rate', 0):.4f}%

TECHNISCHE INDIKATOREN:
- RSI(14): {indicators.get('rsi', 50):.1f}
- EMA9/21/50: {indicators.get('ema_alignment', 'MIXED')}
- MACD Hist: {indicators.get('macd_hist', 0):.6f} (Trend: {indicators.get('macd_trend', 'FLAT')})
- BB Position: {indicators.get('bb_position', 0.5):.2f} (0=lower, 0.5=mid, 1=upper)
- ATR(14): {indicators.get('atr', 0):.6f} ({indicators.get('atr_pct', 0):.2f}% vom Preis)
- ADX: {indicators.get('adx', 20):.1f}
- Spread: {indicators.get('spread_pct', 0):.3f}%

PORTFOLIO STATUS:
- Bankroll: {format_usd(bankroll)}
- Offene Positionen: {len(open_positions)}/2
- Unrealisierter PnL: {format_usd(unrealized)}
- Heutige Performance: {format_pct(today_pnl_pct)} ({wins}W/{losses}L)
- Letzte 3 Trades: {recent_summary}

FUNDING ANALYSE:
- Aktuelle 8h Funding Rate: {indicators.get('funding_rate', 0):.4f}%
- Annualisiert: {indicators.get('funding_annualized_pct', 0):.1f}%
- Einordnung: {indicators.get('funding_label', 'NEUTRAL')}
- Funding-Modifier auf Signal Score: {indicators.get('funding_modifier', 0):+.0f} Punkte

MARKT-KONTEXT:
- BTC 1h Trend: {btc_trend}
- Top 3 Movers (1h): {top_movers_str}
- Markt-Sentiment: {market_sentiment}"""


class AIBrain:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._telegram = None

    def set_telegram(self, telegram_bot) -> None:
        self._telegram = telegram_bot

    async def evaluate_signal(
        self,
        signal: SignalEvent,
        bankroll: float,
        open_positions: list[dict],
        today_trades: list[dict],
        recent_trades: list[dict],
        btc_trend: str = "NEUTRAL",
        market_sentiment: str = "NEUTRAL",
        top_movers: list[dict] | None = None,
    ) -> dict | None:
        strategy = load_strategy()
        ai_config = strategy.get("ai", {})
        model = ai_config.get("model", "claude-sonnet-4-6")
        max_tokens = ai_config.get("max_tokens", 500)
        temperature = ai_config.get("temperature", 0.3)
        timeout = ai_config.get("timeout_seconds", 30)

        user_message = _build_user_message(
            signal, bankroll, open_positions, today_trades,
            recent_trades, btc_trend, market_sentiment, top_movers or [],
        )

        start_time = time.time()
        try:
            response = self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
                timeout=timeout,
            )
        except anthropic.APITimeoutError:
            logger.warning("AI Brain timeout after {}s, skipping trade", timeout)
            return None
        except anthropic.RateLimitError:
            logger.warning("AI Brain rate limited, skipping trade")
            return None
        except anthropic.AuthenticationError as e:
            logger.critical("Anthropic API key invalid or expired! {}", e)
            if self._telegram:
                await self._telegram.notify_alert(
                    "Anthropic API Key ungueltig oder abgelaufen! "
                    "Bot kann keine AI-Entscheidungen treffen. "
                    "Bitte API Key pruefen und erneuern."
                )
            return None
        except anthropic.PermissionDeniedError as e:
            logger.critical("Anthropic API permission denied (billing?): {}", e)
            if self._telegram:
                await self._telegram.notify_alert(
                    "Anthropic API Zugriff verweigert (Billing-Problem?). "
                    "Bitte Account-Status pruefen."
                )
            return None
        except Exception as e:
            logger.error("AI Brain error: {}", e)
            return None

        latency = time.time() - start_time
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens

        # Cost calculation: Sonnet 4.6 = $3/M input, $15/M output
        cost = (input_tokens / 1_000_000 * 3.0) + (output_tokens / 1_000_000 * 15.0)

        await self.db.log_api_cost("anthropic", model, input_tokens, output_tokens, cost)
        logger.info(
            "AI Brain call: {:.1f}s | {} in / {} out tokens | ${:.4f}",
            latency, input_tokens, output_tokens, cost,
        )

        # Parse response
        text = response.content[0].text.strip()

        # Try to extract JSON from the response
        try:
            # Handle cases where AI might wrap in markdown code blocks
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()

            decision = orjson.loads(text)
        except Exception:
            logger.warning("AI Brain returned invalid JSON: {}", text[:200])
            return None

        # Validate decision
        action = decision.get("action", "SKIP")
        if action not in ("LONG", "SHORT", "SKIP"):
            logger.warning("AI Brain returned invalid action: {}", action)
            return None

        if action == "SKIP":
            logger.info("AI Brain decided to SKIP: {}", decision.get("reasoning", ""))
            return decision

        # Enforce hard limits
        leverage = min(decision.get("leverage", 3), 5)
        size_pct = min(decision.get("position_size_pct", 3.0), 5.0)
        decision["leverage"] = leverage
        decision["position_size_pct"] = size_pct

        if "stop_loss_pct" not in decision or decision["stop_loss_pct"] <= 0:
            logger.warning("AI Brain omitted stop_loss, forcing SKIP")
            decision["action"] = "SKIP"
            return decision

        logger.info(
            "AI Brain decision: {} {} | {}x | Size: {}% | SL: {}% | Reason: {}",
            action, signal.token, leverage, size_pct,
            decision.get("stop_loss_pct", 0), decision.get("reasoning", ""),
        )

        return decision

    async def optimize_review(self, stats: dict) -> dict | None:
        """Call AI to review and potentially adjust strategy parameters."""
        strategy = load_strategy()
        ai_config = strategy.get("ai", {})
        model = ai_config.get("model", "claude-sonnet-4-6")

        system_prompt = (
            "Du bist ein Trading-Strategie-Optimizer. Du erhältst Performance-Statistiken eines autonomen Trading-Bots. "
            "Schlage Parameter-Anpassungen vor um Performance zu verbessern. "
            "Antworte NUR in YAML-Format oder 'NO_CHANGES'. "
            "Ändere NIEMALS die harten Risk-Limits (max_leverage, max_position_size, max_drawdown). "
            "Du darfst anpassen: signal_threshold, indicator weights, trailing_stop_tiers, cooldown_minutes, take_profit_targets."
        )

        user_message = f"""PERFORMANCE STATISTIKEN (letzte 24h):
- Trades: {stats.get('total_trades', 0)}
- Win Rate: {stats.get('win_rate', 0):.1f}%
- Avg Win: {format_pct(stats.get('avg_win', 0))}
- Avg Loss: {format_pct(stats.get('avg_loss', 0))}
- Profit Factor: {stats.get('profit_factor', 0):.2f}
- Max Drawdown: {format_pct(stats.get('max_drawdown', 0))}
- Avg Hold Time: {stats.get('avg_hold_minutes', 0):.0f} min
- Best Token: {stats.get('best_token', 'N/A')}
- Worst Token: {stats.get('worst_token', 'N/A')}

AKTUELLE PARAMETER:
signal_threshold: {strategy.get('signal', {}).get('min_score', 65)}
weights: {strategy.get('signal', {}).get('weights', {})}
trailing_stop_tiers: {strategy.get('trading', {}).get('trailing_stop', {})}
take_profit_targets: {strategy.get('trading', {}).get('take_profit', {})}
cooldown: {strategy.get('trading', {}).get('cooldown', {})}

Schlage Anpassungen vor oder antworte 'NO_CHANGES'."""

        start_time = time.time()
        try:
            response = self.client.messages.create(
                model=model,
                max_tokens=800,
                temperature=0.3,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
                timeout=30,
            )
        except Exception as e:
            logger.error("Optimizer AI error: {}", e)
            return None

        latency = time.time() - start_time
        cost = (response.usage.input_tokens / 1_000_000 * 3.0) + (response.usage.output_tokens / 1_000_000 * 15.0)
        await self.db.log_api_cost("anthropic", model, response.usage.input_tokens, response.usage.output_tokens, cost)

        text = response.content[0].text.strip()
        logger.info("Optimizer AI response ({:.1f}s): {}", latency, text[:200])

        if "NO_CHANGES" in text.upper():
            return None

        return {"raw_response": text}
