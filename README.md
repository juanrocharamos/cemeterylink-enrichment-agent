# CemeteryLink Enrichment Agent

AI-assisted workflow for researching, resolving, creating and enriching cemetery and grave records in [CemeteryLink](https://cemeterylink.com/) while keeping source evidence separate from generated text.

The project was built from a real recurring data-enrichment workflow. Its main goal is not to generate prose, but to reduce repetitive research and data-entry work while keeping automated actions auditable and conservative.

## Demo

Production workflow demo: https://youtu.be/SYaM1aNUqE0

## What the agent does

At a high level, the worker:

1. Retrieves people with a documented burial place from Wikidata.
2. Resolves the cemetery against existing CemeteryLink records.
3. Reuses an existing cemetery when the match is sufficiently supported.
4. Researches a missing cemetery using structured geographic data and web sources.
5. Creates the cemetery only when enough location evidence is available.
6. Retrieves structured person data such as dates, places, occupations and citizenships.
7. Creates a new grave record or enriches an existing one when the incoming data adds useful information.
8. Skips or abstains when evidence is insufficient or conflicting.
9. Stores processing state, research results and cursors in SQLite so runs can be inspected and resumed.

The strongest demonstrated capability is grave-level enrichment. Cemetery narrative enrichment is intentionally more conservative and can remain limited when reliable contextual sources are sparse.

## Design principles

The workflow separates source evidence, deterministic code, AI assistance and human responsibility.

### Source evidence

Structured source data provides the factual basis for burial relationships and biographical fields. Wikidata identifiers are retained as external references, and OpenStreetMap/Nominatim can provide geographic context for cemetery resolution.

### Deterministic code

Code controls:

- source retrieval;
- cemetery matching;
- coordinate and administrative-context checks;
- duplicate detection;
- create/update/skip decisions;
- SQLite processing state;
- retry behavior;
- publication through the CemeteryLink API.

### AI assistance

OpenAI is used for web-assisted cemetery research and for drafting concise cemetery context from retrieved sources.

Generated text is not treated as evidence by itself. If research does not return usable supporting sources, the workflow falls back to structured geographic information rather than inventing historical context.

### Human responsibility

A human operator remains responsible for:

- configuring the intended environment and country;
- reviewing representative outputs;
- investigating ambiguous or incorrect matches;
- changing matching/publication rules;
- deciding when a new scope is ready for larger runs.

## Architecture

```text
Wikidata / SPARQL
       |
       v
Burial candidate
       |
       v
Cemetery resolution
  |             |
  | existing    | missing
  v             v
Reuse       Wikidata + Nominatim/OSM
                |
                v
          Web-assisted research
                |
                v
       Create only if supported
                |
                v
        Person enrichment
                |
                v
      Create / merge grave
                |
                v
       CemeteryLink API
                |
                v
        SQLite audit state
```

The local SQLite database contains four main tables:

- `processed` — outcome of each processed burial candidate;
- `cemetery_research` — cached cemetery research and source URLs;
- `osm_cache` — cached geographic search results;
- `cursors` — resumable source progress.

## Measured production evidence

The first complete country-level V2 validation was run on Ireland.

| Metric | Result |
| --- | ---: |
| Processed create/update operations | 1,051 |
| Created | 314 |
| Updated | 737 |
| Distinct graves affected | 1,045 |
| Distinct cemetery IDs associated | 236 |
| Cemeteries with stored research | 140 |
| Recorded run window | ~2 h 19 min |
| Source cursor completed | Yes |

These figures come from the retained SQLite audit state and were extracted into the evidence workbook included with the project.

### Iteration from V1 to V2

An earlier workflow processed 8,335 candidates but skipped 6,769 with the same structural failure:

```text
cemetery not found and coordinates missing
```

V2 changed the workflow rather than simply retrying the same process. Cemetery resolution and research became an explicit step: resolve or research the cemetery first, create it when supported, then continue with the grave.

## Second-user validation

A second user tested the workflow on a fresh United Kingdom scope using a preconfigured development environment.

Across two controlled batches:

| Metric | Result |
| --- | ---: |
| Candidates processed | 20 |
| Cemeteries created | 19 |
| Graves created | 20 |
| Runtime errors | 0 |
| Outputs manually reviewed | 5 |
| Accepted without correction | 5 / 5 |

The reviewed sample found no incorrect cemetery/person association and no unsupported or doubtful information. This is a small validation sample, not a global accuracy claim.

The test also exposed a usability issue around the Python launch command and environment setup. The operating instructions were simplified afterward.

## Repository structure

```text
cemeterylink-enrichment-agent/
├── autonomous_worker.py
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── docs/
│   └── CemeteryLink_Enrichment_Agent_Evidence.pdf
└── evidence/
    └── CemeteryLink_SQLite_Evidence.xlsx
```

Raw SQLite databases, production credentials and local `.env` files are intentionally excluded.

## Requirements

- Python 3.10+
- OpenAI API access
- Access to the CemeteryLink WordPress/API environment
- Internet access to Wikidata/QLever, Wikidata API and OpenStreetMap Nominatim

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

For a virtual environment:

```bash
python -m venv .venv
```

Windows:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Linux/macOS:

```bash
./.venv/bin/python -m pip install -r requirements.txt
```

## Configuration

Copy the example configuration:

```bash
cp .env.example .env
```

On Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Then configure the required values:

```env
WP_URL=
WP_USER=
WP_APP_PASSWORD=

OPENAI_API_KEY=
OPENAI_MODEL=gpt-5.6-luna

COUNTRY_NAME=United States
COUNTRY_QID=Q30
WP_COUNTRY=United States
NOMINATIM_COUNTRY_CODE=us

DB_FILE=cemetery_agent_state.db
BATCH_SIZE=25
MAX_PEOPLE_PER_RUN=10
SLEEP_BETWEEN_BATCHES=2
SPARQL_RETRY_SECONDS=60
```

`SOURCE_KEY` can also be supplied explicitly. If omitted, the worker derives it from `COUNTRY_QID`.

The public example deliberately uses:

```env
MAX_PEOPLE_PER_RUN=10
```

This keeps a first run bounded. Set it to `0` only when the configuration has been reviewed and a full source run is intentional.

## Running the worker

Windows:

```powershell
.\.venv\Scripts\python.exe autonomous_worker.py
```

Linux/macOS:

```bash
./.venv/bin/python autonomous_worker.py
```

Before a production-sized run:

1. Confirm the intended CemeteryLink environment.
2. Confirm the country configuration.
3. Confirm the batch/run limit.
4. Back up the relevant SQLite state database.
5. Start with a controlled batch for a new scope.
6. Review representative created and updated records before increasing scale.

## Processing behavior

The worker uses terminal processing states such as:

- `CREATED`
- `UPDATED`
- `UNCHANGED`
- `DUPLICATE`
- `SKIPPED`

`ERROR` is deliberately non-terminal so transient failures can be retried on a later run.

The worker prefers omission over invention. A cemetery/person relationship should not be published merely because a plausible name match exists.

## Review checklist

For a new country or source scope, representative review should include:

- correct cemetery resolution;
- correct grave-to-cemetery association;
- support for newly added facts;
- duplicate handling;
- behavior for an existing grave;
- handling of weak or conflicting evidence;
- absence of unsupported generated claims;
- unexpected skip, create or merge behavior.

## Evidence

Supporting material:

- [Project evidence PDF](docs/CemeteryLink_Enrichment_Agent_Evidence.pdf)
- [SQLite evidence workbook](evidence/CemeteryLink_SQLite_Evidence.xlsx)
- [Production workflow demo](https://youtu.be/SYaM1aNUqE0)

The raw SQLite databases are retained internally as primary audit material rather than published in the repository.

## Current limitations

- Cemetery descriptions can remain basic when reliable contextual sources are limited.
- Open datasets can contain incomplete, outdated or inconsistent burial information.
- Person-level structured data is often richer than cemetery-level context.
- Duplicate detection and cemetery resolution require conservative thresholds because similarly named places may be different locations.
- Generated prose must not introduce unsupported historical or biographical claims.
- Existing records should not be changed merely for stylistic reasons.
- Production writes are automatic after the workflow's checks, so representative human review remains important.
- The second-user manual quality sample contains only five reviewed outputs.
- No controlled manual baseline has yet been measured, so the project does not claim a percentage time saving or financial ROI.

## Security

Never commit:

- `.env`;
- OpenAI API keys;
- WordPress application passwords;
- production credentials;
- raw local SQLite databases containing operational state.

The included `.gitignore` blocks these common local artifacts.

## Project status

Demonstrated so far:

- real recurring workflow;
- country-level production run;
- measurable create/update output;
- explicit separation between evidence, deterministic logic and generated text;
- persistent audit state;
- documented V1 failure and V2 redesign;
- second-user validation on a new geographic scope;
- reusable configuration for additional countries;
- reviewer-facing evidence and production demo.

The current focus is improving cemetery resolution quality and cemetery-level contextual enrichment without weakening evidence requirements.