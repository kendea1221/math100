# pikpak_share_dl

PikPak の共有リンク (`https://mypikpak.com/s/...`) に含まれている動画をダウンロードするスクリプト（個人利用向け）。

## 使い方

```bash
pip install requests
# 匿名で試す（動画を一覧表示してダウンロード）
python pikpak_share_dl.py "https://mypikpak.com/s/<share_id>/<folder_id>/<file_id>"
# 匿名でリンクが取れない場合は PikPak アカウントでログイン
PIKPAK_USER=you@example.com PIKPAK_PASS=xxxx python pikpak_share_dl.py "<URL>"
```

オプション: `-o DIR` 保存先 / `-p CODE` 共有パスワード / `--all` 動画以外も取得 /
`--list` リンクを表示するだけ / `--debug` API のレスポンスを表示

URL の構造: `/s/<share_id>/<parent_id>/<file_id>`。file_id まで入っていればそのファイルのみ、
parent_id までならそのフォルダ以下を再帰的に、share_id だけなら共有全体を対象にします。

## 仕組み（API の流れ）

1. `POST user.mypikpak.com/v1/shield/captcha/init` で captcha_token を取得
   （`captcha_sign` = client_id+version+package+device_id+timestamp に salt を順に MD5）
2. `GET api-drive.mypikpak.com/drive/v1/share` → `pass_code_token` とルートのファイル一覧
3. `GET /drive/v1/share/detail` でフォルダを再帰的にたどる
4. `GET /drive/v1/share/file_info` → `web_content_link` / `medias[].link.url` が直リンク
5. 匿名でリンクが返らない場合: ログイン → `POST /drive/v1/share/restore` で自分のドライブに保存 →
   `GET /drive/v1/tasks/{id}` の `trace_file_ids` で新しい ID を得て `GET /drive/v1/files/{id}` からリンク取得

## うまくいかないとき

- `captcha init failed` / `captcha_invalid`: PikPak が salt やクライアントのバージョンを変えた可能性。
  スクリプト先頭の `CLIENT_VERSION` と `ALGORITHMS` を更新してください。
- 一番確実な手動の方法: ブラウザでログインして共有ページを開き、DevTools の Network タブで
  `file_info` または `files/` のレスポンスから `web_content_link` をコピーして
  `curl -L -o video.mp4 "<link>"` / `aria2c -x8 "<link>"` でダウンロード。
