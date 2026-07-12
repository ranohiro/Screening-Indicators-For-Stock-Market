# 株価データベース（`stock_data.db`）構造・仕様書

このドキュメントは、日本株投資アプリの各機能や分析ロジックで使用される SQLite データベース [stock_data.db](file:///Users/hiranotakahiro/Projects/銘柄スクリーニング検証/data/stock_data.db) のデータ構造と仕様について説明したものです。

---

## 1. 概要
`stock_data.db` は、全上場銘柄の日足株価データ、企業情報、およびテーマ分析によって算出された時系列流入スコアなどを一元管理する SQLite データベースです。

主にトレンドテーマ検知機能では、以下のテーブルを参照・更新します。
1. **`daily_prices`** (日足・株価指標テーブル): 動意株スクリーニングの計算元データ。
2. **`daily_theme_scores`** (テーマ別日次スコア蓄積テーブル): 計算されたテーマランキング結果の保存先。
3. **`companies`** (銘柄基本情報マスターテーブル): 銘柄コードから市場や正式名などの付帯情報を引くためのマスタ。

---

## 2. 主要テーブルのスキーマ定義とデータ例

### 2.1. `daily_prices`（日足・株価指標テーブル）
全銘柄の毎日の日足情報および時価総額等を格納します。動意株判定用の「移動平均出来高」や「年初来高値（過去250日高値）」の算出ベースになります。

#### スキーマ定義
| カラム名 | データ型 | NULL | キー | 説明 |
| :--- | :--- | :--- | :--- | :--- |
| `code` | `TEXT` | NO | PK (複合) | 4桁の銘柄コード（例: `"1301"`） |
| `date` | `TEXT` | NO | PK (複合) | 取引日付。形式は `YYYYMMDD` （例: `"20241101"`） |
| `open` | `REAL` | YES | - | 始値（円） |
| `high` | `REAL` | YES | - | 高値（円） |
| `low` | `REAL` | YES | - | 安値（円） |
| `close` | `REAL` | YES | - | 終値（円） |
| `volume` | `REAL` | YES | - | 出来高（株数） |
| `trading_value` | `REAL` | YES | - | 売買代金（千円） |
| `market_cap_total` | `REAL` | YES | - | 当日の時価総額（百万円） |
| `vwap` | `REAL` | YES | - | 出来高加重平均株価 |
| `base_price` | `REAL` | YES | - | 基準価格（前日終値など、制限値幅の基準） |
| `limit_up` | `REAL` | YES | - | ストップ高制限値（円） |
| `limit_down` | `REAL` | YES | - | ストップ安制限値（円） |

#### 実際のデータ例
```sql
sqlite> SELECT * FROM daily_prices WHERE code = '1301' AND date = '20241101';
code             = 1301
date             = 20241101
open             = 4155.0
high             = 4155.0
low              = 4075.0
close            = 4080.0
volume           = 21200.0
trading_value    = 86969.0
market_cap_total = 49279.0
vwap             = (NULL)
base_price       = (NULL)
limit_up         = (NULL)
limit_down       = (NULL)
```
- **解釈**: 2024年11月1日時点の極洋（1301）は、終値 4,080円、出来高 21,200株、時価総額は 492億7,900万円でした。

---

### 2.2. `daily_theme_scores`（テーマ別日次スコア蓄積テーブル）
毎日大引け後に計算される「注目テーマ」のスコア、順位、牽引銘柄などの結果を時系列で蓄積するテーブルです。

#### スキーマ定義
| カラム名 | データ型 | NULL | キー | 説明 |
| :--- | :--- | :--- | :--- | :--- |
| `date` | `TEXT` | NO | PK (複合) | 計算対象日。形式は `YYYYMMDD` （例: `"20260626"`） |
| `theme_name` | `TEXT` | NO | PK (複合) | テーマ名。`ticker_dictionary.json`に定義されるタグ（例: `"小売り"`） |
| `score` | `REAL` | YES | - | そのテーマの資金流入スコア |
| `rank` | `INTEGER` | YES | - | その日のテーマランキングの順位（1から開始） |
| `prev_rank` | `INTEGER` | YES | - | 前日の順位。前日ランク外の場合は NULL 等 |
| `consecutive_days` | `INTEGER` | YES | - | そのテーマが連続して一定順位（例: Top 20）以内に留まった日数 |
| `driving_stocks` | `TEXT` | YES | - | 当該テーマを牽引した上位銘柄コードのカンマ区切りリスト（例: `"3544,3086,9854"`） |

#### 実際のデータ例
```sql
sqlite> SELECT * FROM daily_theme_scores WHERE date = '20260626' LIMIT 2;
date             = 20260626
theme_name       = 小売り
score            = 125.766057160232
rank             = 1
prev_rank        = (NULL)
consecutive_days = 1
driving_stocks   = 3544,3086,9854

date             = 20260626
theme_name       = 中国関連
score            = 84.5352064810954
rank             = 2
prev_rank        = (NULL)
consecutive_days = 1
driving_stocks   = 4248,6384,3950
```
- **解釈**: 2026年6月26日の注目テーマランキングにおいて、1位は「小売り」（スコア: 約125.8、連続1日目、主な牽引銘柄: 3544, 3086, 9854）、2位は「中国関連」（スコア: 約84.5、連続1日目、牽引銘柄: 4248, 6384, 3950）であったことを示します。

---

### 2.3. `companies`（銘柄マスターテーブル）
全上場銘柄の基本的な企業属性マスタです。

#### スキーマ定義
| カラム名 | データ型 | NULL | キー | 説明 |
| :--- | :--- | :--- | :--- | :--- |
| `code` | `TEXT` | NO | PK | 4桁の銘柄コード（例: `"1301"`） |
| `name` | `TEXT` | YES | - | 正式企業名（例: `"極洋"`） |
| `market` | `TEXT` | YES | - | 上場市場区分（プライム、スタンダード、グロース等） |
| `industry` | `TEXT` | YES | - | 東証33業種等の分類区分 |
| `list_date` | `TEXT` | YES | - | 上場日（YYYYMMDD） |
| `website_url` | `TEXT` | YES | - | 企業の公式ホームページURL |
| `updated_at` | `TIMESTAMP` | YES | - | レコードの最終更新日時 |

#### 実際のデータ例
```sql
sqlite> SELECT * FROM companies WHERE code = '1301';
code        = 1301
name        = 極洋
market      = プライム
industry    = 水産・農林業
list_date   = 19490516
website_url = https://www.kyokuyo.co.jp/
updated_at  = 2024-11-01 10:00:00
```
