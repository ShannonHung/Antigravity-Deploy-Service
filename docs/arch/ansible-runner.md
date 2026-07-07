# Ansible 執行架構：`run-ansible.sh` 運作原理與即時／結束 Log 觀看

本文說明 deploy-service 如何透過 SSH 指令 API 觸發 Ansible playbook，重點放在
四件事：

1. **Ansible 執行的整條呼叫鏈是怎麼跑起來的**（從 API 一路到 node1/node2）。
2. **撰寫 `run-ansible.sh` 時必須考量哪些問題**（為什麼每一段都長這樣）。
3. **要看「即時 log」或「結束後的 log」時，背後是怎麼做到的**。
4. **run 怎麼撐過 deploy-service 重啟、結果怎麼被補寫回來、kill 的語意**
   （§3.5 程序存活與孤兒復原、§3.6 kill 語意 —— 這兩節是踩過 exit 141 與「kill 沒
   反應」之後得到的設計，務必讀）。

相關程式碼：

| 檔案 | 角色 |
|------|------|
| `ansible/run-ansible.sh` | 真正啟動 runner image 的啟動腳本 |
| `ansible/Dockerfile`、`ansible/ansible.cfg` | runner image（純 Ubuntu + ansible，無 ENTRYPOINT） |
| `data/allow-commands-admin.json` | 白名單：定義 API 可呼叫哪些 `run-ansible.sh` 變體 |
| `app/services/command_service.py` | 觸發指令、`_build_step_wrapper`（脫鉤/握手）、跨 pod kill、`get_command_trace` 讀 log、`_heal_from_marker` 孤兒復原 |
| `app/api/v1/command.py` | kill 端點（`force` override、非 killable 回 409） |
| `docker-compose-nodes.yml` | 本機的 node1 / node2 / control_node 測試環境 |

---

## 1. 整條執行鏈

```
deploy-service (FastAPI)
   │  ① 使用者打 POST /api/v1/command/execution（帶 command_name + arguments）
   │  ② CommandService 比對白名單、驗證參數、把 {run_id} 之類佔位符解析掉
   │  ③ SSH 進 control_node（data/SSH-control_node.json，port 2224）
   ▼
control_node（Ansible 跳板機 / jump host）
   │  ④ 執行 ansible/run-ansible.sh
   │       - git clone 全新 inventory（用完即刪）
   │       - docker pull / docker run ansible runner image（Docker-out-of-Docker）
   ▼
ansible runner container（純 Ubuntu + ansible，跑完即退出）
   │  ⑤ ansible-playbook -i /inventory/... /playbooks/ping.yml
   │       透過 ansible 的 ssh connection plugin
   ▼
node1 / node2（被管理的目標主機）
```

幾個關鍵設計概念：

- **control_node 是跳板機，不是執行端。** deploy-service 永遠只 SSH 到
  control_node，由 control_node 上的 `run-ansible.sh` 負責後續所有事情。這讓
  deploy-service 不需要知道任何 Ansible 細節，只要會跑一條被白名單批准的指令。

- **Docker-out-of-Docker（DooD）。** `run-ansible.sh` 在 control_node 容器內執
  行，但它呼叫的 `docker run` 是透過掛載進來的 host docker socket
  (`/var/run/docker.sock`) 對 **host daemon** 下指令。也就是說 ansible
  container 其實是 host 的兄弟容器，不是 control_node 的子容器。這個事實會直接
  影響到下面所有 bind-mount 的路徑設計（見 §2）。

- **runner image 沒有 ENTRYPOINT。** 它就是一台裝好 ansible 的 Ubuntu，
  `/ansible.cfg`、`/playbooks`、`/collections` 都烤在 root。容器要執行什麼，完全
  由 `run-ansible.sh` 傳進去的 `ansible-playbook ...` 決定，跑完就退出。

### 為什麼跳板機這層是必要的

Ansible 需要：Python 環境、ansible 本體、collections、SSH client、能連到目標主機
的網路位置。把這些全部塞進 deploy-service 既肥又難維運。改成「deploy-service 只
負責 SSH 觸發一條腳本」之後：

- deploy-service 的攻擊面只有「SSH + 白名單指令」，跟既有的 SSH command API 共用同
  一套防注入保證。
- Ansible 的版本、collections、playbook 都封裝在一個可獨立重建、可掃描、可版本控管
  的 image 裡。

---

## 2. 撰寫 `run-ansible.sh` 要考量的問題

這支腳本看起來只是「clone + docker run」，但每一段都是為了解決一個具體問題。以下逐
項說明「為什麼要這樣寫」。

### 2.1 防注入：所有使用者值都是離散參數，絕不拼字串

最重要的一條紀律：**永遠不要用 `eval`，也不要把使用者輸入塞進一條 shell 字串。**

