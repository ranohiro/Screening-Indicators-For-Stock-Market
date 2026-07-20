import sqlite3
import json
import pandas as pd
import numpy as np
import os
import datetime
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import roc_auc_score
import lightgbm as lgb
import shap
import warnings

warnings.filterwarnings('ignore')

# データベース・ファイル設定
DB_PATH = 'data/stock_data.db'
JSON_PATH = 'data/ticker_dictionary.json'
SCREENING_CSV_PATH = 'data/exports/screening_all_v01.csv'

# ラベリング設定: 1大相場エピソード内の上位何%を正例とするか
TOP_PERCENTILE = 0.35  # 上位35%を正例(1)、下位65%を負例(0)
# ※ グリッドサーチ(10%〜50%)の結果、ROC-AUCが最大(0.8996)となる35%を採用（2026-07-20）

def load_data():
    """データベース・JSONファイル・スクリーニングCSVからデータを読み込む"""
    print("  - データベースに接続しデータを取得しています...")
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = os.path.join(root_dir, DB_PATH)

    if not os.path.exists(db_path):
        print(f"警告: データベースファイルが見つかりません: {db_path}")
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    conn = sqlite3.connect(db_path)

    # daily_prices の取得
    query_prices = """
    SELECT code, date, open, high, low, close, volume
    FROM daily_prices
    ORDER BY code, date
    """
    df_prices = pd.read_sql_query(query_prices, conn)
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df_prices[col] = pd.to_numeric(df_prices[col], errors='coerce')
    df_prices['date'] = pd.to_datetime(df_prices['date'], format='%Y%m%d', errors='coerce')
    df_prices.set_index(['code', 'date'], inplace=True)
    df_prices.sort_index(inplace=True)

    conn.close()

    # ticker_dictionary.json の読み込み
    json_path = os.path.join(root_dir, JSON_PATH)
    ticker_info = []
    if os.path.exists(json_path):
        with open(json_path, 'r', encoding='utf-8') as f:
            ticker_dict = json.load(f)
            for info in ticker_dict:
                code = info.get('code')
                row = {
                    'code': str(code) if code is not None else '',
                    'industry': info.get('industry', 'Unknown')
                }
                tags = info.get('layered_tags', {}).get('Core_Product_Tech', [])
                if isinstance(tags, list) and len(tags) > 0:
                    row['core_product_tech'] = tags[0]
                else:
                    row['core_product_tech'] = 'Unknown'
                ticker_info.append(row)
    else:
        print(f"警告: 銘柄辞書ファイルが見つかりません: {json_path}")

    df_tickers = pd.DataFrame(ticker_info)
    if not df_tickers.empty:
        df_tickers['industry'] = df_tickers['industry'].astype('category')
        df_tickers['core_product_tech'] = df_tickers['core_product_tech'].astype('category')

    # スクリーニングCSV の読み込み
    screening_path = os.path.join(root_dir, SCREENING_CSV_PATH)
    if not os.path.exists(screening_path):
        print(f"警告: スクリーニングCSVが見つかりません: {screening_path}")
        return df_prices, df_tickers, pd.DataFrame()

    df_raw = pd.read_csv(screening_path, dtype=str)

    # 必要列のみ抽出・型変換
    df_screening = df_raw[['銘柄コード', 'ゴールデンクロス(5MA>25MA)の日付', 'デッドクロス(5MA<25MA)の日付']].copy()
    df_screening.columns = ['code', 'gc_date', 'dc_date']

    # 銘柄コード: 文字列のまま保持（DBと合わせるため先頭ゼロも維持）
    df_screening['code'] = df_screening['code'].str.strip()

    df_screening['gc_date'] = pd.to_datetime(df_screening['gc_date'], format='%Y%m%d', errors='coerce')
    df_screening['dc_date'] = pd.to_datetime(df_screening['dc_date'], format='%Y%m%d', errors='coerce')
    df_screening = df_screening.dropna(subset=['gc_date', 'dc_date']).reset_index(drop=True)

    # エピソードIDを付与（後のグルーピングで使用）
    df_screening['episode_id'] = df_screening.index

    print(f"  - daily_prices: {len(df_prices)}行")
    print(f"  - ticker_info: {len(df_tickers)}行")
    print(f"  - 大相場エピソード数 (screening_all_v01): {len(df_screening)}件")

    return df_prices, df_tickers, df_screening


