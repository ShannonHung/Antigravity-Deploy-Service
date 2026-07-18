# fake-api HTTPS + CA verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve the local fake inventory API over HTTPS using a self-built two-layer CA, and make deploy-service verify it via an `INVENTORY_CA` setting.

**Architecture:** A `make certs` target generates a Root CA (`ca.key`/`ca.crt`) and a CA-signed server cert (`server.key`/`server.crt`, SAN=localhost,127.0.0.1) into `fake-api/certs/`, then copies `ca.crt` into `deploy-service/data/`. `make inventory-api` runs uvicorn with TLS. deploy-service adds `INVENTORY_CA`; the client's TLS `verify` value becomes `INVENTORY_CA if INVENTORY_CA else INVENTORY_API_VERIFY_SSL`. `InventoryClient` already forwards `verify_ssl` to httpx, so only config + the DI factory change.

**Tech Stack:** openssl, uvicorn (TLS flags), FastAPI, httpx, pydantic-settings, pytest.

## Global Constraints

- All `make` / `uvicorn` / `git` commands run from the git root = `deploy-service/`.
- Run tests with `APP_ENV=test uv run pytest ...`.
- Reset the settings cache in tests with `get_settings.cache_clear()` (settings are `lru_cache`'d).
- Cert files must never enter version control (`.gitignore`).
- Mirror the existing GitLab CA pattern: `verify = <CA path> if <CA path> else <bool>`.
- Do NOT modify `InventoryClient` internals — it already accepts `verify_ssl` and forwards it to `httpx.AsyncClient(verify=...)`.
- Branch: `feat/fake-api-https-ca` (already created; spec already committed).

---

### Task 1: Add `INVENTORY_CA` config + wire CA into the DI factory

**Files:**
- Modify: `app/core/config.py` (Inventory API block, after line 84 `INVENTORY_API_VERIFY_SSL`)
- Modify: `app/core/dependencies.py:157-164` (`_build_inventory_client`)
- Test: `tests/unit/test_inventory_client.py` (append)

**Interfaces:**
- Consumes: `get_settings()` → `Settings` with fields `INVENTORY_API_URL`, `INVENTORY_API_TOKEN`, `INVENTORY_API_TIMEOUT_SECONDS`, `INVENTORY_API_VERIFY_SSL: bool`, and new `INVENTORY_CA: str`.
- Produces: `_build_inventory_client() -> InventoryClient` whose `._verify_ssl` equals `INVENTORY_CA` (str) when set, else `INVENTORY_API_VERIFY_SSL` (bool). `InventoryClient._verify_ssl` is the attribute set from the `verify_ssl` constructor arg (`app/clients/inventory_client.py:86`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_inventory_client.py`:

```python
# ── _build_inventory_client verify selection ─────────────────────────────────

from app.core.config import get_settings
from app.core.dependencies import _build_inventory_client


def test_build_inventory_client_uses_ca_path_when_set(monkeypatch):
    monkeypatch.setenv("INVENTORY_CA", "data/ca.crt")
    monkeypatch.setenv("INVENTORY_API_VERIFY_SSL", "false")
    get_settings.cache_clear()
    try:
        client = _build_inventory_client()
        assert client._verify_ssl == "data/ca.crt"
    finally:
        get_settings.cache_clear()


def test_build_inventory_client_falls_back_to_verify_true(monkeypatch):
    monkeypatch.setenv("INVENTORY_CA", "")
    monkeypatch.setenv("INVENTORY_API_VERIFY_SSL", "true")
    get_settings.cache_clear()
    try:
        client = _build_inventory_client()
        assert client._verify_ssl is True
    finally:
        get_settings.cache_clear()


def test_build_inventory_client_falls_back_to_verify_false(monkeypatch):
    monkeypatch.setenv("INVENTORY_CA", "")
    monkeypatch.setenv("INVENTORY_API_VERIFY_SSL", "false")
    get_settings.cache_clear()
    try:
        client = _build_inventory_client()
        assert client._verify_ssl is False
    finally:
        get_settings.cache_clear()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `APP_ENV=test uv run pytest tests/unit/test_inventory_client.py -k build_inventory_client -v`
Expected: FAIL — `INVENTORY_CA` unknown / `_verify_ssl` mismatch (CA path not honored yet).

- [ ] **Step 3: Add the config field**

In `app/core/config.py`, immediately after the `INVENTORY_API_VERIFY_SSL: bool = True` line (currently line 84), add:

```python
    # CA cert file path for verifying the Inventory API's TLS cert.
    # When set, it takes precedence over INVENTORY_API_VERIFY_SSL (mirrors GITLAB_CA).
    INVENTORY_CA: str = ""
```

- [ ] **Step 4: Wire the CA into the factory**

In `app/core/dependencies.py`, replace the body of `_build_inventory_client` (lines 157-164) with:

```python
def _build_inventory_client() -> InventoryClient:
    s = get_settings()
    # INVENTORY_CA (CA file path) wins; otherwise fall back to the bool switch.
    verify = s.INVENTORY_CA if s.INVENTORY_CA else s.INVENTORY_API_VERIFY_SSL
    return InventoryClient(
        base_url=s.INVENTORY_API_URL,
        token_manager=_get_inventory_token_manager(),
        timeout=s.INVENTORY_API_TIMEOUT_SECONDS,
        verify_ssl=verify,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `APP_ENV=test uv run pytest tests/unit/test_inventory_client.py -k build_inventory_client -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Run the full inventory client test file (no regressions)**

Run: `APP_ENV=test uv run pytest tests/unit/test_inventory_client.py -v`
Expected: PASS (all).

- [ ] **Step 7: Commit**

```bash
git add app/core/config.py app/core/dependencies.py tests/unit/test_inventory_client.py
git commit -m "feat: add INVENTORY_CA and use it for inventory TLS verification"
```

---

### Task 2: `make certs` target — generate the two-layer PKI

**Files:**
- Modify: `Makefile` (add `certs` target; add its help line near line 24)
- Modify: `.gitignore` (append cert exclusions)

**Interfaces:**
- Consumes: `openssl` on PATH.
- Produces: `fake-api/certs/{ca.key,ca.crt,server.key,server.csr,server.crt}` and a copy at `data/ca.crt`. Server cert has `subjectAltName=DNS:localhost,IP:127.0.0.1`.

- [ ] **Step 1: Add cert ignores to `.gitignore`**

Append to `.gitignore`:

```
# ── Local TLS certs for fake-api (generated by `make certs`) ──
fake-api/certs/
data/ca.crt
```

- [ ] **Step 2: Add the `certs` target to the Makefile**

Add this target (and keep it `.PHONY`). Note: recipe lines MUST be tab-indented, and each shell line runs in its own subshell, so keep the SAN one-liner self-contained.

```makefile
# certs: 產生本機 fake-api HTTPS 用的兩層 PKI（Root CA + server 憑證）
.PHONY: certs
certs:
	mkdir -p fake-api/certs
	openssl genrsa -out fake-api/certs/ca.key 4096
	openssl req -x509 -new -nodes -key fake-api/certs/ca.key -sha256 -days 3650 \
		-subj "/CN=fake-inventory-local-ca" -out fake-api/certs/ca.crt
	openssl genrsa -out fake-api/certs/server.key 2048
	openssl req -new -key fake-api/certs/server.key -subj "/CN=localhost" \
		-out fake-api/certs/server.csr
	openssl x509 -req -in fake-api/certs/server.csr \
		-CA fake-api/certs/ca.crt -CAkey fake-api/certs/ca.key -CAcreateserial \
		-days 825 -sha256 -out fake-api/certs/server.crt \
		-extfile <(printf "subjectAltName=DNS:localhost,IP:127.0.0.1")
	cp fake-api/certs/ca.crt data/ca.crt
	@echo "✅ certs generated in fake-api/certs/ and ca.crt copied to data/"
```

Also add a help line near line 24 (next to the `make inventory-api` help entry):

```makefile
	@echo "  make certs         產生 fake-api HTTPS 憑證（Root CA + server cert）"
```

> Note: `<(...)` process substitution requires the recipe to run under bash. If the default make shell is `/bin/sh` and this fails, add `SHELL := /bin/bash` near the top of the Makefile. Verify in Step 3.

- [ ] **Step 3: Run the target and verify output**

Run: `make certs`
Expected: no error; ends with the ✅ line. Then verify:

```bash
ls fake-api/certs/    # ca.key ca.crt server.key server.csr server.crt (+ ca.srl)
test -f data/ca.crt && echo "ca.crt copied"
openssl x509 -in fake-api/certs/server.crt -noout -text | grep -A1 "Subject Alternative Name"
```
Expected: SAN line shows `DNS:localhost, IP Address:127.0.0.1`.

- [ ] **Step 4: Verify the cert chain is valid**

Run: `openssl verify -CAfile fake-api/certs/ca.crt fake-api/certs/server.crt`
Expected: `fake-api/certs/server.crt: OK`

- [ ] **Step 5: Confirm certs are git-ignored**

Run: `git status --short`
Expected: NO `fake-api/certs/...` or `data/ca.crt` entries appear (only `Makefile`, `.gitignore`, and pre-existing unrelated changes).

- [ ] **Step 6: Commit**

```bash
git add Makefile .gitignore
git commit -m "feat: add make certs target for fake-api local TLS PKI"
```

---

### Task 3: Serve fake-api over HTTPS + point deploy-service dev at it

**Files:**
- Modify: `Makefile:75-76` (`inventory-api` recipe + its help line at ~line 24)
- Modify: `.env.dev:25,28` (`INVENTORY_API_URL`, add `INVENTORY_CA`)

**Interfaces:**
- Consumes: `fake-api/certs/server.key`, `fake-api/certs/server.crt` (from Task 2), `data/ca.crt` (from Task 2).
- Produces: HTTPS endpoint at `https://localhost:9001`; dev deploy-service configured with `INVENTORY_API_URL=https://localhost:9001` and `INVENTORY_CA=data/ca.crt`.

- [ ] **Step 1: Switch the inventory-api recipe to HTTPS**

Replace the recipe line at `Makefile:76`:

```makefile
	APP_ENV=dev $(UV) run uvicorn fake-api.main:app --reload --port 9001
```

with:

```makefile
	APP_ENV=dev $(UV) run uvicorn fake-api.main:app --reload --port 9001 \
		--ssl-keyfile fake-api/certs/server.key \
		--ssl-certfile fake-api/certs/server.crt
```

Update the help line (near line 24) to note HTTPS:

```makefile
	@echo "  make inventory-api 啟動本機假 Inventory API（HTTPS，port 9001）"
```

- [ ] **Step 2: Point dev config at HTTPS + CA**

In `.env.dev`, change line 25 from `INVENTORY_API_URL=http://localhost:9001` to:

```
INVENTORY_API_URL=https://localhost:9001
```

And add a new line after `INVENTORY_API_VERIFY_SSL=false` (line 28):

```
INVENTORY_CA=data/ca.crt
```

(Leave `INVENTORY_API_VERIFY_SSL=false` as-is; `INVENTORY_CA` now takes precedence.)

- [ ] **Step 3: Start the HTTPS fake-api (background) and verify TLS**

Run (in one terminal / background):
`make inventory-api`

Then verify from another shell — a request WITH the CA succeeds, WITHOUT it fails:

```bash
curl -sS --cacert fake-api/certs/ca.crt \
  -H "Authorization: Token fake-inventory-token" \
  https://localhost:9001/inventory/hosts/node1 -o /dev/null -w "with-ca: %{http_code}\n"

curl -sS https://localhost:9001/inventory/hosts/node1 -o /dev/null \
  -w "no-ca: %{http_code}\n" 2>&1 | head -1
```
Expected: `with-ca: 200` (or 404 if node1 absent — either proves TLS + auth worked);
the no-ca call prints a TLS certificate-verification error (non-zero curl exit).

Stop the background server after verifying.

- [ ] **Step 4: Commit**

```bash
git add Makefile .env.dev
git commit -m "feat: serve fake-api over HTTPS and point dev deploy-service at it via INVENTORY_CA"
```

---

### Task 4: Write the `fake-api/docs/readme.md` tutorial

**Files:**
- Modify: `fake-api/docs/readme.md` (currently empty)

**Interfaces:**
- Consumes: everything built in Tasks 1–3 (the `make certs` / `make inventory-api` targets, `INVENTORY_CA` setting).
- Produces: a self-contained step-by-step tutorial.

- [ ] **Step 1: Write the tutorial**

Write `fake-api/docs/readme.md` covering these sections (prose in the user's Chinese, code blocks as-is). Each openssl command must be shown AND explained:

1. **為什麼要兩層 CA** — Root CA 是信任錨(自簽、只簽發不直接服務);server 憑證由它簽發、給 fake-api 跑 HTTPS 用。換 server 憑證時 deploy-service 只信任 CA,不用改設定。
2. **一鍵產生** — `make certs`,並列出它產出的檔案與各自用途:
   - `ca.key` Root CA 私鑰(簽發用,最敏感)
   - `ca.crt` Root CA 公開憑證(deploy-service 拿來驗證)
   - `server.key` fake-api 的私鑰
   - `server.crt` fake-api 的憑證(由 CA 簽發,含 SAN)
3. **每條 openssl 指令逐步解釋**:
   - `openssl genrsa` → 產生 RSA 私鑰
   - `openssl req -x509 -new -nodes -key ca.key` → 用私鑰自簽產生 Root CA 憑證(`-nodes`=不加密私鑰)
   - `openssl genrsa` (server) + `openssl req -new` → 產 server 私鑰與 CSR(憑證簽署請求)
   - `openssl x509 -req -CA ca.crt -CAkey ca.key -CAcreateserial ... -extfile` → 用 CA 簽發 server 憑證,`subjectAltName` 為何必要(現代 TLS 只看 SAN 不看 CN)
4. **啟動 HTTPS 版** — `make inventory-api`(已帶 `--ssl-keyfile/--ssl-certfile`)。
5. **deploy-service 端設定** — 在 `.env.dev` 設 `INVENTORY_API_URL=https://localhost:9001` 與 `INVENTORY_CA=data/ca.crt`;說明 `verify = INVENTORY_CA if INVENTORY_CA else INVENTORY_API_VERIFY_SSL` 的優先順序(對照 `GITLAB_CA`)。
6. **驗證連線** — 提供兩個 curl 範例(帶 `--cacert` 成功 vs 不帶失敗),以及 `openssl verify -CAfile fake-api/certs/ca.crt fake-api/certs/server.crt` 檢查簽章鏈。
7. **注意事項** — 憑證被 `.gitignore` 排除、不進版控;server 憑證有效期(825 天)、過期就重跑 `make certs`;這是本機開發用,勿用於 prod。

- [ ] **Step 2: Verify the doc references match reality**

Run:
```bash
grep -q "make certs" fake-api/docs/readme.md && \
grep -q "INVENTORY_CA" fake-api/docs/readme.md && \
grep -q "subjectAltName" fake-api/docs/readme.md && echo "doc references OK"
```
Expected: `doc references OK`

- [ ] **Step 3: Commit**

```bash
git add fake-api/docs/readme.md
git commit -m "docs: add fake-api HTTPS + CA setup tutorial"
```

---

## Self-Review

**1. Spec coverage:**
- §Makefile `make certs` → Task 2 ✓
- §`make inventory-api` HTTPS → Task 3 Step 1 ✓
- §config `INVENTORY_CA` → Task 1 Step 3 ✓
- §`_build_inventory_client` verify logic → Task 1 Step 4 ✓
- §`.gitignore` cert exclusion → Task 2 Step 1 ✓
- §`.env.dev` URL + CA → Task 3 Step 2 ✓
- §unit test verify logic (3 cases) → Task 1 Step 1 ✓
- §readme tutorial → Task 4 ✓
- §驗收標準 (curl with/without cacert, openssl verify, git status clean) → Task 2 Steps 4–5, Task 3 Step 3 ✓

**2. Placeholder scan:** No TBD/TODO; all code and commands are concrete. ✓

**3. Type consistency:** `_verify_ssl` attribute name matches `app/clients/inventory_client.py:86`; `_build_inventory_client` signature matches `dependencies.py`; `verify = INVENTORY_CA if INVENTORY_CA else INVENTORY_API_VERIFY_SSL` consistent across Task 1 and Task 4 doc. ✓

**Note for executor:** `.env.dev` already has `INVENTORY_API_VERIFY_SSL=false`; the plan intentionally leaves it and relies on `INVENTORY_CA` precedence. The process-substitution `<(...)` in `make certs` needs bash — if the recipe fails under `/bin/sh`, add `SHELL := /bin/bash` to the Makefile (called out in Task 2 Step 2).
