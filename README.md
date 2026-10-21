# LINE Desktop automation

Personal research and automation of the author's own LINE account.
Not affiliated with LINE. Use at your own risk.

License: TBD

Account ids and the bot mention name are not stored in this tree.
Copy [`automation/bot_config.example.json`](automation/bot_config.example.json)
to `automation/bot_config.json` (gitignored) and replace the placeholders,
or set `LINE_OWNER_MID`, `LINE_BOT_MID`, and `LINE_BOT_MENTION`.
When `bot_config.json` is absent, the tools keep running with non-routable
placeholder defaults.

- [Architecture](ARCHITECTURE.md) — components, data flow, and limitations
- [automation/README.md](automation/README.md) — how to run the tools
