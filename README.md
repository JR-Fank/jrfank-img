# jrfank-img

jrfank.cc 的照片網頁檔。第 6 步部署成 `img.jrfank.cc`（要回 `Access-Control-Allow-Origin`，抽卡卡包的 canvas 網點圖要讀像素）。

- `<系列>/<照片 id>-<600|1200|2400>.webp`：長邊 600 / 1200 / 2400 px 三檔；原圖不夠大就停在原圖尺寸（不放大），這時大的幾檔尺寸相同。
- 一律 sRGB、去掉全部 metadata（EXIF、GPS、XMP、ICC）。拍攝參數另外寫在網站倉庫的 `content/series/*.json`。
- `manifest.json`：每張的實際尺寸、位元組數、原圖 SHA-256。
- 原圖**不在這裡**：`~/Code/jrfank-site-assets/photo-originals/<系列>/<照片 id>.<副檔名>`（來源紀錄在那裡的 `SOURCES.md`）。

## 加照片

```bash
# 1. 原圖放進 ~/Code/jrfank-site-assets/photo-originals/<系列>/<照片 id>.jpg，SOURCES.md 記一筆
python3 tools/webimg.py build <系列>/<照片 id>   # 2. 出三檔 webp，更新 manifest.json
python3 tools/webimg.py exif  <系列>/<照片 id>   # 3. 印出 EXIF 欄位，貼進網站的 content/series/<系列>.json
```

需要 ImageMagick 7（`brew install imagemagick`，要有 WebP）。sRGB 描述檔用 macOS 內建的 `/System/Library/ColorSync/Profiles/sRGB Profile.icc`。

## 本機預覽

網站倉庫跑 `pnpm img`（在 http://localhost:4300 提供這個資料夾，帶 CORS），`pnpm dev` 會從那裡讀圖。
