
# /// script
# dependencies = [
#   "pandas",
#   "zstandard",
# ]
# ///

import glob
import os
import pandas as pd

def top_10_mean(df, col):
    th = df[col].quantile(0.95)
    top_10 = df[df[col] >= th]
    return top_10[col].mean()

def low_10_mean(df, col):
    th = df[col].quantile(0.05)
    top_10 = df[df[col] <= th]
    return top_10[col].mean()

def all_mean(df, col):
    return df[col].mean()





def process_csv(fname, kkk, threshold):
    dfs = []
    files = glob.glob(fname)
    total_files = len(files)
    print(f"Found {total_files} CSV files to process.")
    
    for i, fname in enumerate(files):
        print(f"\rLoading file {i+1}/{total_files}", end="")
        try:
            if os.path.getsize(fname) > 0:
                comp = 'zstd' if fname.lower().endswith(('.zst', '.zstd')) else 'infer'
                dfs.append(pd.read_csv(fname, compression=comp))
        except pd.errors.EmptyDataError:
            continue
    print() # Newline after finishing
    
    if not dfs:
        print("No valid data found in CSV files.")
        return

    print("Concatenating data frames...")
    df_a = pd.concat(dfs, ignore_index=True)

    if kkk == "WL":
        df_a = df_a[~(df_a['WL3'] > 1000)]  # fix wrong data
    
    print("Segmenting data...")
    # dfs_a = [df_a.iloc[0:10]]
    dfs_a = []
    
    # Vectorized segmentation
    if kkk == "AL":
        mask = (df_a[f'{kkk}1'] >= threshold) | (df_a[f'{kkk}2'] >= threshold) | (df_a[f'{kkk}3'] >= threshold)
    elif kkk == "WL":
        mask = (df_a[f'{kkk}1'] >= threshold) | (df_a[f'{kkk}2'] >= threshold) | (df_a[f'{kkk}3'] >= threshold)
    elif kkk == "VL":
        mask = (df_a[f'{kkk}1'] < threshold) | (df_a[f'{kkk}2'] < threshold) | (df_a[f'{kkk}3'] < threshold)
    
    if mask.any():
        group_id = (mask != mask.shift()).cumsum()
        valid_groups = df_a[mask].groupby(group_id[mask])
        dfs_a.extend([group for _, group in valid_groups])
        print(len(dfs_a))
    
    print(f"Found {len(dfs_a)} groups over the threshold.")


    outline = []
    for i, df in enumerate(dfs_a):
        if len(df) < 290:
            continue
        print(f"\rProcessing group {i+1}/{len(dfs_a)}", end="")

        top5 = [int(top_10_mean(df, f'{kkk}{i+1}')) for i in range(3)]
        low5 = [int(low_10_mean(df, f'{kkk}{i+1}')) for i in range(3)]
        allmean = [int(all_mean(df, f'{kkk}{i+1}')) for i in range(3)]

        summary = [[], [], [], [], [], [], [], [], [], []]

        summary[0].append(df['timestamp'].iloc[0])
        summary[1].append(top5[0])
        summary[2].append(top5[1])
        summary[3].append(top5[2])
        summary[4].append(allmean[0])
        summary[5].append(allmean[1])
        summary[6].append(allmean[2])
        summary[7].append(low5[0])
        summary[8].append(low5[1])
        summary[9].append(low5[2])

        ss = [str(summary[i][-1]) for i in range(10)]
        outline.append(",".join(ss))

    with open(f"power-summary_{kkk}.csv", "w") as f:
        f.write("timestamp,high_1,high_2,high_3,avg_1,avg_2,avg_3,low_1,low_2,low_3\n")
        for ss in outline:
            f.write(ss + "\n")
   


def main():
    # current
    # process_csv("e:/2025_長隆brogent_log_xz/*_powermeter_current_*.csv", "AL", 250)
    process_csv("\\\\192.168.1.239\\Brogent\\營銷中心\\業務行銷二部\\02_維保案件Log存放區\\R0082_長隆風暴\\_csv\\*\\*_powermeter_current_*.csv.zst", "AL", 250)
    
    # # voltage
    process_csv("\\\\192.168.1.239\\Brogent\\營銷中心\\業務行銷二部\\02_維保案件Log存放區\\R0082_長隆風暴\\_csv\\*\\*_powermeter_voltage_*.csv.zst", "VL", 385)

    # # watt
    process_csv("\\\\192.168.1.239\\Brogent\\營銷中心\\業務行銷二部\\02_維保案件Log存放區\\R0082_長隆風暴\\_csv\\*\\*_powermeter_watt_*.csv.zst", "WL", 60)


if __name__ == '__main__':
    main()
