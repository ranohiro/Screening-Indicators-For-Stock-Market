import sqlite3
import json
import pandas as pd
import numpy as np
import os
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.model_selection import TimeSeriesSplit
import lightgbm as lgb
import warnings

warnings.filterwarnings('ignore')

# データベース設定
# srcから見た相対パスに修正
DB_PATH = 'data/stock_data.db'
CSV_PATH = 'data/exports/screening_all_v01.csv'
JSON_PATH = 'data/ticker_dictionary.json'

def load_data():
    """データベースから必要なデータを読み込む"""
    print("  - データベースに接続しデータを取得しています...")
    # プロジェクトルートからのパスを想定
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = os.path.join(root_dir, DB_PATH)

    if not os.path.exists(db_path):
        print(f"警告: データベースファイルが見つかりません: {db_path}")
        # ダミーデータ生成ロジックが必要な場合は後で追加
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

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

    # daily_trade_indicatorsの取得
    query_indicators = """
    SELECT code, date, vwap_dev, vol_ratio, ma5_slope, ma25_slope, consecutive_candles
    FROM daily_trade_indicators
    ORDER BY code, date
    """
    df_indicators = pd.read_sql_query(query_indicators, conn)
    df_indicators['date'] = pd.to_datetime(df_indicators['date'], format='%Y%m%d', errors='coerce')
    df_indicators.set_index(['code', 'date'], inplace=True)
    df_indicators.sort_index(inplace=True)

    conn.close()

    # サマリーCSVの取得
    csv_path = os.path.join(root_dir, CSV_PATH)
    df_summary = pd.DataFrame()
    if os.path.exists(csv_path):
        try:
            df_summary = pd.read_csv(csv_path)
        except Exception as e:
            print(f"警告: サマリーCSVの読み込みに失敗しました: {e}")

    print(f"  - daily_prices: {len(df_prices)}行")
    print(f"  - daily_trade_indicators: {len(df_indicators)}行")

    return df_prices, df_indicators, df_summary

def construct_universe(df_prices):
    """GC期間を特定しユニバースを作成する"""
    print("  - 5MAと25MAを計算しています...")
    # 各銘柄ごとにMAを計算
    df = df_prices.copy()

    # 銘柄ごとの処理を高速化するため groupby を使用
    grouped = df.groupby(level='code')

    df['ma5'] = grouped['close'].rolling(window=5, min_periods=1).mean().reset_index(level=0, drop=True)
    df['ma25'] = grouped['close'].rolling(window=25, min_periods=1).mean().reset_index(level=0, drop=True)

    # GC判定 (5MA > 25MA)
    df['is_gc'] = df['ma5'] > df['ma25']

    # GC/DCの切り替わりを特定
    # is_gcが前の行と異なる場合 True となるフラグ
    df['gc_changed'] = grouped['is_gc'].shift(1) != df['is_gc']

    # 新しいGC期間が始まった日 (GC開始)
    df['gc_start'] = df['is_gc'] & df['gc_changed']

    # DCになった日 (GC終了)
    df['dc_start'] = (~df['is_gc']) & df['gc_changed']

    print("  - GC期間を抽出し、ターゲット変数を計算しています...")

    # 各銘柄ごとにGC期間のIDを振る
    # gc_startがTrueになるたびにcumsumでIDが増加
    df['gc_period_id'] = grouped['gc_start'].cumsum()

    # GC期間のデータのみ抽出 (is_gc == True)
    # DCになった日も含める必要があるため、少し工夫が必要
    # DCになった日の終値を取得するために、前方にシフトしたデータを使うアプローチ等があるが、
    # シンプルに各GC期間IDごとに、その期間が終わった次の日(DC発生日)の終値を取得する

    results = []

    for code, group in df.groupby(level='code'):
        # グループ内のデータを日付順にソート (念のため)
        group = group.sort_index(level='date')

        # is_gcの列だけ取得
        is_gc = group['is_gc'].values
        closes = group['close'].values
        dates = group.index.get_level_values('date')

        n = len(is_gc)

        for i in range(n):
            if is_gc[i]:
                # GC期間中である
                # この期間がいつ終わるか(最初の is_gc == False)を探す
                dc_idx = -1
                for j in range(i + 1, n):
                    if not is_gc[j]:
                        dc_idx = j
                        break

                if dc_idx != -1:
                    # DC日が特定できた場合のみ有効なデータとする
                    dc_price = closes[dc_idx]
                    current_price = closes[i]

                    # ターゲットスコア = (DC日終値 - 当日終値) / 当日終値
                    target_score = (dc_price - current_price) / current_price if current_price > 0 else 0

                    results.append({
                        'code': code,
                        'date': dates[i],
                        'target_score': target_score,
                        'dc_date': dates[dc_idx],
                        'dc_price': dc_price
                    })

    df_universe = pd.DataFrame(results)
    if not df_universe.empty:
        df_universe.set_index(['code', 'date'], inplace=True)
        # 元の株価データ等と結合
        df_universe = df_universe.join(df_prices, how='left')

    print(f"  - 抽出されたGC期間サンプル数: {len(df_universe)}行")
    return df_universe

