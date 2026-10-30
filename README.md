# jrfank-img

jrfank.cc 的照片網頁檔。EdgeOne Pages 國際站（獨立專案）連這個倉庫自動部署成 **`https://pic.jrfank.cc`**。網站倉庫是 `JR-Fank/jrfank-site`。

## 內容

- `<系列>/<照片 id>-<檔>.<版本>.webp`：長邊 600 / 1200 / 1800 / 2400 / 4000 五檔。原圖不夠大就停在原圖尺寸（不放大），更大的檔不做。
- `reviews/<文章 slug>/<照片 id>-<檔>.<版本>.webp`：評測文章的圖片（照片 id ＝`<文章 slug>-<序號>`，序號 0 是封面），配方完全一樣。只接受三段路徑；一般系列是兩段 `<系列>/<id>`，**系列不能叫 `reviews`**（`webimg.py` 和 Actions 都會擋）。截圖、小圖同樣不放大（例如 700 px 寬的圖只有 600 和 700 兩檔）。
  - **版本** = `sha256(原圖 SHA-256 + 出圖配方)` 前 10 碼。換原圖或改配方，網址就變，所以 `.webp` 可以設一年的 `immutable` 快取。同一張原圖、同一個配方重出，網址不變。
- 一律 sRGB（有內嵌描述檔的，例如 Display P3，轉成 sRGB），去掉全部 metadata（EXIF、GPS、XMP、ICC）。HEIC 若標成 Rec. ITU-R BT.2100 PQ/HLG，`webimg.py` 會先用 macOS ColorSync 轉成 sRGB 再交給 ImageMagick；一般 HEIC 和 Display P3 繼續走 ImageMagick 的既有路徑。
- `manifest.json`：照片**技術資料的唯一來源**（`webimg.py` 產生，不要手改）。每張：
  - `sizes`：各檔的檔名、實際寬高、位元組數、SHA-256
  - `color`（整張平均色）、`blur`（長邊 16px 的 WebP data URI，網站放大模糊當占位）
  - `exif`（相機、鏡頭、焦距、光圈、快門、ISO、日期；讀不到的沒有）、`original`（原圖寬高）、`lowRes`（原圖長邊不到 1600；只對新出圖的照片更新，後台改看 `original` 尺寸自己判斷）
  - `source`、`sourceSha256`（原圖檔名與雜湊）
  - `transparent: true`：原圖有透明背景（只有這種才有這個欄位），網頁檔保留透明，網站不在它底下墊占位色
  - `video`（只有影片的條目）／`live`（Live Photo）：`duration`、`width`、`height`、`audio`、`files`（各檔 `file`、`bytes`、`sha256`）；`live` 另有 `source`、`sourceSha256`（影片原檔）。見下面「影片與 Live Photo」
  - 文章圖放在另一區 **`reviews`**（鍵 `reviews/<文章>/<照片 id>`，欄位一樣）；`photos` 區只有作品照片。後台只讀 `photos`，所以文章圖不會出現在後台的「未分配」。
- `exif-overrides.json`：EXIF 讀錯時改這裡（值寫 `null` = 刪掉那個欄位），改完 `build`（圖檔不會重做）。
- `edgeone.json`：全部回 `Access-Control-Allow-Origin: *`（抽卡的 canvas 網點圖要讀像素）；`.webp`、`.mp4` 快取一年 `immutable`，其他（manifest 等）5 分鐘。

**編輯資料**（系列、章節、說明、精選、隱藏、相框、底片欄位）不在這裡，在網站倉庫的 `content/series/*.json`。網站用 `pnpm sync:photos` 把這裡的 `manifest.json` 原樣複製成 `content/photos.json`（釘住網站用的版本）。

