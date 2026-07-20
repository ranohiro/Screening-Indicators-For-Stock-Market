"""
percentile_search.py
====================================================
TOP_PERCENTILE（大相場内エントリー正解ラベルの上位何%か）を
動的にグリッドサーチして最適値を探索するスクリプト。

データ読み込み・特徴量生成は1回だけ実行し、
ラベリング→学習→評価をパーセンタイル値ごとに繰り返す。

出力: data/exports/percentile_search_YYYYMMDD_HHMMSS/
  - search_results.csv  : 各パーセンタイルの評価結果
  - search_results.png  : ROC-AUC / Model Return の比較グラフ
  - evaluation_log.txt  : 詳細ログ
====================================================
"""

import sqlite3
import json
import pandas as pd
import numpy as np
import os
import datetime
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import seaborn as sns
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import roc_auc_score
import lightgbm as lgb
import warnings

warnings.filterwarnings('ignore')

# -------------------------------------------------------
# パス設定
# -------------------------------------------------------
DB_PATH = 'data/stock_data.db'
JSON_PATH = 'data/ticker_dictionary.json'
SCREENING_CSV_PATH = 'data/exports/screening_all_v01.csv'

# -------------------------------------------------------
# 探索するパーセンタイル候補（上位X%を正例とする）
# -------------------------------------------------------
PERCENTILE_CANDIDATES = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50]


# -------------------------------------------------------
# データ読み込み（1回だけ実行）
# -------------------------------------------------------
def load_data():
    """データベース・JSONファイル・スクリーニングCSVからデータを読み込む"""
    print("  - データベースに接続しデータを取得しています...")
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = os.path.join(root_dir, DB_PATH)

    if not os.path.exists(db_path):
        print(f"警告: データベースファイルが見つかりません: {db_path}")
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    conn = sqlite3.connect(db_path)

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
                row['core_product_tech'] = tags[0] if isinstance(tags, list) and len(tags) > 0 else 'Unknown'
                ticker_info.append(row)

    df_tickers = pd.DataFrame(ticker_info)
    if not df_tickers.empty:
        df_tickers['industry'] = df_tickers['industry'].astype('category')
        df_tickers['core_product_tech'] = df_tickers['core_product_tech'].astype('category')

    screening_path = os.path.join(root_dir, SCREENING_CSV_PATH)
    df_raw = pd.read_csv(screening_path, dtype=str)
    df_screening = df_raw[['銘柄コード', 'ゴールデンクロス(5MA>25MA)の日付', 'デッドクロス(5MA<25MA)の日付']].copy()
    df_screening.columns = ['code', 'gc_date', 'dc_date']
    df_screening['code'] = df_screening['code'].str.strip()
    df_screening['gc_date'] = pd.to_datetime(df_screening['gc_date'], format='%Y%m%d', errors='coerce')
    df_screening['dc_date'] = pd.to_datetime(df_screening['dc_date'], format='%Y%m%d', errors='coerce')
    df_screening = df_screening.dropna(subset=['gc_date', 'dc_date']).reset_index(drop=True)
    df_screening['episode_id'] = df_screening.index

    print(f"  - daily_prices: {len(df_prices)}行 / ticker_info: {len(df_tickers)}件 / エピソード: {len(df_screening)}件")
    return df_prices, df_tickers, df_screening


