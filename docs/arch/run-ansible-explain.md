# 腳本核心防禦

```bash
set -euo pipefail
```

- `-e` (errexit): 只要有指令失敗（回傳非 0 值），腳本立即停止，不會繼續執行危險的後續動作。
- `-u` (nounset): 只要用到沒定義過的變數，直接報錯停止。防止因打錯變數名稱導致邏輯錯誤。
- `-o` pipefail: 在管道（pipe，例如 A | B）中，只要 A 失敗，整個管道的結果就會被視為失敗，而不僅僅是看最後一個指令 B。

# 參數解析邏輯 (The while loop)

```bash
while [[ $# -gt 0 ]]; do
  case "$1" in
    --playbook)       PLAYBOOK="$2"; shift 2 ;;
    --inventory)      INVENTORY="$2"; shift 2 ;;
    --inventory-ref)  INVENTORY_REF="$2"; shift 2 ;;
    --tags)           TAGS="$2"; shift 2 ;;
    --limit)          LIMIT="$2"; shift 2 ;;
    --extra-vars)     EXTRA_VARS="$2"; shift 2 ;;
    --image)          IMAGE="$2"; shift 2 ;;
    --no-pull)        PULL=0; shift ;;
    --log-dir)        LOG_DIR="$2"; shift 2 ;;
    --run-id)         RUN_ID="$2"; shift 2 ;;
    --log-retention-days) LOG_RETENTION_DAYS="$2"; shift 2 ;;
    --ssh-key)        SSH_KEY="$2"; shift 2 ;;
    -h|--help)        usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done
```

- `$#` 代表參數總數，`-gt 0` 表示只要還有參數沒處理完。
- `shift 2`: 把處理過的兩個參數（例如 `--playbook` 和 `ping.yml`）丟掉，讓原本的 `$3` 變成新的 `$1`。這是一個非常優雅的輪詢（Iteration）寫法。
- `*) echo "Unknown argument: $1" >&2; usage; exit 2 ;;`
  - case 語句中，`*` 代表「任何字元」。當前面的選項（如 `--playbook`、`--inventory` 等）都沒有匹配到時，腳本就會掉進這個 `*)` 分支。
  - `echo "Unknown argument: $1" >&2` —— 錯誤輸出
  - `>&2` 把這些錯誤訊息導向到 標準錯誤（Stderr）。
- `usage; exit 2 ;;` —— 終止與結束
  - `usage`: 呼叫腳本開頭定義好的說明函數，告訴使用者「你用錯了，正確用法是這樣」。
  - `exit 2`: 終止腳本並回傳退出代碼 2。0 代表成功，非 0 代表失敗。2 通常被慣例用來表示「語法錯誤」或「參數錯誤」。

# 處理「短暫性」資源 (Trap & Mktemp)

這段代碼確保了腳本執行完後，環境是乾淨的，不會留下垃圾。

```bash
CLONE_DIR="$(mktemp -d "$CLONE_PARENT/ansible-inventory.XXXXXX")"
cleanup() { rm -rf "$CLONE_DIR"; }
trap cleanup EXIT
```

- `mktemp -d`: 創建一個唯一的臨時目錄，避免多個腳本同時執行時發生檔案衝突。
  - 那個 `XXXXXX`（六個連續的 X）其實是 mktemp 指令的一個「特殊佔位符（Template）」。當你執行 mktemp 時，它會自動將這六個 X 隨機替換為字母和數字的組合，目的是為了確保你創建的目錄名稱是絕對唯一（Unique）的。
  - mktemp 會在執行時動態產生類似 `ansible-inventory.a7b9c2`、`ansible-inventory.1f8d3k` 這樣的名字。因為這些名字是隨機生成的，它們「撞名」的機率微乎其微。
- `trap ... EXIT`: 這是「保證執行」機制。無論腳本是執行成功、執行失敗、甚至被使用者強制中斷（Ctrl+C），cleanup 函數都會執行，刪除那個臨時目錄。

# 陣列參數組裝 (CMD_ARGS)

這是刻意使用的防 Injection（注入攻擊）寫法：

```bash
CMD_ARGS=(ansible-playbook -i "/inventory/$INVENTORY" "/playbooks/$PLAYBOOK")
[[ -n "$TAGS"       ]] && CMD_ARGS+=(--tags "$TAGS")
[[ -n "$LIMIT"      ]] && CMD_ARGS+=(--limit "$LIMIT")
[[ -n "$EXTRA_VARS" ]] && CMD_ARGS+=(--extra-vars "$EXTRA_VARS")
```

這裡沒有使用「字串拼裝」，而是使用 Bash Array (陣列)。

- 例子說明：假設你用了 `--tags "deploy,web"`。
- 如果是字串拼裝，很可能產生 `ansible-playbook --tags deploy,web`，若這時使用者惡意輸入 `--tags "; rm -rf / ;"`，你的伺服器就完了。
- 使用 Array 寫法，Bash 會確保每個參數都被當作獨立的「單元」傳遞給 Docker，Docker 執行時會把它當作單純的字串，這叫 Argument Separation，是資安防護的核心。
- 舉例來說，當你執行 `${CMD_ARGS[@]}` 時，Bash 內部會這樣轉換：
    - 原始陣列內容：
      - `CMD_ARGS[0] = ansible-playbook`
      - `CMD_ARGS[1] = --tags`
      - `CMD_ARGS[2] = a; rm -rf /` (注意：這整串被當作一個單元)
    - 執行時的行為：Bash 在把參數傳給 Docker（或任何執行檔）時，它會告訴系統：「這裡有三個參數」。
      - 系統會直接把 `a; rm -rf /` 這個「字串」原封不動地丟給 Ansible。
      - Ansible 收到的參數會是：`--tags` 對應的值是 `a; rm -rf /`。
      - Ansible 當然處理不了這個標籤，所以它會報錯（例如 tag not found），但它絕對不會去執行那個 `rm -rf`，因為那個分號 `;` 對 Docker 來說只是內容的一部分，而不是 Shell 的命令分隔符。

```bash
# 模擬一個惡意參數
MALICIOUS="a; echo HACKED"

# --- 狀況 A：字串拼接 (不安全) ---
# 會輸出 HACKED，因為分號被當成了指令的分隔符
eval echo "ansible-playbook --tags $MALICIOUS"

# --- 狀況 B：陣列傳遞 (安全) ---
# 會完整印出分號和 echo，說明它被當作單純的字串，不會觸發指令
ARGS=(ansible-playbook --tags "$MALICIOUS")
echo "${ARGS[@]}"
```

# 處理 Docker 中的指令退出碼 (PIPESTATUS)

```bash
set +e
docker run ... | tee "$LOG_FILE"
RUN_EXIT="${PIPESTATUS[0]}"
set -e
```

- 我們用了 `tee` 把輸出同時導向螢幕和檔案。
- 如果直接用 `docker run`，`$?` 拿到的會是 `tee` 的結束代碼（通常是 0），而不是 `docker run`（Ansible 實際執行結果）的代碼。
- `${PIPESTATUS[0]}`：這是 Bash 的內建陣列，存著上一條指令中每個 pipe 步驟的退出狀態。索引 0 正好就是 docker run 的結果。這樣我們就能精確知道 Ansible 到底是成功還是失敗。