"""
Roblox x Discord profile bot (discord.py 2.x)

Setup:
    pip install -U discord.py aiohttp

Run:
    set DISCORD_TOKEN=xxxx      (Windows)
    export DISCORD_TOKEN=xxxx   (Linux/macOS)
    python bot.py
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# ────────────────────────────── CONFIG ──────────────────────────────
OWNER_ID = 1105884989482086400
NO_PERMISSION_MSG = "❌ You don't have permission to set up or change this system!"
DB_FILE = Path("links.json")  # {"discord_id": roblox_id}

# Accent colors by presence
COLOR_ONLINE = 0x2ECC71
COLOR_INGAME = 0x00A2FF
COLOR_STUDIO = 0xF39C12
COLOR_OFFLINE = 0x5B6275
COLOR_ERROR = 0xED4245

PRESENCE = {
    0: ("⚫", "Offline", COLOR_OFFLINE),
    1: ("🟢", "Online", COLOR_ONLINE),
    2: ("🎮", "In game", COLOR_INGAME),
    3: ("🛠️", "In Roblox Studio", COLOR_STUDIO),
}


# ────────────────────────────── STORAGE ──────────────────────────────
def load_links() -> dict[str, int]:
    if DB_FILE.exists():
        try:
            return json.loads(DB_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_link(discord_id: int, roblox_id: int) -> None:
    data = load_links()
    data[str(discord_id)] = roblox_id
    DB_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_link(discord_id: int) -> Optional[int]:
    return load_links().get(str(discord_id))


# ────────────────────────────── ROBLOX API ──────────────────────────────
class RobloxAPI:
    def __init__(self) -> None:
        self.session: Optional[aiohttp.ClientSession] = None

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "RobloxDiscordProfileBot/1.0"},
        )

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    async def _request(self, method: str, url: str, **kw: Any) -> Optional[dict]:
        """Return JSON or None on failure so one bad endpoint doesn't break the profile"""
        try:
            assert self.session
            async with self.session.request(method, url, **kw) as r:
                if r.status != 200:
                    body = (await r.text())[:200]
                    print(f"[roblox] {method} {url} -> {r.status} {body}")
                    return None
                return await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            print(f"[roblox] {method} {url} failed: {exc!r}")
            return None

    async def username_to_id(self, username: str) -> Optional[int]:
        data = await self._request(
            "POST",
            "https://users.roblox.com/v1/usernames/users",
            json={"usernames": [username], "excludeBannedUsers": False},
        )
        if data and data.get("data"):
            return int(data["data"][0]["id"])
        return None

    async def _thumb(self, kind: str, uid: int, size: str) -> Optional[str]:
        data = await self._request(
            "GET",
            f"https://thumbnails.roblox.com/v1/users/{kind}",
            params={"userIds": uid, "size": size, "format": "Png", "isCircular": "false"},
        )
        if data and data.get("data"):
            return data["data"][0].get("imageUrl")
        return None

    async def _count(self, uid: int, what: str) -> Optional[int]:
        data = await self._request("GET", f"https://friends.roblox.com/v1/users/{uid}/{what}/count")
        return data.get("count") if data else None

    async def _presence(self, uid: int) -> Optional[dict]:
        data = await self._request(
            "POST", "https://presence.roblox.com/v1/presence/users", json={"userIds": [uid]}
        )
        if data and data.get("userPresences"):
            return data["userPresences"][0]
        return None

    async def _groups(self, uid: int) -> list[dict]:
        data = await self._request("GET", f"https://groups.roblox.com/v2/users/{uid}/groups/roles")
        return data.get("data", []) if data else []

    async def full_profile(self, uid: int) -> Optional[dict]:
        user, presence, friends, followers, followings, body, head, groups = await asyncio.gather(
            self._request("GET", f"https://users.roblox.com/v1/users/{uid}"),
            self._presence(uid),
            self._count(uid, "friends"),
            self._count(uid, "followers"),
            self._count(uid, "followings"),
            self._thumb("avatar", uid, "720x720"),
            self._thumb("avatar-headshot", uid, "420x420"),
            self._groups(uid),
        )
        if not user:
            return None
        return {
            "id": uid,
            "name": user["name"],
            "display": user.get("displayName") or user["name"],
            "description": (user.get("description") or "").strip(),
            "created": user.get("created"),
            "verified": user.get("hasVerifiedBadge", False),
            "banned": user.get("isBanned", False),
            "presence": presence,
            "friends": friends,
            "followers": followers,
            "followings": followings,
            "body": body,
            "head": head,
            "groups": groups,
        }

    async def outfit(self, uid: int) -> list[str]:
        data = await self._request("GET", f"https://avatar.roblox.com/v1/users/{uid}/currently-wearing")
        ids = (data or {}).get("assetIds", [])[:10]

        async def name(aid: int) -> str:
            d = await self._request("GET", f"https://economy.roblox.com/v2/assets/{aid}/details")
            label = d.get("Name", f"Asset {aid}") if d else f"Asset {aid}"
            return f"[{label}](https://www.roblox.com/catalog/{aid})"

        return list(await asyncio.gather(*(name(i) for i in ids)))

    async def friends_list(self, uid: int) -> Optional[list[dict]]:
        """Return None when the request fails, [] when the list is really empty"""
        data = await self._request("GET", f"https://friends.roblox.com/v1/users/{uid}/friends")
        if data is None:
            return None
        return data.get("data", [])


