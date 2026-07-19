# Logged 指令前置階段失敗的可觀測性

**日期:** 2026-07-19
**分支:** `feat/logged-command-failure-observability`

## 背景

`run_ansible` 是一個 `logged` 類型的 SSH 指令。logged 指令的 SSH 包裝器
(`command_executor.py` `_build_step_wrapper`)執行的是:

```sh
echo $$ >&2; echo READY >&2; exec "$@" > /dev/null 2>&1 < /dev/null
```

腳本的 stdout/stderr 被導向 `/dev/null`,故意與 SSH channel 切斷,好讓長時間的
ansible 任務在 deploy-service 的 pod 掛掉時仍能存活(輸出不再流經 SSH channel,
關閉 channel 不會 SIGPIPE 把程序連鎖弄死)。輸出改由 `run-ansible.sh` 自行
`tee` 到 control_node 上的 `{run_id}.log`,再由 `/view`(trace)端點與 heal
邏輯另外讀取。

## 問題

當 `run-ansible.sh` 在 **ansible 執行前的階段**(clone inventory、載入 secret、
inventory 檔案檢查等)失敗時,錯誤訊息哪裡都沒留下:

1. **API 的 `output` 是 `null`** —— SSH channel 被導向 `/dev/null`,
   `_collect_output` 收到空字串,`_apply_output_policy` 走到 `if not output:
   return None`。
2. **control_node 的 `{run_id}.log` 也沒有錯誤** —— `main()` 目前只有
   `run_normal` 內的 `docker ... 2>&1 | tee "$LOG_FILE"` 這一步會 tee,
   `clone_inventory` 等前置階段的輸出走的是腳本自己的 stdout/stderr(→
   `/dev/null`)。
3. **若 pod 中途死掉,連 `.exit` marker 都沒有** —— `.exit` marker 只在
   `run_normal` 尾端寫。前置階段失敗時沒有 marker,`_heal_from_marker` 會把這個
   run 永遠當成 RUNNING,直到 Redis TTL 過期。

實際案例:一個 clone 失敗(`fatal: could not read Username for
'https://gitlab.com'`,exit 128)的 run,API 回應是 `status: failed`、
`exit_status: 128`、`output: null`。使用者手動在 control_node 執行才看得到錯誤,
因為終端機沒有把輸出導到 `/dev/null`。

## 目標

讓 logged 指令在**任何階段**失敗時:
- 完整輸出(含 clone / secret / inventory 階段)都進 control_node 的 `{id}.log`。
- API 回應的 `output` 直接帶回 log 檔尾端最後 N 行(N =
  `COMMAND_LOG_FAILURE_TAIL_LINES`,預設 50),使用者不必另外開 `/view`。
- 前置階段失敗也寫 `.exit` marker,heal 路徑不會把失敗的 run 誤判成永遠 RUNNING。

## 非目標

- 不改變 logged 指令「輸出與 SSH channel 切斷以存活 pod 死亡」的核心設計。
- 不改變非 logged 指令的行為(它們的 output 就是 SSH channel 收到的完整內容)。
- 不把 `parse_args` / `load_secrets` / `resolve_log_file` 這幾個**最前置**步驟的
  輸出納入 log 檔(見「設計決策」)。

## 設計

### A. `run-ansible.sh`

**A1. `main()` 全程包一層 tee。**
在 `resolve_log_file` 之後(`LOG_FILE` 路徑此時已確定、`LOG_DIR` 已 `mkdir`),把
後續所有階段包進單一 tee:

```sh
main() {
  parse_args "$@"
  load_secrets
  ensure_vault_client
  resolve_inventory_repo
  resolve_token
  resolve_log_file          # 設定 LOG_FILE + mkdir LOG_DIR;此後才能 tee
  run_stages 2>&1 | tee "$LOG_FILE"
  exit "${PIPESTATUS[0]}"    # run_stages 的 exit code,不是 tee 的
}
```

`run_stages` 內含 `clone_inventory` → `build_cmd_args` →
`build_docker_base_args` → 依 `MODE` 分派 `run_debug` / `run_dry_run` /
`run_normal`。

