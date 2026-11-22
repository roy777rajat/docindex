# CleanDocs AI - handbook index

Everything you need to run, change, move and clean up CleanDocs AI. Open the guide you need:

| # | Guide | Use it when |
|---|---|---|
| a | [Operator handbook](operator-handbook.md) | You want to **upload** a file or ZIP, **track** it, **reconcile**, read the results, fix a problem, or deal with **Render sleep** |
| b | [Delete assets from the AWS account](aws-teardown.md) | You are leaving this AWS account (or stopping the project) and want **zero charges**: inventory of every resource and the one-command teardown |
| c | [Set up in a new AWS account](new-account-setup.md) | You want a **fresh infrastructure** in a new account: values you need, where to find them, step by step |
| d | [Delete documents](delete-documents.md) | You want to **delete one file, several files, a whole upload or everything** (and re-ingest) |
| e | [Deploy to Render and push to Git](render-and-git-deploy.md) | You changed code or settings and need to **push and deploy** the right part |

## Background and reference
| Guide | What it is |
|---|---|
| [Project state](PROJECT-STATE.md) | **Read first when you come back**: what exists, what is done, what is pending, known limits, enhancements, resource names |
| [Change and deploy recipes](change-and-deploy.md) | How to change chunking, add a file type or a service, rotate secrets |
| [Chunk metadata contract](citations.md) | Which attributes every indexed chunk carries (owner, doc type, tags, page...) for the future query-api |
| [Phase 10 plan](phase10-metadata-v2-plan.md) | Owner, LLM enrichment, deletion design and decisions |
| [Render deployment (first time)](deploy-render.md) | Blueprint, environment groups, real URLs |
| [Local testing](local-testing.md) | Run all services on one local port and test them with curl |
| [Operations runbook](operations.md) | Replay failed files, re-check a document |
| [Reconciliation](phase8-reconciliation.md) | What each check proves and how the final status is decided |
| [Orchestration](phase7-orchestration.md) | The Step Functions workflows and the gate |
| [AWS infrastructure](phase2-infrastructure.md) | The first deployment of the core stack |
| [Graph skeleton](graph-skeleton.md) | The unfinished knowledge-graph stage |

## The commands you will use most (PowerShell, from `E:\dev\docindex`)
```powershell
.\scripts\warm-up.ps1                       # wake the sleeping Render services before an upload
.\scripts\cleandocs.ps1 list                # what is stored
.\scripts\cleandocs.ps1 delete doc_xxx      # delete one document
.\scripts\cleandocs.ps1 delete-all          # delete everything (asks you to type DELETE ALL)
.\scripts\show-config.ps1                   # bucket, Knowledge Base id, ARNs of this deployment
.\scripts\teardown-account.ps1 -Inventory   # what exists in this AWS account (deletes nothing)
```
Security: never commit secrets, keys, `secrets\`, `render-urls.json` or scratch JSON files with document text (they are in `.gitignore`).
