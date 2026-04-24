from discord.ext import commands


def _user_setter(self, value):
    self._connection.user = value


# Make Bot.user settable so tests can assign bot.user = mock_user
commands.Bot.user = commands.Bot.user.setter(_user_setter)