def create_features(df_universe, df_indicators):
    """特徴量を生成し結合する"""
    print("  - 特徴量を生成・結合しています...")

    # 出来高ベースアップ指標の計算 (過去5日平均 / 過去20日平均)
    # 元のdf_prices (df_universeに含まれるvolume列) を使って計算するが、
    # 既に df_universe には gc期間のデータしかないため、全体での計算が難しい。
    # ここでは簡易的に、df_universe内で rolling ではなく、あらかじめ計算してマージするか、
    # indicators のデータを利用する。

    # daily_trade_indicators に vol_ratio などの出来高特徴量が既にある前提
    df = df_universe.copy()

    # df_indicators との結合
    # df_indicatorsもマルチインデックスなので、そのままjoinできる
    df = df.join(df_indicators, how='left', rsuffix='_ind')

    # 欠損値の処理 (前方補完、それでも残れば0埋め)
    # groupbyを使用するためリセットインデックス
    df_reset = df.reset_index()

    # codeごとに前方補完
    cols_to_fill = ['vwap_dev', 'vol_ratio', 'ma5_slope', 'ma25_slope', 'consecutive_candles']
    for col in cols_to_fill:
        if col in df_reset.columns:
            df_reset[col] = df_reset.groupby('code')[col].ffill().fillna(0)

    # インデックスを元に戻す
    df = df_reset.set_index(['code', 'date'])

    # ターゲット変数のクリッピング（外れ値対策）
    df['target_score'] = df['target_score'].clip(lower=-0.5, upper=2.0)

    print(f"  - 特徴量結合後のサンプル数: {len(df)}行")
    return df