def create_features(df_prices, df_tickers):
    """特徴量を動的生成する（全データ保持状態で行う）"""
    print("  - 特徴量を動的生成しています...")
    df = df_prices.copy()

    # インデックスをリセットして欠損値を補完
    df_reset = df.reset_index()
    price_cols = ['open', 'high', 'low', 'close']
    for col in price_cols:
        if col in df_reset.columns:
            df_reset[col] = df_reset.groupby('code')[col].ffill().bfill()
    if 'volume' in df_reset.columns:
        df_reset['volume'] = df_reset['volume'].fillna(0)
    df = df_reset.set_index(['code', 'date'])

    # 銘柄ごとの処理
    grouped = df.groupby(level='code')

    # 移動平均の計算
    ma5 = grouped['close'].transform(lambda x: x.rolling(window=5, min_periods=1).mean())
    ma25 = grouped['close'].transform(lambda x: x.rolling(window=25, min_periods=1).mean())

    df['ma5'] = ma5
    df['ma25'] = ma25

    # 特徴量: ma5_slope, ma25_slope (前日比変化率)
    df['ma5_slope'] = df['ma5'] / grouped['ma5'].shift(1) - 1
    df['ma25_slope'] = df['ma25'] / grouped['ma25'].shift(1) - 1

    # 計算用一時カラム削除
    df.drop(columns=['ma5', 'ma25'], inplace=True)

    # 出来高の移動平均
    vol_ma5 = grouped['volume'].transform(lambda x: x.rolling(window=5, min_periods=1).mean())
    vol_ma20 = grouped['volume'].transform(lambda x: x.rolling(window=20, min_periods=1).mean())

    # 特徴量: vol_ratio, vol_ratio_5d
    epsilon = 1e-9
    df['vol_ratio'] = df['volume'] / (vol_ma20 + epsilon)
    df['vol_ratio_5d'] = vol_ma5 / (vol_ma20 + epsilon)

    # df_tickers の結合 (industry, core_product_tech)
    if not df_tickers.empty:
        df = df.reset_index()
        df = pd.merge(df, df_tickers, on='code', how='left')

        if 'industry' in df.columns:
            if 'Unknown' not in df['industry'].cat.categories:
                df['industry'] = df['industry'].cat.add_categories('Unknown')
            df['industry'] = df['industry'].fillna('Unknown')

        if 'core_product_tech' in df.columns:
            if 'Unknown' not in df['core_product_tech'].cat.categories:
                df['core_product_tech'] = df['core_product_tech'].cat.add_categories('Unknown')
            df['core_product_tech'] = df['core_product_tech'].fillna('Unknown')

        # テーマ別地合い特徴量の算出
        print("  - テーマ別(Core_Product_Tech)のモメンタム特徴量を算出しています...")
        if 'core_product_tech' in df.columns:
            theme_grouped = df.groupby(['date', 'core_product_tech'], observed=True)
            theme_avg = theme_grouped[['ma5_slope', 'vol_ratio']].mean().reset_index()
            theme_avg.rename(columns={
                'ma5_slope': 'theme_ma5_slope_avg',
                'vol_ratio': 'theme_vol_ratio_avg'
            }, inplace=True)
            df = pd.merge(df, theme_avg, on=['date', 'core_product_tech'], how='left')

        df = df.set_index(['code', 'date'])

    # 初期期間など計算不能なNaNを除外
    subset_cols = ['ma5_slope', 'ma25_slope', 'vol_ratio', 'vol_ratio_5d']
    if 'theme_ma5_slope_avg' in df.columns:
        subset_cols.extend(['theme_ma5_slope_avg', 'theme_vol_ratio_avg'])
    df = df.dropna(subset=subset_cols)

    print(f"  - 特徴量結合後のサンプル数: {len(df)}行")
    return df


