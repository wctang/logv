
# /// script
# dependencies = [
#   "pandas",
#   "pyarrow",
#   "zstandard",
# ]
# ///

import glob
import os
import pandas as pd

def top_10_mean(df, col, absolute=False):
    if col not in df.columns:
        return 0.0
    s = df[col].dropna()
    if absolute:
        s = s.abs()
    if s.empty:
        return 0.0
    th = s.quantile(0.90)
    top_10 = s[s >= th]
    return top_10.mean()

def all_mean(df, col, absolute=False):
    if col not in df.columns:
        return 0.0
    s = df[col].dropna()
    if absolute:
        s = s.abs()
    if s.empty:
        return 0.0
    return s.mean()

def process_csv(fnameptn, output_file):
    files = glob.glob(fnameptn)
    total_files = len(files)
    print(f"Found {total_files} CSV files to process.")
    
    with open(output_file, 'w', encoding='utf-8') as f_out:
        f_out.write("timestamp,temp_1_avg,temp_1_top10avg,temp_2_avg,temp_2_top10avg,torque_1_abs_avg,torque_1_abs_top10avg,torque_2_abs_avg,torque_2_abs_top10avg\n")

        for i, fname in enumerate(files):
            # print the date from fname
            date_str = os.path.basename(fname).split('.')[0].replace('log-', '')
            print(f"\r{i+1:3}/{total_files} {date_str}", end="")
            try:
                if os.path.getsize(fname) > 0:
                    comp = 'zstd' if fname.lower().endswith(('.zst', '.zstd')) else 'infer'
                    csv = pd.read_csv(fname, engine="pyarrow", compression=comp)

                    count = 0
                    if 'mpc_ctrl_mode' in csv.columns:
                        is_playing = csv['mpc_ctrl_mode'] == 'playing'
                        segment_starts = is_playing & ~is_playing.shift(fill_value=False)
                        segment_ids = segment_starts.cumsum()
                        
                        playing_rows = csv[is_playing]
                        groups = playing_rows.groupby(segment_ids[is_playing])
                        count = len(groups)
                        
                    if count > 0:
                        print(f" - Plays: {count}")
                        
                        for play_index, (group_id, group_df) in enumerate(groups, 1):
                            avg_1 = all_mean(group_df, 'mpc_ctrl_cy_temperature_1')
                            top10_1 = top_10_mean(group_df, 'mpc_ctrl_cy_temperature_1')
                            avg_2 = all_mean(group_df, 'mpc_ctrl_cy_temperature_2')
                            top10_2 = top_10_mean(group_df, 'mpc_ctrl_cy_temperature_2')
                            
                            torque_avg_1 = all_mean(group_df, 'servo_torque_1', absolute=True)
                            torque_top10_1 = top_10_mean(group_df, 'servo_torque_1', absolute=True)
                            torque_avg_2 = all_mean(group_df, 'servo_torque_2', absolute=True)
                            torque_top10_2 = top_10_mean(group_df, 'servo_torque_2', absolute=True)
                            
                            first_ts = group_df['timestamp'].iloc[0] if 'timestamp' in group_df.columns else date_str
                            f_out.write(f"{first_ts},{avg_1:.2f},{top10_1:.2f},{avg_2:.2f},{top10_2:.2f},{torque_avg_1:.2f},{torque_top10_1:.2f},{torque_avg_2:.2f},{torque_top10_2:.2f}\n")
                            f_out.flush()
                    else:
                        first_ts = csv['timestamp'].iloc[0] if not csv.empty and 'timestamp' in csv.columns else date_str
                        f_out.write(f"{first_ts},,,,,,,,\n")
                        f_out.flush()

            except pd.errors.EmptyDataError:
                continue
        print() # Newline after finishing
        print(f"Saved summary to {output_file}")
   


def main():
    for cn in ['101', '102', '103', '104', '105']:
        process_csv(f"\\\\192.168.1.239\\Brogent\\營銷中心\\業務行銷二部\\02_維保案件Log存放區\\R0167_HTB\\_csv\\*\\log-*.log_mpc_ctrl_{cn}.csv.zst", f"analysis_summary_{cn}.csv")
  


if __name__ == '__main__':
    main()