**原圖不在這裡**：
- 正本：Cloudflare R2 私有 bucket `jrfank-originals`，鍵是 `<系列>/<照片 id>.<副檔名>`（文章圖 `reviews/<文章>/<照片 id>.<副檔名>`），另有來源紀錄 `SOURCES.md`。
- 本機快取：`~/Code/jrfank-site-assets/photo-originals/`（同樣的結構）、文章圖 `~/Code/jrfank-site-assets/review-originals/<文章>/`（少一層 `reviews/`，另有 `SOURCES.json` 下載紀錄）；都不進任何倉庫。本機要出文章圖，把 `review-originals` 放到原圖目錄的 `reviews/` 底下。

## 出圖配方

| 項目 | 設定 |
| --- | --- |
| 縮放 | ImageMagick 7 `-filter Lanczos -resize NxN>`（`>` = 不放大） |
| 銳化 | 縮小後 `-unsharp 0x0.6+0.5+0.008`（輕微，200% 下看沒有光暈） |
| 色彩 | 有 ICC 的 `-profile "/System/Library/ColorSync/Profiles/sRGB Profile.icc"`，再 `-colorspace sRGB`；HEIC 的 Rec. 2100 PQ/HLG 先用 macOS ColorSync `sips -m "/System/Library/ColorSync/Profiles/sRGB Profile.icc" -s format png` 轉成暫存 sRGB PNG；沒有 profile 的一般輸入當作 sRGB |
| 編碼 | WebP `quality 88`、`method 6`、`use-sharp-yuv`（紅色霓虹、的士紅邊緣的色度比較乾淨） |
| 占位 | 最小一檔 `-scale 1x1!` 取平均色；`-resize 16x16>` quality 50 做預覽 |
| 透明 | 有 alpha 而且不是全不透明：保留透明（WebP alpha 無損），只銳化顏色（`-channel RGB`），占位先鋪白底再算；有 alpha 但全不透明：`-alpha off`。沒有 alpha 的原圖指令和加這條之前完全一樣（hk-1、hk-7 強制重出逐位元組相同），所以 `RECIPE` 沒改 |

改了任何一項，要改 `tools/webimg.py` 的 `RECIPE` 字串，全部照片的版本跟著變。

### HEIC 與 HDR

`webimg.py` 只在副檔名是 `.heic`／`.heif` 時查詢 macOS `sips -g profile`。profile 含 `2100`、`PQ` 或 `HLG` 才會走 ColorSync 的 sRGB 轉檔；Display P3 只查詢 profile，實際轉換仍由 ImageMagick 的 ICC `-profile` 完成，普通 JPEG、PNG、WebP 和一般 HEIC 不會走 `sips` 轉檔。ImageMagick 回報 `colorspace=sRGB` 不能證明 PQ 輸入已經完成 tone/color conversion。

ColorSync 產生的 PNG 只存在單張照片的暫存目錄，最後的 WebP 仍由同一套 Lanczos、銳化、WebP 編碼及 `-strip` 流程產生；manifest 的 `colorConverted` 會在 P3 或 HDR 真的經過轉換時為 `true`。本機需要 macOS 內建的 `sips`、ImageMagick 7、可讀 HEIC 的 `libheif` coder，以及 `/System/Library/ColorSync/Profiles/sRGB Profile.icc`；GitHub Actions 也是 macOS runner。

方向尺寸取和出圖相同的 ImageMagick `-auto-orient` 結果，不再只看 EXIF Orientation 數字手動交換寬高。部分 HEIC decoder 已經把像素轉正卻保留 `EXIF:Orientation=6`；直接交換會把 manifest 寫成錯的橫幅比例，和實際 WebP 像素不一致。

這個 HDR 分支已納入 `RECIPE` v3。既有 v2 條目如果原圖 SHA-256 沒變，而且原檔是 JPEG、PNG、TIFF 或 WebP，`build`（包括 `--reuse-manifest` 的逐條驗證）會把它視為已是最新：這些格式不走新增的 HDR 分支，沿用既有版本和網址。HEIC／HEIF 不沿用 v2，原圖 SHA-256 改變也照常以目前配方重做；`--force` 仍會強制重做。

