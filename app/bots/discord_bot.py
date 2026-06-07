"""Discord slash-command adapter — the on-demand demo front door.

``/causal-agent question:<text> [treatment:<choice>]`` acks within Discord's 3s
window (defer), submits to the HTTP ingress, polls ``/status`` while editing the
reply as the pipeline advances, then posts the business narrative + the plain-
language interpretation + the recovered estimate.

Run (token + an API reachable at config.API_BASE_URL):
    python -m app.bots.discord_bot
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

from app.bots import api_client
from app.core import config
from app.sim import effects

logger = logging.getLogger(__name__)

_TREATMENT_CHOICES = [app_commands.Choice(name=t, value=t) for t in effects.TRUE_EFFECTS]


class CausalBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        # Guild-scoped sync is instant; global sync can take ~1h to propagate.
        if config.DISCORD_GUILD_ID:
            guild = discord.Object(id=int(config.DISCORD_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


client = CausalBot()


@client.event
async def on_ready() -> None:
    logger.info("Discord bot ready as %s", client.user)


@client.tree.command(name="causal-agent", description="Ask a causal question over the order data.")
@app_commands.describe(
    question="The causal question, in plain English.",
    treatment="Optional: pin a known treatment for a deterministic run.",
)
@app_commands.choices(treatment=_TREATMENT_CHOICES)
async def causal_agent(
    interaction: discord.Interaction,
    question: str,
    treatment: app_commands.Choice[str] | None = None,
) -> None:
    await interaction.response.defer(thinking=True)  # ack within 3s
    spec = api_client.pinned_spec(treatment.value if treatment else None)
    try:
        task_id = await asyncio.to_thread(api_client.submit_analysis, question, spec)
    except Exception:  # noqa: BLE001 — any client/transport error
        # Log the detail server-side; don't post internal URLs/stack info to the channel.
        logger.exception("submit_analysis failed")
        await interaction.followup.send("⚠️ Couldn't reach the analysis service. Please try again.")
        return

    result = await _poll(interaction, task_id)
    if result is not None:
        await interaction.edit_original_response(content=None, embed=_embed(question, result))


async def _poll(interaction: discord.Interaction, task_id: str) -> dict | None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + config.BOT_POLL_TIMEOUT_S
    last_stage: str | None = None
    while loop.time() < deadline:
        try:
            status = await asyncio.to_thread(api_client.fetch_status, task_id)
        except Exception:  # noqa: BLE001
            logger.exception("fetch_status failed for task %s", task_id)
            await interaction.edit_original_response(content="⚠️ Lost contact with the analysis service.")
            return None

        if api_client.is_terminal(status):
            if status.get("state") == "SUCCESS" and status.get("result"):
                return status["result"]
            await interaction.edit_original_response(
                content="❌ The analysis failed. Try rephrasing the question."
            )
            return None

        stage = api_client.stage_label(status)
        if stage != last_stage:
            last_stage = stage
            await interaction.edit_original_response(content=f"🔬 {stage}…")
        await asyncio.sleep(config.BOT_POLL_INTERVAL_S)

    await interaction.edit_original_response(content="⏱️ Timed out waiting for the analysis.")
    return None


def _embed(question: str, result: dict) -> discord.Embed:
    s = api_client.summarize_result(result)
    embed = discord.Embed(
        title="Causal analysis", description=(s["narrative"] or "(no narrative produced)")[:4000]
    )
    embed.add_field(name="Question", value=question[:1024], inline=False)
    if s["interpretation"]:
        embed.add_field(name="What I answered", value=s["interpretation"][:1024], inline=False)
    if s["stat_line"]:
        embed.add_field(name="Estimate", value=s["stat_line"][:1024], inline=False)
    if s["error"]:
        embed.add_field(name="Note", value=s["error"][:1024], inline=False)
    return embed


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not config.DISCORD_BOT_TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is not set — export it before running the bot.")
    client.run(config.DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
