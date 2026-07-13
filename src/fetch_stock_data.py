import sqlite3
import pandas as pd
import argparse
import os

def fetch_data(code, db_path='../data/stock_data.db', output_path=None):
    if not os.path.exists(db_path):
        print(f"Error: Database not found at {db_path}")
        return

    conn = sqlite3.connect(db_path)

    # Tables to fetch that have 'code'
    time_series_tables = {
        'daily_prices': 'date',
        'daily_financials': 'date',
        'weekly_margin': 'date',
        'financial_results': 'date',
        'stock_daily_indicators': 'date',
        'daily_trade_indicators': 'date',
        'stock_eps_revisions': 'revision_date',
        'corporate_disclosures': 'disclosure_date'
    }

    dfs = []

    for table, date_col in time_series_tables.items():
        # Check if table exists
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        if not cursor.fetchone():
            continue

        # Fetch data for the specific code
        query = f"SELECT * FROM {table} WHERE code = ?"
        df = pd.read_sql_query(query, conn, params=(code,))

        if df.empty:
            continue

        if table == 'corporate_disclosures':
            # Drop excluded columns
            cols_to_drop = ['ai_summary', 'title', 'document_url', 'created_at', 'pdf_text']
            df = df.drop(columns=[c for c in cols_to_drop if c in df.columns])

        if date_col != 'date' and date_col in df.columns:
            df = df.rename(columns={date_col: 'date'})

        # Sort by date for diff calculation
        if 'date' in df.columns:
            df = df.sort_values('date').reset_index(drop=True)

            # Identify numeric columns, exclude 'code', 'date', 'id' etc.
            numeric_cols = df.select_dtypes(include='number').columns.tolist()
            exclude_from_diff = ['id', 'code', 'date']
            numeric_cols = [c for c in numeric_cols if c not in exclude_from_diff]

            # Calculate 1d_diff, 1w_diff (5 days), 1m_diff (20 days)
            diff_1d = df[numeric_cols].diff(1).add_suffix('_1d_diff')
            diff_1w = df[numeric_cols].diff(5).add_suffix('_1w_diff')
            diff_1m = df[numeric_cols].diff(20).add_suffix('_1m_diff')

            df = pd.concat([df, diff_1d, diff_1w, diff_1m], axis=1)

            # Add table name as prefix to columns to avoid collision, except for 'code' and 'date'
            rename_dict = {col: f"{table}_{col}" for col in df.columns if col not in ['code', 'date']}
            df = df.rename(columns=rename_dict)

            dfs.append(df)

    if not dfs:
        print(f"No time series data found for code {code}.")
        conn.close()
        return

    # Merge all time-series dataframes on 'date' and 'code'
    merged_df = dfs[0]
    for df in dfs[1:]:
        # Outer join on date and code
        merged_df = pd.merge(merged_df, df, on=['code', 'date'], how='outer')

    merged_df = merged_df.sort_values('date').reset_index(drop=True)

    # Fetch companies info
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='companies'")
    if cursor.fetchone():
        comp_df = pd.read_sql_query("SELECT * FROM companies WHERE code = ?", conn, params=(code,))
        if not comp_df.empty:
            # Rename company columns
            comp_rename = {col: f"companies_{col}" for col in comp_df.columns if col != 'code'}
            comp_df = comp_df.rename(columns=comp_rename)
            merged_df = pd.merge(merged_df, comp_df, on='code', how='left')

    conn.close()

    if output_path is None:
        output_path = f"stock_{code}_all_data.csv"

    merged_df.to_csv(output_path, index=False, encoding='utf-8-sig')
    print(f"Data saved to {output_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch stock data and calculate differences.")
    parser.add_argument("code", help="Stock code to fetch data for")
    parser.add_argument("--db", default="../data/stock_data.db", help="Path to database")
    parser.add_argument("--out", default=None, help="Output CSV path")
    args = parser.parse_args()

    fetch_data(args.code, db_path=args.db, output_path=args.out)