def construct_universe_with_screening_labels(df_features, df_screening):
    """
    screening_all_v01.csv に記録された大相場エピソードを対象に、
    1エピソードごとに独立して target_score を計算し上位30%を正例(1)とするラベリングを行う。
    """
    print("  - 大相場エピソードをユニバース化し、ラベリングを行っています...")

    df_features_reset = df_features.reset_index()

    episode_records = []
    skipped_count = 0
    valid_count = 0

    for _, ep in df_screening.iterrows():
        code = ep['code']
        gc_date = ep['gc_date']
        dc_date = ep['dc_date']
        episode_id = ep['episode_id']

        # 該当銘柄の期間データを抽出
        mask = (
            (df_features_reset['code'] == code) &
            (df_features_reset['date'] >= gc_date) &
            (df_features_reset['date'] <= dc_date)
        )
        ep_df = df_features_reset[mask].copy()

        if len(ep_df) < 3:
            # 有効データが少なすぎるエピソードはスキップ
            skipped_count += 1
            continue

        # DC日の終値をDBから取得（ep_dfのdc_date行から取得）
        dc_row = ep_df[ep_df['date'] == dc_date]
        if dc_row.empty:
            # DC日の終値が直接取得できない場合は最終日を代替に使用
            dc_price = ep_df.iloc[-1]['close']
            dc_date_actual = ep_df.iloc[-1]['date']
        else:
            dc_price = dc_row.iloc[0]['close']
            dc_date_actual = dc_date

        if pd.isna(dc_price) or dc_price <= 0:
            skipped_count += 1
            continue

        # target_score の計算: (DC日終値 - 当日終値) / 当日終値
        ep_df['target_score'] = np.where(
            ep_df['close'] > 0,
            (dc_price - ep_df['close']) / ep_df['close'],
            np.nan
        )
        ep_df = ep_df.dropna(subset=['target_score'])
        if len(ep_df) < 3:
            skipped_count += 1
            continue

        # target_score を外れ値クリッピング
        ep_df['target_score'] = ep_df['target_score'].clip(lower=-0.5, upper=2.0)

        # 1エピソード内での上位30%を正例(1)、下位70%を負例(0) としてラベリング
        threshold = ep_df['target_score'].quantile(1.0 - TOP_PERCENTILE)
        ep_df['is_top_entry'] = (ep_df['target_score'] >= threshold).astype(int)

        # エピソード情報の追加
        ep_df['episode_id'] = episode_id
        ep_df['dc_date'] = dc_date_actual
        ep_df['dc_price'] = dc_price

        # GC開始日からの経過日数を計算
        ep_df = ep_df.sort_values('date')
        ep_df['days_since_gc'] = range(len(ep_df))

        episode_records.append(ep_df)
        valid_count += 1

    if not episode_records:
        print("  - 有効な大相場エピソードが見つかりませんでした。")
        return pd.DataFrame()

    df_universe = pd.concat(episode_records, ignore_index=True)
    df_universe.set_index(['code', 'date'], inplace=True)

    positive_rate = df_universe['is_top_entry'].mean()
    print(f"  - 有効エピソード数: {valid_count}件 (スキップ: {skipped_count}件)")
    print(f"  - ユニバース総サンプル数: {len(df_universe)}行")
    print(f"  - 正例率 (is_top_entry=1): {positive_rate:.2%} (目標: {TOP_PERCENTILE:.0%})")

    return df_universe


