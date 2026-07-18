# fake-api HTTPS + 自建 CA 教學

這份文件說明 `fake-api`（本機假 Inventory API）如何用一組「自建 CA」讓 `deploy-service`
透過 HTTPS 連線，而不需要關掉 SSL 驗證。目標是讓你看完之後，能理解每一條
`openssl` 指令在做什麼、為什麼要這樣分兩層、以及設定要怎麼接起來。

## 1. 為什麼要兩層 CA

整套設定分成兩張憑證：

- **Root CA**（`ca.key` + `ca.crt`）：自己簽自己（self-signed）的信任錨
  （trust anchor）。它不會被拿來直接服務任何連線，唯一的工作是「簽發」其他憑證。
- **Server 憑證**（`server.key` + `server.crt`）：由 Root CA 簽發，實際給
  `fake-api` 拿來跑 HTTPS 用的憑證。

為什麼不乾脆讓 `fake-api` 自己簽一張自簽憑證就好？因為分兩層之後：

- `deploy-service` 只需要信任「Root CA」這一張憑證（`INVENTORY_CA=data/ca.crt`），
  不用管 server 憑證實際內容是什麼。
- 之後如果 server 憑證過期、或要重新產生（例如加新的網域），只要還是用同一把
  Root CA 簽發，`deploy-service` 端完全不用改設定 —— 它信任的是「簽發者」，
  不是某一張特定的憑證。
- 這模擬了真實世界的 PKI 架構（瀏覽器信任 DigiCert / Let's Encrypt 這類根
  CA，而不是逐一信任每個網站的憑證）。

一句話：**Root CA 是信任的起點，server 憑證是實際被驗證、被使用的那一張，
兩者用簽章鏈（certificate chain）串起來。**

## 2. 一鍵產生：`make certs`

```bash
make certs
```

這個 target 會在 `fake-api/certs/` 產出以下檔案，並把 `ca.crt` 複製一份到
`data/ca.crt`（給 `deploy-service` 讀取用）：

| 檔案 | 用途 |
|---|---|
| `ca.key` | Root CA 的私鑰。簽發其他憑證要用它簽名，**最敏感的一份**，絕對不能外流或進版控。 |
| `ca.crt` | Root CA 的公開憑證（自簽）。`deploy-service` 透過 `INVENTORY_CA` 讀這個檔案，用它來驗證 `fake-api` 的憑證是不是「自己人簽的」。 |
| `server.key` | `fake-api` 自己的私鑰，配對 `server.crt` 使用，同樣不能外洩。 |
| `server.crt` | `fake-api` 實際拿來跑 HTTPS 的憑證，由 Root CA 簽發，內含 SAN（見下方第 3 節）。 |

執行完會看到：

```
✅ certs generated in fake-api/certs/ and ca.crt copied to data/
```

## 3. 每條 openssl 指令逐步解釋

`make certs` 底層其實就是四個步驟、五條指令。以下逐條拆解。

### 3.1 產生 Root CA 私鑰

```bash
openssl genrsa -out fake-api/certs/ca.key 4096
```

`genrsa` 產生一把 RSA 私鑰，`4096` 是金鑰長度（bits）。這把私鑰之後會被用來
「簽名」其他憑證，所以長度給大一點（4096 而不是 server 端用的 2048），
代表它扮演的角色更關鍵、有效期也更長（見下一步 3650 天）。

### 3.2 用私鑰自簽出 Root CA 憑證

```bash
openssl req -x509 -new -nodes -key fake-api/certs/ca.key -sha256 -days 3650 \
    -subj "/CN=fake-inventory-local-ca" -out fake-api/certs/ca.crt
```

拆解每個參數：

- `req` — 憑證請求（certificate request）相關操作。
- `-x509` — 不要只產生一份「請求」，直接輸出一張自簽憑證（X.509 格式）。
  這就是「Root CA 自己簽自己」的關鍵參數。
- `-new` — 產生新的請求（配合 `-x509` 就是「新請求 + 直接自簽」）。
- `-nodes` — **no DES**，意思是「不要幫這把私鑰加密碼保護」。因為
  `ca.key` 是本機開發用、且會被腳本自動讀取，不需要每次輸入 passphrase；
  正式環境的 CA 私鑰通常不會加這個參數。
- `-key fake-api/certs/ca.key` — 用剛剛產生的私鑰來簽這張憑證（自己簽自己）。
- `-sha256` — 簽章用的雜湊演算法。
- `-days 3650` — 有效期 10 年，因為這是本機開發用的信任錨，不需要常換。
- `-subj "/CN=fake-inventory-local-ca"` — 直接用參數帶入憑證主體
  （Subject）的 Common Name，跳過互動式問答。
- `-out fake-api/certs/ca.crt` — 輸出檔名，就是最終的 Root CA 公開憑證。

