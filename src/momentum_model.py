import sqlite3
import json
import pandas as pd
import numpy as np
import os
import datetime
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.model_selection import TimeSeriesSplit
import lightgbm as lgb
import shap
import warnings

warnings.filterwarnings('ignore')

# データベース・ファイル設定
# srcから見た相対パスに修正
DB_PATH = 'data/stock_data.db'
JSON_PATH = 'data/ticker_dictionary.json'

def load_data():
    """データベースから必要なデータを読み込む"""
    print("  - データベースに接続しデータを取得しています...")
    # プロジェクトルートからのパスを想定
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = os.path.join(root_dir, DB_PATH)

    if not os.path.exists(db_path):
        print(f"警告: データベースファイルが見つかりません: {db_path}")
        return pd.DataFrame(), pd.DataFrame()

    conn = sqlite3.connect(db_path)

    # daily_pricesの取得
    query_prices = """
    SELECT code, date, open, high, low, close, volume
    FROM daily_prices
    ORDER BY code, date
    """
    df_prices = pd.read_sql_query(query_prices, conn)
    df_prices['date'] = pd.to_datetime(df_prices['date'], format='%Y%m%d', errors='coerce')
    df_prices.set_index(['code', 'date'], inplace=True)
    df_prices.sort_index(inplace=True)

    conn.close()

    # ticker_dictionary.json の読み込み
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    json_path = os.path.join(root_dir, JSON_PATH)

    ticker_info = []
    if os.path.exists(json_path):
        with open(json_path, 'r', encoding='utf-8') as f:
            ticker_dict = json.load(f)
            # 各銘柄情報を平坦化してリストに格納
            for code, info in ticker_dict.items():
                row = {'code': str(code), 'industry': info.get('industry', 'Unknown')}

                # Core_Product_Techタグの抽出
                tags = info.get('layered_tags', {}).get('Core_Product_Tech', [])
                if isinstance(tags, list) and len(tags) > 0:
                    row['core_product_tech'] = tags[0] # 簡易的に最初のタグを使用
                else:
                    row['core_product_tech'] = 'Unknown'

                ticker_info.append(row)
    else:
        print(f"警告: 銘柄辞書ファイルが見つかりません: {json_path}")

    df_tickers = pd.DataFrame(ticker_info)
    if not df_tickers.empty:
        df_tickers['industry'] = df_tickers['industry'].astype('category')
        df_tickers['core_product_tech'] = df_tickers['core_product_tech'].astype('category')

    print(f"  - daily_prices: {len(df_prices)}行")
    print(f"  - ticker_info: {len(df_tickers)}行")

    return df_prices, df_tickers

