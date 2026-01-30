import json
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

ROLE_COLLECTIONNEUR_ID = int(os.getenv("ROLE_COLLECTIONNEUR_ID", "0"))
ROLE_COLLECTIONNEUR_ID_NEW_USER = int(os.getenv("ROLE_COLLECTIONNEUR_ID_NEW_USER", "0"))
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
NFT_COLLECTION_NAME = os.getenv("NFT_COLLECTION_NAME", "World Icons Cards")

BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
CARDS_PATH = os.path.join(DATA_DIR, "cards.json")
TIERS_PATH = os.path.join(DATA_DIR, "tiers.json")


def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@dataclass(frozen=True)
class Card:
    key: str
    name: str
    uri: str
    image_url: str
    weight: int

    @staticmethod
    def from_dict(d: dict) -> "Card":
        return Card(
            key=str(d.get("key", "")),
            name=str(d.get("name", "")),
            uri=str(d.get("uri", "")),
            image_url=str(d.get("image_url", "")),
            weight=int(d.get("weight", 0)),
        )


@dataclass(frozen=True)
class Tier:
    name: str
    min_weight: int

    @staticmethod
    def from_dict(d: dict) -> "Tier":
        return Tier(
            name=str(d.get("name", "")),
            min_weight=int(d.get("min_weight", 0)),
        )


class CardRepository:
    def __init__(self, cards_path: str, tiers_path: str):
        self.cards_path = cards_path
        self.tiers_path = tiers_path
        self._cards: List[Card] = []
        self._tiers: List[Tier] = []
        self.reload()

    def reload(self) -> None:
        raw_cards = load_json(self.cards_path)
        self._cards = [Card.from_dict(c) for c in raw_cards]
        tiers: List[Tier] = []
        if os.path.exists(self.tiers_path):
            raw_tiers = load_json(self.tiers_path)
            if isinstance(raw_tiers, list):
                tiers = [Tier.from_dict(t) for t in raw_tiers]
        if not tiers:
            tiers = self._tiers_from_cards()
        # Trier du min_weight le plus haut au plus bas
        self._tiers = sorted(tiers, key=lambda t: t.min_weight, reverse=True)

    def _tiers_from_cards(self) -> List[Tier]:
        weights = sorted({c.weight for c in self._cards if c.weight > 0}, reverse=True)
        return [Tier(name=f"Poids >= {w}", min_weight=w) for w in weights]

    @property
    def cards(self) -> List[Card]:
        return self._cards

    @property
    def tiers(self) -> List[Tier]:
        return self._tiers

    def total_weight(self) -> int:
        return sum(max(0, c.weight) for c in self._cards)

    def probability(self, card: Card) -> float:
        total = self.total_weight()
        return (card.weight / total) if total > 0 else 0.0

    def tier_for_card(self, card: Card) -> str:
        w = card.weight
        for t in self._tiers:
            if w >= t.min_weight:
                return t.name
        return "Inconnu"

    def summary_by_tier(self) -> Dict[str, Tuple[int, float]]:
        """
        returns: { tier_name: (sum_weight, pct) }
        """
        total = self.total_weight()
        sums: Dict[str, int] = {}
        for c in self._cards:
            tier = self.tier_for_card(c)
            sums[tier] = sums.get(tier, 0) + max(0, c.weight)

        out: Dict[str, Tuple[int, float]] = {}
        for tier, wsum in sums.items():
            pct = (wsum / total * 100) if total > 0 else 0.0
            out[tier] = (wsum, pct)

        # garder un ordre cohérent = ordre tiers.json
        ordered: Dict[str, Tuple[int, float]] = {}
        for t in self._tiers:
            if t.name in out:
                ordered[t.name] = out[t.name]
        # ajouter les éventuels tiers inconnus
        for k, v in out.items():
            if k not in ordered:
                ordered[k] = v
        return ordered

    def find_card(self, query: str) -> Optional[Card]:
        q = normalize(query)
        if not q:
            return None

        # match exact sur key
        for c in self._cards:
            if normalize(c.key) == q:
                return c

        # match exact sur name
        for c in self._cards:
            if normalize(c.name) == q:
                return c

        # match partiel sur name
        for c in self._cards:
            if q in normalize(c.name):
                return c

        return None

    def top_cards(self, n: int = 10) -> List[Card]:
        return sorted(self._cards, key=lambda c: c.weight, reverse=True)[: max(1, n)]