**A2. EXIT trap 寫 `=== EXIT N ===` 與 `.exit` marker。**
在 `resolve_log_file` 之後、`run_stages` 之前安裝 EXIT trap,讓**任何階段**離開
(含 `clone_inventory` 失敗、`set -e` 觸發的中止)都:
- 把 `=== EXIT N ===` 附加到 log 檔(N 為離開碼);
- 若有 `RUN_ID`,把 N 寫入 `{RUN_ID}.exit` marker(沿用現有的
  `printf ... > tmp && mv -f` 原子寫法)。

取代目前只在 `run_normal` 尾端寫 marker 的邏輯。trap 需 idempotent —— 已由
`run_normal` 正常寫過就不重覆寫(用一個 `_FINALIZED` 旗標守衛,或讓 trap 成為唯一
寫入點、`run_normal` 不再自寫)。**採後者**:marker 與 `=== EXIT ===` 的寫入集中
在 trap,`run_normal` 移除自己那段,單一寫入點更不易漏。

**A3. `run_normal` 不再自行 tee。**
`run_normal` 內的 `docker run ... 2>&1 | tee "$LOG_FILE"` 改為
`docker run ... 2>&1`(輸出已被 A1 的外層 tee 接手)。仍以 `${PIPESTATUS[0]}`
無誤地捕捉 docker 的離開碼,並以該碼作為 `run_stages` 的離開碼傳出,讓 A2 的 trap
寫出正確的 N。

**A4. `--debug` / `--dry-run` 模式。**
這兩個模式的輸出同樣進外層 tee 沒有問題;DRYRUN 的早退(`resolve_log_file` 內
`exit 0`)發生在 tee 尚未接手前,維持原行為。`--debug` 啟動 idle 容器供人工
`docker exec`,其 `docker exec -it` 的互動輸出不受影響(那是另一個 TTY)。

### B. deploy-service —— 新增共用 helper `_read_log_tail`

在 `SshSupport`(`command_ssh.py`)新增以下簽章(位置理由見 C 節 —— executor 與
StateHelpers 都持有 `self._ssh`,放共用 SSH 層兩者皆可呼叫):

```python
async def _read_log_tail(self, state: CommandState, n: int) -> Optional[str]:
    """SSH 連回 control_node 讀 {id}.log 的最後 n 行。

    路徑為伺服器產生,仍以 shlex.quote 保護。SSH 失敗回傳 None
    (不讓 poll 變成 5xx);log 檔不存在或為空回傳 None。
    """
```

實作:`tail -n {n} {shlex.quote(run_log_path)}`,透過
`_ssh._connect_to_control_node(state)` 開連線。SSH 例外
(`UpstreamTimeout/Unavailable`)吞掉並回 `None`;`tail` 非 0(檔案不存在)回
`None`;空字串回 `None`。

`n` 從 `settings.COMMAND_LOG_FAILURE_TAIL_LINES` 取。

### C. Fast-path 回填(`command_executor.py`)

**helper 位置決定:** `_read_log_tail` 放在 `SshSupport`(不是 B 節暫寫的
`StateHelpers`)—— 它只需要 SSH 能力,`CommandExecutor` 已持有 `self._ssh`、
`StateHelpers` 已持有 `self._ssh`,兩者都能共用,避免 executor 反向依賴
StateHelpers。B 節的簽章與行為不變,只是實際掛在 `SshSupport` 上。

**回填點:** `_handle_async_execution` 的 `_execution_task` 內,現有流程是:

```python
returncode, output = await self._collect_output(final_process)
success = returncode == 0
stored_output = self._apply_output_policy(logged, success, output)
```

改為:當 `logged and not success and not output`(logged 指令、失敗、且 SSH
channel 的 output 為空)時,先以 log tail 取代空 output:

```python
if context.cmd_config.logged and not success and not output:
    output = await self._ssh._read_log_tail(
        state, settings.COMMAND_LOG_FAILURE_TAIL_LINES
    ) or ""
stored_output = self._apply_output_policy(logged, success, output)
```

`_apply_output_policy` **不改** —— 它對非空 output 取尾 N 行的邏輯仍正確,收到已是
tail 的 output 時再取一次尾 N 行是無害的 no-op。條件限定「output 空才回填」,
非 logged 指令與「logged 但 channel 有輸出」的既有行為完全不動。

### D. Heal-path 回填(`command_state_helpers.py`)