def create_features(df_prices, df_tickers):
    """特徴量を動的生成する（全データ保持状態で行う）"""
    print("  - 特徴量を動的生成しています...")
    df = df_prices.copy()

    # 銘柄ごとの処理
    grouped = df.groupby(level='code')

    # MAの計算 (transformを使用してMultiIndexを維持したまま計算)
    ma5 = grouped['close'].transform(lambda x: x.rolling(window=5, min_periods=1).mean())
    ma25 = grouped['close'].transform(lambda x: x.rolling(window=25, min_periods=1).mean())

    # 特徴量: ma5_slope, ma25_slope (前日比)
    # dfに直接追加してからgroupbyでシフトする方が安全
    df['ma5'] = ma5
    df['ma25'] = ma25

    df['ma5_slope'] = df['ma5'] / grouped['ma5'].shift(1) - 1
    df['ma25_slope'] = df['ma25'] / grouped['ma25'].shift(1) - 1

    # 計算用の一時カラムは削除
    df.drop(columns=['ma5', 'ma25'], inplace=True)

    # 出来高の移動平均
    vol_ma5 = grouped['volume'].transform(lambda x: x.rolling(window=5, min_periods=1).mean())
    vol_ma20 = grouped['volume'].transform(lambda x: x.rolling(window=20, min_periods=1).mean())

    # 特徴量: vol_ratio, vol_ratio_5d
    # 0割りを防ぐため、分母に微小値を足す
    epsilon = 1e-9
    df['vol_ratio'] = df['volume'] / (vol_ma20 + epsilon)
    df['vol_ratio_5d'] = vol_ma5 / (vol_ma20 + epsilon)

    # df_tickers の結合 (industry, core_product_tech等)
    if not df_tickers.empty:
        # code列を軸に結合するためにインデックスをリセット
        df = df.reset_index()
        df = pd.merge(df, df_tickers, on='code', how='left')

        # 欠損したカテゴリカル変数を 'Unknown' にする
        if 'industry' in df.columns:
            if 'Unknown' not in df['industry'].cat.categories:
                df['industry'] = df['industry'].cat.add_categories('Unknown')
            df['industry'] = df['industry'].fillna('Unknown')

        if 'core_product_tech' in df.columns:
            if 'Unknown' not in df['core_product_tech'].cat.categories:
                df['core_product_tech'] = df['core_product_tech'].cat.add_categories('Unknown')
            df['core_product_tech'] = df['core_product_tech'].fillna('Unknown')

        # テーマ（地合い）特徴量の動的算出
        print("  - テーマ別(Core_Product_Tech)のモメンタム特徴量を算出しています...")
        if 'core_product_tech' in df.columns:
            # 日付 x テーマ ごとの ma5_slope と vol_ratio の平均を計算
            theme_grouped = df.groupby(['date', 'core_product_tech'], observed=True)
            theme_avg = theme_grouped[['ma5_slope', 'vol_ratio']].mean().reset_index()
            theme_avg.rename(columns={
                'ma5_slope': 'theme_ma5_slope_avg',
                'vol_ratio': 'theme_vol_ratio_avg'
            }, inplace=True)

            # 元のデータフレームにマージ
            df = pd.merge(df, theme_avg, on=['date', 'core_product_tech'], how='left')

        df = df.set_index(['code', 'date'])

    # 初期期間など計算不能なNaNを除外する
    subset_cols = ['ma5_slope', 'ma25_slope', 'vol_ratio', 'vol_ratio_5d']
    if 'theme_ma5_slope_avg' in df.columns:
        subset_cols.extend(['theme_ma5_slope_avg', 'theme_vol_ratio_avg'])
    df = df.dropna(subset=subset_cols)

    print(f"  - 特徴量結合後のサンプル数: {len(df)}行")
    return df

def construct_universe(df_features):
    """GC期間を特定しユニバースを作成する (ベクトル化)"""
    print("  - 5MAと25MAを計算しています...")
    # 各銘柄ごとにMAを計算
    df = df_features.copy()

    # 銘柄ごとの処理を高速化するため groupby を使用
    grouped = df.groupby(level='code')

    df['ma5'] = grouped['close'].transform(lambda x: x.rolling(window=5, min_periods=1).mean())
    df['ma25'] = grouped['close'].transform(lambda x: x.rolling(window=25, min_periods=1).mean())

    # GC判定 (5MA > 25MA)
    df['is_gc'] = df['ma5'] > df['ma25']

    print("  - GC期間を抽出し、ターゲット変数を計算しています (ベクトル化)...")

    # --- 高速化（ベクトル化）ロジック ---

    # インデックスから日付を列に出しておく
    df_reset = df.reset_index()

    # GCではない（~is_gc）行の `date` と `close` を抽出し、
    # 新しいカラム `next_dc_date` と `next_dc_price` にセット。
    # GCである行には NaN が入る。
    df_reset['next_dc_date'] = np.where(~df_reset['is_gc'], df_reset['date'], pd.NaT)
    df_reset['next_dc_price'] = np.where(~df_reset['is_gc'], df_reset['close'], np.nan)

    # 銘柄ごとに bfill() （後ろ向き補完）を行う
    # これにより、GC期間中の各行に「その後に初めて来るDC日の日付と終値」が埋まる
    df_reset[['next_dc_date', 'next_dc_price']] = df_reset.groupby('code')[['next_dc_date', 'next_dc_price']].bfill()

    # GC開始日からの経過日数を計算
    # is_gc == False の場合は経過日数を 0 にリセットする工夫
    # is_gc の True が連続する長さを測る
    is_gc_int = df_reset['is_gc'].astype(int)
    # GCではない行をブロックの境界としてcumsumし、ブロックごとのIDを作る
    df_reset['gc_block'] = (~df_reset['is_gc']).groupby(df_reset['code']).cumsum()
    # ブロック内で is_gc_int の cumsum をとると、GC期間中のみカウントアップされる
    df_reset['days_since_gc'] = df_reset.groupby(['code', 'gc_block'])['is_gc'].cumsum()

    # is_gc が True の行だけを抽出し、ユニバースとする
    df_universe = df_reset[df_reset['is_gc']].copy()

    # DC日が補完されなかった（最後までGCのまま終わった）行は除外
    df_universe = df_universe.dropna(subset=['next_dc_date', 'next_dc_price'])

    # ターゲット変数の計算
    # (DC日終値 - 当日終値) / 当日終値
    df_universe['target_score'] = np.where(
        df_universe['close'] > 0,
        (df_universe['next_dc_price'] - df_universe['close']) / df_universe['close'],
        0
    )

    # カラム名のリネーム（元の互換性のため）
    df_universe.rename(columns={'next_dc_date': 'dc_date', 'next_dc_price': 'dc_price'}, inplace=True)

    # 再びインデックスをセット
    df_universe.set_index(['code', 'date'], inplace=True)

    # ターゲット変数のクリッピング（外れ値対策）
    df_universe['target_score'] = df_universe['target_score'].clip(lower=-0.5, upper=2.0)

    print(f"  - 抽出されたGC期間サンプル数: {len(df_universe)}行")
    return df_universe

