# Energy Dashboard の統計

状態: OAuth Application 発行前のプレアルファ版

この連携は、Octopus Energy Japan から取得した区間使用量を Home
Assistant の外部長期統計へ変換します。正式なインストール方法は、OEJP
から Public OAuth Application が発行され、実アカウント検証が完了した後に
公開します。

## 提供予定の統計

- 供給地点ごとの買電量（import energy、kWh）
- OEJP が売電方向を提供する場合の売電量（export energy、kWh）

統計は1時間単位の使用量と累積値を持ちます。累積値の起点は、この連携が
ローカル ledger に保存している最も古い reading です。電力量計の生涯値では
ありません。

OEJP が返す公式コストの内部モデルはありますが、OAuth 権限、通貨、区間の
完全性、訂正方法が確認されるまでは公開しません。この4点は現時点でいずれも
未確認です。詳細は
[契約・料金プラン・請求情報](CONTRACT_AND_BILLING.md)を参照してください。
利用者が入力した単価による単純な料金推定は Energy Dashboard へ登録しません。

## 遅延および訂正

OEJP の30分 reading はリアルタイム値ではなく、遅れて追加または訂正される
場合があります。この連携は存在しない reading をゼロとして作成しません。
遅延 reading が届いた場合は、対象時刻とそれ以降の累積値を再計算します。

API が以前の reading を削除した場合は、影響する供給地点・方向の統計だけを
Home Assistant Recorder 上で再構築します。再起動、同じデータの再取得、訂正の
再処理後も同じ結果になるよう設計されています。

## プライバシー

統計 ID には、Home Assistant ごとに異なるローカル HMAC 識別子を使用します。
Account number、SPIN、Supply Point ID、住所、メールアドレス、OAuth token は
統計 ID、名称、ログ、診断情報へそのまま表示しません。外部 telemetry は
送信しません。

## リリース後の設定

OAuth 対応版の正式リリース後、Home Assistant の
**設定 → ダッシュボード → エネルギー**から、
`octopus_energy_japan` の買電または売電統計を選択します。具体的な画面手順は
リリース前のクリーンインストール検証後に追記します。

実装上の規範仕様は英語版
[`../ENERGY_STATISTICS.md`](../ENERGY_STATISTICS.md) を参照してください。