`_heal_from_marker` 在 `code != 0`(失敗)時,`mark_failed` 前呼叫
`_read_log_tail` 取 log 尾 N 行,帶入 `output`:

```python
tail = await self._read_log_tail(state, settings.COMMAND_LOG_FAILURE_TAIL_LINES)
async def updater(s):
    if success:
        s.mark_success(code, "")
    else:
        s.mark_failed(f"Recovered from control_node marker: exit {code}.",
                      exit_code=code, output=tail)
```

`_read_log_tail` 已在同一次 heal 的 SSH 往返附近;可考慮合併連線,但正確性優先,先
各自開連線,如成為效能問題再優化(YAGNI)。

## 資料流(修改後)

```
run-ansible.sh clone 失敗 (exit 128)
   │  外層 tee → {id}.log 收到 "fatal: could not read Username..."
   │  EXIT trap → {id}.log 附加 "=== EXIT 128 ==="、寫 {id}.exit = 128
   ▼
deploy-service fast-path:
   returncode=128, channel output="" (被 /dev/null 切斷)
   → logged 且失敗且 output 空 → _read_log_tail(state, 50)
   → stored_output = log 尾 50 行
   ▼
API output: "fatal: could not read Username for 'https://gitlab.com'..."

(若 pod 中途死亡改走 heal-path,靠 {id}.exit=128 恢復,同樣以
 _read_log_tail 帶入 output)
```

## 錯誤處理

- `_read_log_tail` 的 SSH 失敗一律吞成 `None` —— 回填失敗不得讓 poll 變 5xx,
  維持「control_node 短暫不可用不影響回報」的既有原則。
- log 檔不存在(例如 `resolve_log_file` 之前就掛)→ `tail` 非 0 → `None`,
  output 維持 null(這類最前置失敗罕見且 exit code 仍回報)。
- trap 的 idempotency:marker/`=== EXIT ===` 集中在 trap 單一寫入點,消除重覆
  寫入疑慮。

## 測試

**run-ansible.sh(bash 單元測試,沿用現有 DRYRUN / fake-docker hook)**
- clone 失敗時:`{id}.log` 含 clone 的錯誤文字;`{id}.exit` 存在且內容為 clone 的
  離開碼;`=== EXIT N ===` 出現在 log 尾。
- 正常成功:`{id}.log` 含 ansible 輸出;`{id}.exit` = 0;marker 只寫一次
  (不因 trap 與 run_normal 雙寫而重覆)。
- DRYRUN 早退:行為不變(tee 尚未接手)。

**deploy-service(pytest unit,`asyncio_mode=auto`)**
- `_read_log_tail`:mock SSH conn,驗證回傳尾 N 行;SSH 例外 → `None`;
  `tail` 非 0 → `None`;空輸出 → `None`。
- Fast-path:mock `_read_log_tail`,logged 指令 exit≠0 且 channel output 空 →
  stored output = tail;output 非空 → 維持既有(不覆蓋);非 logged → 不呼叫
  `_read_log_tail`。
- Heal-path:`_heal_from_marker` code≠0 → `mark_failed` 帶入 tail;code=0 →
  不帶;`_read_log_tail` 回 None → output 為 None 不拋錯。

## 設計決策

**tee 起點在 `resolve_log_file` 之後,而非 `main()` 最開頭。**
`LOG_FILE` 路徑由 `resolve_log_file` 決定並 `mkdir -p "$LOG_DIR"`,在它之前無從
tee。`parse_args` / `load_secrets` / `resolve_log_file` 這幾步的失敗很罕見且通常是
參數/檔案權限問題,API 層仍能取得 exit code;為此把 tee 起點往前推需要「連
LOG_FILE 都算不出來時怎麼辦」的額外處理,成本大於效益。使用者已確認接受此切法。

**`_read_log_tail` 放在 `SshSupport`。**
它只需要 SSH 能力,放共用層讓 `CommandExecutor`(fast-path)與 `StateHelpers`
(heal-path)都能呼叫,避免 executor 反向依賴 StateHelpers。

**marker/`=== EXIT ===` 集中於 EXIT trap 單一寫入點。**
消除「trap 與 run_normal 雙寫」的重覆與競態,並自動覆蓋 clone 失敗等早退路徑。
