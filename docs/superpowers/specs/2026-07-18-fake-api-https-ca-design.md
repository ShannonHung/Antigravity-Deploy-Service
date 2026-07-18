# fake-api HTTPS + 自建 CA + deploy-service CA 驗證

Date: 2026-07-18

## 背景與目標

`deploy-service/fake-api/` 是本機開發用的假 Inventory + Cluster API,目前只提供
HTTP(uvicorn port 9001)。目標:

1. 用**自建的兩層 PKI**(Root CA → server 憑證)讓 fake-api 提供 HTTPS。
2. deploy-service 的 `InventoryClient` 連線時用該 CA 驗證 fake-api 的憑證。
3. 全程 openssl 步驟寫進 `fake-api/docs/readme.md` 當教學文件(使用者要「學著建立這一切」)。
4. 產生憑證的過程自動化成一個 Makefile target,使用者不用手打 openssl。
5. 憑證檔案**不 push 到 GitHub**。

這模仿 deploy-service 既有的 GitLab CA 模式:
`app/repositories/gitlab_pipeline_repository.py:68`
```python
ssl_verify: str | bool = settings.GITLAB_CA if settings.GITLAB_CA else True
```

## 現況(已存在的東西)

- `INVENTORY_API_VERIFY_SSL: bool = True`(`app/core/config.py:84`)已存在。
- `InventoryClient.__init__` 已接受 `verify_ssl` 並傳給
  `httpx.AsyncClient(verify=...)`(`app/clients/inventory_client.py:80,97`)。
  httpx 的 `verify` 參數可接受 `bool` **或字串 CA 檔案路徑**,所以 client 本身不需改。
- `_build_inventory_client()`(`app/core/dependencies.py:157`)已把
  `verify_ssl=s.INVENTORY_API_VERIFY_SSL` 傳進去。
- `make inventory-api`(`Makefile:75`)以純 HTTP 啟動 uvicorn port 9001。

## 兩層 PKI 架構

```
Root CA (ca.key + ca.crt)          ← 自簽,離線保管
      │ 簽發
      ▼
Server cert (server.key + server.crt)   ← fake-api 用它跑 HTTPS
      CN/SAN = localhost, 127.0.0.1
```

信任關係:
```
deploy-service (InventoryClient, httpx)
   verify = INVENTORY_CA (= data/ca.crt 路徑)
        │  HTTPS
        ▼
   fake-api :9001  (server.crt 由 ca.crt 簽發)

httpx 用 ca.crt 驗證 server.crt 的簽章鏈 → 通過 → 連線成功
```

關鍵:deploy-service **只信任 ca.crt**,不直接信任 server.crt。之後換 server 憑證
只要用同一個 CA 重簽,deploy-service 不用改設定。

## 檔案佈局

```
fake-api/certs/            ← 全部憑證的產生地(整個資料夾被 .gitignore 排除)
    ca.key    ca.crt       ← Root CA(產生 + 簽發都在這)
    server.key server.crt  ← fake-api 的 HTTPS 憑證(CN/SAN = localhost, 127.0.0.1)
    server.csr            ← 中間產物(可保留或清掉)
deploy-service/data/
    ca.crt                 ← 從 fake-api/certs/ 複製過來,deploy-service 驗證用
```

職責劃分:CA 與 server 憑證的產生/簽發都屬於 fake-api,集中在 `fake-api/certs/`;
deploy-service 只需要 `ca.crt` 一份公開憑證來驗證,由 Makefile 複製過去。

## 元件與改動

### 1. Makefile 新 target `make certs`

用 openssl 產生整套 PKI 並複製 `ca.crt` 到 deploy-service。步驟:

1. `mkdir -p fake-api/certs`
2. Root CA:
   - `openssl genrsa -out fake-api/certs/ca.key 4096`
   - `openssl req -x509 -new -nodes -key fake-api/certs/ca.key -sha256 -days 3650
      -subj "/CN=fake-inventory-local-ca" -out fake-api/certs/ca.crt`
3. Server key + CSR:
   - `openssl genrsa -out fake-api/certs/server.key 2048`
   - `openssl req -new -key fake-api/certs/server.key
      -subj "/CN=localhost" -out fake-api/certs/server.csr`
4. 用 CA 簽發 server 憑證(帶 SAN):透過 `-extfile`/`-addext` 加上
   `subjectAltName=DNS:localhost,IP:127.0.0.1`
   - `openssl x509 -req -in fake-api/certs/server.csr
      -CA fake-api/certs/ca.crt -CAkey fake-api/certs/ca.key -CAcreateserial
      -days 825 -sha256 -out fake-api/certs/server.crt
      -extfile <(printf "subjectAltName=DNS:localhost,IP:127.0.0.1")`
