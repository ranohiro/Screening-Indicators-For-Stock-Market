import sqlite3
import pandas as pd
import numpy as np
import json
import os
import argparse

# Paths
DB_PATH = 'data/stock_data.db'
DICT_PATH = 'data/ticker_dictionary.json'
OUTPUT_DIR = 'data/exports'

def load_ticker_dictionary():
    if not os.path.exists(DICT_PATH):
        print(f"Warning: Ticker dictionary not found at {DICT_PATH}")
        return {}
    
    with open(DICT_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # Map code -> ticker dictionary info
    dict_map = {}
    for item in data:
        code = item.get('code')
        if not code:
            continue
        
        layered_tags = item.get('layered_tags', {})
        sector_tags = layered_tags.get('Sector_Major', [])
        core_techs = layered_tags.get('Core_Product_Tech', [])
        
        dict_map[code] = {
            'name_dict': item.get('name'),
            'sector_tags': sector_tags,
            'industry_dict': item.get('industry'),
            'core_techs': core_techs
        }
    return dict_map

def screen_single_stock(code, name, db_industry, ticker_dict, conn):
    # Fetch price data sorted by date
    query = "SELECT date, open, high, low, close FROM daily_prices WHERE code = ? ORDER BY date ASC"
    df = pd.read_sql_query(query, conn, params=(code,))
    
    # Convert price columns to numeric
    for col in ['open', 'high', 'low', 'close']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
        
    # Drop rows where critical price columns are NaN
    df = df.dropna(subset=['high', 'low', 'close']).reset_index(drop=True)
    
    if len(df) < 75:  # Need at least 75 days to compute 75MA
        return []
    
    # Compute moving averages
    df['ma5'] = df['close'].rolling(5).mean()
    df['ma25'] = df['close'].rolling(25).mean()
    df['ma75'] = df['close'].rolling(75).mean()
    
    # Identify Golden Cross (GC) / Dead Cross (DC) conditions of 5MA and 25MA
    df['gc_active'] = (df['ma5'] > df['ma25']).astype(int)
    df['prev_gc_active'] = df['gc_active'].shift(1).fillna(0).astype(int)
    
    df['is_gc'] = (df['gc_active'] == 1) & (df['prev_gc_active'] == 0)
    df['is_dc'] = (df['gc_active'] == 0) & (df['prev_gc_active'] == 1)
    
    # Get sector tag and other tags from dictionary
    dict_info = ticker_dict.get(code)
    if dict_info:
        sector_majors = dict_info.get('sector_tags', [])
        industry_tag = dict_info.get('industry_dict') or db_industry or ""
        core_techs = dict_info.get('core_techs', [])
    else:
        sector_majors = []
        industry_tag = db_industry or ""
        core_techs = []
        
    matched_periods = []
    current_period = None
    
    for idx, row in df.iterrows():
        if row['is_gc']:
            current_period = {
                'gc_date': row['date'],
                'highs': [],
                'lows': [],
                'perfect_order_days': 0,
                'dates': []
            }
        
        if current_period is not None:
            current_period['highs'].append(row['high'])
            current_period['lows'].append(row['low'])
            current_period['dates'].append(row['date'])
            
            # Check for perfect order: 5MA > 25MA > 75MA
            if not pd.isna(row['ma75']):
                if row['ma5'] > row['ma25'] > row['ma75']:
                    current_period['perfect_order_days'] += 1
            
            if row['is_dc']:
                # Period completed
                gc_date = current_period['gc_date']
                dc_date = row['date']
                duration = len(current_period['dates'])
                
                # Check filter conditions
                if duration >= 20 and current_period['perfect_order_days'] > 0:
                    high_max = max(current_period['highs'])
                    low_min = min(current_period['lows'])
                    if low_min > 0 and (high_max / low_min) >= 2.0:
                        # Find dates of lowest low and highest high
                        min_low_idx = current_period['lows'].index(low_min)
                        min_low_date = current_period['dates'][min_low_idx]
                        
                        max_high_idx = current_period['highs'].index(high_max)
                        max_high_date = current_period['dates'][max_high_idx]
                        
                        matched_periods.append({
                            'code': code,
                            'name': name,
                            'sector_majors': sector_majors,
                            'industry': industry_tag,
                            'core_techs': core_techs,
                            'gc_date': gc_date,
                            'dc_date': dc_date,
                            'min_low': low_min,
                            'min_low_date': min_low_date,
                            'max_high': high_max,
                            'max_high_date': max_high_date
                        })
                current_period = None
                
    return matched_periods

def main():
    parser = argparse.ArgumentParser(description="Stock Screening Tool")
    parser.add_argument('--ticker', type=str, default=None, help="Target ticker code to run dry-run (e.g. 285A)")
    args = parser.parse_args()
    
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    # Load ticker dictionary
    ticker_dict = load_ticker_dictionary()
    
    # Connect to DB
    conn = sqlite3.connect(DB_PATH)
    
    # Fetch companies to process
    if args.ticker:
        query = "SELECT code, name, industry FROM companies WHERE code = ?"
        companies = pd.read_sql_query(query, conn, params=(args.ticker,))
        output_file = os.path.join(OUTPUT_DIR, f'screening_{args.ticker.lower()}.csv')
    else:
        query = "SELECT code, name, industry FROM companies"
        companies = pd.read_sql_query(query, conn)
        output_file = os.path.join(OUTPUT_DIR, 'screening_all.csv')
        
    print(f"Loaded {len(companies)} companies to screen.")
    
    all_results = []
    
    for idx, row in companies.iterrows():
        code = row['code']
        name = row['name']
        industry = row['industry']
        
        if not code:
            continue
            
        periods = screen_single_stock(code, name, industry, ticker_dict, conn)
        if periods:
            all_results.extend(periods)
            
    conn.close()
    
    # Flatten the results to support "1 tag = 1 cell"
    # Find the maximum number of tags among the matched results to decide how many columns are needed
    max_sectors = max(len(r['sector_majors']) for r in all_results) if all_results else 1
    max_techs = max(len(r['core_techs']) for r in all_results) if all_results else 1
    
    # Ensure at least 1 column is defined even if empty
    max_sectors = max(max_sectors, 1)
    max_techs = max(max_techs, 1)
    
    flat_results = []
    for r in all_results:
        row_dict = {
            '銘柄コード': r['code'],
            '銘柄名': r['name']
        }
        
        # Populate Sector_Major columns
        for i in range(max_sectors):
            col_name = f'Sector_Major_{i+1}'
            if i < len(r['sector_majors']):
                row_dict[col_name] = r['sector_majors'][i]
            else:
                row_dict[col_name] = ""
                
        # Populate industry
        row_dict['industry'] = r['industry']
        
        # Populate Core_Product_Tech columns
        for i in range(max_techs):
            col_name = f'Core_Product_Tech_{i+1}'
            if i < len(r['core_techs']):
                row_dict[col_name] = r['core_techs'][i]
            else:
                row_dict[col_name] = ""
                
        # Populate rest of details
        row_dict['ゴールデンクロス(5MA>25MA)の日付'] = r['gc_date']
        row_dict['デッドクロス(5MA<25MA)の日付'] = r['dc_date']
        row_dict['期間中安値'] = r['min_low']
        row_dict['期間中安値の日付'] = r['min_low_date']
        row_dict['期間中高値'] = r['max_high']
        row_dict['期間中高値の日付'] = r['max_high_date']
        
        flat_results.append(row_dict)
        
    df_results = pd.DataFrame(flat_results)
    
    if df_results.empty:
        # Build default headers list
        headers = ['銘柄コード', '銘柄名']
        headers += [f'Sector_Major_{i+1}' for i in range(max_sectors)]
        headers += ['industry']
        headers += [f'Core_Product_Tech_{i+1}' for i in range(max_techs)]
        headers += [
            'ゴールデンクロス(5MA>25MA)の日付',
            'デッドクロス(5MA<25MA)の日付',
            '期間中安値',
            '期間中安値の日付',
            '期間中高値',
            '期間中高値の日付'
        ]
        df_results = pd.DataFrame(columns=headers)
        
    df_results.to_csv(output_file, index=False, encoding='utf-8-sig')
    print(f"Screening complete. Saved {len(df_results)} rows to {output_file}")

if __name__ == '__main__':
    main()