## 影片與 Live Photo

原檔鍵一樣是 `<系列>/<id>.<副檔名>`，影片副檔名 `mov`、`mp4`、`m4v`（不分大小寫）：

- **Live Photo**：同一個 id 同時有圖片原檔和影片原檔（例如 `hk/hk-20.heic` + `hk/hk-20.mov`）。靜態圖照舊出 WebP（版本、檔名和只有圖片時完全一樣），影片是動態部分，manifest 加 `live` 欄位。聲音一律去掉。
- **影片**：只有影片原檔。第一格畫面（轉正、HDR 轉 SDR）當封面，走同一套 WebP 五檔；manifest 加 `video` 欄位，聲音保留。`exif` 只從影片讀相機型號和拍攝日期（`com.apple.quicktime.creationdate`，沒有才用 UTC 的 `creation_time`），`exif-overrides.json` 照樣可以改。

`tools/webvid.py` 最多出四檔 MP4：`<id>-1280.<版本>.mp4`、`<id>-1920.<版本>.mp4`、`<id>-2560.<版本>.mp4`、`<id>-3840.<版本>.mp4`。1280／1920 沿用既有規則（原片較小時第一檔停在原片尺寸）；2560／3840 只有原片長邊至少達到該檔才做，不放大，寬高取偶數。

| 項目 | 設定 |
| --- | --- |
| 編碼 | H.264 High、yuv420p、CRF 22、preset slow；maxrate 1280 檔 3M、1920 檔 6M、2560 檔 10M、3840 檔 16M（bufsize 兩倍）；`-movflags +faststart`；x264 固定 4 執行緒（重出逐位元組相同） |
| 色彩 | 輸出一律標 BT.709。`color_transfer` 是 `arib-std-b67`（HLG）或 `smpte2084`（PQ）時用 `zscale`（線性、npl=100）＋`tonemap=hable` 轉成 BT.709 SDR |
| 方向 | 依旋轉資訊轉正（ffmpeg autorotate），輸出不帶旋轉標記 |
| 隱私 | `-map_metadata -1 -map_chapters -1`，只取一條畫面（和一條聲音），`+bitexact`：GPS／位置、裝置、軟體、建立時間和 iPhone 的中繼資料軌都不進輸出 |
| 聲音 | 影片：AAC 128k 立體聲；Live Photo：無 |
| 限制 | 超過 30 秒拒絕；每檔都要在 20 MB 內，依片長計算的 maxrate 若低於最低碼率（1280：2M、1920：3.5M、2560：6M、3840：10M），就不出該檔，其他檔與整張照常完成。實際輸出超過 20 MB 時用 CRF 26 重做：1280 用 2M 且一定要出，其他檔用同一份片長預算，仍超過只略過該檔。`check` 對所有輸出檔（WebP、MP4）設 24 MB 上限（EdgeOne 單檔 25 MB） |

**版本**：`webvid.py` 的 `VIDEO_RECIPE` 是既有 1280／1920 的影片版本號；只有影片的條目版本 = `sha256(影片 SHA-256 + RECIPE + VIDEO_RECIPE)` 前 10 碼（封面 WebP 和這兩檔 MP4 共用），Live Photo 的 1280／1920 MP4 用 `sha256(影片 SHA-256 + VIDEO_RECIPE + "live")` 前 10 碼。新增的 2560／3840 另用 `HI_TIER_RECIPE` 算版本，所以替已上線條目補高檔時，原有 1280／1920 的檔名與位元組不變。最低碼率與略過規則另用 `OMISSION_POLICY` 版本，只讓先前缺的檔重新判斷，不改任何已存在 MP4 的版本。改影片配方不會動到一般照片。

**失敗**：太長、太大或讀不了的影片只讓那一張失敗（中文原因印在輸出裡），其他照片照常出圖、manifest 照常更新；`build` 結束時退出碼 3。設了 `JRFANK_BUILD_FAILURES` 就把失敗的鍵寫進那個檔，`publish-photos.py` 和部署確認會跳過它們。Actions 照常提交其他照片，最後一步再讓 run 失敗。