做完這一步，`ca.crt` 就是一張「誰都可以驗證簽章，但只有握有 `ca.key`
的人才能簽發新憑證」的信任錨。

### 3.3 產生 server 私鑰 + CSR（憑證簽署請求）

```bash
openssl genrsa -out fake-api/certs/server.key 2048
openssl req -new -key fake-api/certs/server.key -subj "/CN=localhost" \
    -out fake-api/certs/server.csr
```

第一行跟 3.1 一樣是產生私鑰，只是給 server 端用，長度用一般常見的 2048
即可（不像 Root CA 需要更長的保護期）。

第二行產生 **CSR（Certificate Signing Request，憑證簽署請求）**：

- `req -new` — 這次沒有 `-x509`，所以不是自簽，而是產生一份「請求」
  ——「我是 `localhost`，這是我的公鑰（附在 `server.key` 裡），
  請幫我簽一張憑證」。
- `-key fake-api/certs/server.key` — 用 server 自己的私鑰產生請求
  （私鑰不會被放進 CSR，只有對應的公鑰會）。
- `-subj "/CN=localhost"` — 這張憑證要宣稱自己是 `localhost`。
- `-out fake-api/certs/server.csr` — 輸出 CSR 檔案，等著 Root CA 來簽。

CSR 本身**還不是憑證**，它只是「還沒被任何人簽名的申請書」。

### 3.4 用 Root CA 簽發 server 憑證（含 SAN）

```bash
openssl x509 -req -in fake-api/certs/server.csr \
    -CA fake-api/certs/ca.crt -CAkey fake-api/certs/ca.key -CAcreateserial \
    -days 825 -sha256 -out fake-api/certs/server.crt \
    -extfile <(printf "subjectAltName=DNS:localhost,IP:127.0.0.1")
```

這是整套流程的核心 —— 讓 Root CA 真正「簽發」這張 server 憑證：

- `x509 -req` — 處理一份既有的請求（CSR），輸出成 X.509 憑證。
- `-in fake-api/certs/server.csr` — 輸入的 CSR，也就是 3.3 產生的申請書。
- `-CA fake-api/certs/ca.crt` / `-CAkey fake-api/certs/ca.key` — 指定用
  哪張 CA 憑證 + 對應私鑰來簽發。這就是「簽章鏈」的關鍵：`server.crt`
  裡面會留下「由這把 CA 私鑰簽名」的證據，之後任何人只要有 `ca.crt`
  就能驗證這個簽章是否成立（見第 6 節 `openssl verify`）。
- `-CAcreateserial` — 建立一個序號檔（`ca.srl`），CA 每簽一張憑證都要有
  唯一序號，避免序號衝突。
- `-days 825` — server 憑證的有效期，825 天（約 2 年 3 個月）。這個數字
  不是隨便選的：業界（CA/Browser Forum、各瀏覽器廠商）對「終端伺服器憑證」
  訂出的建議/上限就是 825 天，比 Root CA 短很多——因為伺服器憑證換發成本低、
  也應該常態性輪替。
- `-sha256` — 簽章雜湊演算法，跟 CA 憑證一致。
- `-out fake-api/certs/server.crt` — 輸出最終的 server 憑證。
- `-extfile <(printf "subjectAltName=DNS:localhost,IP:127.0.0.1")` —
  這是**必要且不能省略**的一段。`<(...)` 是 bash 的 process substitution，
  把 `printf` 的輸出當成一個「暫存檔」餵給 `-extfile`，內容只有一行：
  `subjectAltName=DNS:localhost,IP:127.0.0.1`。

  **為什麼一定要有 SAN（Subject Alternative Name）？**
  早期 TLS 只看憑證的 Common Name（CN，就是 3.3 那個 `/CN=localhost`）
  來判斷「這張憑證是不是核發給我正在連的這個網域/IP」。但現代瀏覽器與
  TLS 函式庫（Chrome、curl、Python `ssl`/`httpx` 等）**已經完全不看
  CN**，只認 `subjectAltName` 擴展欄位。如果簽發憑證時沒帶 SAN，
  即使 CN 寫的是 `localhost`，用戶端一樣會回報主機名稱不符
  （hostname mismatch）而拒絕連線。這裡同時列出 `DNS:localhost`
  與 `IP:127.0.0.1`，涵蓋用網域名稱與用 IP 兩種連線方式。

  > 註：因為 `Makefile` 裡有 `SHELL := /bin/bash`，這條指令的
  > `<(...)` process substitution 才能正常運作；如果用 `/bin/sh`
  > 執行會失敗。

### 3.5 複製 Root CA 給 deploy-service 用

```bash
cp fake-api/certs/ca.crt data/ca.crt
```