# -------------------------------------------------------
# 特徴量生成（1回だけ実行）
# -------------------------------------------------------
def create_features(df_prices, df_tickers):
    print("  - 特徴量を動的生成しています...")
    df = df_prices.copy()

    df_reset = df.reset_index()
    for col in ['open', 'high', 'low', 'close']:
        if col in df_reset.columns:
            df_reset[col] = df_reset.groupby('code')[col].ffill().bfill()
    df_reset['volume'] = df_reset['volume'].fillna(0)
    df = df_reset.set_index(['code', 'date'])

    grouped = df.groupby(level='code')
    ma5 = grouped['close'].transform(lambda x: x.rolling(5, min_periods=1).mean())
    ma25 = grouped['close'].transform(lambda x: x.rolling(25, min_periods=1).mean())
    df['ma5'] = ma5
    df['ma25'] = ma25
    df['ma5_slope'] = df['ma5'] / grouped['ma5'].shift(1) - 1
    df['ma25_slope'] = df['ma25'] / grouped['ma25'].shift(1) - 1
    df.drop(columns=['ma5', 'ma25'], inplace=True)

    vol_ma5 = grouped['volume'].transform(lambda x: x.rolling(5, min_periods=1).mean())
    vol_ma20 = grouped['volume'].transform(lambda x: x.rolling(20, min_periods=1).mean())
    epsilon = 1e-9
    df['vol_ratio'] = df['volume'] / (vol_ma20 + epsilon)
    df['vol_ratio_5d'] = vol_ma5 / (vol_ma20 + epsilon)

    if not df_tickers.empty:
        df = df.reset_index()
        df = pd.merge(df, df_tickers, on='code', how='left')
        for cat_col in ['industry', 'core_product_tech']:
            if cat_col in df.columns:
                if 'Unknown' not in df[cat_col].cat.categories:
                    df[cat_col] = df[cat_col].cat.add_categories('Unknown')
                df[cat_col] = df[cat_col].fillna('Unknown')

        print("  - テーマ別モメンタム特徴量を算出しています...")
        if 'core_product_tech' in df.columns:
            theme_avg = df.groupby(['date', 'core_product_tech'], observed=True)[['ma5_slope', 'vol_ratio']].mean().reset_index()
            theme_avg.rename(columns={'ma5_slope': 'theme_ma5_slope_avg', 'vol_ratio': 'theme_vol_ratio_avg'}, inplace=True)
            df = pd.merge(df, theme_avg, on=['date', 'core_product_tech'], how='left')
        df = df.set_index(['code', 'date'])

    subset_cols = ['ma5_slope', 'ma25_slope', 'vol_ratio', 'vol_ratio_5d']
    if 'theme_ma5_slope_avg' in df.columns:
        subset_cols.extend(['theme_ma5_slope_avg', 'theme_vol_ratio_avg'])
    df = df.dropna(subset=subset_cols)

    print(f"  - 特徴量生成完了: {len(df)}行")
    return df


# -------------------------------------------------------
# ユニバース構築 + ラベリング（パーセンタイルを引数で指定）
# -------------------------------------------------------
def construct_universe(df_features, df_screening, top_percentile):
    df_features_reset = df_features.reset_index()
    episode_records = []

    for _, ep in df_screening.iterrows():
        code, gc_date, dc_date, episode_id = ep['code'], ep['gc_date'], ep['dc_date'], ep['episode_id']

        mask = (
            (df_features_reset['code'] == code) &
            (df_features_reset['date'] >= gc_date) &
            (df_features_reset['date'] <= dc_date)
        )
        ep_df = df_features_reset[mask].copy()
        if len(ep_df) < 3:
            continue

        dc_row = ep_df[ep_df['date'] == dc_date]
        dc_price = dc_row.iloc[0]['close'] if not dc_row.empty else ep_df.iloc[-1]['close']
        dc_date_actual = dc_date if not dc_row.empty else ep_df.iloc[-1]['date']

        if pd.isna(dc_price) or dc_price <= 0:
            continue

        ep_df['target_score'] = np.where(
            ep_df['close'] > 0,
            (dc_price - ep_df['close']) / ep_df['close'],
            np.nan
        )
        ep_df = ep_df.dropna(subset=['target_score'])
        if len(ep_df) < 3:
            continue

        ep_df['target_score'] = ep_df['target_score'].clip(lower=-0.5, upper=2.0)
        threshold = ep_df['target_score'].quantile(1.0 - top_percentile)
        ep_df['is_top_entry'] = (ep_df['target_score'] >= threshold).astype(int)
        ep_df['episode_id'] = episode_id
        ep_df['dc_date'] = dc_date_actual
        ep_df['dc_price'] = dc_price
        ep_df = ep_df.sort_values('date')
        ep_df['days_since_gc'] = range(len(ep_df))
        episode_records.append(ep_df)

    if not episode_records:
        return pd.DataFrame()

    df_universe = pd.concat(episode_records, ignore_index=True)
    df_universe.set_index(['code', 'date'], inplace=True)
    return df_universe