```bash
CMD_ARGS=(ansible-playbook -i "/inventory/$INVENTORY" "/playbooks/$PLAYBOOK")
[[ -n "$TAGS"       ]] && CMD_ARGS+=(--tags "$TAGS")
[[ -n "$LIMIT"      ]] && CMD_ARGS+=(--limit "$LIMIT")
[[ -n "$EXTRA_VARS" ]] && CMD_ARGS+=(--extra-vars "$EXTRA_VARS")
...
docker run ... "$IMAGE" "${CMD_ARGS[@]}"
```

指令以 **bash 陣列**逐一組裝，再以 `"${CMD_ARGS[@]}"` 展開成獨立的 argv。這跟
deploy-service 那層用 `shlex.join` + positional argument 的策略是一致的：使用者
給的 `inventory`、`limit` 永遠是「一個參數」，不會被當成 shell 語法解讀。正則／黑名
單檢查是縱深防禦，**這個 argv 傳遞才是承重的防注入保證。**

> 修改這支腳本時：只要看到有人想把使用者值拼進字串再 `eval`，就是一個 bug。

### 2.2 「永遠最新」：inventory 每次重新 clone、image 每次 pull

```bash
git clone --depth 1 --branch "$INVENTORY_REF" "$INVENTORY_REPO" "$CLONE_DIR"
trap cleanup EXIT       # cleanup() { rm -rf "$CLONE_DIR"; }
...
[[ "$PULL" -eq 1 ]] && docker pull "$IMAGE"
```

- **Inventory 每次跑都全新 clone，跑完用 `trap ... EXIT` 刪掉。** 你永遠不會指向某
  個本機 checkout，因此不可能用到過期的 inventory。要哪一份就用「相對 repo root 的
  路徑」去選（例如 `taipei/multinode.ini`）。
- **Image 每次跑前先 `docker pull`。** 因為團隊每週 code review 後會用相同的
  `latest` tag 重新發佈（同名、新內容）。本機測試自建 image 時用 `--no-pull` 跳過。

考量點：自動清理要早做、且要保證會做。inventory 用 `trap EXIT` 保證即使中途失敗也
會刪。log 的清理（見 §2.6）則刻意放在「開工前」而不是「結束後」，避免長跑或被 kill
的 run 跳過清理。

### 2.3 DooD 帶來的路徑陷阱：clone 目錄與掛載路徑必須 host 一致

這是整支腳本最容易踩雷的地方。因為 ansible container 是在 **host daemon** 上啟動的，
`docker run -v <path>:...` 裡的 `<path>` 會對 **host 檔案系統**解析，而不是
control_node 容器內部的檔案系統。

所以：

```bash
# 不能 clone 到 control_node 私有的 /tmp（host 看不到），
# 要 clone 到「腳本旁邊」這種 host 也掛得到的位置。
CLONE_PARENT="${CLONE_PARENT:-$SCRIPT_DIR/.run-tmp}"
```

對應到 `docker-compose-nodes.yml`，control_node 把整個 repo 用 **相同的絕對路徑**
掛進容器：

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
  - ${PWD}:${PWD}          # 容器內路徑 == host 路徑，這樣 -v 才對得上
