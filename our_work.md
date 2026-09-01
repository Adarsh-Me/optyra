Bilkul, poori detail deta hoon. Baat do hisso me samjho — pehle **jo cheezein aapko provide/decide karni hain**, phir **jo steps aapko khud karne hain** (aur beech me friend ka kya role hai). Code ka ab kuch bhi pending nahi hai — sirf credentials, decisions aur deployment aapke/friend ke haath me hai.

---

## PART 1 — Aapko mujhe (project ko) kya provide karna hai

Ye 4 credentials/chizein aapko banana hain, kyunki ye sirf aapke account se ban sakti hain:

**1. GitHub Token (GH_TOKEN) — sabse zaroori, ye bina system chal hi nahi sakta**
- github.com pe apne **main account** se jao → Settings → Developer settings → Fine-grained personal access tokens → Generate new token
- Naam kuch bhi do (jaise "optyra-monitor"), **Expiration 90 days** rakho (report ka rule hai, rotation simple hai)
- **Repository access: "Public repositories (read-only)"** ye option select karo
- **Permissions: kuch bhi mat do — zero** (isse token ka blast radius almost zero hota hai; ye kuch likh nahi sakta, private repos nahi dekh sakta)
- Jo token generate hoga wo ek baar dikhenga — use copy karke `.env` file me `GH_TOKEN=` ke aage paste karna hoga

**2. Telegram Bot Token + Chat ID (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)**
- Telegram me **@BotFather** ko message karo → `/newbot` → bot ka naam do → jo token milega wo `TELEGRAM_BOT_TOKEN` me jayega
- Chat ID ke liye: **@userinfobot** ko message karo, wo aapka numeric ID bata dega — wo `TELEGRAM_CHAT_ID` me jayega
- Ek zaroori step: apne naye bot ko apne account se ek baar `/start` message bhejo (bot sirf unhe message bhej sakta hai jinhone usse baat ki ho)

**3. Gemini API Key (AI_API_KEY) — optional hai, free hai, iske bina bhi bot chalega**
- aistudio.google.com pe jao → Google account se login → "Get API key" → Create
- Free tier aapke liye kaafi hai (20–80 calls/day system karta hai, limit se bahut kam)
- Ye key nahi doge to notifications aayengi, bas AI summary/verdict ke bina — fail-open design hai, kuch break nahi hoga

**4. Org list ka final decision (config/orgs.yaml)**
- Maine 14 orgs ka starter list dala hai (apache, kubernetes, llvm, rust-lang, nodejs, zulip, oppia, scikit-learn, pytorch, sympy, coq, blender, gitlabhq, mpi4py) — ye best-effort seed hai
- Aapko ye decide karna hai: **kaunse 50–100 orgs** monitor karne hain, unme se **top ~25 ko tier 1** (fast polling) banao, baaki tier 2
- Har org ki `gsoc_years` (pichhle 6 saal me GSoC me participation) bhi update karna — report kehta hai ye 30 min ka ek-time kaam hai, public GSoC archive lists se kar lo
- Ye pure text file hai — simple edit hai, code touch karne ki zaroorat nahi

**5. (Optional) Personal preferences — config/ai_criteria.yaml**
- Abhi default criteria theek hain (huge builds, GPU/cluster, proprietary SDKs, Windows-only — sab no-go marked hai, aur Python/Go/TS/Rust preference set hai)
- Agar aapki language preference alag hai (jaise Java/C++ me bhi interested ho) to is file me ek line edit kar dena — ye design me hi aisa hai ki tuning ke liye code nahi chhedna padta

---

## PART 2 — Aapke side se kya karna hai (order me, step by step)

**STEP A — GitHub repo setup (aaj, ~10 min)**
1. GitHub pe naya repository banao, naam `optyra`
2. **Public repo banano** — report recommend karta hai (free GitHub Actions minutes, free GHCR image hosting, free CI)
3. Main branch push karo (folder already git repo hai, 6 commits ke saath)
4. Push karte hi CI workflow chalega — green tick aana chahiye (ruff lint + 76 tests + docker build)

