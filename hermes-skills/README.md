# Hermes skills (quantFinance)

Copy a skill folder (e.g. `quant-ladder/`) into your Hermes data directory:

- Docker (this repo): `quantFinance/hermes-data/skills/<skill-name>/`
- Ensure the skill contains `SKILL.md` as required by [Hermes skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills/).

`quant-ladder` uses `QUANT_API_URL` and `HERMES_QUANT_INTERNAL_KEY` injected by `docker-compose.yml` on the **hermes** service. Set `HERMES_QUANT_INTERNAL_KEY` in `.env` to a long random string; the same value must be on **quant-api** and **hermes**.

If you previously installed `quant-wukong-ladder`, remove that folder and use `quant-ladder` instead.