```

考量點：**只要你在腳本裡產生一個之後要 `docker run -v` 掛進 ansible container 的路
徑，那個路徑就必須是 host 看得到的、而且最好 host 與容器內一致。** 否則會出現
「腳本明明 clone 成功，ansible container 卻說 inventory 不存在」的詭異錯誤。

### 2.4 嚴格驗證所有會變成路徑／檔名的輸入

```bash
# inventory 不能是絕對路徑、不能含 ..（防止逃出 clone 目錄）
case "$INVENTORY" in
  /*|*..*) echo "Error: --inventory must be a relative path inside the repo." >&2; exit 2 ;;
esac
[[ -f "$CLONE_DIR/$INVENTORY" ]] || { echo "inventory not found"; exit 2; }

# run-id 會直接變成 log 檔名 → 嚴格白名單字元
[[ "$RUN_ID" =~ ^[A-Za-z0-9_-]+$ ]] || exit 2

# retention 必須是非負整數
[[ "$LOG_RETENTION_DAYS" =~ ^[0-9]+$ ]] || exit 2
```

考量點：任何「來自外部、又會變成路徑或檔名」的值都要驗證 —— 不只是為了防注入，也是
為了**避免路徑穿越**（`../` 逃出 clone 目錄）和**避免拿不存在的檔案去跑、得到一堆難
懂的 ansible 錯誤**。腳本特意在 inventory 找不到時，列出 repo 裡可用的 inventory 檔，
讓呼叫端能快速修正。

### 2.5 連得到目標主機：`host.docker.internal` 與 SSH key

```bash
docker run --rm \
  --add-host host.docker.internal:host-gateway \
  -v "$SSH_KEY":/root/.ssh/id_key:ro \
  -e ANSIBLE_PRIVATE_KEY_FILE=/root/.ssh/id_key \
  ...
```

- node1/node2 把 SSH port 發佈到 host（2222 / 2223）。ansible container 透過
  `host.docker.internal` 連回 host。`--add-host ...:host-gateway` 是為了讓這在
  **Linux** 上也成立（macOS / Docker Desktop 本來就隱含支援）。
- SSH 私鑰以 read-only 掛進容器，並用 `ANSIBLE_PRIVATE_KEY_FILE` 指給 ansible 用。
  腳本一開始就檢查 key 存在 (`[[ -f "$SSH_KEY" ]]`)，否則直接退出，避免跑到一半才在
  ansible 那層失敗。
- `ansible.cfg` 裡 `host_key_checking=False` 加上
  `-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null`，是為了讓非互動式
  SSH 不會卡在 host-key 確認的提示。

### 2.6 結束碼要正確傳遞 + 寫終止標記 + log 自我清理

```bash
set +e
docker run ... 2>&1 | tee "$LOG_FILE"
RUN_EXIT="${PIPESTATUS[0]}"        # ← ansible/docker 那端的結束碼，不是 tee 的
set -e

echo "=== EXIT $RUN_EXIT ===" >> "$LOG_FILE"      # 人類看得到的標記
[[ -n "$RUN_ID" ]] && printf '%s\n' "$RUN_EXIT" \
  > "$LOG_DIR/$RUN_ID.exit.tmp" && mv -f ... "$LOG_DIR/$RUN_ID.exit"   # 機器解析的 sidecar
exit "$RUN_EXIT"
```

- runner image **不會自己寫 ansible log 檔**，所以腳本把 container 的
  stdout/stderr 用 `tee` 寫到 control_node 上的 `<run_id>.log`。

- **結束碼必須是 ansible 的，不是 `tee` 的。** `cmd | tee` 預設回傳 `tee` 的結束碼
  （通常 0），會吃掉 ansible 的非零碼。原本靠 `set -o pipefail` 解決；現在因為要在
  pipeline 之後**多做事**（寫標記），改用 `set +e` 暫時關掉「遇錯即停」，再用
  `${PIPESTATUS[0]}` 精準取出 pipeline **左端**（docker/ansible）的結束碼，最後
  `exit "$RUN_EXIT"` 把真正的碼還回去。這樣等待這支腳本的人（deploy-service 的快路
  徑 task）仍看得到 success/failed。`run_ansible_fail` 這個白名單指令就是用來驗證這
  條路徑。

- **終止標記（terminal marker）：log 檔就是這個 run 結果的「真相來源」。** 這是支撐
  「孤兒復原」(§3.5) 的關鍵設計。腳本跑完後一定會記錄真正的結束碼，**即使失敗也要記**：

  - 在 log 結尾 append 一行 `=== EXIT <code> ===`（`/view` 裡人眼直接看得到）；
  - 若有 `--run-id`（代表是 deploy-service 觸發的 run），額外寫一個
    `<run_id>.exit` sidecar，內容就是結束碼。

  **為什麼要 sidecar 而不只是 log 行？** sidecar 用 `tmp + mv` 原子寫入，讀取端永遠
  不會讀到寫到一半的半截檔；而且 `cat <id>.exit` 解析比「去 log 尾巴撈 `=== EXIT`」
  乾淨太多。兩個都寫：log 行給人看，sidecar 給機器讀。

  > **情境**：deploy-service 的某個 pod 在 ansible 跑到一半時被 OOM / 重啟。run 在
  > control_node 上**繼續跑完**並寫下 `=== EXIT 0 ===` + `<id>.exit`。稍後任何一個
  > pod 收到輪詢，就能 SSH 回來讀這個碼、把卡在 RUNNING 的 Redis 狀態修正成 success。
  > 沒有這個標記，那個 run 會永遠卡 RUNNING 直到 TTL 過期 —— 對使用者就是「永遠轉圈」。

- **log 自我清理放在「開工前」**：

```bash
if [[ "$LOG_RETENTION_DAYS" -gt 0 && -n "$LOG_DIR" && -d "$LOG_DIR" ]]; then
  find "$LOG_DIR" -maxdepth 1 -type f -name '*.log' -mtime "+$LOG_RETENTION_DAYS" -delete
fi
```

  考量點：(a) 放開工前，保證長跑／被 kill 的 run 不會跳過清理；(b) 多重 guard
  (`-n "$LOG_DIR" && -d "$LOG_DIR"`)，避免 `LOG_DIR` 為空時把刪除範圍擴大成整個檔
  系統；(c) 只刪「比保留窗更舊」的檔，所以同時在跑的 `<run_id>.log` 永遠安全，並行
  執行不會互相砍檔。

### 2.7 可測試性：`DRYRUN` hook

```bash
if [[ "${DRYRUN:-0}" == "1" ]]; then
  echo "DRYRUN log file: $LOG_FILE"
  exit 0
fi
```

考量點：腳本在「任何 docker / git 動作之前」提供一個檢查點，印出解析後的 log 路徑就
退出。`tests/integration/test_run_ansible_script.py` 靠它做單元測試，**不需要網路或
docker**。撰寫這類腳本時保留一個純參數解析的退出點，會讓測試輕鬆很多。

### 2.8 Secrets：inventory token 與 vault 密碼（`--secret-path`）

> **SCRIPT_VERSION 2.0.0 起**：舊的 `--token-file` 與 GIT_ASKPASS helper 已移除，統
> 一改成「source 一個 env 檔」。

私有 inventory clone 需要 `INVENTORY_TOKEN`，ansible-vault 解密需要
`ANSIBLE_VAULT_PASSWORD`。這兩個 secret 都由 **control_node 本機**的一個 KEY=VALUE
env 檔提供，用 `--secret-path <path>`（或 `SECRET_PATH` 環境變數）指定：

```bash
# ~/.secrets.env（chmod 600）
INVENTORY_TOKEN=glpat-xxx
ANSIBLE_VAULT_PASSWORD=s3cr3t
```

```bash
load_secrets() {
  # source 等於執行程式碼 → 拒絕 group/other 可存取的檔案（否則可寫 = RCE）
  [[ "${mode: -2}" == "00" ]] || exit 2      # 必須 chmod 600/400
  set -a; . "$SECRET_PATH"; set +a           # 匯入並 export
}
```

考量點：**secret 只活在真正用它的機器上**，不放進白名單 JSON（那會進 git、也會被
`get_user_commands` API 回傳）、不經 deploy-service、不過網路。`set -a` 讓匯入的 key
自動 export，後面 git 與 `docker run -e` 才拿得到。

#### token：走 git env-config，不進 argv / URL / `.git/config`

```bash
auth_b64="$(printf 'oauth2:%s' "$CLONE_TOKEN" | base64 | tr -d '\n')"
GIT_CONFIG_COUNT=1 \
  GIT_CONFIG_KEY_0="http.extraHeader" \
  GIT_CONFIG_VALUE_0="Authorization: Basic $auth_b64" \
  git clone --depth 1 --branch "$INVENTORY_REF" "$INVENTORY_REPO" "$CLONE_DIR"
```

token 以 HTTP Authorization header 經 git 的**環境式設定**（`GIT_CONFIG_*`，git ≥
2.31）注入，只存在於 git 行程的環境（base64）。因為 clone 用的是**乾淨 URL**，所以
token 不會被寫進 clone 的 `.git/config` —— 這很重要，因為那個目錄稍後會被
`-v ...:/inventory:ro` 掛進 ansible container。token 全程不進 argv（`ps` 看不到）、不
進 URL、不進任何 log / summary。

#### vault：自動產生密碼檔、預設 `ANSIBLE_VAULT_PASSWORD_FILE`

ansible-core 沒有原生讀 `ANSIBLE_VAULT_PASSWORD` env 的能力，只認
`ANSIBLE_VAULT_PASSWORD_FILE`（指向容器內一個檔案）。所以只要 sourced 到
`ANSIBLE_VAULT_PASSWORD`，`ensure_vault_client()` 就在腳本旁**自動產生**一個密碼檔
（`.ansible_vault`，chmod 600、非可執行、已 gitignore、每次執行重寫）：

```bash
( umask 077; printf '%s' "$ANSIBLE_VAULT_PASSWORD" > "$VAULT_PASS_FILE" )
```

然後 `docker run` 這樣接：

```bash
-v "$VAULT_PASS_FILE":/ansible_vault:ro \      # 把密碼檔掛進容器（DooD host 一致）
-e ANSIBLE_VAULT_PASSWORD_FILE=/ansible_vault  # 預設指好，ansible 直接讀檔解密
```

考量點：ansible 直接讀掛進去的檔當密碼，**argv 裡只會有「路徑」、不會有密碼**，log／
summary 也不會出現密碼。因為 `ANSIBLE_VAULT_PASSWORD_FILE` 已預設，`ansible-playbook`
不用再加任何 vault 參數——normal run 與 `--debug` 容器裡手動跑都直接能解密。

> **取捨**：這個 `.ansible_vault` 檔**含明文密碼**（跟 `secrets.env` 一樣落在
> control_node 上，chmod 600、gitignored）。這是為了「直接 mount 一個檔」的極簡做法
> 換來的——省掉了「產生一支讀 env 的 client 腳本 + 用 value-less `-e` 把密碼灌進容器
> 環境」那層 indirection。若你偏好「掛進去的檔不含 secret」，那層 client-script 版本
> 也是可行的替代。

### 2.9 `--debug` 可省略 playbook / inventory

`-d` 是「起一個 idle 容器（`sleep infinity`）讓人 `docker exec` 進去戳」的模式，所以
它不一定要有 playbook / inventory：

- **兩個都省略** → 跳過 inventory clone（git 完全不跑）、不掛 `/inventory`，起一個純
  debug 容器；vault 若有設定仍會掛好、env 仍預設（見 §2.8）。
- **兩個都給** → 照常 clone + 掛 `/inventory`，行為不變。
- **只給一個** → 視為筆誤，直接報錯退出。

非 debug 模式仍強制要 playbook + inventory。

---

## 3. 怎麼看 log（即時 / 結束後）

先釐清 log 的物理位置：

- ansible 的輸出 → runner container 的 stdout
- → `run-ansible.sh` 用 `tee` 寫到 **control_node 上的** `<log-dir>/<run_id>.log`
  （白名單裡是 `/var/log/ansible-runs/<run_id>.log`）
- deploy-service 本身**不持有**這個檔；它在需要時才 SSH 進 control_node 去讀。

`{run_id}` 是 deploy-service 在組裝指令時注入的 server 端值（不是使用者參數），所以
log 檔名在指令真正執行前就已經確定、可預測、可被後續查詢用到。白名單上對應的指令都標
記了 `"logged": true`。

### 3.1 結束後的 log（最終結果）

兩種拿法：

1. **API 輪詢狀態** —— `GET /api/v1/command/execution/{command_id}`。對 `logged`
   指令，deploy-service 套用「輸出政策」(`_apply_output_policy`)：**成功時不存任何
   輸出**（完整 log 在 control_node 檔案 + `/view` 取得），**失敗時只存最後幾行**
   (`COMMAND_LOG_FAILURE_TAIL_LINES`，預設 50 行) 方便快速看錯誤。這是為了不讓大量
   ansible 輸出灌爆 Redis。

   > 注意：這個輪詢端點不只是「讀 Redis」。當 Redis 狀態還是 `RUNNING`／`KILLING`
   > 但 run 其實已經在 control_node 上結束時，它會 SSH 回去讀終止標記、**就地把狀態
   > 修正**（孤兒復原）。詳見 §3.5。

2. **直接看 control_node 上的檔** —— `/var/log/ansible-runs/<run_id>.log`，跑完仍在
   （直到 retention 視窗過期才被清掉，預設 3 天）。

### 3.2 即時 log（串流觀看）

即時觀看由 `CommandService.get_command_trace` 提供，對外是兩個端點：

- `GET /api/v1/command/execution/{id}/trace/ui?byte_offset=0&line_num=1`
  —— 增量回傳「新長出來的那幾行」，已渲染成 HTML，並回傳新的
  `next_byte_offset` / `next_line_num` 游標。
- `GET /api/v1/command/execution/{id}/view`
  —— 一個瀏覽器 HTML 殼，會自動帶著游標反覆輪詢上面的 `/trace/ui`，做出串流效果。

背後機制（`_read_remote_log`）：

```python
# SSH 進 control_node，用 byte offset 做增量 tail
size_res = await conn.run(f"stat -c %s {quoted_path}")          # 目前檔案大小
tail_res = await conn.run(f"tail -c +{byte_offset + 1} {quoted_path}")  # 只取新內容
```

設計上的考量點：

- **以 byte offset 做增量**：每次只 `tail` 出上次游標之後的位元組，前端帶著
  `next_byte_offset` 回來，不會重複抓整個檔。檔案還沒建立（剛啟動）時 `stat` 失敗，
  回傳 `(0, "")` 表示「還沒有東西」。
- **不渲染半行**：如果新內容不是以換行結尾，會把最後那段不完整的行「留著」
  (`held_back`)，游標只前進到最後一個換行，下一次輪詢再補上。避免畫面出現被截斷的半
  行。
- **大小上限保護**：超過 `COMMAND_LOG_SOFT_CAP_BYTES`（5 MB）給 `size_warning`；超
  過 `COMMAND_LOG_HARD_CAP_BYTES`（10 MB）直接 `too_large=True` 停止串流，避免把瀏覽
  器與 control_node 拖垮。
- **`too_large` 時告訴使用者「去哪看」**：瀏覽器放棄渲染了，但完整 log 還在 control_node
  上。所以 `too_large` 的回應會額外帶 `log_host` / `log_port` / `log_user` /
  `log_file_path`（全部取自 `CommandState`），前端的錯誤面板把它們組成一條可複製的
  `ssh <user>@<host> -p <port> tail -f <path>`，ops 直接貼上就能在 control_node 看完整
  log。這些欄位只在 `too_large` 時帶出，正常的增量切片不帶（保持回應精簡）。

### 3.3 為什麼有些 playbook「看得到即時串流」、有些卻一次噴完

這是 ansible 的 buffering 行為，不是腳本的問題，但會直接影響你「即時 log」的體感：

> **ansible 會把單一 task 的輸出緩衝到該 task 結束才一次吐出。** 所以對「一個 task」
> 下 `loop` 跑 30 次，會在最後一次性 dump 全部 —— 完全沒有即時感。

`clock.yml` 用的技巧是：把「印一次時間」拆成 `include_tasks: clock_tick.yml`，再對這
個 include 跑 `loop`。每個被 include 的 task 各自獨立完成、各自即時 flush，
`run-ansible.sh` 的 `tee` 就會一塊一塊寫進 log，viewer 一 tick 一 tick 地看到輸出。

> 撰寫想被「即時觀看」的 playbook 時，要讓輸出分散在多個會各自結束的 task，而不是塞
> 在單一 task 的 loop 裡。

### 3.4 跨 pod 也能讀 / kill

因為 log 只存在 control_node、而執行狀態 (`CommandState`) 存在 Redis，所以**任何一個
deploy-service pod** 都能：

- 用 `state.run_log_path` + `state.ssh_config` 重新 SSH 進 control_node 讀 log；
- 用同樣的方式重連並送 `kill -TERM -<pgid>` / `kill -KILL -<pgid>` 來中止
  （kill 的是 `run-ansible.sh` 的整個 process group；`pgid` 來自 §3.5 的握手）。

這就是為什麼 log 不寫在發起請求的那個 pod 的本機，而是寫在 control_node：狀態與 log
都「外部化」之後，多 pod 部署下的輪詢、串流、kill 才會一致。

讀 log 與 kill「跨 pod 能用」一直都成立。但**「run 能不能撐過 deploy-service 消失」**
以及**「結果由誰寫回 Redis」**這兩件事，原本是有破洞的 —— 這是 §3.5 的主題。

### 3.5 程序存活與孤兒復原（為什麼會出現 exit 141）

這節是整套 ansible 觸發鏈最反直覺、也最重要的一塊。

#### 問題一：run 的輸出綁在 SSH channel 上，channel 一斷就把 run 拖死

deploy-service 是用 `conn.create_process(...)` 在 control_node 上跑
`run-ansible.sh`，而 process 的 stdout/stderr 預設是經由那條 **SSH channel**
（`asyncssh.PIPE`）流回 deploy-service 的。死亡鏈如下：

```
docker(ansible) ──stdout──▶ tee ──┬──▶ <run_id>.log（control_node 本機檔案）
                                   └──▶ SSH channel ──▶ deploy-service
```

- 你關掉／重啟 deploy-service → SSH channel 關閉。
- `tee` 還想往它的 stdout（已關閉的 channel）寫 → 收到 **SIGPIPE** 而死。
- 現在 `docker` 的 stdout 沒有讀者了 → docker/ansible 下一次輸出時也吃 SIGPIPE 而死。
- 結束碼 = `128 + 13`（13 = SIGPIPE）= **`141`**。

> **情境**：你跑 `run_ansible_clock`（會印 30 個 tick），在 tick 1 時把 dev server
> 關掉，重啟後去看 log，發現它停在 tick 1、結尾是 `=== EXIT 141 ===`。直覺會以為
> 「docker 應該繼續跑啊」，但其實它在你關閉的瞬間就被 SIGPIPE 連鎖殺死了。

關鍵澄清：

- **`setsid` 救不了這個。** `setsid` 只給新的 session / process group（對「精準
  kill」有用），它**不會改掉繼承來的 stdout fd**。stdout 還是那條 channel。
- **跟 `killable` 完全無關。** 不管 `killable` 設 true 或 false，只要輸出還綁在 channel
  上，run 就會被 channel 斷線拖死。

#### 修法：對 `logged` 指令，把輸出從 channel 脫鉤

`CommandService._build_step_wrapper` 對 `logged` 指令改用這個 wrapper：

```sh
setsid -w sh -c 'echo $$ >&2; echo READY >&2; exec "$@" > /dev/null 2>&1 < /dev/null' _ <cmd...>
```

- `exec "$@" > /dev/null 2>&1 < /dev/null` —— run 的 stdout/stderr/stdin 全部脫離
  SSH channel。channel 斷掉時，run **毫無感覺**，繼續在 control_node 上跑完。
- **為什麼導向 `/dev/null` 而不是 log 檔？** 因為 `run-ansible.sh` 內部**已經**用
  `tee` 在寫 `<run_id>.log` 了。若 wrapper 再導向同一個檔，就會**雙重寫**。wrapper 的
  職責只有一個：**把 channel 切斷**；寫檔仍然是腳本自己的事。
- 非 `logged` 指令（如 `list_file` 的 `ls | grep`）**維持原樣**——輸出照常串回
  channel，因為這類指令短、它的輸出本身就是結果。

#### 問題二（補盲點）：脫鉤後，「啟動失敗」怎麼被發現？

把輸出導向 `/dev/null` 之後產生一個新風險：如果 `run-ansible.sh` **根本沒起來**
（路徑打錯、log 目錄不能寫），它的錯誤訊息進了 `/dev/null`，channel 也沒東西回來 ——
deploy-service 會以為「還在跑」而**永遠卡 RUNNING**。

解法是在 `exec` **之前**先用 stderr 送一段**握手**（這段仍走 channel）：

- `echo $$ >&2` —— 印出 PGID，deploy-service `readline()` 拿來日後 kill 用。
- `echo READY >&2` —— 一句「我真的進到 exec 了」。

`_execute_pipeline` 對 detached run 會**等這行 `READY`**；收不到就代表 run 在 exec
之前就死了，於是立刻 `raise CommandExecutionException`，把它判為**啟動失敗**，而不是
晾在 RUNNING。

> **情境**：有人把白名單裡的腳本路徑改錯了。送出指令 → deploy-service 連 `READY` 都
> 沒收到 → 立刻回失敗、附「Run failed to start on the control_node」。如果沒有這道握
> 手，你會看到一個空 log + 永遠轉圈的 RUNNING，完全不知道發生什麼事。

#### 問題三：結果由誰寫回 Redis？——孤兒復原

正常情況下，發起請求的那個 pod 有一個 `asyncio.Task` 等 run 結束、把 success/failed
寫回 Redis（**快路徑**）。但如果那個 pod 死了，就沒人寫 —— Redis 會卡在 RUNNING。

有了 §2.6 的終止標記，這個洞就補起來了：**輪詢端點
`get_command_execution_result` 變成第二個寫入者（慢路徑）**：

```
若 Redis 狀態 ∈ {RUNNING, KILLING} 且是 logged 指令：
    SSH 回 control_node，cat <run_id>.exit
        檔案不存在 / 讀不到     → 還在跑，回報原狀態
        EXIT 0                  → _heal_from_marker → mark_success
        EXIT 非 0               → mark_failed（帶結束碼）
```

幾個承重的安全設計：

- 修正用 `update_if(condition = 狀態 ∈ {RUNNING, KILLING})` 這個**條件式原子更新**。
  所以**快路徑或一個完成的 kill（落在終止狀態 KILLED）永遠贏 race**，慢路徑絕不會把
  已經終結的狀態蓋掉。`KILLED` / `SUCCESS` / `FAILED` 不會被「復活」。
- 慢路徑讀標記時若 SSH 失敗，**不讓輪詢變成 5xx** —— 退回回報「最後已知狀態」，下次
  輪詢再試。control_node 短暫不可達不該害整個 API 掛掉。

> **為什麼 `KILLING` 也要被復原？**（這是實測時抓到的 bug）一個 `killable:false` 的
> run，在服務關閉時被 `shutdown_gracefully` 翻成 `KILLING`，然後就**卡死在那裡**——
> 因為它根本沒有「可被 kill」的路徑去走到 `KILLED`。兩道修正：(a) `kill_command` 現在
> 在**任何狀態轉換之前**就先檢查 killable（非 killable 直接不動它，留在 RUNNING）；
> (b) 慢路徑把 `KILLING` 也納入可復原範圍，所以即使一個 kill 被「正在 kill 的 pod 死
> 掉」打斷，最終也能用標記對回真實結果。

一句話：**快路徑（pod 內的 asyncio.Task）現在只是「最佳化」，不再是唯一的結果寫入
者。** 任何 pod、任何時間、甚至整個服務重啟過，都能從 control_node 的標記把真相補回來。

### 3.6 kill 的語意：系統自動 vs 人類明確下令

`killable` 這個旗標原本同時管三種觸發，但它們風險不同。現在的語意是：

> **`killable: false` 的意思是「**系統**不准自作主張砍它」，不是「**任何人**都永遠不能
> 砍」。**

| 觸發來源 | 行為 |
|----------|------|
| **timeout**（自動，`_timeout_wrapper`） | 一律尊重 `killable`（不帶 `force`） |
| **shutdown**（自動，`shutdown_gracefully`） | 一律尊重 `killable`（不帶 `force`） |
| **使用者按 kill**，`killable: true` | 直接 kill（照舊） |
| **使用者按 kill**，`killable: false`，**沒 force** | **回 409**：`"not killable. Retry with ?force=true"` |
| **使用者按 kill**，`killable: false`，**`?force=true`** | **真的 kill**（人類 override） |

實作上：

- 端點 `POST /execution/{id}/kill` 多一個 `force: bool = False` query 參數。非 killable
  且沒 force → 回 **409**，並在訊息裡指出怎麼 override；有 force → 把 `force=True`
  傳給 `kill_command`，繞過它內部的 killable 防護、執行真正的 PGID kill。
- 自動呼叫者（timeout / shutdown）**永遠不帶 force**，所以它們對 `killable: false` 的
  尊重不變。
- `disconnects_ssh: true`（reboot）這類指令**到不了**這條路徑：它們是 fire-and-forget、
  不會留下 RUNNING 的 async 狀態，端點最上面的 `status != RUNNING` 檢查就先擋掉了。

> **為什麼要這樣分？**（實測時抓到的問題）原本端點不管 service 做了什麼，**一律回
> `accepted`**。於是你對一個 `killable:false` 的 run 送 kill，拿到「Kill request
> accepted」——但 service 其實正確地**什麼都沒做**。回應在騙人。新語意把這件事講清
> 楚：不能砍就明說 409 並給 override 的方法；要 override 就得**明確多打一個
> `?force=true`**，等於一道「你確定?」的閘，不會手滑誤砍一個刻意標成不可中斷的操作
> （例如跑到一半的 migration）。

---

## 4. 快速操作對照

完整請求範例見 `rest_client/ansible.http`。前置條件：

```bash
docker compose -f docker-compose-nodes.yml up -d --build   # nodes + control_node
cd ansible && make build                                   # 自建 image（白名單用 --no-pull）
make dev                                                    # deploy-service, port 8001
```

| 想做的事 | 怎麼做 |
|----------|--------|
| 觸發 ping 全部主機 | `POST /api/v1/command/execution`，`command_name=run_ansible_ping_all` |
| 看即時串流 | 跑 `run_ansible_clock`，瀏覽器開 `.../execution/<id>/view` |
| 輪詢最終狀態（含孤兒復原） | `GET /api/v1/command/execution/{id}` |
| 增量拉 log（自寫前端） | `GET /api/v1/command/execution/{id}/trace/ui?byte_offset=&line_num=` |
| 中止執行（可殺的指令） | `POST /api/v1/command/execution/{id}/kill` |
| 強制中止不可殺的指令（人類 override） | `POST /api/v1/command/execution/{id}/kill?force=true` |
| 驗證失敗會正確回報 | 跑 `run_ansible_fail`（ping ok 後跑 `/bin/false`） |

---

## 5. 一句話總結每個設計決策

- **跳板機 + DooD**：deploy-service 不碰 ansible，只 SSH 跑一條白名單腳本；ansible 封
  裝在可重建的 image。
- **argv 陣列、不 `eval`**：承重的防注入保證。
- **每次 clone inventory、每次 pull image**：杜絕過期 artifact。
- **secret 走 `--secret-path` source（token 用 git env-config、vault 自動產生 client）**：
  secret 只活在 control_node 本機、不進 git／不過 deploy-service；token 不進 argv／URL／
  `.git/config`，vault 密碼靠 value-less `-e` 進容器、`ANSIBLE_VAULT_PASSWORD_FILE` 預設
  好讓 ansible 自動解密（normal 與 `--debug` 皆然）。
- **clone 路徑 host 一致**：DooD 下 `-v` 對 host 解析，否則掛載對不上。
- **`${PIPESTATUS[0]}` + `tee`**：image 不寫 log，靠 tee 寫檔；取 pipeline 左端的碼才
  能讓 ansible 的失敗穿透回來（pipeline 後還要寫終止標記，故用 `set +e` 取代 pipefail）。
- **終止標記（`=== EXIT` + `<id>.exit` sidecar）**：log 檔即真相來源，支撐孤兒復原。
- **`logged` 指令把輸出導 `/dev/null` 脫離 channel + `READY` 握手**：run 能撐過
  deploy-service 消失（解 exit 141），同時偵測得到啟動失敗，不卡 RUNNING。
- **輪詢端點是第二寫入者（慢路徑）**：用 `update_if` 條件式更新從標記復原 RUNNING／
  KILLING，且 kill／快路徑永遠贏 race；SSH 失敗退回最後已知狀態、不 5xx。
- **log 寫 control_node、state 寫 Redis**：跨 pod 都能讀／串流／kill。
- **kill 區分「系統自動」與「人類 force」**：`killable:false` 擋自動 kill；人類可用
  `?force=true` 明確 override，API 不再用 `accepted` 騙人。
- **byte-offset 增量 tail + 留半行 + 大小上限**：穩定的即時觀看體驗。