5. `cp fake-api/certs/ca.crt data/ca.crt`

target 需 `.PHONY`。SAN 是必要的 —— 現代 TLS client(含 httpx)只看 SAN,不看 CN。

### 2. `make inventory-api` 改為 HTTPS

在既有 uvicorn 指令加上:
```
--ssl-keyfile fake-api/certs/server.key --ssl-certfile fake-api/certs/server.crt
```
port 9001 變成 HTTPS。target 描述文字(`Makefile:24`)一併更新提到 HTTPS。

### 3. deploy-service config 新增 `INVENTORY_CA`

`app/core/config.py`,在 Inventory API 區塊:
```python
INVENTORY_CA: str = ""  # CA 憑證檔案路徑;有設定時 InventoryClient 用它驗證 TLS
```

### 4. `_build_inventory_client()` 帶入 CA

`app/core/dependencies.py:157`,計算 verify 值:
```python
verify = s.INVENTORY_CA if s.INVENTORY_CA else s.INVENTORY_API_VERIFY_SSL
return InventoryClient(
    base_url=s.INVENTORY_API_URL,
    token_manager=_get_inventory_token_manager(),
    timeout=s.INVENTORY_API_TIMEOUT_SECONDS,
    verify_ssl=verify,
)
```
`INVENTORY_CA` 優先;沒設定時 fallback 到既有的 `INVENTORY_API_VERIFY_SSL`(bool),
保留「關閉驗證」的開關能力。`InventoryClient` 本身不改。

### 5. `.gitignore`

新增兩行(路徑相對於 git root = `deploy-service/`):
```
fake-api/certs/
data/ca.crt
```

### 6. `.env.dev`

設定讓本機開發用 HTTPS + CA:
```
INVENTORY_API_URL=https://localhost:9001
INVENTORY_CA=data/ca.crt
```
(這些是本機開發覆寫值,不影響 test / prod。)`INVENTORY_CA=data/ca.crt`
是相對於 CWD 的路徑;所有 make / uvicorn 指令都從 `deploy-service/` 目錄執行,
故相對路徑成立。

### 7. unit test

參考 `tests/unit/test_gitlab_pipeline_repository.py` 的風格(不需真實連線):
測 `_build_inventory_client()` 建出來的 client 的 verify 選擇邏輯。

- `INVENTORY_CA` 有設定 → client 的 `_verify_ssl` == 該路徑字串。
- `INVENTORY_CA` 空 + `INVENTORY_API_VERIFY_SSL=True` → `_verify_ssl` is True。
- `INVENTORY_CA` 空 + `INVENTORY_API_VERIFY_SSL=False` → `_verify_ssl` is False。

作法:用 `get_settings.cache_clear()` + monkeypatch 環境變數(或直接建
`InventoryClient` 驗證 `_verify_ssl` 屬性)。放在
`tests/unit/test_inventory_client.py`(若已存在則追加)。純邏輯、不起 TLS。

### 8. `fake-api/docs/readme.md` 教學文件

從空白寫成完整教學,涵蓋:

1. 為什麼要兩層 CA(Root CA vs server 憑證的角色)。
2. 每條 openssl 指令逐步解釋在做什麼(genrsa / req -x509 / CSR / x509 -req 簽發 / SAN)。
3. 憑證檔案各自的用途與存放位置。
4. 怎麼用 `make certs` 一鍵產生。
5. 怎麼用 `make inventory-api` 啟動 HTTPS 版。
6. deploy-service 端如何設定 `INVENTORY_CA` 並驗證連線。
7. 驗證步驟:用 `openssl s_client` 或 `curl --cacert` 測 HTTPS 是否通。
8. 憑證不進版控(`.gitignore`)、以及憑證過期/重簽的注意事項。

## 非目標(YAGNI)

- 不做憑證自動輪替 / 到期告警。
- 不改 test / prod 環境設定(維持既有 HTTP + bool verify,除非之後另外要求)。
- 不對 fake-api 加入 client 憑證(mTLS);只做 server-side TLS。
- 不改 `InventoryClient` 的內部實作。

## 驗收標準

- `make certs` 產出 4 個憑證檔並複製 `ca.crt` 到 `data/`。
- `make inventory-api` 以 HTTPS 啟動;`curl --cacert fake-api/certs/ca.crt https://localhost:9001/...` 成功、`curl` 不帶 cacert 會 TLS 失敗。
- deploy-service 設 `INVENTORY_CA=data/ca.crt` 後能成功呼叫 inventory endpoint。
- unit test 通過:verify 選擇邏輯三種情境正確。
- `git status` 不顯示任何憑證檔(被 .gitignore 排除)。