roblox = RobloxAPI()


# ────────────────────────────── EMBED BUILDERS ──────────────────────────────
def fmt(n: Optional[int]) -> str:
    return f"{n:,}" if isinstance(n, int) else "—"


def fmt_date(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return f"{dt.day} {dt:%B %Y}"


def base_embed(p: dict, member: Optional[discord.abc.User], color: int, thumb: bool = True) -> discord.Embed:
    e = discord.Embed(color=color, url=f"https://www.roblox.com/users/{p['id']}/profile")
    if member is not None:
        e.set_author(name=member.display_name, icon_url=member.display_avatar.url)
    else:
        e.set_author(name="Roblox player search")
    if thumb and p["head"]:
        e.set_thumbnail(url=p["head"])
    e.set_footer(text=f"Roblox ID {p['id']}")
    return e


def overview_embed(p: dict, member: Optional[discord.abc.User]) -> discord.Embed:
    pres = p["presence"] or {}
    icon, status, color = PRESENCE.get(pres.get("userPresenceType", 0), PRESENCE[0])
    if p["banned"]:
        icon, status, color = "⛔", "Banned", COLOR_ERROR

    badge = " ✅" if p["verified"] else ""
    e = base_embed(p, member, color)
    e.title = f"{p['display']}{badge}"

    lines = [f"**@{p['name']}**", f"{icon} {status}"]
    location = pres.get("lastLocation")
    if location and pres.get("userPresenceType") in (1, 2, 3):
        lines.append(f"-# {location}")
    e.description = "\n".join(lines)

    if p["description"]:
        bio = p["description"][:200] + ("…" if len(p["description"]) > 200 else "")
        e.add_field(name="About", value=f">>> {bio}", inline=False)

    # Short lines so nothing wraps next to the thumbnail (the Roblox ID is in the footer)
    e.add_field(
        name="Stats",
        value=(
            f"**{fmt(p['friends'])}** Friends • **{fmt(p['followers'])}** Followers\n"
            f"**{fmt(p['followings'])}** Following • **{len(p['groups'])}** Groups\n"
            f"Created **{fmt_date(p['created'])}**"
        ),
        inline=False,
    )

    if p["body"]:
        e.set_image(url=p["body"])
    return e


def outfit_embed(p: dict, member: Optional[discord.abc.User], items: list[str]) -> discord.Embed:
    e = base_embed(p, member, COLOR_INGAME)
    e.title = f"Current outfit • {p['display']}"
    e.description = "\n".join(f"`{i:02}` {x}" for i, x in enumerate(items, 1)) or "*Couldn't load the outfit*"
    if p["body"]:
        e.set_image(url=p["body"])
    return e


def friends_embed(p: dict, member: Optional[discord.abc.User], friends: Optional[list[dict]]) -> discord.Embed:
    e = base_embed(p, member, COLOR_ONLINE)
    e.title = f"Friends • {p['display']}"
    if friends is None:
        e.description = "*Couldn't load the friends list from Roblox, try again in a moment*"
        return e
    shown = friends[:15]
    e.description = (
        "\n".join(
            f"• [{f.get('displayName') or f.get('name', 'Unknown')}]"
            f"(https://www.roblox.com/users/{f['id']}/profile) (@{f.get('name', '?')})"
            for f in shown
        )
        or "*No friends to show*"
    )
    total = p.get("friends")
    if isinstance(total, int) and total > len(shown):
        e.description += f"\n\n-# Showing {len(shown)} of {total}"
    return e


def groups_embed(p: dict, member: Optional[discord.abc.User]) -> discord.Embed:
    e = base_embed(p, member, COLOR_STUDIO)
    e.title = f"Groups • {p['display']}"
    groups = sorted(p["groups"], key=lambda g: g["group"]["name"].lower())[:10]
    e.description = (
        "\n".join(
            f"• [{g['group']['name']}](https://www.roblox.com/groups/{g['group']['id']})"
            for g in groups
        )
        or "*Not in any groups*"
    )
    return e


# ────────────────────────────── UI COMPONENTS ──────────────────────────────
class ProfileView(discord.ui.View):
    def __init__(self, invoker_id: int, member: Optional[discord.abc.User], profile: dict) -> None:
        super().__init__(timeout=180)
        self.invoker_id = invoker_id
        self.member = member
        self.profile = profile
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "This panel belongs to whoever ran the command, use `/profile` to get your own", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    # Select menu: switch tabs
    @discord.ui.select(
        placeholder="Select a tab",
        options=[
            discord.SelectOption(label="Overview", value="overview", emoji="📊", default=True),
            discord.SelectOption(label="Current outfit", value="outfit", emoji="👕"),
            discord.SelectOption(label="Friends", value="friends", emoji="👥"),
            discord.SelectOption(label="Groups", value="groups", emoji="🏷️"),
        ],
        row=0,
    )
    async def tab(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        choice = select.values[0]
        await interaction.response.defer()
        p, m = self.profile, self.member

        if choice == "outfit":
            embed = outfit_embed(p, m, await roblox.outfit(p["id"]))
        elif choice == "friends":
            embed = friends_embed(p, m, await roblox.friends_list(p["id"]))
        elif choice == "groups":
            embed = groups_embed(p, m)
        else:
            embed = overview_embed(p, m)

        for opt in select.options:
            opt.default = opt.value == choice
        await interaction.edit_original_response(embed=embed, view=self)


# ────────────────────────────── BOT & COMMANDS ──────────────────────────────
async def start_web_server() -> None:
    """Tiny HTTP endpoint so Render sees an open port and an uptime pinger has something to hit"""

    async def health(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000")))
    await site.start()


class RobloxBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=discord.Intents.default())

    async def setup_hook(self) -> None:
        await roblox.start()
        await start_web_server()
        await self.tree.sync()

    async def close(self) -> None:
        await roblox.close()
        await super().close()


bot = RobloxBot()


@bot.tree.command(name="profile", description="View a linked Roblox profile")
@app_commands.describe(member="Member to look up")
async def profile_cmd(interaction: discord.Interaction, member: discord.User) -> None:
    target = member
    roblox_id = get_link(target.id)
    if not roblox_id:
        return await interaction.response.send_message(
            f"❌ **{target.display_name}** hasn't linked a Roblox account", ephemeral=True
        )

    await interaction.response.defer()
    data = await roblox.full_profile(roblox_id)
    if not data:
        return await interaction.followup.send("❌ Couldn't reach Roblox, try again in a moment")

    view = ProfileView(interaction.user.id, target, data)
    view.message = await interaction.followup.send(embed=overview_embed(data, target), view=view, wait=True)


@bot.tree.command(name="search", description="Look up any Roblox player by username")
@app_commands.describe(username="Roblox username")
async def search_cmd(interaction: discord.Interaction, username: str) -> None:
    await interaction.response.defer()
    uid = await roblox.username_to_id(username.strip())
    if not uid:
        return await interaction.followup.send("❌ No Roblox player found with that username")

    data = await roblox.full_profile(uid)
    if not data:
        return await interaction.followup.send("❌ Couldn't reach Roblox, try again in a moment")

    # Nothing is saved here, this is a plain lookup
    view = ProfileView(interaction.user.id, None, data)
    view.message = await interaction.followup.send(embed=overview_embed(data, None), view=view, wait=True)


@bot.tree.command(name="link",description="Owner only: link a Discord member to a Roblox account")
@app_commands.describe(member="Discord member", roblox_username="Roblox username")
async def link_cmd(interaction: discord.Interaction, member: discord.User, roblox_username: str) -> None:
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message(NO_PERMISSION_MSG, ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    uid = await roblox.username_to_id(roblox_username)
    if not uid:
        return await interaction.followup.send("❌ That username doesn't exist on Roblox")

    save_link(member.id, uid)
    await interaction.followup.send(f"✅ Linked {member.mention} to **{roblox_username}** (`{uid}`)")


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Missing DISCORD_TOKEN environment variable")
    bot.run(token)