def train_and_evaluate(df_model):
    """ウォークフォワード検証でモデルの学習と評価を行う"""
    print("  - モデルの学習と評価を開始します (ウォークフォワード検証)")

    # タイムスタンプ付きの出力ディレクトリ作成
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    output_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    export_dir = os.path.join(output_dir, 'data', 'exports', f"{timestamp}_momentum_screening_model_outputs")
    os.makedirs(export_dir, exist_ok=True)

    log_file_path = os.path.join(export_dir, 'evaluation_log.txt')
    def log_print(msg):
        print(msg)
        with open(log_file_path, 'a', encoding='utf-8') as f:
            f.write(msg + '\n')

    log_print(f"出力ディレクトリを作成しました: {export_dir}")
    log_print(f"ユニバース総サンプル数: {len(df_model)}行")
    log_print(f"ラベリング設定: 1大相場ごとの上位{TOP_PERCENTILE:.0%}を正例(1)")

    # 特徴量リスト (momentum_model.py と共通)
    features = [
        'ma5_slope', 'ma25_slope', 'vol_ratio', 'vol_ratio_5d', 'days_since_gc'
    ]
    if 'theme_ma5_slope_avg' in df_model.columns:
        features.extend(['theme_ma5_slope_avg', 'theme_vol_ratio_avg'])
    if 'industry' in df_model.columns:
        features.append('industry')

    target_label = 'is_top_entry'
    target_raw = 'target_score'

    # インデックスをリセットして日付順にソート
    df = df_model.reset_index().sort_values('date')

    if len(df) < 10:
        log_print(f"データが少なすぎます ({len(df)}行)。学習をスキップします。")
        return

    # TimeSeriesSplit の設定
    n_splits = min(3, len(df) // 2)
    if n_splits < 2:
        n_splits = 2
    tscv = TimeSeriesSplit(n_splits=n_splits)

    feature_importances = []
    baseline_returns = []
    model_returns = []
    roc_aucs = []

    fold = 1
    last_model = None
    last_X_test = None

    for train_idx, test_idx in tscv.split(df):
        train_df = df.iloc[train_idx].copy()
        test_df = df.iloc[test_idx].copy()

        # --- データリーク防止処理 ---
        test_start_date = test_df['date'].min()
        valid_train_mask = train_df['dc_date'] < test_start_date
        train_df = train_df[valid_train_mask]

        if len(train_df) == 0:
            log_print(f"Fold {fold}: 有効な学習データがありません。スキップします。")
            fold += 1
            continue

        # 学習・テストデータの準備
        X_train = train_df[features]
        y_train = train_df[target_label]
        X_test = test_df[features]
        y_test = test_df[target_label]

        # 正例・負例の両方が存在するか確認
        if y_train.nunique() < 2 or y_test.nunique() < 2:
            log_print(f"Fold {fold}: 学習またはテストデータに正例・負例の両方が存在しません。スキップします。")
            fold += 1
            continue

        # LightGBM Classifier の定義
        model = lgb.LGBMClassifier(
            n_estimators=100,
            learning_rate=0.05,
            random_state=42,
            n_jobs=-1,
            class_weight='balanced'
        )
        model.fit(X_train, y_train)

        # 予測確率の取得 (クラス '1': 上位エントリーである確率)
        preds_proba = model.predict_proba(X_test)[:, 1]
        test_df['pred_proba'] = preds_proba

        # ROC-AUC の計算
        try:
            roc_auc = roc_auc_score(y_test, preds_proba)
            roc_aucs.append(roc_auc)
        except Exception:
            roc_auc = float('nan')

        # 評価: 予測確率の上位20%のサンプルを選択
        pred_threshold = test_df['pred_proba'].quantile(0.8)
        selected_df = test_df[test_df['pred_proba'] >= pred_threshold]

        # ベースラインリターン (全テストサンプルの平均 target_score)
        baseline_return = test_df[target_raw].mean()
        # モデルリターン (選択サンプルの平均 target_score)
        model_return = selected_df[target_raw].mean() if not selected_df.empty else 0.0
        # 選択サンプル内の大相場命中率 (is_top_entry=1 の割合)
        model_precision = selected_df[target_label].mean() if not selected_df.empty else 0.0
        baseline_precision = test_df[target_label].mean()

        baseline_returns.append(baseline_return)
        model_returns.append(model_return)

        # 特徴量重要度の保存
        importance = pd.DataFrame({
            'feature': features,
            'importance': model.feature_importances_,
            'fold': fold
        })
        feature_importances.append(importance)
        last_model = model
        last_X_test = X_test

        log_print(f"\nFold {fold}:")
        log_print(f"  - Train={len(train_df)}行, Test={len(test_df)}行")
        log_print(f"  - Train正例率={y_train.mean():.2%}, Test正例率={y_test.mean():.2%}")
        log_print(f"  - ROC-AUC={roc_auc:.4f}")
        log_print(f"  - 選択されたサンプル数={len(selected_df)}行")
        log_print(f"  - Baseline Return: {baseline_return:.4f} | Model Return: {model_return:.4f}")
        log_print(f"  - Baseline 命中率: {baseline_precision:.2%} | Model 命中率(Precision): {model_precision:.2%}")
        fold += 1

    # --- 結果の集計と出力 ---
    if feature_importances:
        avg_baseline = np.mean(baseline_returns)
        avg_model = np.mean(model_returns)
        avg_roc_auc = np.nanmean(roc_aucs) if roc_aucs else float('nan')

        log_print("\n" + "=" * 60)
        log_print("【最終評価結果】")
        log_print("=" * 60)
        log_print(f"ランダム選択リターン (Baseline): {avg_baseline:.4f}")
        log_print(f"モデル選択リターン   (Model)  : {avg_model:.4f}")
        log_print(f"優位性 (Model - Baseline)     : {avg_model - avg_baseline:.4f}")
        log_print(f"平均 ROC-AUC                  : {avg_roc_auc:.4f}")

        df_imp = pd.concat(feature_importances)
        avg_imp = df_imp.groupby('feature')['importance'].mean().sort_values(ascending=False).reset_index()

        log_print("\n【特徴量重要度 (Feature Importance)】")
        for i, row in avg_imp.iterrows():
            log_print(f"{i+1}. {row['feature']}: {row['importance']:.2f}")

        # 特徴量重要度グラフの保存
        plt.figure(figsize=(10, 6))
        sns.barplot(x='importance', y='feature', data=avg_imp)
        plt.title('Feature Importance (LightGBM - Big Move Entry Timing)')
        plt.tight_layout()
        plot_path = os.path.join(export_dir, 'feature_importance.png')
        plt.savefig(plot_path)
        plt.close()
        log_print(f"\n特徴量重要度のグラフを保存しました: {plot_path}")

        # モデルの保存とSHAP値の出力（最後のFoldのモデルを使用）
        if last_model is not None and last_X_test is not None:
            model_path = os.path.join(export_dir, 'momentum_model_screening_labels.txt')
            last_model.booster_.save_model(model_path)
            log_print(f"モデルを保存しました: {model_path}")

            log_print("テストデータに対するSHAP値を計算しています...")
            try:
                explainer = shap.TreeExplainer(last_model)
                shap_values = explainer.shap_values(last_X_test)

                if isinstance(shap_values, list):
                    shap_values = shap_values[1]

                plt.figure(figsize=(10, 6))
                shap.summary_plot(shap_values, last_X_test, show=False)
                shap_plot_path = os.path.join(export_dir, 'shap_summary_plot.png')
                plt.savefig(shap_plot_path, bbox_inches='tight')
                plt.close()
                log_print(f"SHAP summary plotを保存しました: {shap_plot_path}")
            except Exception as e:
                log_print(f"SHAPの計算中にエラーが発生しました: {e}")
    else:
        log_print("評価可能なFoldがありませんでした。")


def main():
    print("【大相場エントリー確率予測モデル】構築を開始します...")
    print(f"  使用ラベル: screening_all_v01.csv (大相場エピソードごとに上位{TOP_PERCENTILE:.0%}を正解)")

    # 1. データの読み込み
    print("\nステップ1: データの読み込み...")
    df_prices, df_tickers, df_screening = load_data()

    if df_prices.empty:
        print("株価データが空のため、処理を終了します。")
        return
    if df_screening.empty:
        print("スクリーニングCSVが空のため、処理を終了します。")
        return

    # 2. 特徴量生成（全データがある状態で行う）
    print("\nステップ2: 特徴量の動的生成...")
    df_features_all = create_features(df_prices, df_tickers)

    # 3. ユニバース構築とラベリング
    print("\nステップ3: 大相場エピソードのユニバース化とラベリング...")
    df_universe = construct_universe_with_screening_labels(df_features_all, df_screening)

    if df_universe.empty:
        print("有効なユニバースデータがありません。処理を終了します。")
        return

    # 4 & 5. モデル学習と評価
    print("\nステップ4&5: モデル学習と評価...")
    train_and_evaluate(df_universe)

    print("\n完了しました。")


if __name__ == "__main__":
    main()
