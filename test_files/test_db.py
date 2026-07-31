import sqlite3
import pandas as pd

df = pd.read_excel('FixMorph-Dataset/Main-data-set.xlsx')
row = df.iloc[0]

conn = sqlite3.connect('mystique_cache.db')
pc = row['old_version_patch_commit'].split('^')[0] if '^' in row['old_version_patch_commit'] else row['old_version_patch_commit']
pc_sha = pc
target_path = row['file_path_in_the_old_version']

c_pc = conn.execute("SELECT content FROM files WHERE sha=? AND path=?", (pc_sha, target_path)).fetchone()
if c_pc:
    print('fscrypt_has_permitted_context' in c_pc[0])
    print(c_pc[0][:100])
else:
    print("Not found in DB")
