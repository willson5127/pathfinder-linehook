# Pathfinder LINE Webhook

獨立 FastAPI 服務，不依賴看板或行情資料庫。接收 LINE 事件、辨識聊天室，以及將 crawler 的掃描訊息推送至明確設定的收件對象。所有真實憑證與 ID 都先留白。

## 本機啟動

在本目錄執行：

```powershell
Copy-Item .env.example .env
uv sync
uv run uvicorn app:app --host 127.0.0.1 --port 8080
```

`GET /healthz` 提供存活檢查。設定留白時可以啟動，但相關收發／管理 API 回傳 503。修改環境變數後需重啟服務。

## 稍後補上的設定

| 設定 | 用途／取得方式 |
|---|---|
| `LINE_CHANNEL_SECRET` | 原本 Messaging API Channel 的 Basic settings → Channel secret；用來驗證 LINE 簽章 |
| `LINE_CHANNEL_ACCESS_TOKEN` | 同一 Channel 的 Messaging API → Channel access token；用來呼叫 LINE API |
| `LINE_OWNER_USER_ID` | 你的個人 LINE userId。私訊 Bot `/id` 後抄入；目前僅保存與顯示主帳號身分，不提供 LINE 聊天指令的管理權限 |
| `LINE_TARGET_IDS` | 要接收策略通知的 userId／groupId／roomId，多個以逗號分隔。留白就不推播 |
| `ADMIN_API_TOKEN` | 自行產生的隨機密碼，只供你查管理 API；不是 LINE Token |
| `PUBLISH_API_TOKEN` | 另一組隨機密碼，供 crawler 呼叫通知 API |
| `CHAT_DB_PATH` | SQLite 事件 ID／聊天室 ID 儲存位置，預設 `data/chats.sqlite3` |

個人帳號的登入密碼、公開 LINE ID、Bot 的 `@...` ID，均不能代替上述 Channel 憑證或收件 userId。`/admin/bot` 的 `userId` 是 **Bot 自己**，不是你的主帳號 ID。程式不會推測或自動指派主帳號。

產生兩組本機 Token：執行兩次 `uv run python -c "import secrets; print(secrets.token_urlsafe(32))"`，分別填入 ADMIN 與 PUBLISH 設定。不要提交 `.env`，Fly.io 使用 Secrets，不將憑證寫入映像。

## 找回主帳號與聊天室代碼

1. 補好 Channel Secret、Access Token 與兩組 API Token，部署 HTTPS 服務。
2. 在 LINE Developers 的同一個 Channel 設定 Webhook URL：`https://你的網域/webhook`，按 Verify 並啟用 Use webhook。空的 `events` 驗證請求可以通過。
3. 用你的個人 LINE 帳號私訊 Bot `/id` 或 `/whoami`，Bot 會回覆你的 userId。
4. 要通知群組時，開啟 Channel 的 Allow bot to join group chats，把 Bot 邀入目標群組，在群組傳 `/id`，取得該群的 groupId。LINE 沒有提供 userId 時，不會自行猜測。
5. 你也可以透過受保護的 `/admin/chats` 查已出現的聊天室與發話使用者 ID。這裡列出的是所有驗證過的來源，不代表全部獲准收通知。
6. 將確認過的主帳號填入 `LINE_OWNER_USER_ID`、收件對象填入 `LINE_TARGET_IDS`，重啟服務。

如果 Access Token 尚未補入，只要 Channel Secret 正確，Webhook 仍會記錄 ID，但不能回覆 `/id`；可以用管理 API 查詢。不保存一般訊息內容、reply token 或完整事件。離群／封鎖事件會將聊天室標記為停用，避免繼續推送。

管理 API（PowerShell）：

```powershell
$adminHeaders = @{ Authorization = "Bearer $env:ADMIN_API_TOKEN" }
Invoke-RestMethod https://你的網域/admin/status -Headers $adminHeaders
Invoke-RestMethod https://你的網域/admin/chats -Headers $adminHeaders
Invoke-RestMethod https://你的網域/admin/bot -Headers $adminHeaders
```

以上範例的環境變數需自行設定；`.env` 僅由 Python 服務讀取，不會自動載入 PowerShell。

## crawler 串接

保留 `POST /publishmessage/` 的 form `msg` 欄位，也接受 JSON `{"msg":"訊息內容"}`。發送需要 `Authorization: Bearer <PUBLISH_API_TOKEN>`。HTTP 200 表示 LINE API 接受請求，不代表使用者已讀或一定送達。

現有 `pathfinder-crawler/proc/strategy_scanner/webhook.py` 已加入可選設定：

```dotenv
LINE_WEBHOOK_URL=https://你的網域/publishmessage/
LINE_WEBHOOK_API_TOKEN=與服務的PUBLISH_API_TOKEN相同
```

設定在 crawler 的環境或其 `.env`，由既有 `load_dotenv()` 載入。不設定時維持舊 URL 與呼叫方式；切換新服務時必須補上 Token。請求裡的 `to` 等收件欄位不會覆蓋服務設定。

crawler 保留按配方分段的排版。Webhook 會把超長文字再切成每則最多 5000 個 UTF-16 code units，優先沿換行拆段；單行超長則按 Unicode 字元切開，不會截掉內容或拆開 Emoji 的 UTF-16 代理對。組合 Emoji（例如 ZWJ 序列）仍可能分在不同訊息；不額外插入段號或標題，全部段落串回可還原原文。

每次 LINE push 請求最多放 5 則，超過則依序分批，每個設定的收件者都收到完整段落。成功回應保留 `sent_count`（完成的收件者數），另提供 `parts_per_target`（每個收件者的段數）與 `sent_message_count`（LINE API 已確認接受的文字段落總數）。請求原本的 1 MiB 大小上限仍保留。

部分失敗會停止後續批次並回報上述進度，加上從 0 起算的 `failed_target_index`、`failed_batch_start`。沒有自動重送，避免部分成功後重複發送；連線逾時的批次也可能已被 LINE 接受，請先核對再人工處理。事件 ID 去重保留七天；目前設計給單一程序／單一 Machine，並非多副本的工作佇列。

## Fly.io 部署範本

`fly.toml.example` 是範本，尚未部署，App 名稱需自行確認。舊網址拼字是 `stockpathfider.fly.dev`，不要自行改成不同拼字。

```powershell
Copy-Item fly.toml.example fly.toml
# 編輯 fly.toml 的 app 名稱，再登入你的 Fly.io 帳號。
fly auth login
fly volumes create linehook_data --region nrt --size 1 -a 你的App名稱
# 將 LINE 憑證與兩組 API Token 加入此 App 的 Fly Secrets。
# LINE_OWNER_USER_ID / LINE_TARGET_IDS 也在確認後加入 Secrets。
fly deploy --ha=false -a 你的App名稱
```

保留單一 Machine，使用同區域 Volume 保存 SQLite。若舊 App 已有 Machine／Volume，部署前先核對既有設定，避免另建錯誤掛載或多副本。Volume 不會自動成為備份，需另外安排備份。更新現有 App 之前，先保留其原有映像與設定。

## 測試

```powershell
uv run python -m unittest discover -s tests -v
```

測試以模擬 LINE API 執行，不發送真實訊息；涵蓋簽章驗證、ID 擷取、去重、API 權限、收件限制、空白設定、離群、長度／大小限制與上游錯誤。

官方參考：[接收 Webhook](https://developers.line.biz/en/docs/messaging-api/receiving-messages/)、[驗證簽章](https://developers.line.biz/en/docs/messaging-api/verify-webhook-signature/)、[Messaging API](https://developers.line.biz/en/reference/messaging-api/)。