# --------- Discord setup ----------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True

repo = CardRepository(CARDS_PATH, TIERS_PATH)


class WorldIconsBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)


bot = WorldIconsBot()


# ---------- /lootrate ----------
@bot.tree.command(
    name="lootrate",
    description="Affiche les taux (probabilités) calculés depuis les poids des cartes.",
)
async def lootrate(interaction: discord.Interaction):
    try:
        repo.reload()
    except Exception as e:
        await interaction.response.send_message(
            f"Erreur données: `{e}`", ephemeral=True
        )
        return

    total = repo.total_weight()
    if total <= 0:
        await interaction.response.send_message(
            "Poids total = 0, impossible de calculer des probabilités.", ephemeral=True
        )
        return


    embed = discord.Embed(title="Taux de loot : ")

    # Toutes les cartes
    all_cards = repo.cards
    all_lines = []
    for c in all_cards:
        p = repo.probability(c) * 100
        all_lines.append(f"**{c.name}** : {p:.3f}%")

    field_chunks = []
    current = []
    current_len = 0
    for line in all_lines:
        line_len = len(line) + 1
        if current and current_len + line_len > 1024:
            field_chunks.append(current)
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len
    if current:
        field_chunks.append(current)

    for i, chunk in enumerate(field_chunks):
        name = "Toutes les cartes" if i == 0 else "Toutes les cartes (suite)"
        embed.add_field(name=name, value="\n".join(chunk), inline=False)
    embed.set_footer(text="World Icons Cards - /lootrate")

    await interaction.response.send_message(embed=embed)


# ---------- /cardinfo ----------
@bot.tree.command(
    name="cardinfo",
    description="Affiche les détails d'une carte (poids + proba + image).",
)
@app_commands.describe(nom_carte="Nom (complet ou partiel) ou key de la carte")
async def cardinfo(interaction: discord.Interaction, nom_carte: str):
    try:
        repo.reload()
    except Exception as e:
        await interaction.response.send_message(
            f"Erreur données: `{e}`", ephemeral=True
        )
        return

    card = repo.find_card(nom_carte)
    if not card:
        await interaction.response.send_message(
            f"Aucune carte trouvée pour : **{nom_carte}**", ephemeral=True
        )
        return

    p = repo.probability(card) * 100
    tier = repo.tier_for_card(card)

    embed = discord.Embed(title=f"Carte: {card.name}")
    embed.add_field(name="Key", value=card.key or "N/A", inline=True)
    embed.add_field(name="Rareté (tier)", value=tier, inline=True)
    embed.add_field(name="Poids", value=str(card.weight), inline=True)
    embed.add_field(name="Probabilité (sur un loot)", value=f"{p:.5f}%", inline=True)
    embed.add_field(name="URI", value=card.uri or "N/A", inline=False)

    if card.image_url:
        embed.set_image(url=card.image_url)

    await interaction.response.send_message(embed=embed)