def train_and_evaluate(df_model):
    """ウォークフォワード検証でモデルの学習と評価を行う"""
    print("  - モデルの学習と評価を開始します (ウォークフォワード検証)")

    # 特徴量リスト
    features = [
        'vwap_dev', 'vol_ratio', 'ma5_slope', 'ma25_slope', 'consecutive_candles'
    ]
    target = 'target_score'

    # 必要なカラムが存在するか確認し、欠損値を0埋め
    for f in features:
        if f not in df_model.columns:
            print(f"警告: 特徴量 {f} が存在しません。0で埋めます。")
            df_model[f] = 0

    # インデックスをリセットして日付順にソート
    df = df_model.reset_index().sort_values('date')

    # 十分なデータがない場合はスキップ
    if len(df) < 10:
        print(f"データが少なすぎます ({len(df)}行)。学習をスキップします。")
        return

    # TimeSeriesSplit の設定 (例えば3分割)
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
        # 学習データのうち、テスト期間の開始時点で「まだGC期間が終わっていない（DCを迎えていない）」
        # サンプルは、未来の情報(DC日終値)を使っているためリークとなる。これを除外する。
        test_start_date = test_df['date'].min()
        valid_train_mask = train_df['dc_date'] < test_start_date
        train_df = train_df[valid_train_mask]

        if len(train_df) == 0:
            print(f"Fold {fold}: 有効な学習データがありません。スキップします。")
            fold += 1
            continue

        X_train = train_df[features]
        y_train = train_df[target]
        X_test = test_df[features]
        y_test = test_df[target]

        # ターゲット変数が大きいもの（初動）に重み付け
        # 簡単な例: scoreが0より大きい場合、重みを増やす
        sample_weight = np.where(y_train > 0, 1.0 + y_train * 2, 1.0)

        # LightGBMモデルの定義
        model = lgb.LGBMRegressor(
            n_estimators=100,
            learning_rate=0.05,
            random_state=42,
            n_jobs=-1
        )

        model.fit(X_train, y_train, sample_weight=sample_weight)

        # 予測
        preds = model.predict(X_test)
        test_df['pred_score'] = preds

        # 評価: 予測スコアの上位20%を「選択銘柄」とする
        threshold = test_df['pred_score'].quantile(0.8)
        selected_df = test_df[test_df['pred_score'] >= threshold]

        # ベースラインリターン (テスト期間の全サンプルの平均ターゲットスコア = ランダム選択)
        baseline_return = test_df['target_score'].mean()
        # モデルリターン (選択銘柄の平均ターゲットスコア)
        model_return = selected_df['target_score'].mean() if not selected_df.empty else 0

        baseline_returns.append(baseline_return)
        model_returns.append(model_return)

        # 特徴量重要度の保存
        importance = pd.DataFrame({
            'feature': features,
            'importance': model.feature_importances_,
            'fold': fold
        })
        feature_importances.append(importance)

        print(f"Fold {fold}: Train={len(train_df)}行, Test={len(test_df)}行 | "
              f"Baseline Return: {baseline_return:.4f}, Model Return: {model_return:.4f}")
        fold += 1

    # --- 結果の集計と出力 ---
    if feature_importances:
        # 平均リターンの比較
        avg_baseline = np.mean(baseline_returns)
        avg_model = np.mean(model_returns)
        print("-" * 50)
        print(f"【評価結果】")
        print(f"ランダム選択リターン (Baseline): {avg_baseline:.4f}")
        print(f"モデル選択リターン (Model)   : {avg_model:.4f}")
        print(f"優位性 (Model - Baseline)  : {avg_model - avg_baseline:.4f}")

        # 特徴量重要度の集計
        df_imp = pd.concat(feature_importances)
        avg_imp = df_imp.groupby('feature')['importance'].mean().sort_values(ascending=False).reset_index()

        print("-" * 50)
        print("【特徴量重要度 (Feature Importance)】")
        for i, row in avg_imp.iterrows():
            print(f"{i+1}. {row['feature']}: {row['importance']:.2f}")

        # グラフとして保存
        output_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        export_dir = os.path.join(output_dir, 'data', 'exports')
        os.makedirs(export_dir, exist_ok=True)

        plt.figure(figsize=(10, 6))
        sns.barplot(x='importance', y='feature', data=avg_imp)
        plt.title('Feature Importance (LightGBM)')
        plt.tight_layout()
        plot_path = os.path.join(export_dir, 'feature_importance.png')
        plt.savefig(plot_path)
        print(f"特徴量重要度のグラフを保存しました: {plot_path}")
    else:
        print("評価可能なFoldがありませんでした。")

def main():
    print("モメンタム投資戦略ベースラインモデルの構築を開始します...")

    # 1. データの読み込み
    print("ステップ1: データの読み込み...")
    df_prices, df_indicators, df_summary = load_data()

    if df_prices.empty:
        print("データが空のため、処理を終了します。")
        return

    # 2. ユニバース構築
    print("ステップ2: ユニバースの構築...")
    df_universe = construct_universe(df_prices)

    if df_universe.empty:
        print("有効なユニバースデータがありません。処理を終了します。")
        return

    # 3. 特徴量生成
    print("ステップ3: 特徴量生成...")
    df_features = create_features(df_universe, df_indicators)

    if df_features.empty:
        print("特徴量生成後のデータがありません。処理を終了します。")
        return

    # 4 & 5. モデル学習と評価
    print("ステップ4&5: モデル学習と評価...")
    train_and_evaluate(df_features)

    print("完了しました。")

if __name__ == "__main__":
    main()
