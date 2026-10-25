"""A paginated participant picker showing channel members' real display names."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord


class ParticipantSelect(discord.ui.Select):
    def __init__(self, picker: ParticipantPicker):
        self.picker = picker
        self.page_ids = picker.pages[picker.page]
        options = [discord.SelectOption(label=picker.people[uid][:100], value=str(uid),
                                        default=uid in picker.selected) for uid in self.page_ids]
        super().__init__(placeholder="Choose people in this chat", min_values=0,
                         max_values=len(options), options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        if not await self.picker.interaction_check(interaction):
            return
        self.picker.selected.difference_update(self.page_ids)
        self.picker.selected.update(int(value) for value in self.values if int(value) in self.page_ids)
        self.picker.refresh()
        await interaction.response.edit_message(content=self.picker.description(), view=self.picker)


class ParticipantPicker(discord.ui.View):
    def __init__(self, owner_id: int, people: dict[int, str],
                 action: Callable[[discord.Interaction, dict[int, str]], Awaitable[None]],
                 *, minimum: int = 1, label: str = "Use these people", other_than: int | None = None):
        super().__init__(timeout=300)
        if not people:
            raise ValueError("There are no people to choose from in this chat.")
        self.owner_id, self.people, self.action = owner_id, dict(people), action
        self.minimum, self.other_than = minimum, other_than
        ids = sorted(people, key=lambda uid: (people[uid].casefold(), uid))
        self.pages = [ids[i:i + 25] for i in range(0, len(ids), 25)]
        self.page = 0
        self.selected = set(people)
        self.submitted = False
        self.confirm.label = label
        self.refresh()

    def description(self) -> str:
        return (f"Choose who's included: **{len(self.selected)} people selected**. "
                f"Page {self.page + 1} of {len(self.pages)}. Bots are excluded.")

    def refresh(self) -> None:
        self.clear_items()
        self.add_item(ParticipantSelect(self))
        if len(self.pages) > 1:
            self.previous.disabled = self.page == 0
            self.next_page.disabled = self.page == len(self.pages) - 1
            self.add_item(self.previous)
            self.add_item(self.next_page)
        self.add_item(self.confirm)
        self.add_item(self.cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This picker belongs to the person who ran the command.", ephemeral=True)
            return False
        if self.submitted:
            await interaction.response.send_message("This selection was already handled. Run the command again.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Previous names", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self.interaction_check(interaction):
            return
        self.page = max(0, self.page - 1)
        self.refresh()
        await interaction.response.edit_message(content=self.description(), view=self)

    @discord.ui.button(label="More names", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self.interaction_check(interaction):
            return
        self.page = min(len(self.pages) - 1, self.page + 1)
        self.refresh()
        await interaction.response.edit_message(content=self.description(), view=self)

    @discord.ui.button(label="Use these people", emoji="✅", style=discord.ButtonStyle.success, row=1)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self.interaction_check(interaction):
            return
        chosen = {uid: name for uid, name in self.people.items() if uid in self.selected}
        if len(chosen) < self.minimum:
            await interaction.response.send_message(f"Choose at least {self.minimum} people.", ephemeral=True)
            return
        if self.other_than is not None and not any(uid != self.other_than for uid in chosen):
            await interaction.response.send_message("Choose at least one person besides yourself.", ephemeral=True)
            return
        self.submitted = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content=f"✅ Using {len(chosen)} people from this chat.", view=self)
        self.stop()
        await self.action(interaction, chosen)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self.interaction_check(interaction):
            return
        self.submitted = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Cancelled.", view=self)
        self.stop()