**失敗報告**：設了 `JRFANK_BUILD_FAILURES_JSON` 就另外寫一份 JSON：`{"schema": 1, "failures": [{"key", "kind", "code", "message", "duration"?}]}`。`key` 是照片鍵（不含副檔名），`kind` 是 `live`／`video`，`code` 是 `too-long`（超過 30 秒，帶 `duration` 秒數）、`too-large`（超過 20 MB）、`hdr`（HDR 轉 SDR 失敗）、`unreadable`（讀不了、沒有畫面軌或長度）、`ffmpeg`（其他編碼失敗）、`tool`（找不到 ffmpeg）。有失敗時 Actions 在「出圖」之後馬上存成 artifact **`photo-build-failures`**（保留 30 天，內含 `build-failures.json`），並把表格寫進 job summary；後台讀這個 artifact，在失敗的那一張上顯示中文原因。

**ffmpeg**：Homebrew 核心的 `ffmpeg` 沒有 zimg（沒有 `zscale`），要裝 keg-only 的 **`ffmpeg-full`**（`brew install ffmpeg-full`，不會覆蓋原本的 ffmpeg）。`webvid.py` 依序找 `JRFANK_FFMPEG_DIR`、`/opt/homebrew/opt/ffmpeg-full/bin`、PATH；沒有 `zscale` 時 HDR 影片會以清楚的錯誤失敗，SDR 影片照常。

`stray_files`／`prune`、`check`、`publish-photos.py` 和部署確認都涵蓋 MP4。本機測試：`python3 tools/test_webvid.py`（用 ffmpeg 現做測試片：SDR、10-bit HLG、直拍、帶 GPS、25 秒 4K、超過 30 秒、HEIC＋MOV、只有 MOV）。

## 品質

2026-10-11 抽 5 張比對，挑最難壓的類型：

| 照片 | 類型 |
| --- | --- |
| `objects/objects-3` | Display P3、大片散景＋細顆粒、機身磨砂質感 |
| `shantou/shantou-4` | Display P3、天空漸層、海面 |
| `hk/hk-7` | 直幅（4160 × 6240） |
| `hk/hk-3` | 夜景、暗部多、ISO 3200 雜訊（Apple Wide Color） |
| `gba/gba-12` | 細節最多（舊三檔裡位元組最大的之一） |

方法：同一個配方縮好、銳化好的無損 PNG 當參考，各品質編成 WebP 再解回來，用 SSIMULACRA2（libjxl）打分；再把參考和各品質在 100% 下並排裁切，用眼睛看。SSIMULACRA2 的參考線：70 高品質、80 並排 1:1 很難分辨、90 閃爍比對也看不出。

SSIMULACRA2（括號內是位元組）：

| 照片 · 檔 | q82 | q85 | q88 | q90 | q92 |
| --- | --- | --- | --- | --- | --- |
| objects-3 · 1200 | 75.4 (39 KB) | 77.8 (45 KB) | 80.1 (54 KB) | 82.1 (63 KB) | 84.0 (73 KB) |
| objects-3 · 2400 | 71.3 (120 KB) | 73.6 (155 KB) | 76.3 (214 KB) | 78.1 (274 KB) | 81.1 (355 KB) |
| shantou-4 · 2400 | 75.7 (413 KB) | 78.1 (469 KB) | 79.7 (541 KB) | 82.8 (611 KB) | 84.3 (702 KB) |
| hk-7 · 2400 | 76.0 (444 KB) | 78.4 (502 KB) | 81.2 (582 KB) | 83.1 (652 KB) | 84.8 (748 KB) |
| hk-3 · 2400 | 79.1 (1001 KB) | 81.6 (1113 KB) | 84.1 (1255 KB) | 86.2 (1403 KB) | 87.4 (1544 KB) |
| gba-12 · 1200 | 78.5 (244 KB) | 80.9 (273 KB) | 83.8 (312 KB) | 85.5 (348 KB) | 86.6 (372 KB) |
| gba-12 · 2400 | 75.4 (693 KB) | 79.0 (781 KB) | 83.0 (928 KB) | 84.7 (1003 KB) | 86.2 (1140 KB) |
| gba-12 · 4000 | 75.9 (1322 KB) | 79.0 (1499 KB) | 82.4 (1810 KB) | 84.0 (2042 KB) | 86.1 (2371 KB) |