# ---------- /sui ----------
@bot.tree.command(name="sui", description="Affiche le prix actuel de SUI (et SOL).")
@app_commands.describe(devise="Devise fiat (eur, usd, etc.)")
async def sui(interaction: discord.Interaction, devise: str = "eur"):
    devise = (devise or "eur").lower().strip()
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": "sui,solana", "vs_currencies": devise}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    await interaction.response.send_message(
                        f"CoinGecko error HTTP {resp.status}: {text[:200]}",
                        ephemeral=True,
                    )
                    return
                data = await resp.json()
    except Exception as e:
        await interaction.response.send_message(f"Erreur API: `{e}`", ephemeral=True)
        return

    sui_price = data.get("sui", {}).get(devise)
    sol_price = data.get("solana", {}).get(devise)

    embed = discord.Embed(title="Prix crypto (CoinGecko)")
    embed.add_field(
        name="SUI",
        value=(f"{sui_price} {devise.upper()}" if sui_price is not None else "N/A"),
        inline=True,
    )
    embed.add_field(
        name="SOL",
        value=(f"{sol_price} {devise.upper()}" if sol_price is not None else "N/A"),
        inline=True,
    )
    embed.set_footer(text="Source: CoinGecko - /sui")
    await interaction.response.send_message(embed=embed)


# ---------- /verify (optionnel, conservé) ----------
@bot.tree.command(
    name="verify",
    description="Vérifie un wallet et attribue le rôle Collectionneur si NFT trouvé.",
)
@app_commands.describe(wallet="Adresse Solana du wallet (base58)")
async def verify(interaction: discord.Interaction, wallet: str):
    if not interaction.guild:
        await interaction.response.send_message(
            "Commande utilisable uniquement sur le serveur.", ephemeral=True
        )
        return

    if not HELIUS_API_KEY:
        await interaction.response.send_message(
            "HELIUS_API_KEY manquant dans .env", ephemeral=True
        )
        return

    url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    payload = {
        "jsonrpc": "2.0",
        "id": "wic-verify",
        "method": "getAssetsByOwner",
        "params": {"ownerAddress": wallet, "page": 1, "limit": 100},
    }

    await interaction.response.defer(ephemeral=True)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=15) as resp:
                data = await resp.json()
    except Exception as e:
        await interaction.followup.send(f"Erreur Helius: `{e}`", ephemeral=True)
        return

    items = (data.get("result", {}) or {}).get("items", []) or []

    found = False
    for it in items:
        content = it.get("content", {}) or {}
        metadata = content.get("metadata", {}) or {}

        collection_name = metadata.get("collection") or ""
        if not collection_name:
            grouping = it.get("grouping", []) or []
            for g in grouping:
                if g.get("group_key") == "collection":
                    collection_name = g.get("group_value") or ""
                    break

        if normalize(NFT_COLLECTION_NAME) in normalize(collection_name):
            found = True
            break

    if not found:
        await interaction.followup.send(
            "Je n'ai pas trouvé de NFT de la collection sur ce wallet (selon les données renvoyées).",
            ephemeral=True,
        )
        return

    role = interaction.guild.get_role(ROLE_COLLECTIONNEUR_ID)
    if not role:
        await interaction.followup.send(
            "ROLE_COLLECTIONNEUR_ID invalide (rôle introuvable).", ephemeral=True
        )
        return

    member = interaction.guild.get_member(interaction.user.id)
    if not member:
        await interaction.followup.send(
            "Impossible de récupérer ton profil membre sur ce serveur.", ephemeral=True
        )
        return

    try:
        await member.add_roles(
            role, reason="Vérification NFT réussie (World Icons Cards)"
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "Je n'ai pas la permission d'attribuer ce rôle. Vérifie la hiérarchie des rôles.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        "Vérification réussie ! Rôle **Collectionneur** attribué.", ephemeral=True
    )


@bot.event
async def on_ready():
    print(f"Connecté en tant que {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_member_join(member: discord.Member):
    if not ROLE_COLLECTIONNEUR_ID_NEW_USER:
        return

    role = member.guild.get_role(ROLE_COLLECTIONNEUR_ID_NEW_USER)
    if not role:
        return

    try:
        await member.add_roles(role, reason="Auto assign Collectionneur role")
    except discord.Forbidden:
        return


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN manquant dans .env")
    bot.run(DISCORD_TOKEN)
