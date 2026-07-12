# 株価データベース（`stock_data.db`）構造・仕様書

このドキュメントは、日本株投資アプリの各機能や分析ロジックで使用される SQLite データベース [stock_data.db](file:///Users/hiranotakahiro/Projects/銘柄スクリーニング検証/data/stock_data.db) のデータ構造と仕様について説明したものです。

---

## 1. 概要
`stock_data.db` は、全上場銘柄の日足株価データ、企業情報、財務・信用取引情報、テクニカル指標、適時開示データ、およびテーマ分析によって算出された時系列流入スコアなどを一元管理する SQLite データベースです。

データベース内には以下の **17個のテーブル** が定義されています。

| # | テーブル名 | 説明 |
| :--- | :--- | :--- |
| 1 | [`companies`](#21-companies銘柄マスターテーブル) | 銘柄コードから市場や正式名などの付帯情報を引くためのマスタテーブル。 |
| 2 | [`daily_prices`](#22-daily_prices日足株価指標テーブル) | 全銘柄の毎日の日足情報および時価総額等を格納する基本テーブル。 |
| 3 | [`daily_financials`](#23-daily_financials日次財務データ) | PER、PBR、EPS、配当利回りなどの日次更新される財務・投資指標テーブル。 |
| 4 | [`weekly_margin`](#24-weekly_margin週次信用残高テーブル) | 週次で発表される制度信用・一般信用の買い残・売り残データを格納するテーブル。 |
| 5 | [`daily_indices`](#25-daily_indices日次指数データテーブル) | 日経平均やTOPIXなどの主要株価指数の日次データを格納するテーブル。 |
| 6 | [`analysis_history`](#26-analysis_history個別銘柄等の分析履歴テーブル) | 各銘柄に対して行われた分析処理の実行履歴を記録するテーブル。 |
| 7 | [`sector_trade_data`](#27-sector_trade_data部門別売買動向データテーブル) | 海外投資家や個人投資家などの投資部門別売買動向を記録するテーブル。 |
| 8 | [`daily_sector_performance`](#28-daily_sector_performance業種別規模別日次パフォーマンステーブル) | 東証業種や時価総額規模別の合成指数および日次騰落統計を格納するテーブル。 |
| 9 | [`economic_events`](#29-economic_events経済イベントテーブル) | 国内外の主要経済指標の発表スケジュールと予想・実績データを格納するテーブル。 |
| 10 | [`financial_results`](#210-financial_results決算実績予想データテーブル) | 企業の四半期・通期決算実績および会社発表予想データを格納するテーブル。 |
| 11 | [`stock_daily_indicators`](#211-stock_daily_indicators日次銘柄指標テーブル) | 日々の銘柄別騰落率（1日・1週・1ヶ月）や時価総額クラスを格納するテーブル。 |
| 12 | [`daily_trade_indicators`](#212-daily_trade_indicators日次取引指標テクニカルファンダメンタルズテーブル) | テクニカル指標（RSI、MACD、乖離率等）やモメンタム、複数移動平均線を網羅する指標テーブル。 |
| 13 | [`daily_market_stats`](#213-daily_market_stats市場全体統計テーブル) | 各市場セグメントの値上がり・値下がり銘柄数などの全体統計テーブル。 |
| 14 | [`stock_eps_revisions`](#214-stock_eps_revisionseps修正履歴テーブル) | アナリストや会社予想によるEPS修正履歴を記録するテーブル。 |
| 15 | [`corporate_disclosures`](#215-corporate_disclosures適時開示企業発表資料テーブル) | 適時開示（TDnet/EDINET）のタイトル、URL、AIによる要約や感情分析スコアを格納するテーブル。 |
| 16 | [`disclosure_analysis_logs`](#216-disclosure_analysis_logs開示分析ログテーブル) | 適時開示情報の収集・AI処理の実行ログ（消費トークンや処理時間）を記録するテーブル。 |
| 17 | [`daily_theme_scores`](#217-daily_theme_scoresテーマ別日次スコア蓄積テーブル) | 計算された資金流入テーマランキング結果や牽引銘柄の保存先テーブル。 |

---

## 2. 各テーブルのスキーマ定義

### 2.1. `companies`（銘柄マスターテーブル）
全上場銘柄の基本的な企業属性を格納するマスタテーブルです。

#### スキーマ定義
| カラム名 | データ型 | NULL | キー | 説明 |
| :--- | :--- | :--- | :--- | :--- |
| `code` | `TEXT` | YES | PK | 4桁の銘柄コード（例: `"1301"`） |
| `name` | `TEXT` | YES | - | 正式企業名（例: `"極洋"`） |
| `market` | `TEXT` | YES | - | 上場市場区分（プライム、スタンダード、グロース等） |
| `industry` | `TEXT` | YES | - | 東証33業種等の分類区分 |
| `list_date` | `TEXT` | YES | - | 上場日。形式は `YYYYMMDD` （例: `"19490516"`） |
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

---

### 2.2. `daily_prices`（日足・株価指標テーブル）
全銘柄の毎日の日足情報および時価総額等を格納します。動意株判定用の「移動平均出来高」や「年初来高値（過去250日高値）」の算出ベースになります。

#### スキーマ定義
| カラム名 | データ型 | NULL | キー | 説明 |
| :--- | :--- | :--- | :--- | :--- |
| `code` | `TEXT` | YES | PK (複合) | 4桁の銘柄コード（例: `"1301"`） |
| `date` | `TEXT` | YES | PK (複合) | 取引日付。形式は `YYYYMMDD` （例: `"20241101"`） |
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

---

### 2.3. `daily_financials`（日次財務データ）
日次で更新・計算される、PER、PBR、EPS、BPS、配当利回りなどの主要投資指標を格納します。

#### スキーマ定義
| カラム名 | データ型 | NULL | キー | 説明 |
| :--- | :--- | :--- | :--- | :--- |
| `code` | `TEXT` | YES | PK (複合) | 4桁の銘柄コード |
| `date` | `TEXT` | YES | PK (複合) | 取引日付（`YYYYMMDD`） |
| `market_cap` | `REAL` | YES | - | 時価総額（百万円） |
| `shares_outstanding` | `REAL` | YES | - | 発行済株式数（株） |
| `per_forecast` | `REAL` | YES | - | 予想PER（倍） |
| `pbr_actual` | `REAL` | YES | - | 実績PBR（倍） |
| `eps_forecast` | `REAL` | YES | - | 予想EPS（円） |
| `bps_actual` | `REAL` | YES | - | 実績BPS（円） |
| `dividend_yield` | `REAL` | YES | - | 予想配当利回り（％） |
| `min_investment` | `REAL` | YES | - | 最低投資金額（円） |
| `dps_forecast` | `REAL` | YES | - | 予想1株当たり配当金（円、デフォルト: 0.0） |

---

### 2.4. `weekly_margin`（週次信用残高テーブル）
週次で発表される制度信用取引・一般信用取引の買い残・売り残高および信用倍率を格納します。

#### スキーマ定義
| カラム名 | データ型 | NULL | キー | 説明 |
| :--- | :--- | :--- | :--- | :--- |
| `code` | `TEXT` | YES | PK (複合) | 4桁の銘柄コード |
| `date` | `TEXT` | YES | PK (複合) | 算出基準日（通常は金曜日の日付 `YYYYMMDD`） |
| `sell_balance_total` | `REAL` | YES | - | 信用売り残高合計（株） |
| `buy_balance_total` | `REAL` | YES | - | 信用買い残高合計（株） |
| `ratio` | `REAL` | YES | - | 信用倍率（買い残高合計 / 売り残高合計） |
| `sell_balance_ins` | `REAL` | YES | - | 制度信用売り残高（株） |
| `buy_balance_ins` | `REAL` | YES | - | 制度信用買い残高（株） |
| `sell_balance_gen` | `REAL` | YES | - | 一般信用売り残高（株） |
| `buy_balance_gen` | `REAL` | YES | - | 一般信用買い残高（株） |

---

### 2.5. `daily_indices`（日次指数データテーブル）
日経平均株価やTOPIX、マザーズ指数などの国内主要指数の日次データを格納します。

#### スキーマ定義
| カラム名 | データ型 | NULL | キー | 説明 |
| :--- | :--- | :--- | :--- | :--- |
| `code` | `TEXT` | NO | PK (複合) | 指数コード（例: `"0000"`） |
| `name` | `TEXT` | YES | - | 指数名（例: `"日経平均株価"`, `"TOPIX"`） |
| `date` | `TEXT` | NO | PK (複合) | 取引日付（`YYYYMMDD`） |
| `close` | `REAL` | YES | - | 終値 |
| `change_ratio` | `REAL` | YES | - | 前日比騰落率（％） |
| `market_cap_index` | `REAL` | YES | - | 指数時価総額（浮動株ベース） |
| `volume` | `REAL` | YES | - | 売買単位換算後株式数（指数の出来高に相当） |
| `銘柄数` | `INTEGER` | YES | - | 指数を構成する銘柄数 |

---

### 2.6. `analysis_history`（個別銘柄等の分析履歴テーブル）
各銘柄に対して実行された分析やスクリーニングのログ情報を記録します。

#### スキーマ定義
| カラム名 | データ型 | NULL | キー | 説明 |
| :--- | :--- | :--- | :--- | :--- |
| `id` | `INTEGER` | YES | PK | 自動インクリメントID |
| `stock_code` | `TEXT` | NO | - | 対象の4桁銘柄コード |
| `company_name` | `TEXT` | YES | - | 企業名 |
| `analyzed_at` | `TEXT` | NO | - | 分析実行日時（形式: `YYYY-MM-DD HH:MM:SS`） |
| `user_name` | `TEXT` | YES | - | 実行ユーザー名 |
| `success` | `INTEGER` | YES | - | 分析処理成否（`1`: 成功、`0`: 失敗、デフォルト: 1） |

---

### 2.7. `sector_trade_data`（部門別売買動向データテーブル）
日本取引所グループ（JPX）から取得される、投資部門別（海外投資家、個人、自己など）の週次・日次売買代金ネット差額（買越額・売越額）を格納します。

#### スキーマ定義
| カラム名 | データ型 | NULL | キー | 説明 |
| :--- | :--- | :--- | :--- | :--- |
| `date` | `TEXT` | YES | PK | 取引日もしくは算出基準週の日付（`YYYYMMDD`） |
| `foreign_net` | `INTEGER` | YES | - | 海外投資家ネット買付額（千円） |
| `individual_net` | `INTEGER` | YES | - | 個人投資家ネット買付額（千円） |
| `proprietary_net` | `INTEGER` | YES | - | 証券会社自己部門ネット買付額（千円） |
| `updated_at` | `TEXT` | YES | - | 最終更新日時 |
| `trust_net` | `INTEGER` | YES | - | 信託銀行ネット買付額（千円） |
| `n225_close` | `REAL` | YES | - | 日経平均株価終値 |
| `n225_change_pct` | `REAL` | YES | - | 日経平均株価騰落率（％） |
| `individual_total_net` | `INTEGER` | YES | - | 個人全体ネット買付額（千円） |
| `individual_cash_net` | `INTEGER` | YES | - | 個人（現金）ネット買付額（千円） |
| `individual_margin_net` | `INTEGER` | YES | - | 個人（信用）ネット買付額（千円） |
| `investment_trust_net` | `INTEGER` | YES | - | 投資信託ネット買付額（千円） |
| `business_corp_net` | `INTEGER` | YES | - | 事業法人ネット買付額（千円） |
| `other_corp_net` | `INTEGER` | YES | - | その他法人ネット買付額（千円） |
| `trust_bank_net` | `INTEGER` | YES | - | 信託銀行詳細区分ネット買付額（千円） |
| `insurance_net` | `INTEGER` | YES | - | 保険会社ネット買付額（千円） |
| `city_regional_bank_net` | `INTEGER` | YES | - | 都銀・地銀等ネット買付額（千円） |

---

### 2.8. `daily_sector_performance`（業種別・規模別日次パフォーマンス）
業種別、または時価総額規模別グループの日次騰落パフォーマンス統計や平均売買代金を格納します。

#### スキーマ定義
| カラム名 | データ型 | NULL | キー | 説明 |
| :--- | :--- | :--- | :--- | :--- |
| `industry` | `TEXT` | YES | PK (複合) | 業種分類名（例: `"電気機器"`, `"全体"`） |
| `market_cap_range` | `TEXT` | YES | PK (複合) | 時価総額規模分類（例: `"Large"`, `"Mid"`, `"Small"`, `"Total"`） |
| `date` | `TEXT` | YES | PK (複合) | 取引日付（`YYYYMMDD`） |
| `composite_close` | `REAL` | YES | - | セクター合成指数終値 |
| `trading_value_total` | `REAL` | YES | - | セクター内合計売買代金 |
| `trading_value_5d_avg` | `REAL` | YES | - | 売買代金5日移動平均 |
| `trading_value_20d_avg` | `REAL` | YES | - | 売買代金20日移動平均 |
| `momentum_ratio` | `REAL` | YES | - | モメンタム比率 |
| `market_cap_total` | `REAL` | YES | - | セクター合計時価総額 |
| `composite_open` | `REAL` | YES | - | セクター合成指数始値 |
| `composite_high` | `REAL` | YES | - | セクター合成指数高値 |
| `composite_low` | `REAL` | YES | - | セクター合成指数安値 |
| `up_count` | `INTEGER` | YES | - | 値上がり銘柄数（デフォルト: 0） |
| `down_count` | `INTEGER` | YES | - | 値下がり銘柄数（デフォルト: 0） |
| `stock_count` | `INTEGER` | YES | - | 該当セクター内の構成銘柄数（デフォルト: 0） |

---

### 2.9. `economic_events`（経済イベント）
主要国で発表される経済指標のスケジュールと予想・実績値を格納します。

#### スキーマ定義
| カラム名 | データ型 | NULL | キー | 説明 |
| :--- | :--- | :--- | :--- | :--- |
| `id` | `INTEGER` | YES | PK | 自動インクリメントID |
| `title` | `TEXT` | YES | - | 経済指標・イベント名（例: `"米国雇用統計"`） |
| `country` | `TEXT` | YES | - | 発表国（例: `"アメリカ"`, `"日本"`） |
| `date` | `TEXT` | YES | - | 発表日時 |
| `impact` | `TEXT` | YES | - | 市場予想影響度（例: `"High"`, `"Medium"`, `"Low"`） |
| `forecast` | `TEXT` | YES | - | 指標の事前市場予想値 |
| `previous` | `TEXT` | YES | - | 指標の前回実績値 |
| `actual` | `TEXT` | YES | - | 指標の今回確定実績値 |
| *ユニーク制約* | - | - | - | `(title, country, date)` の組み合わせで重複不可 |

---

### 2.10. `financial_results`（決算実績・予想データ）
上場企業の各四半期・通期の決算実績数値および翌期予想数値を格納します。

#### スキーマ定義
| カラム名 | データ型 | NULL | キー | 説明 |
| :--- | :--- | :--- | :--- | :--- |
| `code` | `TEXT` | YES | PK (複合) | 4桁の銘柄コード（外部キー: `companies (code)`） |
| `date` | `TEXT` | YES | - | データ登録日 |
| `settlement_date` | `TEXT` | YES | PK (複合) | 決算期末日（`YYYYMMDD`） |
| `announcement_date` | `TEXT` | YES | - | 決算発表日（`YYYYMMDD`） |
| `sales` | `REAL` | YES | - | 売上高（百万円） |
| `operating_income` | `REAL` | YES | - | 営業利益（百万円） |
| `ordinary_income` | `REAL` | YES | - | 経常利益（百万円） |
| `net_income` | `REAL` | YES | - | 当期純利益（百万円） |
| `total_assets` | `REAL` | YES | - | 総資産（百万円） |
| `net_assets` | `REAL` | YES | - | 純資産（百万円） |
| `capital` | `REAL` | YES | - | 資本金（百万円） |
| `interest_bearing_debt` | `REAL` | YES | - | 有利子負債残高（百万円） |
| `equity_ratio` | `REAL` | YES | - | 自己資本比率（％） |
| `roe` | `REAL` | YES | - | ROE（自己資本利益率）（％） |
| `roa` | `REAL` | YES | - | ROA（総資産利益率）（％） |
| `shares_outstanding` | `REAL` | YES | - | 決算期末時点の発行済株式数 |
| `raw_data_json` | `TEXT` | YES | - | 決算詳細・その他財務項目の元JSONデータ |
| `forecast_sales` | `REAL` | YES | - | 翌期予想売上高（百万円） |
| `forecast_operating_income` | `REAL` | YES | - | 翌期予想営業利益（百万円） |
| `forecast_ordinary_income` | `REAL` | YES | - | 翌期予想経常利益（百万円） |
| `forecast_net_income` | `REAL` | YES | - | 翌期予想純利益（百万円） |
| `forecast_eps` | `REAL` | YES | - | 翌期予想EPS（円） |

---

### 2.11. `stock_daily_indicators`（日次銘柄指標）
銘柄別の日次騰落率情報や、大型・中型・小型といった時価総額分類を格納します。

#### スキーマ定義
| カラム名 | データ型 | NULL | キー | 説明 |
| :--- | :--- | :--- | :--- | :--- |
| `date` | `TEXT` | YES | PK (複合) | 取引日付（`YYYYMMDD`） |
| `code` | `TEXT` | YES | PK (複合) | 4桁の銘柄コード |
| `name` | `TEXT` | YES | - | 企業名 |
| `industry` | `TEXT` | YES | - | 業種分類 |
| `close` | `REAL` | YES | - | 終値 |
| `return_1d` | `REAL` | YES | - | 1日騰落率（％、前日比） |
| `return_1w` | `REAL` | YES | - | 1週間騰落率（％） |
| `return_1m` | `REAL` | YES | - | 1ヶ月騰落率（％） |
| `market_cap` | `REAL` | YES | - | 時価総額（百万円） |
| `market_cap_class` | `TEXT` | YES | - | 時価総額規模クラス（`大型`, `中型`, `小型`等） |
| `volume` | `REAL` | YES | - | 出来高 |
| *インデックス* | - | - | - | `idx_stock_indicators_date_industry` (`date`, `industry`) |

---

### 2.12. `daily_trade_indicators`（日次取引指標・テクニカル/ファンダメンタルズ）
株価のモメンタム、トレンドの傾き、複数期間移動平均、バリュエーション、信用倍率を網羅する、スクリーニング処理の要となるテーブルです。

#### スキーマ定義
| カラム名 | データ型 | NULL | キー | 説明 |
| :--- | :--- | :--- | :--- | :--- |
| `date` | `TEXT` | YES | PK (複合) | 取引日付（`YYYYMMDD`） |
| `code` | `TEXT` | YES | PK (複合) | 4桁の銘柄コード（外部キー: `companies (code)`） |
| `vwap_dev` | `REAL` | YES | - | VWAPからの乖離率（％） |
| `gap_pct` | `REAL` | YES | - | 前日終値からの窓開け（ギャップ）率（％） |
| `vol_inc_pct` | `REAL` | YES | - | 出来高前日比増加率（％） |
| `vol_ratio` | `REAL` | YES | - | 出来高移動平均比率（出来高の急増度合い） |
| `day_range_pct` | `REAL` | YES | - | 日中値幅（高値-安値）比率（％） |
| `atr_norm` | `REAL` | YES | - | 正規化されたATR（平均真の値幅率） |
| `ma5_slope` | `REAL` | YES | - | 5日移動平均線の傾き |
| `consecutive_candles` | `INTEGER` | YES | - | 連続陽線（プラス）または陰線（マイナス）日数 |
| `delta_rsi` | `REAL` | YES | - | RSIの前日差（変化幅） |
| `macd_hist` | `REAL` | YES | - | MACDヒストグラム値 |
| `dist_5d_high` | `REAL` | YES | - | 5日高値からの乖離率（％） |
| `dist_5d_low` | `REAL` | YES | - | 5日安値からの乖離率（％） |
| `n225_ma5_slope` | `REAL` | YES | - | 日経平均の5日移動平均線傾き |
| `sector_ma5_slope` | `REAL` | YES | - | 所属セクター指数の5日移動平均線傾き |
| `sector_vol_ratio` | `REAL` | YES | - | セクター全体の出来高変化比率 |
| `ma25_slope` | `REAL` | YES | - | 25日移動平均線の傾き |
| `ma75_slope` | `REAL` | YES | - | 75日移動平均線の傾き |
| `ma25_dev` | `REAL` | YES | - | 25日移動平均線からの乖離率（％） |
| `dist_20d_high` | `REAL` | YES | - | 20日高値からの乖離率（％） |
| `dist_20d_low` | `REAL` | YES | - | 20日安値からの乖離率（％） |
| `ytd_high_dist` | `REAL` | YES | - | 年初来高値からの乖離率（％） |
| `ytd_low_dist` | `REAL` | YES | - | 年初来安値からの乖離率（％） |
| `wma13_slope` | `REAL` | YES | - | 13週加重移動平均線の傾き |
| `wma26_slope` | `REAL` | YES | - | 26週加重移動平均線の傾き |
| `wma52_slope` | `REAL` | YES | - | 52週加重移動平均線の傾き |
| `wrsi14` | `REAL` | YES | - | 加重RSI（14日） |
| `market_cap` | `REAL` | YES | - | 当日の時価総額（百万円） |
| `per` | `REAL` | YES | - | 当日のPER（倍） |
| `pbr` | `REAL` | YES | - | 当日のPBR（倍） |
| `eps` | `REAL` | YES | - | 当日のEPS（円） |
| `delta_avg_per` | `REAL` | YES | - | 過去平均PERとの変化差分 |
| `delta_eps` | `REAL` | YES | - | EPS変化率（％） |
| `margin_ratio` | `REAL` | YES | - | 当週の信用倍率 |
| `margin_ratio_3m` | `REAL` | YES | - | 3ヶ月前の信用倍率 |
| `margin_ratio_rel` | `REAL` | YES | - | 過去平均対比での相対的な信用倍率推移 |
| `rsi14` | `REAL` | YES | - | RSI（14日） |
| `n225_ma25_slope` | `REAL` | YES | - | 日経平均の25日移動平均線傾き |
| `n225_ma75_slope` | `REAL` | YES | - | 日経平均の75日移動平均線傾き |
| `sector_ma25_slope` | `REAL` | YES | - | 所属セクター指数の25日移動平均線傾き |
| `sector_ma75_slope` | `REAL` | YES | - | 所属セクター指数の75日移動平均線傾き |
| `sector_cap_ma25_slope` | `REAL` | YES | - | 所属セクター時価総額加重指数の25日平均傾き |
| `sector_cap_ma75_slope` | `REAL` | YES | - | 所属セクター時価総額加重指数の75日平均傾き |
| *インデックス* | - | - | - | `idx_dti_date` (`date`) |

---

### 2.13. `daily_market_stats`（市場全体統計）
東証プライム、スタンダード、グロース、あるいは市場全体での値上がり・値下がり・変わらず銘柄数を日次で集計したテーブルです。

#### スキーマ定義
| カラム名 | データ型 | NULL | キー | 説明 |
| :--- | :--- | :--- | :--- | :--- |
| `date` | `TEXT` | YES | PK (複合) | 取引日付（`YYYYMMDD`） |
| `segment` | `TEXT` | YES | PK (複合) | 市場セグメント区分（`プライム`, `スタンダード`, `グロース`, `全体`等） |
| `advances` | `INTEGER` | YES | - | 値上がり銘柄数 |
| `declines` | `INTEGER` | YES | - | 値下がり銘柄数 |
| `unchanged` | `INTEGER` | YES | - | 変わらず（価格変化なし）銘柄数 |
| `total_count` | `INTEGER` | YES | - | 集計対象の合計銘柄数 |

---

### 2.14. `stock_eps_revisions`（EPS修正履歴テーブル）
アナリストコンセンサスや会社発表による、1株当たり純利益（EPS）の予想上方・下方修正の変更履歴を記録します。

#### スキーマ定義
| カラム名 | データ型 | NULL | キー | 説明 |
| :--- | :--- | :--- | :--- | :--- |
| `code` | `TEXT` | YES | PK (複合) | 4桁の銘柄コード |
| `revision_date` | `TEXT` | YES | PK (複合) | 修正情報が公表された日付（`YYYYMMDD`） |
| `old_eps` | `REAL` | YES | - | 修正前の予想EPS（円） |
| `new_eps` | `REAL` | YES | - | 修正後の予想EPS（円） |
| `shares_at_revision` | `REAL` | YES | - | 修正時点の発行済株式数（株） |
| `implied_ni` | `REAL` | YES | - | 予想EPSと発行済株式数から算出される暗黙の当期純利益（百万円） |
| *インデックス* | - | - | - | `idx_ser_date` (`revision_date`) |

---

### 2.15. `corporate_disclosures`（適時開示・企業発表資料）
適時開示（TDnet/EDINET）から取得された開示文書の情報および、AI（LLM）によってスコアリング・感情分析された結果を格納します。

#### スキーマ定義
| カラム名 | データ型 | NULL | キー | 説明 |
| :--- | :--- | :--- | :--- | :--- |
| `id` | `INTEGER` | YES | PK | 自動インクリメントID |
| `code` | `TEXT` | NO | - | 4桁の銘柄コード |
| `disclosure_date` | `TEXT` | NO | - | 開示公開日時 |
| `title` | `TEXT` | NO | - | 開示資料のタイトル（例: `"業績予想の修正に関するお知らせ"`） |
| `category` | `TEXT` | YES | - | 配信分類カテゴリ |
| `display_category` | `TEXT` | YES | - | フロント表示用カテゴリ |
| `score` | `REAL` | YES | - | AI総合インパクトスコア（デフォルト: 0） |
| `score_earnings` | `REAL` | YES | - | 業績に関するインパクトスコア（デフォルト: 0） |
| `score_material` | `REAL` | YES | - | 新材料・業務提携などに関するインパクトスコア（デフォルト: 0） |
| `score_cap_adj` | `REAL` | YES | - | 時価総額を加味した調整スコア（デフォルト: 1.0） |
| `buzz_score` | `REAL` | YES | - | SNSやニュース等での注目度予想スコア（デフォルト: 0） |
| `buzz_reason` | `TEXT` | YES | - | 注目度スコアの背景・理由 |
| `ai_summary` | `TEXT` | YES | - | AIによって生成された開示内容の日本語3行要約 |
| `direction` | `TEXT` | YES | - | 株価への予測影響方向（`Positive`, `Negative`, `Neutral`等） |
| `market_cap_at_disclosure` | `REAL` | YES | - | 開示時点での対象銘柄の時価総額（百万円） |
| `document_url` | `TEXT` | YES | - | 開示資料（PDF）への直リンクURL |
| `created_at` | `TIMESTAMP` | YES | - | レコード登録日時（デフォルト: 現地時間） |
| `pdf_text` | `TEXT` | YES | - | PDF内から抽出したテキストデータ（全文） |
| *ユニーク制約* | - | - | - | `(code, disclosure_date, title)` の組み合わせで重複不可 |
| *インデックス* | - | - | - | `idx_cd_date` (`disclosure_date`), `idx_cd_score` (`disclosure_date`, `score DESC`) |

---

### 2.16. `disclosure_analysis_logs`（開示分析ログ）
適時開示AI分析エンジンの日次処理実行ログを格納します。

#### スキーマ定義
| カラム名 | データ型 | NULL | キー | 説明 |
| :--- | :--- | :--- | :--- | :--- |
| `date` | `TEXT` | YES | PK | 処理実行日（`YYYYMMDD`） |
| `api_total` | `INTEGER` | YES | - | 当日TDnet等から取得した総適時開示数（デフォルト: 0） |
| `filter_excluded` | `INTEGER` | YES | - | ノイズフィルタで除外された数（デフォルト: 0） |
| `ai_targets` | `INTEGER` | YES | - | AI分析（LLM API）へ送信された対象数（デフォルト: 0） |
| `positive_count` | `INTEGER` | YES | - | ポジティブ判定された開示文書数（デフォルト: 0） |
| `ai_elapsed_sec` | `REAL` | YES | - | AIの推論・処理に要した総秒数（デフォルト: 0） |
| `total_elapsed_sec` | `REAL` | YES | - | バッチ全体の実行にかかった総秒数（デフォルト: 0） |
| `consumed_tokens` | `INTEGER` | YES | - | LLM APIで消費した累計トークン数（デフォルト: 0） |
| `created_at` | `TIMESTAMP` | YES | - | ログ生成日時（デフォルト: 現地時間） |

---

### 2.17. `daily_theme_scores`（テーマ別日次スコア蓄積テーブル）
毎日大引け後に計算される「注目テーマ」のスコア、順位、牽引銘柄などの結果を時系列で蓄積するテーブルです。

#### スキーマ定義
| カラム名 | データ型 | NULL | キー | 説明 |
| :--- | :--- | :--- | :--- | :--- |
| `date` | `TEXT` | YES | PK (複合) | 計算対象日。形式は `YYYYMMDD` （例: `"20260626"`） |
| `theme_name` | `TEXT` | YES | PK (複合) | テーマ名。`ticker_dictionary.json`に定義されるタグ（例: `"小売り"`） |
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