100% 並排看：
- 夜景（hk-3）、天空與海（shantou-4）、直幅（hk-7）、細節（gba-12）：q82 起就看不出差別。
- **散景顆粒和磨砂表面（objects-3）最先露餡**：q82、q85 把背景的細顆粒抹平、機身磨砂質感變塑膠，皮膚紋理也變平滑，並排看得出來；**q88** 顆粒和質感都在，看不出差別；q90 以上沒有再多的可見改善。

→ 選 **q88**（五檔同一個品質）。代價：比舊版（q78／80／82、沒有銳化）大約多 65–75%，例如 1200 檔平均 94 KB → 164 KB。比對的腳本和裁切圖沒有進倉庫。

現況（67 張）：600 平均 51 KB、1200 164 KB、1800 344 KB、2400 582 KB、4000 1.29 MB；全部 318 檔約 148 MB。

## 加照片、換原圖

平常走後台（7b）：上傳原圖到 R2 → 觸發 Actions。手動做：

```bash
# 1. 原圖傳到 R2（鍵 = <系列>/<照片 id>.<副檔名>），本機快取也放一份
npx wrangler r2 object put jrfank-originals/hk/hk-10.jpeg --file ~/Code/jrfank-site-assets/photo-originals/hk/hk-10.jpeg --remote
# 2. 觸發出圖（或在 GitHub 的 Actions → build photos → Run workflow 填 hk/hk-10.jpeg）
gh workflow run build-photos.yml -R JR-Fank/jrfank-img -f photos="hk/hk-10.jpeg"
# 3. 網站倉庫：content/series/hk.json 加照片（file: "hk/hk-10"），再
pnpm sync:photos
```

本機直接出圖也可以（要 macOS + `brew install imagemagick`；有影片原檔再 `brew install ffmpeg-full`）：

```bash
python3 tools/webimg.py build                    # 全部（原圖和配方都沒變的跳過）
python3 tools/webimg.py build hk/hk-8 --force    # 指定幾張；--force 重做
python3 tools/webimg.py check                    # 核對 manifest 和檔案
python3 tools/webimg.py prune                    # 刪掉 manifest 沒引用的舊版本檔
```

換原圖會產生新版本的檔，**舊版本的檔留著**（網站還沒 `sync:photos`、重新部署前還在用）。網站部署好以後再 `prune`、提交。

原圖目錄用環境變數 `JRFANK_ORIGINALS` 改（Actions 指到下載的暫存目錄）。

## GitHub Actions（`build photos`）

`.github/workflows/build-photos.yml`：手動（`workflow_dispatch`，參數 `photos`、`force`）或 `repository_dispatch`（`event_type: build-photos`，`client_payload.photos` 陣列）→ 從 R2 下載原圖 → `webimg.py build` → `check` → 提交、推送 → EdgeOne 自動部署。跑在 macOS（和本機同一套 ImageMagick 與 sRGB 描述檔）。

指定單張時，同一個 id 的其他原檔（Live Photo 的圖片／影片）會一起下載，只換靜態圖也不會弄掉 `live`。下載的原檔裡有影片才 `brew install ffmpeg-full`。

