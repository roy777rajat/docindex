# Deploy to Render and push to Git

[Back to the handbook index](README.md)

Nothing deploys by itself: Render `autoDeploy` is **off** on purpose (all 16 services share `shared/`, so an automatic deploy would rebuild all 16
images). A `git push` only stores the code; you then deploy what changed.

## 1. Push your changes to Git
```powershell
cd E:\dev\docindex
git status                      # look at the list: no secrets\, .env, render-urls.json, ex.json, m.json, test documents
git add -A
git commit -m "what changed and why"
git push
```
If `git push` asks you to log in, use your GitHub account (browser prompt) or a personal access token. Never put a token in a file in the repo.
Check on GitHub: repo `roy777rajat/docindex` -> the latest commit shows your files; open `docs/README.md` for the handbook index.

## 2. Which part must be deployed?
| What you changed | Deploy | How |
|---|---|---|
| Inside one `services/<name>-service/` | that Render service | Render -> the service -> **Manual Deploy -> Deploy latest commit**, or `.\scripts\deploy-render.ps1 -Only <name>` |
| Anything in `shared/` | **all 16** Render services | `.\scripts\deploy-render.ps1` (or Manual Deploy each) |
| `workflows/`, `workers/`, `infrastructure/cleandocs-orchestration.yaml` | AWS orchestration stack | `$env:SERVICE_AUTH_SECRET = "<your secret>"` then `.\scripts\deploy-orchestration.ps1 -UrlsFile render-urls.json` |
| `infrastructure/cleandocs-core.yaml` | AWS core stack | `.\scripts\deploy-infra.ps1` |
| `scripts/build_render_yaml.py` (env vars, new service) | regenerate the Blueprint, sync it, then deploy the services that changed | `python scripts\build_render_yaml.py` -> push -> Render -> Blueprint `cleandocs` -> **Manual Sync** |
| A Render environment variable | save it in Render (the service redeploys by itself) | Render -> service -> Environment |
| Only docs, tests or scripts | nothing | just push |

**Blueprint sync only applies `render.yaml` settings (new services, env vars); it never rebuilds code.** Code needs a Manual Deploy.

## 3. Deploy with the script (all or some services)
```powershell
$env:RENDER_API_KEY = "rnd_..."          # Render -> Account Settings -> API Keys -> Create (delete it afterwards)
.\scripts\deploy-render.ps1                      # all 16, waits until each is live
.\scripts\deploy-render.ps1 -Only upload,manifest
.\scripts\deploy-render.ps1 -NoWait
```
The script has not been proven against Render. If it fails, use the dashboard: each service -> **Manual Deploy -> Deploy latest commit**.
Check: every service shows **Live**, and `https://<service-url>/health/ready` answers `ready`.

## 4. Test after a deploy
```powershell
.\scripts\warm-up.ps1                                         # Render sleeps; wake everything first
$env:SERVICE_AUTH_SECRET = "<your secret>"
python scripts\smoke_test_pipeline.py --urls-file render-urls.json
```
Then upload a real file and read the reconciliation: [Operator handbook](operator-handbook.md), sections 3-5.

## 5. Run the tests before you push (about 1 minute, no AWS used)
```powershell
uv run --python 3.12 --with-requirements requirements-local.txt --with pydantic-settings --with "pytest>=8,<9" --with "httpx>=0.27,<1" --with "moto[dynamodb,s3,sqs]>=5,<6" --with-editable shared python -m pytest tests -q
```
Everything must be green before a push.

## 6. If a deploy broke something
Render -> the service -> **Events** -> pick the previous deploy -> **Rollback**; or `git revert <commit>`, push, deploy again.

## 7. First-time Render setup (new account or new workspace)
See [New AWS account setup](new-account-setup.md), section 7, and `deploy-render.md` (Blueprint, environment groups, real URLs).

## More
Recipes (change a setting, chunking, add a file type, add a service, rotate secrets): [Change and deploy handbook](change-and-deploy.md).
