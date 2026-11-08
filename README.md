<div align="center">

# Kuly Logistics Document Agent

**A review workspace for multilingual logistics documents, with source evidence and traceable exports.**

![Agent System](https://img.shields.io/badge/Agent_System-LangGraph-7c3aed)
![Backend](https://img.shields.io/badge/Backend-FastAPI-059669)
![Frontend](https://img.shields.io/badge/Frontend-React_19-2563eb)
![Python](https://img.shields.io/badge/Python-3.12-111827)

[Explore the documentation](docs/README.md) | [Watch the recorded walkthrough](outputs/%E6%BC%94%E7%A4%BA%E8%A7%86%E9%A2%91_20261014/%E7%89%A9%E6%B5%81%E5%8D%95%E8%AF%81%E6%99%BA%E8%83%BD%E4%BD%93_%E6%BC%94%E7%A4%BA%E6%88%90%E7%89%87_%E6%97%81%E7%99%BD%E5%AD%97%E5%B9%95.mp4)

</div>

Kuly brings document recognition, translation, cross-document checks, human review, and export into one workflow. It is built for invoices, packing lists, bills of lading, and related materials belonging to the same shipment.

The repository contains the application source, locked dependencies, deployment tooling, implementation records, synthetic demonstration materials, and final presentation assets. The five application pages connect to the real backend: conversation, document review, document management, extensions, and settings.

## Preview

| Input | Result |
| --- | --- |
| A shipment's PDF, DOCX, JPEG, or PNG materials | Recognized content and source references |
| Documents in different languages | Target-language translations for human review |
| Related invoices, packing lists, and transport documents | Comparison matrices and issues linked to their evidence |
| Reviewed documents | Downloadable translated DOCX/PDF files and recognition PDFs |

The [recorded walkthrough](outputs/%E6%BC%94%E7%A4%BA%E8%A7%86%E9%A2%91_20261014/%E7%89%A9%E6%B5%81%E5%8D%95%E8%AF%81%E6%99%BA%E8%83%BD%E4%BD%93_%E6%BC%94%E7%A4%BA%E6%88%90%E7%89%87_%E6%97%81%E7%99%BD%E5%AD%97%E5%B9%95.mp4) includes narration and subtitles. The [project report](outputs/%E8%B7%A8%E5%A2%83%E7%89%A9%E6%B5%81%E5%8D%95%E8%AF%81%E6%99%BA%E8%83%BD%E4%BD%93%E9%A1%B9%E7%9B%AE%E6%8A%A5%E5%91%8A_v2.pdf) explains the competition submission. The homepage's illustrated document journey is a fictional scenario; its editable animation source is in [vedeo/source](vedeo/source/README.md).

## Capabilities

| Capability | Implementation | Practical value |
| --- | --- | --- |
| Document ingestion | Controlled PDF, DOCX, JPEG, and PNG upload; native PDF parsing and configurable OCR | Keeps accepted materials within explicit format and resource limits |
| Translation | Configurable OpenAI-compatible model roles and versioned prompts | Lets operators change providers and models through environment configuration |
| Shipment comparison | Frozen comparison scopes, document relationships, cargo matching, and evidence-backed issues | Makes discrepancies across a shipment visible and reviewable |
| Human review | Original and adopted values, source previews, review decisions, and export checks | Records the basis for a final decision |
| Traceable execution | LangGraph checkpoints, background queues, events, and attempt records | Supports progress tracking and controlled recovery |
| Document management | Search, versions, archived conversations, deletion, and retention tooling | Gives operators a consistent view of stored materials |
| Extensions | Skills, MCP configuration, preferences, and memory controls | Supports configurable assistance within the application's permission model |

## Workflow

```text
Shipment documents
  |
  v
Upload and validate -> Recognize content -> Extract source-linked fields
  |
  v
Declare comparison scope -> Cross-check related documents -> Translate
  |
  v
Inspect evidence -> Record human review decisions -> Confirm export
  |
  v
Download translated documents and recognition files
```

## Quick start

Use Python 3.12 in the project's uv environment, Node.js 20 or newer, npm, and Docker with Compose. Keep the existing Conda-first terminal configuration. Dependency setup belongs to this project; do not install Python packages globally.

Clone through SSH and create the local configuration:

```bash
git clone git@github.com:hhhx-lab/logistics.kuly.git
cd logistics.kuly
cp .env.example .env
```

Fill in `.env` before starting services. The [environment template](.env.example) contains Chinese comments describing each variable's purpose, source, and necessity. Configure the database, checkpoints, Redis, object storage, authentication keys, and any external services you enable. Existing private configuration can be restored locally instead of copying the template.

Prepare the locked application dependencies:

```bash
uv sync --project src/backend --python 3.12 --frozen
npm ci --prefix src/frontend
```

Prepare the local infrastructure, then start the application:

```bash
docker compose up -d
./scripts/start-local.sh
```

The infrastructure command may pull Docker images on a fresh machine. The application launcher uses existing dependencies and images; it does not install packages. Its startup checks include migrations, checkpoints, and service readiness. See the [startup and shutdown guide](docs/%E4%BD%BF%E7%94%A8%E4%BA%A4%E4%BB%98/%E6%9C%AC%E5%9C%B0%E4%B8%80%E9%94%AE%E5%90%AF%E5%8A%A8%E4%B8%8E%E5%81%9C%E6%AD%A2.md) for service reuse and troubleshooting.

- Frontend: [http://localhost:5173](http://localhost:5173)
- API: [http://127.0.0.1:8000](http://127.0.0.1:8000)
- Stop: press Ctrl+C in the launcher terminal, or run `./scripts/start-local.sh stop`.

Development seed accounts, when initialized, are `demo/demo` and `demo2/demo`, in separate tenants. They are development examples; deployed accounts follow the deployment configuration.

## Configuration

| Configuration | Purpose |
| --- | --- |
| `DATABASE_URL`, `CHECKPOINT_DATABASE_URL`, `REDIS_URL` | Domain storage, graph checkpoints, and task queues |
| `S3_*` | Private object storage and browser-accessible upload endpoints |
| `SESSION_SIGNING_KEY`, `MCP_CREDENTIAL_MASTER_KEY` | Session signing and encryption of saved MCP credentials |
| `CHAT_MODEL_*`, `EXTRACT_MODEL_*`, `TRANSLATE_MODEL_*` | Provider endpoints, API keys, models, and per-role controls |
| `MINERU_*` | OCR provider connection and parsing options |
| `PARSER_ADAPTER`, `MODEL_ADAPTER` | Explicit parsing and model adapter selection |
| `MATERIAL_MODE`, `SERVICE_POLICIES` | Material acceptance mode and verified provider policies |

The template defaults to `synthetic_only` materials and `deterministic_fake` adapters. Those adapters exercise the engineering flow with synthetic results. For actual recognition and translation, configure the required providers and choose `mineru` and `openai_compatible` as documented in the template.

Store keys and tokens only in private environment files. `VITE_` variables are exposed to the browser and must contain public information. Production setup examples are in [ops/deploy.env.example](ops/deploy.env.example), [ops/native/server.env.example](ops/native/server.env.example), and [ops/backup.env.example](ops/backup.env.example).

## Repository layout

```text
.
|-- README.md
|-- .env.example
|-- Agent.md
|-- docker-compose.yml
|-- src/
|   |-- backend/     # FastAPI, LangGraph, Celery, Alembic, tests, uv.lock
|   `-- frontend/    # React, TypeScript, Vite, package-lock.json
|-- scripts/         # Local launch, infrastructure initialization, sample tools
|-- ops/             # Container/native deployment, backup, recovery, monitoring
|-- docs/            # Requirements, architecture, implementation, evaluation
|-- vedeo/           # Editable animation source, creative plans, original render
`-- outputs/         # Final project report, recorded walkthrough, subtitles
```

The existing `vedeo` directory name is retained for compatibility with the animation assets and documentation.

## Verification

After preparing dependencies, use these development checks:

```bash
cd src/frontend
npm run typecheck
npm run build
cd ../backend
.venv/bin/python -m pytest
```

Some backend checks require configured infrastructure or provider access. Consult the implementation and evaluation records for their setup and scope. Historical verification records describe their original runs; they do not claim that every scenario has been rerun on a new deployment.

## Documentation and delivery

- [Documentation index](docs/README.md): requirements, architecture, interface contracts, implementation records, and operating guides.
- [Engineering architecture](docs/%E6%8A%80%E6%9C%AF%E6%96%B9%E6%A1%88/%E7%89%A9%E6%B5%81%E5%8D%95%E8%AF%81%E6%99%BA%E8%83%BD%E4%BD%93_%E5%B7%A5%E7%A8%8B%E6%8A%80%E6%9C%AF%E6%96%B9%E6%A1%88.md): backend, frontend, storage, and workflow design.
- [Shipment comparison implementation record](docs/%E5%BC%80%E5%8F%91%E8%AE%B0%E5%BD%95/%E5%90%8E%E7%AB%AF/%E5%90%8C%E7%A5%A8%E6%A0%B8%E5%AF%B9%E4%B8%93%E9%A1%B9%E5%AE%9E%E6%96%BD%E5%8F%B0%E8%B4%A6_X0%E8%87%B3X6.md): completed slices, evidence, and remaining acceptance conditions.
- [Synthetic demonstration package](docs/%E4%BD%BF%E7%94%A8%E4%BA%A4%E4%BB%98/%E6%B1%87%E6%BC%94%E7%A4%BA%E4%BE%8B%E5%8C%85_20261014/README_%E6%B1%87%E6%BC%94%E6%93%8D%E4%BD%9C%E6%89%8B%E5%86%8C.md): sample materials and a repeatable walkthrough.
- [Kuly deployment guide](docs/%E4%BD%BF%E7%94%A8%E4%BA%A4%E4%BB%98/Kuly%E6%9C%8D%E5%8A%A1%E5%99%A8%E9%83%A8%E7%BD%B2%E4%B8%8E%E8%BF%90%E7%BB%B4.md): server deployment and operations.
- [Final deliverables](outputs/README.md): project report, demonstration video, and subtitles.

## Current boundaries

The recorded native-PDF and complex demonstration flows cover recognition, translation, review, and export. The implementation records also identify outstanding acceptance work: broader layouts, authorized external samples, professional regulatory review, and specific conversation-summary cases. There is no published production accuracy claim in this repository.

HS classification and regulatory obligations require qualified review. The bounded IFTMIN parser is an engineering component; EDI is outside the current upload whitelist. Real-document mode requires verified storage and external-service policies. Recovery of an uncertain external request requires investigation because another attempt may incur another provider charge.

## Repository handoff and local recovery

The project was imported into the existing `main` branch on 2026-11-08. The import excludes private environment files, private deployment handoffs, `.codex`, dependency environments, caches, build outputs, repeated source bundles, compressed duplicates, and intermediate narration audio. Final presentation assets and their editable source remain in the repository.

After the verified push, the original local project folder is kept as a handoff folder containing this README, the private root environment, the root environment template, and the private deployment-login environment. To resume development, clone this repository into a working folder and restore private environment files there. GitHub contains templates only; it does not contain those private values.