# -------------------------------------------------------
# 学習・評価（パーセンタイルを変えながら呼び出す）
# -------------------------------------------------------
def run_single_trial(df_model, top_percentile, log_print):
    features = ['ma5_slope', 'ma25_slope', 'vol_ratio', 'vol_ratio_5d', 'days_since_gc']
    if 'theme_ma5_slope_avg' in df_model.columns:
        features.extend(['theme_ma5_slope_avg', 'theme_vol_ratio_avg'])
    if 'industry' in df_model.columns:
        features.append('industry')

    df = df_model.reset_index().sort_values('date')
    n_splits = min(3, len(df) // 2)
    tscv = TimeSeriesSplit(n_splits=max(n_splits, 2))

    fold_results = []

    for fold, (train_idx, test_idx) in enumerate(tscv.split(df), 1):
        train_df = df.iloc[train_idx].copy()
        test_df = df.iloc[test_idx].copy()

        test_start_date = test_df['date'].min()
        train_df = train_df[train_df['dc_date'] < test_start_date]

        if len(train_df) == 0:
            continue

        X_train = train_df[features]
        y_train = train_df['is_top_entry']
        X_test = test_df[features]
        y_test = test_df['is_top_entry']

        if y_train.nunique() < 2 or y_test.nunique() < 2:
            continue

        model = lgb.LGBMClassifier(
            n_estimators=100, learning_rate=0.05,
            random_state=42, n_jobs=-1, class_weight='balanced'
        )
        model.fit(X_train, y_train)
        preds_proba = model.predict_proba(X_test)[:, 1]

        try:
            roc_auc = roc_auc_score(y_test, preds_proba)
        except Exception:
            roc_auc = float('nan')

        test_df = test_df.copy()
        test_df['pred_proba'] = preds_proba
        pred_threshold = test_df['pred_proba'].quantile(0.8)
        selected_df = test_df[test_df['pred_proba'] >= pred_threshold]

        baseline_return = test_df['target_score'].mean()
        model_return = selected_df['target_score'].mean() if not selected_df.empty else 0.0
        model_precision = selected_df['is_top_entry'].mean() if not selected_df.empty else 0.0

        fold_results.append({
            'fold': fold,
            'roc_auc': roc_auc,
            'baseline_return': baseline_return,
            'model_return': model_return,
            'model_precision': model_precision,
        })
        log_print(f"    Fold {fold}: ROC-AUC={roc_auc:.4f} | Baseline={baseline_return:.4f} | Model={model_return:.4f} | Precision={model_precision:.2%}")

    if not fold_results:
        return None

    avg = {
        'top_percentile': top_percentile,
        'avg_roc_auc': np.nanmean([r['roc_auc'] for r in fold_results]),
        'avg_baseline_return': np.mean([r['baseline_return'] for r in fold_results]),
        'avg_model_return': np.mean([r['model_return'] for r in fold_results]),
        'avg_model_precision': np.mean([r['model_precision'] for r in fold_results]),
    }
    avg['avg_advantage'] = avg['avg_model_return'] - avg['avg_baseline_return']
    return avg


# -------------------------------------------------------
# メイン: グリッドサーチ
# -------------------------------------------------------
def main():
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    export_dir = os.path.join(root_dir, 'data', 'exports', f"{timestamp}_percentile_search")
    os.makedirs(export_dir, exist_ok=True)

    log_file_path = os.path.join(export_dir, 'evaluation_log.txt')
    def log_print(msg):
        print(msg)
        with open(log_file_path, 'a', encoding='utf-8') as f:
            f.write(msg + '\n')

    log_print("=" * 60)
    log_print("TOP_PERCENTILE グリッドサーチ")
    log_print(f"探索候補: {[f'{p:.0%}' for p in PERCENTILE_CANDIDATES]}")
    log_print("=" * 60)

    # --- Step 1 & 2: データ読み込みと特徴量生成（1回だけ）---
    print("\nStep 1: データ読み込み...")
    df_prices, df_tickers, df_screening = load_data()
    if df_prices.empty or df_screening.empty:
        print("データが空のため終了します。")
        return

    print("\nStep 2: 特徴量生成（全候補で共通）...")
    df_features_all = create_features(df_prices, df_tickers)

    # --- Step 3: パーセンタイルを変えながらループ ---
    results = []
    for pct in PERCENTILE_CANDIDATES:
        log_print(f"\n{'─' * 50}")
        log_print(f"▶ TOP_PERCENTILE = {pct:.0%}")
        log_print(f"{'─' * 50}")

        df_universe = construct_universe(df_features_all, df_screening, pct)
        if df_universe.empty:
            log_print("  有効データなし。スキップ。")
            continue

        actual_pos_rate = df_universe['is_top_entry'].mean()
        log_print(f"  ユニバース: {len(df_universe)}行 | 実際の正例率: {actual_pos_rate:.2%}")

        result = run_single_trial(df_universe, pct, log_print)
        if result:
            results.append(result)
            log_print(f"  → 平均 ROC-AUC: {result['avg_roc_auc']:.4f} | 優位性: {result['avg_advantage']:.4f} | Model Return: {result['avg_model_return']:.4f}")

    # --- Step 4: 結果集計と出力 ---
    if not results:
        log_print("有効な結果がありませんでした。")
        return

    df_results = pd.DataFrame(results)
    df_results['top_percentile_label'] = df_results['top_percentile'].apply(lambda x: f"{x:.0%}")

    csv_path = os.path.join(export_dir, 'search_results.csv')
    df_results.to_csv(csv_path, index=False, encoding='utf-8-sig')

    log_print("\n" + "=" * 60)
    log_print("【グリッドサーチ結果サマリー】")
    log_print("=" * 60)
    log_print(df_results[['top_percentile_label', 'avg_roc_auc', 'avg_model_return', 'avg_advantage', 'avg_model_precision']].to_string(index=False))

    best_by_roc = df_results.loc[df_results['avg_roc_auc'].idxmax()]
    best_by_ret = df_results.loc[df_results['avg_model_return'].idxmax()]
    best_by_adv = df_results.loc[df_results['avg_advantage'].idxmax()]

    log_print(f"\n🏆 ROC-AUC 最大     : TOP_PERCENTILE = {best_by_roc['top_percentile_label']} (AUC={best_by_roc['avg_roc_auc']:.4f})")
    log_print(f"🏆 Model Return 最大: TOP_PERCENTILE = {best_by_ret['top_percentile_label']} (Return={best_by_ret['avg_model_return']:.4f})")
    log_print(f"🏆 優位性 最大      : TOP_PERCENTILE = {best_by_adv['top_percentile_label']} (Advantage={best_by_adv['avg_advantage']:.4f})")

    # --- Step 5: 可視化 ---
    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    fig.suptitle('TOP_PERCENTILE Grid Search Results', fontsize=14, fontweight='bold')

    metrics = [
        ('avg_roc_auc', 'ROC-AUC', 'steelblue'),
        ('avg_model_return', 'Model Return', 'seagreen'),
        ('avg_advantage', 'Advantage (Model - Baseline)', 'darkorange'),
    ]
    for ax, (col, label, color) in zip(axes, metrics):
        ax.plot(df_results['top_percentile_label'], df_results[col],
                marker='o', linewidth=2, color=color, markersize=8)
        best_idx = df_results[col].idxmax()
        ax.axvline(x=df_results.loc[best_idx, 'top_percentile_label'],
                   color='red', linestyle='--', alpha=0.5, label='Best')
        ax.set_title(label)
        ax.set_xlabel('TOP_PERCENTILE')
        ax.set_ylabel(label)
        ax.grid(True, alpha=0.3)
        ax.legend()

    plt.tight_layout()
    plot_path = os.path.join(export_dir, 'search_results.png')
    plt.savefig(plot_path, dpi=150, bbox_inches='tight')
    plt.close()

    log_print(f"\n結果グラフを保存しました: {plot_path}")
    log_print(f"結果CSVを保存しました: {csv_path}")
    log_print(f"出力ディレクトリ: {export_dir}")
    print("\nグリッドサーチ完了！")


if __name__ == "__main__":
    main()
