# Changing the code and deploying it

[Back to the handbook index](README.md)

Rule of thumb: **edit -> test locally -> push -> deploy the right part -> check**. Nothing deploys by itself (Render `autoDeploy` is off on
purpose) and nothing reaches AWS until you run a deploy script.

## 1. Which part do I have to deploy?
| What you changed | Deploy | Command / action |
|---|---|---|
| Code inside one `services/<name>-service/` | that Render service | Render -> service -> Manual Deploy -> "Deploy latest commit", or `.\scripts\deploy-render.ps1 -Only <name>` |
| Anything in `shared/` | **all 16** Render services (they all embed it) | `.\scripts\deploy-render.ps1` (or Manual Deploy each) |
| `workflows/asl.py`, `workflows/build.py`, `infrastructure/cleandocs-orchestration.yaml`, `workers/` (dispatcher) | AWS orchestration stack | `.\scripts\deploy-orchestration.ps1 -UrlsFile render-urls.json` (needs `$env:SERVICE_AUTH_SECRET`) |
| `infrastructure/cleandocs-core.yaml` (IAM, tables, KB...) | AWS core stack | `.\scripts\deploy-infra.ps1` (check the change set in CloudFormation if unsure) |
| `scripts/build_render_yaml.py` (env vars, new service) | regenerate `render.yaml`, then Render Blueprint sync, then Manual Deploy of affected services | `python scripts\build_render_yaml.py` -> push -> Render -> Blueprint -> Manual Sync |
| Scripts, docs, tests only | nothing (just push) | - |
| A Render environment variable (value change) | restart/redeploy that service | Render -> service -> Environment -> save -> it redeploys |

Blueprint sync only applies `render.yaml` settings (new services, env vars). It does **not** rebuild code: code needs Manual Deploy.

## 2. Standard flow (Windows)
```powershell
cd E:\dev\docindex
# 1. edit files (VS Code)
# 2. run the whole test suite (no AWS used; ~1 minute; expect all green)
uv run --python 3.12 --with-requirements requirements-local.txt --with pydantic-settings --with "pytest>=8,<9" --with "httpx>=0.27,<1" --with "moto[dynamodb,s3,sqs]>=5,<6" --with-editable shared python -m pytest tests -q
# 3. (optional) try it on your PC against real AWS, no Render involved:
.\scripts\run_local.ps1 ; python scripts\local_step.py upload C:\docs\x.pdf ; .\scripts\stop_local.ps1     # see docs\local-testing.md
# 4. check what you are about to commit - no secrets, no personal files:
git status ; git diff --stat
# 5. commit and push
git add -A ; git commit -m "what and why" ; git push
# 6. deploy the right part (table above), wait until Render shows "Live"
# 7. wake and test
.\scripts\warm-up.ps1
python scripts\smoke_test_pipeline.py --urls-file render-urls.json
```
After a service or workflow change, upload a real file and read the reconciliation (operator handbook, sections 3-5).

## 3. Common change recipes
**Change a setting (limits, caps):** Render -> `cleandocs-config` group or the service env var. Examples: `TEXTRACT_MONTHLY_PAGE_CAP`, `ENRICHMENT_MONTHLY_DOC_CAP`, `CHUNK_TARGET_CHARS`, `MAX_FILE_SIZE_MB`. New defaults for a fresh deployment: edit `scripts/build_render_yaml.py`.

**Change chunking or OCR behaviour:** edit `services/chunking-service/chunking_service/chunker.py` or `services/extraction-service/...`; add a test in `tests/`; deploy that service. Existing documents keep their old chunks: use `cleandocs.ps1 delete` and upload again to re-process. (Bumping a service's `VERSION` in its `__init__.py` makes the stage re-run on a replay instead of being skipped.)

**Add a new file type:** add the type in `shared/cleandocs_shared/filetypes.py`, a parser in `services/normalization-service/.../parsers.py`, add it to `SUPPORTED_KINDS` in normalization, tests; deploy **all** (shared changed) and validation + normalization.

**Add chunk metadata:** `services/embedding-service/.../logic.py` `build_attributes` (stay within the 1,500-byte budget; the test checks it), reconciliation `REQUIRED_SIDECAR_KEYS` if mandatory, `docs/citations.md`; re-index existing data (delete and upload again).

**Add a new service (stage):** copy `services/enrichment-service` as a template; add it to `scripts/build_render_yaml.py` (tier + env), `workflows/build.py` SERVICES, `workflows/asl.py` (FILE_STAGES, or SOFT_BEFORE for a stage that must never fail a document), `scripts/local_gateway.py` and `scripts/warm-up.ps1` lists, tests (service count in `tests/unit/test_render_blueprint.py`, `tests/integration/test_workflows.py`). Then: `python scripts\build_render_yaml.py`, push, Render Blueprint sync (creates it), Manual Deploy, `deploy-orchestration.ps1`. If it needs a new AWS permission: edit `cleandocs-core.yaml` and run `deploy-infra.ps1` first.

**Rotate the service secret:** new value in Render group `cleandocs-auth` and `$env:SERVICE_AUTH_SECRET`, run `deploy-orchestration.ps1`, redeploy (restart) all services.

**Rotate AWS keys:** `aws iam list-access-keys --user-name cleandocs-processing`, create a new key (`create-access-keys.ps1` after deleting its `secrets\` file), update the Render group, redeploy, then `aws iam delete-access-key` the old one. Never keep two keys longer than needed.

## 4. Verify a deploy
* Render service shows **Live**; `https://<service-url>/health/ready` returns `ready` (S3 and registry true).
* `.\scripts\warm-up.ps1` shows all UP.
* Step Functions: a new execution of `cleandocs-ingestion` goes green; reconciliation is `COMPLETED`.
* If something broke after a deploy: Render -> service -> Events -> "Rollback" to the previous deploy, or `git revert <commit>`, push, redeploy.

## 5. Rules that keep it safe
1. Never commit `secrets\`, `.env`, `render-urls.json`, `ex.json`, `m.json`, test documents, or any key (`git status` before every commit; `.gitignore` already blocks these).
2. Tests must pass before a push; add a test with each fix.
3. `shared/` changes mean deploy all services - a half-deployed state breaks stage hand-offs.
4. One change set per deploy; check the reconciliation report after it.
5. Update `docs/PROJECT-STATE.md` (sections 5-7) when you finish a phase.