`concurrency: build-photos` 讓 job 依序執行，但排隊事件的 SHA 不會跟著 main 更新。checkout 明確指定最新 `main` 並取完整歷史，避免排隊後仍從舊 manifest 出圖。提交由 `tools/publish-photos.py` 處理：rebase 衝突先 abort；若 rebase 衝突或 push 前 main 又前進，等 3 秒、fetch 並 reset 到最新 `origin/main`，只為本次下載的照片重建條目後再提交推送，最多重試 3 次（共 4 次嘗試）。其他 fetch／權限／檢查錯誤直接失敗。

恢復時從工作目錄外的暫存備份還原本次已出好的 WebP，`webimg.py build --reuse-manifest <快取>` 驗證原圖雜湊、配方及檔案齊全後沿用條目，重新套用最新 EXIF 修正，**不重新編碼圖片**；每次恢復及推送前都跑 `check`。不相關照片保留最新 main 的內容。

推送後，Actions 從 `https://pic.jrfank.cc` 讀取正式 manifest，確認全部作品的鍵與版本和本次提交相符，並檢查本次處理照片及建置期間新增的 WebP 的尺寸與 SHA-256；圖片檢查完成後再讀一次 manifest。最多等 15 分鐘、每 15 秒嘗試一次，失敗時整個 run 失敗且不產生證據。確認成功才上傳固定名稱 `photo-deployment-proof` 的 artifact（保留 90 天），內含根檔 `deployment-proof.json`：schema 1、確認時間、正式 origin、提交 SHA、該提交的 manifest 原始位元組 SHA-256 和全部作品的版本。這是 Actions runner 直接讀取正式圖片站時看到的狀態；之後的回滾或其他 CDN 節點需要另行確認。

**只檢查、不出圖**（`.github/workflows/verify-photos.yml`，`repository_dispatch` 事件 `verify-photos`，`client_payload.photos` 是照片鍵）：刪除作業之類不出圖的提交改過 manifest 之後，沒有一份部署確認對得上目前的 manifest，後台會在等了 3 分鐘以上時觸發它。它在 ubuntu runner 上 checkout 目前的 `main`，跑 `verify_photo_deployment.py --keys …`：正式 manifest 的全部作品版本要和 `main` 相同、指定照片的條目（版本、各檔 WebP、`video`／`live` 的 MP4）要完全一樣，那些檔也要逐一 HEAD＋GET 核對大小與 SHA-256；最多等 5 分鐘，成功才上傳同名 `photo-deployment-proof`（同一種 schema 1 檔）。不讀 R2、不提交，`concurrency` 和 build photos 分開（同一個 group 裡新的排隊會取消還在排隊的出圖）。

本機競爭測試（需要 ImageMagick，不連 GitHub/R2）：`python3 tools/test_publish_photos.py`。測試用兩個暫存 clone 重現 manifest rebase 衝突與 push 被搶先更新，核對雙方照片、WebP 雜湊／修改時間及無變化重跑；暫存 clone 自動清除。

本機部署確認測試（不連正式圖片站）：`python3 tools/test_photo_deployment.py`；使用暫存 Git 倉庫及本機 HTTP 模擬未部署、部分部署、回滾、HTTP 失敗、同大小錯誤雜湊、成功、HEAD 不支援 fallback，`all`／資料夾輸入列出全部下載照片，以及只檢查模式（目前 HEAD、照片站還沒更新、缺檔、照片鍵不對）。

`photos` 可以寫資料夾（以 `/` 結尾）：`reviews/` = 全部文章圖、`reviews/orico-test-mac/` = 一篇，例如 `gh workflow run build-photos.yml -R JR-Fank/jrfank-img -f photos="reviews/"`。

需要的設定（只寫位置，值不進倉庫）：
- Secrets：`R2_ACCESS_KEY_ID`、`R2_SECRET_ACCESS_KEY`（R2 API 權杖，對 `jrfank-originals` 唯讀）
- Variables：`R2_ACCOUNT_ID`（Cloudflare 帳號 ID，不是秘密）

## 本機預覽

網站倉庫跑 `pnpm img`（在 http://localhost:4310 提供這個資料夾，帶 CORS），`pnpm dev` 會從那裡讀圖。
