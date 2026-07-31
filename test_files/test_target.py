import pandas as pd
df = pd.read_excel('FixMorph-Dataset/Main-data-set.xlsx')
row = df.iloc[0]
print(repr(row['target_before_func_code'][:100]))
print('fscrypt_has_permitted_context' in row['target_before_func_code'])
