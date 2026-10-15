# ポケカ日次通知

Amazon.co.jp・ポケモンセンターオンラインに関係する、BOXとBOX入りセットの購入情報を毎朝9時（日本時間）に確認し、変化があればDiscordへ送ります。Python標準ライブラリだけで動作します。

## 現在の取得範囲

- ポケゲトちゃんねるの「再販・抽選情報」カテゴリを全ページ確認。新規記事だけでなく既存記事の更新も拾います。
- 商品仕様の表からBOX単位の商品を判定し、Amazon／ポケモンセンターオンラインの販売情報欄の変更だけを通知します。他店や広告の変更は対象外です。
- ルリナセット・イーブイズセットなどBOX入りセットを含みます。BOX入りと確認できないセットは保留し、単品パック・デッキ・ケース・オリパは通知しません。
- ポケモンカード公式ニュースの、見出しからBOXと分かる抽選・追加販売のお知らせを補助的に確認します。
- 記事から見つけたAmazon商品URLについて、Amazon.co.jp販売・購入可能表示を確認できた場合に在庫変化を判定します。取得失敗や不明なHTMLは在庫なしと扱いません。

**未対応・制約：全ての既存BOX／新商品を網羅する保証はありません。記事にない商品は自動発見できず、BOXを含むか不明なセットは保留されます。ポケモンセンターオンラインの商品在庫の直接監視は未対応です。Amazonはサイトの取得制限により確認できないことがあります。記事更新の通知は、その時点で購入・応募可能という意味ではありません。** 日次確認の間に発生して終わる在庫復活は検出できません。

## 起動

1. このリポジトリの `Settings → Secrets and variables → Actions` で `DISCORD_WEBHOOK_URL` を登録します。URLをコード・設定ファイル・Issueに書かないでください。
2. `Actions → Pokeca daily check → Run workflow` から `test_notification` を選ぶと接続テストを送信します。
3. 通常実行で初回の基準データを作成します。初回に過去記事を大量送信しません。次の実行から新着・変更を通知します。

状態は `monitor-state` ブランチの `state.json` に保存します。公開されるのは記事URL・タイトル・判定・ハッシュなど公開情報のみで、Webhookや個人情報は保存しません。Discord送信確認後に通知済みにします。送信直後の強制終了など、Discord送信とGitHub保存の間で障害が起きた場合は再通知される可能性があります。

取得失敗はActionsの実行結果とSummaryに出ます。Amazonの確認不能件数・BOX判定の保留件数もSummaryで確認できます。変化がない日はDiscordへ送信しません。必要に応じてGitHubの失敗通知を有効にしてください。

GitHubの定期実行は遅延する場合があります。またPublicリポジトリでは長期間リポジトリに活動がないと定期実行が無効化される場合があります。通常の日次状態保存はコミットを作成しますが、運用時はActionsの最終実行日時も確認してください。

## 開発

```sh
python3 -m unittest discover -v
python3 monitor.py --dry-run
```

`--dry-run` は通知・状態保存をしません。日時変更は `.github/workflows/daily.yml` のcron（UTC）を編集します。`config.json` の時刻は説明用で、スケジューラを設定するものではありません。Amazonの直接確認は `check_amazon_stock` で切り替えます。

## 情報源・仕様

- [ポケゲトちゃんねる](https://pokemon-infomation.com/)
- [ポケモンカード公式ニュース](https://www.pokemon-card.com/info/)
- [WordPress Posts API](https://developer.wordpress.org/rest-api/reference/posts/)
- [GitHubの定期実行](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [Discord Webhooks](https://docs.discord.com/developers/resources/webhook)

個人用の非公式ツールです。自動購入や自動応募は行いません。