**STEP B — Local test (aaj hi, ~20 min, laptop pe)**
1. Docker Desktop start karo
2. Ek Postgres container start karo (README me exact command hai)
3. `.env.example` ko `.env` me copy karo, upar wale 4 credentials bharo (ye file git me kabhi nahi jayegi, already gitignored hai)
4. `python -m optyra` chalao — startup pe logs me dikhega "optyra started, healthz on :8080, telegram=on, ai=on"
5. Telegram pe message aana shuru — naye issue ke hisaab se 2–5 min me (test karne ke liye temporarily config me `instant_threshold: 85` ko `50` kar sakte ho, phir wapas)
6. `python scripts/spike.py` chalao — ye 5 orgs ka **real GitHub** data laake scored list print karega; output dekh ke aap khud judge kar lo ki system ka taste sahi hai ya thresholds tune karni hain

**STEP C — 48 ghante observation (report ka bhi yahi kehta hai)**
1. Laptop pe 1–2 din chalne do
2. Notifications ka taste dekho — kitne aa rahe hain, relevant hain ya nahi
3. Agar zyada noise hai → `digest_threshold: 70` ko `75` ya `80` karo; kam aa rahe hain → `min_stars` ya labels adjust karo
4. Ye tuning sirf config file me hai, restart hi kaafi hai

**STEP D — Friend/DevOps ke saath handoff (week 1)**
Friend ko `deploy/runbook.md` dena — usme sab likha hai. Uske kaam ye hain:
1. Box provision karna: pehle **Oracle Cloud Always Free ARM** try karo (2–3 region me signup/capacity check karo; fail ho jaye to **Hetzner CX22 ~€4.5/month**)
2. Ubuntu 24.04 + Docker install
3. Box pe `/opt/optyra/` setup: `deploy/` folder, `config/` folder, aur `.env` file (usme production Postgres ka **strong password** set karna — ye uski taraf se aata hai)
4. `docker compose up -d` → `curl localhost:8080/healthz` se verify
5. **Healthchecks.io** pe free account → check banao → URL `.env` me `HEALTHCHECK_URL` me daalna (ye dead-man switch hai — bot mara to aapko email/Telegram milega)
6. `backup.sh` ka nightly cron set karna + monthly ek baar restore drill
7. (Optional) Self-hosted Actions runner register karna label `optyra` ke saath — isse aap jab bhi version tag push karoge, box pe khud deploy ho jayega

**STEP E — First release (sab verify hone ke baad, ~5 min)**
1. `v0.1.0` tag push karo → GitHub workflow amd64+arm64 image banake GHCR pe push karega
2. Friend box pe `OPTYRA_IMAGE` me wo image set karke compose up kare
3. Bas — system live hai, 24/7

---

## PART 3 — Recurring responsibilities (iska calendar bana lo)

| Kaam | Kitni baar | Kaun |
|---|---|---|
| GH PAT rotate (naya banao, .env badlo, restart, purana revoke) | har 90 din | Aap |
| Backup restore drill | monthly | Friend |
| Notifications dekh ke thresholds tune | jab mood ho | Aap |
| Box OS updates + docker prune | monthly | Friend |

---

## PART 4 — Mujhe (agent) se kab-kya lena

Abhi **mere side se koi cheez pending nahi hai** — main na kisi credential ka wait kar raha hoon, na kisi decision ka. System env-vars aur config files se drive hota hai, to aap values bharoge to code ko chhue bina sab chal jayega.

Mujhe tab bulana jab:
- **Phase 2 features** chahiye: FastAPI+HTMX dashboard, Discord/email channel, analytics page
- **Phase 3**: comment-contention detection, GSoC org auto-curation
- Kuch bug lage ya behavior pasand na aaye (logs kaat ke paste kar dena, main fix kar dunga)
- Thresholds/weights me wo tuning chahiye jo config se possible nahi (almost sab config se ho jati hai)

Ek chhota sa honest note: pehli baar real GitHub token lagaoge to sabse pehla data aane me 3–5 min lag sakte hain (repo sync nightly hai, first startup pe 30 sec delay ke saath chalti hai, aur watermark 24 ghante backfill karta hai) — ye bug nahi, design hai taaki purane 1000+ issues ki baarish na aaye, sirf fresh issues dikhen.

Short me: **aapke side sirf 4 banane wale kaam hain (PAT, bot, key, org list) + repo push + ek local test run; friend ka sirf box + compose up; baaki sab code me ho chuka hai aur tested hai.**