`deploy-service` 不需要、也不應該知道 `server.key`／`server.crt` 的存在，
它只需要那把「信任錨」`ca.crt`，放在 `data/ca.crt`，透過設定檔讀取
（見第 5 節）。

## 4. 啟動 HTTPS 版：`make inventory-api`

```bash
make inventory-api
```

對應的 Makefile 內容：

```make
inventory-api:
	APP_ENV=dev $(UV) run uvicorn fake-api.main:app --reload --port 9001 \
		--ssl-keyfile fake-api/certs/server.key \
		--ssl-certfile fake-api/certs/server.crt
```

`uvicorn` 透過 `--ssl-keyfile` / `--ssl-certfile` 直接讀取 3.3～3.4 產生的
server 私鑰與憑證，在 `9001` port 上跑起 HTTPS（而不是一般的 HTTP）。
啟動後可以用瀏覽器或 curl 連到 `https://localhost:9001`，只是預設不被
系統信任（因為 Root CA 是自己生的，不在系統信任清單裡）——這正是第 5、6
節要解決的問題。

## 5. deploy-service 端設定

在 `.env.dev` 裡：

```bash
INVENTORY_API_URL=https://localhost:9001
INVENTORY_CA=data/ca.crt
```

`app/core/dependencies.py` 的 `_build_inventory_client()` 決定實際傳給
HTTP client 的 `verify` 參數：

```python
verify = INVENTORY_CA if INVENTORY_CA else INVENTORY_API_VERIFY_SSL
```

意思是：

- 如果 `INVENTORY_CA` 有設定路徑（這裡是 `data/ca.crt`），就把這個檔案
  路徑當成 `verify` 傳給底層 HTTP client —— 等同於告訴它「不要用系統的
  信任清單，改用這張自建 CA 來驗證憑證鏈」。
- 如果 `INVENTORY_CA` 是空字串（預設值），才退回去看
  `INVENTORY_API_VERIFY_SSL`（布林值，`true`/`false` 決定要不要驗證
  SSL，`false` 等於完全不驗證，僅適合臨時除錯用）。

這跟 `GITLAB_CA` 的設計是同一套邏輯（`app/core/config.py` 裡兩者並列），
一個是給 GitLab client 用、一個是給 Inventory client 用，優先順序完全一致：
**「有自建 CA 就用自建 CA 驗證，沒有才看要不要跳過驗證」**。

`.env.dev` 目前也保留了 `INVENTORY_API_VERIFY_SSL=false`，但因為
`INVENTORY_CA` 已經設定，實際上會走「用 `data/ca.crt` 驗證」這條路，
`INVENTORY_API_VERIFY_SSL` 這個值不會被用到。

## 6. 驗證連線

先確保 `make certs` 已執行過、且 `make inventory-api` 正在跑。

**帶 `--cacert` 應該成功**（明確告訴 curl 要用哪張 CA 驗證）：

```bash
curl --cacert fake-api/certs/ca.crt \
    -H "Authorization: Token fake-inventory-token" \
    https://localhost:9001/inventory/hosts/node1
```

**不帶 `--cacert` 應該失敗**（curl 找不到系統信任清單裡有這張自簽 CA，
會回報類似 `SSL certificate problem: unable to get local issuer
certificate` 的 TLS 錯誤）：

```bash
curl -H "Authorization: Token fake-inventory-token" \
    https://localhost:9001/inventory/hosts/node1
```

**用 `openssl verify` 直接檢查簽章鏈**（不透過網路連線，純粹驗證檔案）：

```bash
openssl verify -CAfile fake-api/certs/ca.crt fake-api/certs/server.crt
```

如果第 3.4 節的簽發流程正確，這裡應該印出：

```
fake-api/certs/server.crt: OK
```

代表 `server.crt` 確實是由 `ca.crt` 對應的私鑰簽發、且尚未過期。

## 7. 注意事項

- **不進版控**：`fake-api/certs/`（含所有私鑰、憑證、CSR）與
  `data/ca.crt` 都已列在 `.gitignore`，不會被提交。每個開發者在自己的
  機器上執行 `make certs` 各自產生一份即可，不需要共用。
- **有效期限**：server 憑證（`server.crt`）有效期是 825 天，Root CA
  （`ca.crt`）是 3650 天（10 年）。若過期導致 TLS 交握失敗，直接重新
  執行 `make certs` 即可重新產生整組憑證（含新的 Root CA）。
- **僅限本機開發**：這整套自建 CA 流程（`-nodes` 私鑰不加密、CA 私鑰與
  server 私鑰放在同一台機器、`INVENTORY_API_VERIFY_SSL=false` 的
  fallback 值）都只適合本機開發情境，**絕對不要用在 production**。
  正式環境應該用受信任的公開 CA（例如 Let's Encrypt）簽發的憑證。