def train_and_evaluate(df_model):
    """ウォークフォワード検証でモデルの学習と評価を行う"""
    print("  - モデルの学習と評価を開始します (ウォークフォワード検証)")

    # タイムスタンプ付きの出力ディレクトリ作成
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    output_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    export_dir = os.path.join(output_dir, 'data', 'exports', f"{timestamp}_momentum_model_outputs")
    os.makedirs(export_dir, exist_ok=True)

    log_file_path = os.path.join(export_dir, 'evaluation_log.txt')
    def log_print(msg):
        print(msg)
        with open(log_file_path, 'a', encoding='utf-8') as f:
            f.write(msg + '\n')

    log_print(f"出力ディレクトリを作成しました: {export_dir}")

    # 特徴量リスト
    features = [
        'ma5_slope', 'ma25_slope', 'vol_ratio', 'vol_ratio_5d', 'days_since_gc'
    ]

    if 'theme_ma5_slope_avg' in df_model.columns:
        features.extend(['theme_ma5_slope_avg', 'theme_vol_ratio_avg'])

    if 'industry' in df_model.columns:
        features.append('industry')

    target_raw = 'target_score'

    # インデックスをリセットして日付順にソート
    df = df_model.reset_index().sort_values('date')

    # 十分なデータがない場合はスキップ
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

    fold = 1
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

        # --- 分類タスク用ラベリング ---
        # 学習データの target_score 上位20%を 1 とする
        train_threshold = train_df[target_raw].quantile(0.8)
        train_df['is_top_20'] = (train_df[target_raw] >= train_threshold).astype(int)

        # テストデータの target_score 上位20%を 1 とする (評価指標・検証用)
        test_threshold = test_df[target_raw].quantile(0.8)
        test_df['is_top_20'] = (test_df[target_raw] >= test_threshold).astype(int)

        X_train = train_df[features]
        y_train = train_df['is_top_20']
        X_test = test_df[features]
        y_test = test_df['is_top_20']

        # LightGBM Classifier の定義
        model = lgb.LGBMClassifier(
            n_estimators=100,
            learning_rate=0.05,
            random_state=42,
            n_jobs=-1,
            class_weight='balanced'
        )

        model.fit(X_train, y_train)

        # 予測確率の取得 (クラス '1' の確率)
        preds_proba = model.predict_proba(X_test)[:, 1]
        test_df['pred_proba'] = preds_proba

        # 評価: 予測確率の上位20%の銘柄群を抽出
        pred_threshold = test_df['pred_proba'].quantile(0.8)
        selected_df = test_df[test_df['pred_proba'] >= pred_threshold]

        # ベースラインリターン (テスト期間の全サンプルの平均ターゲットスコア = ランダム選択)
        baseline_return = test_df[target_raw].mean()
        # モデルリターン (選択銘柄群の平均ターゲットスコア)
        model_return = selected_df[target_raw].mean() if not selected_df.empty else 0

        baseline_returns.append(baseline_return)
        model_returns.append(model_return)

        # 特徴量重要度の保存
        importance = pd.DataFrame({
            'feature': features,
            'importance': model.feature_importances_,
            'fold': fold
        })
        feature_importances.append(importance)

        log_print(f"Fold {fold}:")
        log_print(f"  - Train={len(train_df)}行, Top20_Threshold={train_threshold:.4f}, PositiveRate={y_train.mean():.2%}")
        log_print(f"  - Test={len(test_df)}行, Top20_Threshold={test_threshold:.4f}")
        log_print(f"  - 選択された銘柄数={len(selected_df)}行")
        log_print(f"  - Baseline Return: {baseline_return:.4f}, Model Return: {model_return:.4f}")
        fold += 1

    # --- 結果の集計と出力 ---
    if feature_importances:
        avg_baseline = np.mean(baseline_returns)
        avg_model = np.mean(model_returns)
        log_print("-" * 50)
        log_print(f"【最終評価結果】")
        log_print(f"ランダム選択リターン (Baseline): {avg_baseline:.4f}")
        log_print(f"モデル選択リターン (Model)   : {avg_model:.4f}")
        log_print(f"優位性 (Model - Baseline)  : {avg_model - avg_baseline:.4f}")

        df_imp = pd.concat(feature_importances)
        avg_imp = df_imp.groupby('feature')['importance'].mean().sort_values(ascending=False).reset_index()

        log_print("-" * 50)
        log_print("【特徴量重要度 (Feature Importance)】")
        for i, row in avg_imp.iterrows():
            log_print(f"{i+1}. {row['feature']}: {row['importance']:.2f}")

        plt.figure(figsize=(10, 6))
        sns.barplot(x='importance', y='feature', data=avg_imp)
        plt.title('Feature Importance (LightGBM Classifier)')
        plt.tight_layout()
        plot_path = os.path.join(export_dir, 'feature_importance.png')
        plt.savefig(plot_path)
        log_print(f"特徴量重要度のグラフを保存しました: {plot_path}")

        # --- モデルの保存とSHAP値の出力（最後のFoldのモデルを使用） ---
        if 'model' in locals() and 'X_test' in locals():
            # モデルの保存
            model_path = os.path.join(export_dir, 'momentum_model_baseline.txt')
            model.booster_.save_model(model_path)
            log_print(f"モデルを保存しました: {model_path}")

            # SHAP値の計算と出力
            log_print("テストデータに対するSHAP値を計算しています...")
            try:
                # TreeExplainer を使用
                explainer = shap.TreeExplainer(model)
                shap_values = explainer.shap_values(X_test)

                # shap_values がリストの場合 (LightGBMの2値分類など)、クラス1のSHAP値を取得
                if isinstance(shap_values, list):
                    shap_values = shap_values[1]

                # SHAP summary plot の作成と保存
                plt.figure(figsize=(10, 6))
                shap.summary_plot(shap_values, X_test, show=False)
                shap_plot_path = os.path.join(export_dir, 'shap_summary_plot.png')
                plt.savefig(shap_plot_path, bbox_inches='tight')
                plt.close()
                log_print(f"SHAP summary plotを保存しました: {shap_plot_path}")
            except Exception as e:
                log_print(f"SHAPの計算中にエラーが発生しました: {e}")

    else:
        log_print("評価可能なFoldがありませんでした。")

def main():
    print("モメンタム投資戦略ベースラインモデルの構築を開始します...")

    # 1. データの読み込み
    print("ステップ1: データの読み込み...")
    df_prices, df_tickers = load_data()

    if df_prices.empty:
        print("データが空のため、処理を終了します。")
        return

    # 2. 特徴量生成 (全データがある状態で行う)
    print("ステップ2: 特徴量の動的生成...")
    df_features_all = create_features(df_prices, df_tickers)

    # 3. ユニバース構築
    print("ステップ3: ユニバースの構築 (GC期間抽出とラベリング)...")
    df_universe = construct_universe(df_features_all)

    if df_universe.empty:
        print("有効なユニバースデータがありません。処理を終了します。")
        return

    # 4 & 5. モデル学習と評価
    print("ステップ4&5: モデル学習と評価...")
    train_and_evaluate(df_universe)

    print("完了しました。")

if __name__ == "__main__":
    main()
